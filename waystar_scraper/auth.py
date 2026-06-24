import json
import re
import asyncio
import time
from pathlib import Path

from playwright.async_api import BrowserContext, Error as PlaywrightError, Page, TimeoutError as PlaywrightTimeoutError

from config import (
    CLAIMS_LISTING_URL,
    GET_CHILD_CUSTS_URL,
    LOGIN_URL,
    SESSION_EXTEND_URL,
    STORAGE_STATE_PATH,
    WaystarConfig,
    resolve_security_answer,
)
from human import HumanSettings, human_click, human_type
from logging_config import get_logger, mask_secret
from network_retry import RetrySettings, retry_transient

log = get_logger("auth")

POST_LOGIN_URL = re.compile(r"https://(claims|general)\.zirmed\.com/.*")
CLAIMS_LISTING_URL_PATTERN = re.compile(
    r"https://claims\.zirmed\.com/Claims/Listing", re.IGNORECASE
)
MFA_URL_FRAGMENT = "AdditionalAuthentication"
CLAIMS_LISTING_MARKER = (
    "#claimListingTableContainer, "
    'form[action*="PerformSearch"], '
    '[id*="claimListing"], '
    "#claimsGrid"
)


class SessionExpiredError(Exception):
    """Saved session cannot access the claims listing (redirected to login)."""


def retry_settings_from_config(config: WaystarConfig) -> RetrySettings:
    return RetrySettings(
        max_attempts=config.network_retry_attempts,
        base_delay_sec=config.network_retry_delay_sec,
    )


def _is_login_url(url: str) -> bool:
    return "login.zirmed.com" in url.lower()


def _is_claims_listing_url(url: str) -> bool:
    return bool(CLAIMS_LISTING_URL_PATTERN.search(url)) and not _is_login_url(url)


def _is_browser_error_page(url: str) -> bool:
    return url.startswith("chrome-error://") or url.startswith("about:blank")


def _login_outcome_url(url: str) -> bool:
    return bool(POST_LOGIN_URL.match(url)) or MFA_URL_FRAGMENT in url


async def _await_final_login_destination(page: Page, timeout_sec: float = 20) -> None:
    """Wait until post-login URL settles (avoids false success on intermediate redirects)."""
    deadline = time.monotonic() + timeout_sec
    last_url = page.url
    stable_since = time.monotonic()

    while time.monotonic() < deadline:
        url = page.url
        if url != last_url:
            last_url = url
            stable_since = time.monotonic()
            await asyncio.sleep(0.3)
            continue

        if MFA_URL_FRAGMENT in url:
            return
        if POST_LOGIN_URL.match(url) and not _is_login_url(url):
            if time.monotonic() - stable_since >= 1.5:
                return

        await asyncio.sleep(0.4)

    log.debug("Post-login URL wait finished | %s", page.url)


async def _page_snapshot(page: Page) -> str:
    try:
        title = await page.title()
    except PlaywrightError:
        title = "(unavailable)"
    return f"url={page.url!r} title={title!r}"


async def _login_error_on_page(page: Page) -> str | None:
    selectors = [
        ".validation-summary-errors",
        ".field-validation-error",
        ".error-message",
        ".alert-danger",
        "#errorMessage",
    ]
    for selector in selectors:
        locator = page.locator(selector)
        if await locator.count() == 0:
            continue
        text = " ".join((await locator.first.inner_text()).split())
        if text:
            return text
    return None


async def _extract_security_question(page: Page) -> str | None:
    selectors = [
        "label:has-text('?')",
        ".security-question",
        "#securityQuestion",
        "p:has-text('?')",
    ]
    for selector in selectors:
        locator = page.locator(selector)
        if await locator.count() == 0:
            continue
        text = " ".join((await locator.first.inner_text()).split())
        if "?" in text:
            return text

    body_text = await page.locator("body").inner_text()
    for line in body_text.splitlines():
        cleaned = line.strip()
        if cleaned.endswith("?") and len(cleaned) > 10:
            return cleaned
    return None


async def _ensure_trust_device_checked(page: Page) -> None:
    checkbox = page.locator(
        'input[type="checkbox"]:near(:text("Trust this device")), '
        'label:has-text("Trust this device") input[type="checkbox"], '
        'input[type="checkbox"]'
    ).first
    if await checkbox.count() == 0:
        log.debug("Trust-device checkbox not found — skipping")
        return
    if not await checkbox.is_checked():
        await checkbox.check()
        log.info("Checked 'Trust this device for future logins'")


async def handle_mfa(page: Page, config: WaystarConfig, human: HumanSettings) -> None:
    log.warning("Additional authentication required (security question)")
    await human.screenshot(page, "mfa_security_question")

    question = await _extract_security_question(page)
    if question:
        log.info("Security question: %s", question)

    await _ensure_trust_device_checked(page)
    await human.delay()

    answer, matched_key = resolve_security_answer(question, config)
    if answer:
        if matched_key:
            log.info(
                "MFA answer matched key %r — auto-filling (answer=%s)",
                matched_key,
                mask_secret(answer),
            )
        else:
            log.info(
                "MFA answer from fallback WAYSTAR_SECURITY_ANSWER — auto-filling (answer=%s)",
                mask_secret(answer),
            )
        answer_input = page.locator(
            'input[name*="Answer" i], input[id*="Answer" i], '
            'input[type="text"]:near(:text("Answer")), '
            'label:has-text("Answer") + input, '
            'input[type="text"]'
        ).first
        await answer_input.wait_for(state="visible", timeout=15_000)
        await human_type(answer_input, answer, human)
        await human.screenshot(page, "mfa_answer_filled")
        await human.delay()

        verify_btn = page.locator(
            'input[type="submit"][value*="Verify" i], '
            'button:has-text("Verify"), '
            'input[value="Verify"]'
        ).first
        await human_click(verify_btn, page, human)
        log.info("Clicked Verify — waiting for redirect...")
        await human.screenshot(page, "mfa_after_verify")
    elif not config.headless:
        log.info(
            "No MFA answer configured — complete the security question "
            "manually in the browser (waiting up to %ss)",
            config.mfa_timeout_sec,
        )
        await human.screenshot(page, "mfa_manual_wait")
    else:
        configured_keys = ", ".join(config.security_answers or {}) or "(none)"
        raise RuntimeError(
            f"Additional authentication required — no answer for question: {question!r}. "
            f"Set WAYSTAR_SECURITY_ANSWERS (keys: {configured_keys}) "
            f"or WAYSTAR_SECURITY_ANSWER, or run with --headed."
        )

    try:
        await page.wait_for_url(POST_LOGIN_URL, timeout=config.mfa_timeout_sec * 1000)
    except PlaywrightTimeoutError as exc:
        inline_error = await _login_error_on_page(page)
        snapshot = await _page_snapshot(page)
        if inline_error:
            log.error("MFA failed — %s | %s", inline_error, snapshot)
            raise RuntimeError(f"MFA verification failed: {inline_error} | {snapshot}") from exc
        log.error("MFA timed out | %s", snapshot)
        raise RuntimeError(
            f"MFA timed out after {config.mfa_timeout_sec}s. {snapshot}"
        ) from exc

    log.info("MFA completed successfully | %s", await _page_snapshot(page))
    await human.screenshot(page, "mfa_success_dashboard")


async def login(
    context: BrowserContext,
    config: WaystarConfig,
    human: HumanSettings,
) -> Page:
    log.info("Login started for user %s", mask_secret(config.username))
    page = await context.new_page()

    log.debug("Opening login page: %s", LOGIN_URL)
    retry = retry_settings_from_config(config)

    async def _goto_login():
        return await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=90_000)

    response = await retry_transient(
        _goto_login,
        label="login page navigation",
        max_attempts=retry.max_attempts,
        base_delay_sec=retry.base_delay_sec,
    )
    if response:
        log.debug("Login page HTTP status: %s", response.status)
    log.info("Login page loaded | %s", await _page_snapshot(page))
    await human.screenshot(page, "login_page")
    await human.delay()

    username_input = page.locator(
        'input[name="Username"], input[name="username"], input#Username, input[type="text"]'
    ).first
    password_input = page.locator(
        'input[name="Password"], input[name="password"], input#Password, input[type="password"]'
    ).first

    await username_input.wait_for(state="visible", timeout=15_000)
    await password_input.wait_for(state="visible", timeout=15_000)
    log.debug("Login form fields found")

    await human_type(username_input, config.username, human)
    await human.delay()
    await human_type(password_input, config.password, human)
    await human.screenshot(page, "login_filled")
    log.debug("Credentials entered (not logged)")

    submit = page.locator(
        'input[type="submit"], button[type="submit"], '
        'button:has-text("Log in"), button:has-text("Login")'
    ).first
    await human.delay()
    log.info("Login form submitted, waiting for redirect or MFA...")
    try:
        async with page.expect_navigation(timeout=45_000, url=_login_outcome_url):
            await human_click(submit, page, human)
    except PlaywrightTimeoutError:
        snapshot = await _page_snapshot(page)
        inline_error = await _login_error_on_page(page)
        if inline_error:
            log.error("Login failed — page error: %s | %s", inline_error, snapshot)
            raise RuntimeError(f"Login failed: {inline_error} | {snapshot}") from None
        if _is_login_url(page.url):
            hint = _login_failure_hint(config)
            log.error("Login failed — still on login page | %s", snapshot)
            raise RuntimeError(
                f"Login failed: no redirect after submit.{hint} {snapshot}"
            ) from None
        if MFA_URL_FRAGMENT not in page.url and not POST_LOGIN_URL.match(page.url):
            log.error("Login timed out waiting for redirect | %s", snapshot)
            raise RuntimeError(
                f"Login timed out (no redirect to claims/general/MFA). {snapshot}"
            ) from None

    await human.screenshot(page, "after_login_submit")
    await _await_final_login_destination(page)

    if MFA_URL_FRAGMENT in page.url:
        await handle_mfa(page, config, human)
    elif _is_login_url(page.url):
        snapshot = await _page_snapshot(page)
        inline_error = await _login_error_on_page(page)
        hint = _login_failure_hint(config)
        detail = inline_error or "still on login page after redirect"
        log.error("Login failed — %s | %s", detail, snapshot)
        raise RuntimeError(f"Login failed: {detail}.{hint} {snapshot}")

    if not POST_LOGIN_URL.match(page.url):
        snapshot = await _page_snapshot(page)
        log.error("Unexpected post-login URL | %s", snapshot)
        raise RuntimeError(f"Unexpected URL after login: {snapshot}")

    log.info("Login succeeded | %s", await _page_snapshot(page))
    await human.screenshot(page, "login_success")
    return page


def _login_failure_hint(config: WaystarConfig) -> str:
    parts = [" Try `--fresh-login` to discard a stale saved session."]
    if config.headless:
        parts.append(
            " Headless login may be blocked — use `--headed` if MFA or captcha appears."
        )
    elif not config.security_answer:
        parts.append(
            " Set WAYSTAR_SECURITY_ANSWER in .env if a security question is shown."
        )
    return "".join(parts)


async def clear_saved_session(
    context: BrowserContext,
    path: Path = STORAGE_STATE_PATH,
) -> None:
    await context.clear_cookies()
    if path.exists():
        path.unlink()
        log.info("Deleted stale session file %s", path)
    log.debug("Browser cookies cleared")


async def extend_session(page: Page, retry: RetrySettings | None = None) -> bool:
    """Ping Waystar session extend endpoint to reset the 30-minute idle timeout."""
    settings = retry or RetrySettings(max_attempts=2)

    async def _extend():
        return await page.request.get(SESSION_EXTEND_URL)

    try:
        response = await retry_transient(
            _extend,
            label="session extend",
            max_attempts=settings.max_attempts,
            base_delay_sec=settings.base_delay_sec,
        )
    except PlaywrightError as exc:
        log.warning("Session extend request failed: %s", exc)
        return False

    if response.ok:
        log.debug("Session extended (HTTP %s)", response.status)
        return True

    log.warning("Session extend returned HTTP %s", response.status)
    return False


async def save_storage_state(context: BrowserContext, path: Path = STORAGE_STATE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    await context.storage_state(path=str(path))
    log.info("Session saved to %s", path)


async def create_context(
    playwright,
    config: WaystarConfig,
    reuse_session: bool = True,
):
    log.info("Launching browser (headless=%s, slow_mo=%sms)", config.headless, config.slow_mo_ms)
    browser = await playwright.chromium.launch(
        headless=config.headless,
        slow_mo=config.slow_mo_ms,
    )

    context_kwargs: dict = {
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "viewport": {"width": 1366, "height": 768},
        "locale": "en-US",
        "timezone_id": "America/New_York",
    }
    if reuse_session and STORAGE_STATE_PATH.exists():
        context_kwargs["storage_state"] = str(STORAGE_STATE_PATH)
        log.info("Reusing saved session from %s", STORAGE_STATE_PATH)
    else:
        log.info("No saved session — fresh login will be required")

    context = await browser.new_context(**context_kwargs)
    log.debug("Browser context created")
    return browser, context


async def ensure_authenticated(
    context: BrowserContext,
    config: WaystarConfig,
    human: HumanSettings,
) -> Page:
    """Validate session by opening the claims listing (not just the dashboard)."""
    log.info("Checking existing session via claims listing")
    page = await context.new_page()
    retry = retry_settings_from_config(config)

    try:
        await navigate_to_claims_listing(page, human, retry=retry)
    except SessionExpiredError:
        log.warning("Session expired — clearing saved session and logging in")
        await page.close()
        await clear_saved_session(context)
        page = await login(context, config, human)
        await save_storage_state(context)
        await navigate_to_claims_listing(page, human, retry=retry)

    if MFA_URL_FRAGMENT in page.url:
        log.warning("MFA required when opening claims listing")
        await handle_mfa(page, config, human)
        await save_storage_state(context)
        await navigate_to_claims_listing(page, human, retry=retry)

    if _is_login_url(page.url):
        snapshot = await _page_snapshot(page)
        raise RuntimeError(
            f"Authentication failed — claims listing still on login. "
            f"Run with `--fresh-login --headed`. {snapshot}"
        )

    log.info("Session valid — claims listing accessible | %s", await _page_snapshot(page))
    await human.screenshot(page, "claims_listing")
    return page


async def resolve_cust_id(page: Page, config: WaystarConfig) -> str:
    if config.cust_id:
        log.info("Using CustId from config: %s", config.cust_id)
        return config.cust_id

    log.info("Fetching CustId from GetChildCusts")
    retry = retry_settings_from_config(config)

    async def _fetch_custs():
        return await page.request.get(
            GET_CHILD_CUSTS_URL,
            headers={"X-Requested-With": "XMLHttpRequest"},
        )

    response = await retry_transient(
        _fetch_custs,
        label="GetChildCusts",
        max_attempts=retry.max_attempts,
        base_delay_sec=retry.base_delay_sec,
    )
    body = await response.text()
    log.debug("GetChildCusts HTTP %s body=%s", response.status, body[:200])

    if not response.ok:
        log.error("GetChildCusts failed with HTTP %s", response.status)
        raise RuntimeError(f"GetChildCusts failed: HTTP {response.status}")

    customers = json.loads(body)
    if not customers:
        log.error("GetChildCusts returned empty list")
        raise RuntimeError("No child customers returned from GetChildCusts")

    match = re.search(r"\((\d+)\)\s*$", customers[0])
    if not match:
        log.error("Could not parse CustId from: %r", customers[0])
        raise RuntimeError(f"Could not parse CustId from: {customers[0]!r}")

    cust_id = match.group(1)
    log.info("Resolved CustId=%s from %r", cust_id, customers[0])
    return cust_id


async def navigate_to_claims_listing(
    page: Page,
    human: HumanSettings | None = None,
    *,
    retry: RetrySettings | None = None,
) -> None:
    """Open claims listing and wait until the search form is ready.

    Uses domcontentloaded + CSRF token — not networkidle, because Waystar/Pendo
    keep background requests open and networkidle never fires.
    """
    settings = retry or RetrySettings()
    log.info("Navigating to claims listing: %s", CLAIMS_LISTING_URL)

    async def _goto_listing():
        return await page.goto(
            CLAIMS_LISTING_URL, wait_until="domcontentloaded", timeout=90_000
        )

    await retry_transient(
        _goto_listing,
        label="claims listing navigation",
        max_attempts=settings.max_attempts,
        base_delay_sec=settings.base_delay_sec,
    )
    snapshot = await _page_snapshot(page)

    if _is_login_url(page.url):
        log.warning("Claims listing redirected to login | %s", snapshot)
        raise SessionExpiredError(
            f"Session cannot access claims listing (login redirect). {snapshot}"
        )

    if MFA_URL_FRAGMENT in page.url:
        log.warning("Claims listing redirected to MFA | %s", snapshot)
        return

    if not _is_claims_listing_url(page.url):
        log.error("Claims listing unexpected URL | %s", snapshot)
        raise RuntimeError(f"Claims listing did not load expected URL. {snapshot}")

    listing_marker = page.locator(CLAIMS_LISTING_MARKER).first
    try:
        await listing_marker.wait_for(state="attached", timeout=60_000)
    except PlaywrightTimeoutError as exc:
        snapshot = await _page_snapshot(page)
        log.error("Claims listing page not ready (missing listing UI) | %s", snapshot)
        raise RuntimeError(
            f"Claims listing did not load search UI within 60s. {snapshot}"
        ) from exc

    token_input = page.locator('input[name="__RequestVerificationToken"]').first
    try:
        await token_input.wait_for(state="attached", timeout=15_000)
    except PlaywrightTimeoutError as exc:
        snapshot = await _page_snapshot(page)
        log.error("Claims listing form not ready (no CSRF token) | %s", snapshot)
        raise RuntimeError(
            f"Claims listing did not load CSRF token within 15s. {snapshot}"
        ) from exc

    log.info("Claims listing ready | %s", await _page_snapshot(page))
    if human:
        await human.screenshot(page, "claims_listing")


async def get_verification_token(page: Page) -> str:
    log.debug("Reading __RequestVerificationToken from %s", page.url)
    token_input = page.locator('input[name="__RequestVerificationToken"]').first
    try:
        await token_input.wait_for(state="attached", timeout=30_000)
    except PlaywrightTimeoutError as exc:
        log.error("CSRF token input not found | %s", await _page_snapshot(page))
        raise RuntimeError(
            f"CSRF token not found on page | {await _page_snapshot(page)}"
        ) from exc

    token = await token_input.get_attribute("value")
    if not token:
        log.error("CSRF token input is empty | %s", await _page_snapshot(page))
        raise RuntimeError("Could not read __RequestVerificationToken from page")

    log.debug("CSRF token acquired (prefix=%s...)", mask_secret(token, visible=6))
    return token


async def refresh_verification_token(
    page: Page,
    human: HumanSettings | None = None,
    *,
    retry: RetrySettings | None = None,
) -> str:
    """Read CSRF token, re-opening claims listing if the tab is on an error page."""
    url = page.url
    if _is_browser_error_page(url) or not _is_claims_listing_url(url):
        log.warning(
            "Tab not on claims listing (%s) — re-navigating before CSRF refresh",
            url,
        )
        await navigate_to_claims_listing(page, human, retry=retry)
    return await get_verification_token(page)
