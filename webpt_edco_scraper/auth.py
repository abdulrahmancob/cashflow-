import asyncio
import base64
import contextvars
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import BrowserContext, Page, async_playwright

from config import (
    BASE_URL,
    DASHBOARD_URL,
    GATEWAY_GRAPHQL_URL,
    GET_NEW_PATIENTS_URL,
    LOGIN_ENTRY_URL,
    SCHEDULER_INDEX_URL,
    STORAGE_STATE_PATH,
    WebPTConfig,
)
from logging_config import get_logger, mask_secret

log = get_logger("auth")

# Parallel-download sets this False so we never click "Yes, oust them!" mid-run.
_allow_oust_var: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "webpt_allow_oust", default=True
)


def set_allow_oust(allow: bool) -> contextvars.Token:
    return _allow_oust_var.set(allow)


def reset_allow_oust(token: contextvars.Token) -> None:
    _allow_oust_var.reset(token)

SSO_MUTATION = """
mutation SSOEmrAuthenticate($data: SSOEmrAuthenticateInput!) {
  ssoEmrAuthenticate(data: $data) {
    success
    csrfToken
    userId
  }
}
"""


class SessionExpiredError(Exception):
    """Saved session is no longer valid."""


class ClinicSwitchError(Exception):
    """Raised when the active clinic could not be switched/verified."""


@dataclass
class SessionState:
    csrf_token: str | None = None
    vega_user_id: str | None = None


@dataclass
class ClinicInfo:
    company_id: str
    facility_id: str
    name: str


def is_auth_redirect_url(url: str) -> bool:
    u = (url or "").lower()
    return (
        "login.webpt.com" in u
        or "auth.webpt.com" in u
        or "auth0" in u
    )


# Back-compat alias for internal call sites / tests.
_is_auth_redirect_url = is_auth_redirect_url


def _is_login_url(url: str) -> bool:
    return is_auth_redirect_url(url)


AUTH_EXPIRED = "auth_expired"
# Refresh JWT when fewer than this many seconds remain (vega_emr_auth ~15 min).
VEGA_JWT_MIN_TTL_SEC = 180


def vega_jwt_seconds_remaining(cookies: list[dict]) -> float | None:
    """Return seconds until vega_emr_auth JWT exp, or None if cookie missing/unreadable."""
    now = time.time()
    for cookie in cookies:
        if cookie.get("name") != "vega_emr_auth":
            continue
        token = cookie.get("value") or ""
        parts = token.split(".")
        if len(parts) < 2:
            continue
        try:
            payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
            payload = json.loads(base64.urlsafe_b64decode(payload_b64))
            exp = payload.get("exp")
            if exp is None:
                return None
            return float(exp) - now
        except Exception:
            continue
    return None


async def ensure_session_fresh(
    page: Page,
    context: BrowserContext,
    config: WebPTConfig,
    *,
    facility_id: str | None = None,
    company_id: str | None = None,
    min_ttl_sec: int = VEGA_JWT_MIN_TTL_SEC,
    allow_oust: bool = True,
    force: bool = False,
) -> SessionState:
    """Re-authenticate when vega JWT is missing, near expiry, or force=True."""
    cookies = await context.cookies()
    remaining = vega_jwt_seconds_remaining(cookies)
    if (
        not force
        and remaining is not None
        and remaining > min_ttl_sec
    ):
        return await refresh_csrf(context, page)

    if force:
        log.info("Forcing session refresh after auth_expired download")
    elif remaining is None:
        log.info("vega_emr_auth missing or unreadable — refreshing session")
    else:
        log.info(
            "vega_emr_auth TTL %.0fs < %ds — refreshing session",
            remaining,
            min_ttl_sec,
        )
    session = await ensure_authenticated(
        page, context, config, allow_oust=allow_oust
    )
    fid = facility_id
    cid = company_id or config.company_id
    if fid and cid:
        await switch_clinic(page, company_id=cid, facility_id=str(fid))
        await page.goto(
            SCHEDULER_INDEX_URL,
            wait_until="domcontentloaded",
            timeout=45000,
        )
        session = await ensure_page_authenticated(
            page, context, config, allow_oust=allow_oust
        )
    return session


def _oust_yes_button(page: Page):
    return (
        page.get_by_role("button", name=re.compile(r"yes.*oust", re.I))
        .or_(page.locator('input[type="submit"][value*="oust" i]'))
        .or_(page.get_by_text(re.compile(r"yes,\s*oust them", re.I)))
        .or_(page.locator('button:has-text("oust them"), a:has-text("oust them")'))
    ).first


async def dismiss_already_signed_in_prompt(
    page: Page, *, allow_oust: bool | None = None
) -> bool:
    """Click 'Yes, oust them!' if single-session conflict dialog is shown.

    When allow_oust is False (parallel-download), refuse to kick the other
    session — that usually means kicking our own shared browser session.
    """
    if allow_oust is None:
        allow_oust = _allow_oust_var.get()

    try:
        body_text = (await page.locator("body").inner_text(timeout=2000)).lower()
    except Exception:
        body_text = ""

    btn = _oust_yes_button(page)
    try:
        visible = await btn.is_visible()
    except Exception:
        visible = False

    if "already signed in" not in body_text and not visible:
        return False

    if not allow_oust:
        raise SessionExpiredError(
            "WebPT single-session conflict detected; refusing to click "
            "'Yes, oust them!' (allow_oust=False). Use one patient worker "
            "and avoid concurrent logins."
        )

    try:
        if not visible:
            await btn.wait_for(state="visible", timeout=3000)
    except Exception:
        if "already signed in" not in body_text:
            return False

    try:
        log.info("WebPT: clicking 'Yes, oust them!' on single-session prompt")
        await btn.click(timeout=5000)
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=30000)
        except Exception:
            pass
        if not _is_on_app_domain(page.url):
            await _wait_for_app_domain(page, timeout_ms=60000)
        await asyncio.sleep(1)
        log.info("Dismissed 'already signed in' prompt")
        return True
    except Exception as exc:
        log.warning("Failed to dismiss 'already signed in' prompt: %s", exc)
        return False


async def _settle_app_page(page: Page, *, allow_oust: bool | None = None) -> None:
    await dismiss_already_signed_in_prompt(page, allow_oust=allow_oust)


def _is_on_app_domain(url: str) -> bool:
    return "app.webpt.com" in (url or "").lower()


def _is_post_login_interstitial_url(url: str) -> bool:
    u = (url or "").lower()
    return "/redirect/" in u or "delegator.webpt.com" in u


def _is_auth0_oops_url(url: str) -> bool:
    """Auth0 lost OAuth transaction state (common after cookie clears mid-login)."""
    u = (url or "").lower()
    return "login.webpt.com" in u and "/authorize/resume" in u


async def _is_auth0_oops_page(page: Page) -> bool:
    """True when Auth0 shows 'couldn't find your session' / Oops page."""
    if not _is_auth0_oops_url(page.url):
        # Also catch oops on other login.webpt.com paths via body text.
        if "login.webpt.com" not in (page.url or "").lower():
            return False
    try:
        body = (await page.locator("body").inner_text(timeout=3000)).lower()
    except Exception:
        return _is_auth0_oops_url(page.url)
    markers = (
        "couldn't find your session",
        "could not find your session",
        "something went wrong",
        "opened too many login",
    )
    return any(m in body for m in markers)


async def _recover_from_auth0_oops(page: Page, context: BrowserContext) -> None:
    """Abort broken OAuth resume; start a fresh authorize via app entry."""
    log.warning(
        "Auth0 Oops/session-lost on %s — clearing cookies and restarting via app entry",
        page.url,
    )
    try:
        await context.clear_cookies()
    except Exception as exc:
        log.debug("clear_cookies during Oops recovery failed: %s", exc)
    await _goto_login_entry(page)
    await _wait_for_auth0_login_page(page, timeout_sec=45)


async def _wait_for_app_domain(page: Page, *, timeout_ms: int = 60000) -> bool:
    """Wait until the browser lands on app.webpt.com."""
    if _is_on_app_domain(page.url):
        return True
    try:
        await page.wait_for_url(
            re.compile(r"https?://app\.webpt\.com/", re.I),
            timeout=timeout_ms,
        )
        return True
    except Exception:
        return _is_on_app_domain(page.url)


async def _wait_for_dashboard(page: Page, *, timeout_ms: int = 90000) -> None:
    """Wait for Auth0 post-login redirects to settle on dashboard.php."""
    deadline = time.monotonic() + timeout_ms / 1000
    url = page.url.lower()

    if "dashboard.php" in url and _is_on_app_domain(page.url):
        try:
            remaining = max(1000, int((deadline - time.monotonic()) * 1000))
            await page.wait_for_load_state("domcontentloaded", timeout=remaining)
        except Exception:
            pass
        return

    await _settle_app_page(page)

    if _is_post_login_interstitial_url(page.url) or not _is_on_app_domain(page.url):
        log.info("Waiting for post-login redirect (current: %s)", page.url)
        while time.monotonic() < deadline:
            await _settle_app_page(page)
            current = page.url.lower()
            if _is_on_app_domain(current) and "dashboard.php" in current:
                log.info("Redirect complete (%s)", page.url)
                try:
                    await page.wait_for_load_state("domcontentloaded", timeout=30000)
                except Exception:
                    pass
                return
            if _is_on_app_domain(current):
                break
            await asyncio.sleep(0.5)

    if _is_on_app_domain(page.url) and "dashboard.php" in page.url.lower():
        return

    remaining_ms = max(1000, int((deadline - time.monotonic()) * 1000))
    log.info("Navigating to dashboard from %s", page.url)
    try:
        await page.goto(DASHBOARD_URL, wait_until="domcontentloaded", timeout=remaining_ms)
    except Exception as exc:
        msg = str(exc)
        if "ERR_ABORTED" in msg and "dashboard.php" in page.url.lower():
            log.info("Dashboard navigation aborted but already on dashboard (%s)", page.url)
            return
        if "ERR_ABORTED" in msg:
            for _ in range(30):
                await asyncio.sleep(0.5)
                if "dashboard.php" in page.url.lower() and _is_on_app_domain(page.url):
                    log.info("Dashboard reached after aborted navigation (%s)", page.url)
                    return
        raise

    await _wait_for_app_domain(page, timeout_ms=max(1000, remaining_ms))
    if "dashboard.php" not in page.url.lower():
        try:
            await page.goto(DASHBOARD_URL, wait_until="domcontentloaded", timeout=30000)
        except Exception:
            pass


def _page_needs_auth(page: Page) -> bool:
    return _is_auth_redirect_url(page.url)


async def ensure_page_authenticated(
    page: Page,
    context: BrowserContext,
    config: WebPTConfig,
    *,
    allow_oust: bool | None = None,
) -> SessionState:
    """Re-auth if the visible page landed on SSO/login after navigation."""
    token = None
    if allow_oust is not None:
        token = set_allow_oust(allow_oust)
    try:
        if _page_needs_auth(page):
            log.warning("Page on auth redirect (%s) — re-authenticating", page.url)
            return await ensure_authenticated(
                page, context, config, allow_oust=allow_oust
            )
        await _settle_app_page(page, allow_oust=allow_oust)
        if _page_needs_auth(page):
            log.warning(
                "Auth redirect after session prompt (%s) — re-authenticating", page.url
            )
            return await ensure_authenticated(
                page, context, config, allow_oust=allow_oust
            )
        return await refresh_csrf(context, page)
    finally:
        if token is not None:
            reset_allow_oust(token)


async def create_context(
    playwright,
    config: WebPTConfig,
    *,
    storage_state: Path | None = None,
) -> BrowserContext:
    launch_kwargs: dict = {"headless": config.headless}
    try:
        browser = await playwright.chromium.launch(channel="chrome", **launch_kwargs)
    except Exception:
        browser = await playwright.chromium.launch(**launch_kwargs)
    kwargs: dict = {
        "base_url": BASE_URL,
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
        ),
        "viewport": {"width": 1280, "height": 800},
    }
    state_path = storage_state or STORAGE_STATE_PATH
    if state_path.exists():
        kwargs["storage_state"] = str(state_path)
        log.info("Loading storage state from %s", state_path)
    return await browser.new_context(**kwargs)


async def safe_close_context(context: BrowserContext) -> None:
    try:
        await context.close()
    except Exception as exc:
        log.warning("Browser cleanup failed (ignored): %s", exc)


async def restart_browser(
    playwright,
    config: WebPTConfig,
    *,
    old_context: BrowserContext,
    clinic: ClinicInfo | None = None,
    reason: str = "driver connection lost",
) -> tuple[BrowserContext, Page, SessionState]:
    log.warning("Restarting browser (%s)", reason)
    await safe_close_context(old_context)
    context = await create_context(playwright, config)
    page = await context.new_page()
    session = await ensure_authenticated(page, context, config)
    if clinic is not None:
        await switch_clinic(
            page,
            company_id=clinic.company_id,
            facility_id=clinic.facility_id,
        )
        await page.goto(
            SCHEDULER_INDEX_URL,
            wait_until="domcontentloaded",
            timeout=30000,
        )
        session = await ensure_authenticated(page, context, config)
    return context, page, session


async def save_storage_state(context: BrowserContext, path: Path | None = None) -> None:
    dest = path or STORAGE_STATE_PATH
    await context.storage_state(path=str(dest))
    log.info("Saved storage state to %s", dest)


def _idem_cookie_from_cookies(cookies: list[dict]) -> str | None:
    preferred_domains = ("app.webpt.com", ".webpt.com")
    for domain in preferred_domains:
        for cookie in cookies:
            if cookie.get("domain") == domain and cookie.get("name") == "app_webpt_com_sess":
                return cookie.get("value")
    for domain in preferred_domains:
        for cookie in cookies:
            if cookie.get("domain") == domain and cookie.get("name") == "IDEM":
                return cookie.get("value")
    for cookie in cookies:
        if cookie.get("name") in ("app_webpt_com_sess", "IDEM"):
            return cookie.get("value")
    return None


def _session_from_vega_auth_cookie(cookies: list[dict]) -> SessionState | None:
    for cookie in cookies:
        if cookie.get("name") != "vega_emr_auth":
            continue
        token = cookie.get("value") or ""
        parts = token.split(".")
        if len(parts) < 2:
            continue
        try:
            payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
            payload = json.loads(base64.urlsafe_b64decode(payload_b64))
            return SessionState(
                csrf_token=payload.get("csrfToken"),
                vega_user_id=payload.get("id"),
            )
        except Exception:
            continue
    return None


async def refresh_csrf(
    context: BrowserContext, page: Page | None = None
) -> SessionState:
    if page is not None:
        try:
            data = await page.evaluate(
                """() => ({
                    csrf: localStorage.getItem('vega_auth_csrf'),
                    userId: (() => {
                        const m = document.cookie.match(/Marketing=([^;]+)/);
                        if (!m) return null;
                        try {
                            return JSON.parse(decodeURIComponent(m[1])).user.userId;
                        } catch { return null; }
                    })(),
                })"""
            )
            if data.get("csrf"):
                log.debug("CSRF from localStorage")
                return SessionState(
                    csrf_token=data["csrf"],
                    vega_user_id=str(data["userId"]) if data.get("userId") else None,
                )
        except Exception as exc:
            log.debug("CSRF localStorage read failed: %s", exc)

    cookies = await context.cookies()
    vega_state = _session_from_vega_auth_cookie(cookies)
    if vega_state and vega_state.csrf_token:
        log.debug("CSRF from vega_emr_auth cookie")
        return vega_state

    session_id = _idem_cookie_from_cookies(cookies)
    if not session_id:
        log.warning("No IDEM cookie found — CSRF refresh may fail")
        return SessionState()

    payload = {
        "operationName": "SSOEmrAuthenticate",
        "query": SSO_MUTATION,
        "variables": {"data": {"sessionId": session_id}},
    }
    response = await context.request.post(
        GATEWAY_GRAPHQL_URL,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Origin": BASE_URL,
            "Referer": f"{BASE_URL}/",
        },
        data=json.dumps(payload),
    )
    if not response.ok:
        log.warning("SSOEmrAuthenticate failed: HTTP %s", response.status)
        return SessionState()

    body = await response.json()
    auth = (body.get("data") or {}).get("ssoEmrAuthenticate") or {}
    if not auth.get("success"):
        log.warning("SSOEmrAuthenticate returned success=false")
        return SessionState()

    state = SessionState(
        csrf_token=auth.get("csrfToken"),
        vega_user_id=auth.get("userId"),
    )
    log.debug("CSRF refreshed (vega user %s)", state.vega_user_id)
    return state


async def _has_session_cookies(context: BrowserContext) -> bool:
    cookies = await context.cookies()
    names = {c.get("name") for c in cookies}
    return bool(names.intersection({"wpt_sso_token", "IDEM", "app_webpt_com_sess"}))


async def _probe_session(context: BrowserContext) -> bool:
    """Check session via cookies + API without navigating the open page."""
    if not await _has_session_cookies(context):
        return False
    try:
        probe = await context.request.get(
            GET_NEW_PATIENTS_URL,
            headers={
                "Accept": "application/json",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": DASHBOARD_URL,
            },
        )
        if probe.status in (401, 403):
            return False
        if _is_login_url(probe.url):
            return False
        if not probe.ok:
            return False
        content_type = (probe.headers.get("content-type") or "").lower()
        if "json" not in content_type:
            return False
        await probe.json()
        return True
    except Exception as exc:
        log.debug("Session probe failed: %s", exc)
        return False


async def _session_ready(page: Page, context: BrowserContext) -> bool:
    """True only when off the login page and CSRF can be obtained."""
    if _is_auth_redirect_url(page.url):
        return False
    if "app.webpt.com" not in page.url.lower():
        return False
    if not await _probe_session(context):
        return False
    state = await refresh_csrf(context, page)
    return bool(state.csrf_token)


async def is_session_valid(page: Page, context: BrowserContext) -> bool:
    if not await _has_session_cookies(context):
        return False
    try:
        response = await page.goto(
            DASHBOARD_URL, wait_until="domcontentloaded", timeout=30000
        )
        final_url = response.url if response else page.url
        if _is_auth_redirect_url(final_url) or _is_auth_redirect_url(page.url):
            return False
        await _settle_app_page(page)
        return await _probe_session(context)
    except Exception as exc:
        log.debug("Session check failed: %s", exc)
        return False


def _auth0_continue_button(page: Page):
    return page.locator(
        'button[type="submit"], button:has-text("Continue"), button[name="action"]'
    ).first


def _auth0_password_locator(page: Page):
    by_role = page.get_by_role("textbox", name=re.compile(r"password", re.I))
    by_name = page.locator('input[name="password"]')
    return by_role.or_(by_name).first


def _auth0_username_locator(page: Page):
    return page.locator(
        'input[name="username"], input[type="email"], input[name="identifier"], '
        '#username, input[inputmode="email"]'
    ).first


async def _auth0_complete_password_step(page: Page, config: WebPTConfig) -> bool:
    password_input = _auth0_password_locator(page)
    try:
        await password_input.wait_for(state="visible", timeout=15000)
        await password_input.wait_for(state="attached", timeout=5000)
    except Exception as exc:
        log.warning("Auth0: password field not visible: %s", exc)
        return False

    try:
        log.info("Auth0: entering password")
        await password_input.click()
        await password_input.press_sequentially(config.password, delay=40)
    except Exception as exc:
        log.warning("Auth0: failed to enter password: %s", exc)
        return False

    submit = _auth0_continue_button(page)
    log.info("Auth0: submitting password")
    try:
        if await submit.count() > 0:
            await submit.click()
        else:
            await page.keyboard.press("Enter")
    except Exception as exc:
        log.warning("Auth0: failed to submit password: %s", exc)
        return False

    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        if _is_on_app_domain(page.url) and not _is_auth_redirect_url(page.url):
            log.info("Auth0: redirected to app (%s)", page.url)
            await _wait_for_dashboard(page)
            await _settle_app_page(page)
            return True
        if _is_post_login_interstitial_url(page.url):
            await _settle_app_page(page)
        await asyncio.sleep(0.5)

    if _is_post_login_interstitial_url(page.url) or (
        not _is_auth_redirect_url(page.url) and not _is_on_app_domain(page.url)
    ):
        log.info(
            "Auth0: still on interstitial after poll — waiting for dashboard (%s)",
            page.url,
        )
        await _wait_for_dashboard(page)
        await _settle_app_page(page)
        return _is_on_app_domain(page.url)

    log.warning("Auth0: login timed out (still on %s)", page.url)
    return False


async def _try_automated_auth0_login(page: Page, config: WebPTConfig) -> bool:
    """Fill Auth0 login form (identifier + password, or password-only step)."""
    if not _is_auth_redirect_url(page.url) and "auth0" not in page.url.lower():
        log.debug("Auth0: not on login page (%s)", page.url)
        return False

    # Auth0 / login.webpt.com can take a few seconds to paint identifier fields.
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=15000)
    except Exception:
        pass
    await asyncio.sleep(1.5)

    on_password_page = "/login/password" in page.url.lower()
    password_input = _auth0_password_locator(page)
    skip_username = on_password_page

    if not skip_username:
        try:
            skip_username = await password_input.is_visible()
            if skip_username:
                log.info("Auth0: password field visible — skipping username step")
        except Exception:
            skip_username = False

    if not skip_username:
        username_input = _auth0_username_locator(page)
        try:
            await username_input.wait_for(state="visible", timeout=25000)
        except Exception as exc:
            try:
                await password_input.wait_for(state="visible", timeout=5000)
                log.info(
                    "Auth0: username hidden — using password-only flow (%s)", page.url
                )
            except Exception:
                log.warning("Auth0: username field not found: %s", exc)
                return False
        else:
            log.info("Auth0: filling username")
            await username_input.fill(config.username)
            continue_btn = _auth0_continue_button(page)
            log.info("Auth0: submitting username")
            if await continue_btn.count() > 0:
                await continue_btn.click()
            else:
                await page.keyboard.press("Enter")

            try:
                await page.wait_for_load_state("domcontentloaded", timeout=15000)
            except Exception:
                pass
    else:
        log.info("Auth0: password-only page detected (%s)", page.url)

    return await _auth0_complete_password_step(page, config)


async def wait_for_manual_login(
    page: Page,
    context: BrowserContext,
    *,
    timeout_sec: float = 300,
) -> None:
    log.info(
        "Complete login in the browser window (up to %.0fs)...", timeout_sec
    )
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        await _settle_app_page(page)
        if await _session_ready(page, context):
            log.info("Manual login detected")
            return
        await asyncio.sleep(1.5)
    raise SessionExpiredError("Timed out waiting for manual login")


async def _goto_login_entry(page: Page) -> None:
    log.info("Opening WebPT (entry: %s)", LOGIN_ENTRY_URL)
    await page.goto(LOGIN_ENTRY_URL, wait_until="domcontentloaded", timeout=90000)
    await asyncio.sleep(2)


async def _wait_for_auth0_login_page(page: Page, *, timeout_sec: float = 20) -> bool:
    """Wait until Auth0 / login.webpt.com form is reachable."""
    if _is_auth_redirect_url(page.url):
        return True
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        if _is_auth_redirect_url(page.url):
            return True
        await asyncio.sleep(0.5)
    return _is_auth_redirect_url(page.url)


async def login(
    page: Page,
    context: BrowserContext,
    config: WebPTConfig,
    *,
    fresh: bool = False,
) -> None:
    if not (config.username or "").strip() or not (config.password or "").strip():
        raise SessionExpiredError(
            "WEBPT_USERNAME/WEBPT_PASSWORD not set — refusing to wait on login page. "
            "Ensure webpt_edco_scraper/.env is loaded (config loads it from package dir)."
        )

    if fresh:
        await context.clear_cookies()
        log.info("Cleared browser cookies for fresh login")

    await _goto_login_entry(page)

    if not fresh and await _session_ready(page, context):
        log.info("Already authenticated")
        return

    # Soft recover: already on app dashboard with cookies — refresh CSRF instead
    # of wiping storage_state (clearing cookies here was forcing useless Auth0 waits).
    if (
        not fresh
        and not _is_auth_redirect_url(page.url)
        and _is_on_app_domain(page.url)
        and await _has_session_cookies(context)
    ):
        await _settle_app_page(page)
        if await _session_ready(page, context):
            log.info("App session recovered on %s without Auth0 re-login", page.url)
            return
        csrf_state = await refresh_csrf(context, page)
        if csrf_state.csrf_token and await _probe_session(context):
            log.info("CSRF recovered on app domain — skipping Auth0")
            return

    # Stale storage_state often lands on delegator interstitial without Auth0
    # fields. Clear cookies once and reopen so automated login can proceed.
    if (
        not fresh
        and not _is_auth_redirect_url(page.url)
        and (
            _is_post_login_interstitial_url(page.url)
            or await _has_session_cookies(context)
        )
        and not _is_on_app_domain(page.url)
    ):
        log.warning(
            "Incomplete session on %s — clearing cookies and retrying fresh login",
            page.url,
        )
        await context.clear_cookies()
        await _goto_login_entry(page)

    if _is_auth_redirect_url(page.url):
        log.info("Auth0 login page detected (%s)", page.url)
    elif not await _wait_for_auth0_login_page(page):
        log.warning(
            "Not on Auth0 after entry (url=%s) — forcing login entry again",
            page.url,
        )
        await _goto_login_entry(page)
        await _wait_for_auth0_login_page(page)

    if not _is_auth_redirect_url(page.url):
        # Soft dashboard / interstitial without Auth0 fields.
        if _is_on_app_domain(page.url) and await _has_session_cookies(context):
            await _settle_app_page(page)
            if await _session_ready(page, context):
                log.info("Staying on app session (%s) — Auth0 not required", page.url)
                return
            csrf_state = await refresh_csrf(context, page)
            if csrf_state.csrf_token and await _probe_session(context):
                log.info("CSRF recovered before Auth0 fallback")
                return
        log.warning(
            "Still not on Auth0 (url=%s) — opening https://login.webpt.com",
            page.url,
        )
        await context.clear_cookies()
        await page.goto(
            "https://login.webpt.com",
            wait_until="domcontentloaded",
            timeout=90000,
        )
        await asyncio.sleep(3)
        await _wait_for_auth0_login_page(page, timeout_sec=45)

    async def _finish_if_logged_in() -> bool:
        if await _session_ready(page, context):
            await _wait_for_dashboard(page)
            await _settle_app_page(page)
            return True
        return False

    async def _abort_oauth_and_restart_via_app() -> None:
        """Full abort only — never clear cookies mid-transaction otherwise."""
        if await _is_auth0_oops_page(page):
            await _recover_from_auth0_oops(page, context)
            return
        log.info("Aborting OAuth transaction — fresh authorize via app entry")
        try:
            await context.clear_cookies()
        except Exception as exc:
            log.debug("clear_cookies on abort failed: %s", exc)
        await _goto_login_entry(page)
        if not await _wait_for_auth0_login_page(page, timeout_sec=45):
            # Fallback only after app entry failed to reach Auth0.
            log.warning(
                "App entry did not reach Auth0 (url=%s) — fallback login.webpt.com",
                page.url,
            )
            await page.goto(
                "https://login.webpt.com",
                wait_until="domcontentloaded",
                timeout=90000,
            )
            await asyncio.sleep(2)
            await _wait_for_auth0_login_page(page, timeout_sec=45)

    if config.headless:
        # Headless: finite automated retries, then fail (no interactive window).
        backoff = 5.0
        for attempt in range(1, 6):
            if await _is_auth0_oops_page(page):
                await _recover_from_auth0_oops(page, context)
            if await _try_automated_auth0_login(page, config):
                log.info("Automated Auth0 login succeeded (headless attempt %s)", attempt)
                await _wait_for_dashboard(page)
                await _settle_app_page(page)
                return
            if await _finish_if_logged_in():
                return
            log.warning(
                "Headless Auth0 attempt %s failed (url=%s) — abort + retry in %.0fs",
                attempt,
                page.url,
                backoff,
            )
            await asyncio.sleep(backoff)
            backoff = min(60.0, backoff * 1.5)
            await _abort_oauth_and_restart_via_app()
        raise SessionExpiredError(
            "Automated login failed and headless mode is on. "
            "Run without --headless: python scraper.py login --fresh-login"
        )

    # Non-headless: one clean OAuth attempt at a time (like a normal browser).
    # Long manual wait WITHOUT clear_cookies / re-navigation mid-flow.
    # Only after a full failure (or Oops) abort and start a new authorize.
    backoff = 5.0
    attempt = 0
    while True:
        attempt += 1
        if await _is_auth0_oops_page(page):
            await _recover_from_auth0_oops(page, context)

        if await _try_automated_auth0_login(page, config):
            log.info("Automated Auth0 login succeeded (attempt %s)", attempt)
            await _wait_for_dashboard(page)
            await _settle_app_page(page)
            return
        if await _finish_if_logged_in():
            return

        log.warning(
            "Auth0 automated attempt %s incomplete (url=%s) — "
            "waiting up to 300s for manual login (no cookie clear)",
            attempt,
            page.url,
        )
        try:
            await wait_for_manual_login(page, context, timeout_sec=300)
            await _wait_for_dashboard(page)
            await _settle_app_page(page)
            return
        except SessionExpiredError:
            pass

        if await _is_auth0_oops_page(page):
            await _recover_from_auth0_oops(page, context)
            continue

        log.info(
            "Manual window elapsed — abort OAuth and retry via app entry in %.0fs "
            "(attempt %s)",
            backoff,
            attempt + 1,
        )
        await asyncio.sleep(backoff)
        backoff = min(90.0, backoff * 1.4)
        try:
            await _abort_oauth_and_restart_via_app()
        except Exception as exc:
            log.warning("Auth0 abort/restart failed: %s — continuing", exc)


async def _log_session_failure_diagnostics(
    page: Page, context: BrowserContext
) -> None:
    has_cookies = await _has_session_cookies(context)
    probe_ok = await _probe_session(context) if has_cookies else False
    csrf_state = await refresh_csrf(context, page)
    log.error(
        "Session validation failed: url=%s cookies=%s probe=%s csrf=%s",
        page.url,
        has_cookies,
        probe_ok,
        bool(csrf_state.csrf_token),
    )


async def ensure_authenticated(
    page: Page,
    context: BrowserContext,
    config: WebPTConfig,
    *,
    fresh_login: bool = False,
    allow_oust: bool | None = None,
) -> SessionState:
    token = None
    if allow_oust is not None:
        token = set_allow_oust(allow_oust)
    try:
        if not fresh_login and await is_session_valid(page, context):
            log.info("Existing session is valid")
            state = await refresh_csrf(context, page)
            if state.csrf_token:
                return state
            log.warning("Saved session missing CSRF — settling app then retry CSRF")
            await _settle_app_page(page, allow_oust=allow_oust)
            state = await refresh_csrf(context, page)
            if state.csrf_token and await _probe_session(context):
                await save_storage_state(context)
                return state

        log.info("Session invalid or missing — performing login")
        await login(page, context, config, fresh=fresh_login)
        await _wait_for_dashboard(page)
        await _settle_app_page(page, allow_oust=allow_oust)
        try:
            await page.wait_for_function(
                "() => localStorage.getItem('vega_auth_csrf') || document.cookie.includes('vega_emr_auth')",
                timeout=60000,
            )
        except Exception:
            log.warning("Timed out waiting for CSRF markers after login")
        if not await _has_session_cookies(context):
            await _log_session_failure_diagnostics(page, context)
            raise SessionExpiredError("Login completed but session cookies are missing")

        ready_deadline = time.monotonic() + 30
        session_ready = False
        while time.monotonic() < ready_deadline:
            if await _session_ready(page, context):
                session_ready = True
                break
            await asyncio.sleep(1)

        if not session_ready:
            await _log_session_failure_diagnostics(page, context)
            raise SessionExpiredError(
                "Login completed but session is not valid (CSRF token missing)"
            )
        await save_storage_state(context)
        return await refresh_csrf(context, page)
    finally:
        if token is not None:
            reset_allow_oust(token)


async def list_clinics(page: Page, company_id: str) -> list[ClinicInfo]:
    """Return clinics from #ClinicChange filtered by company_id."""
    await page.goto(DASHBOARD_URL, wait_until="domcontentloaded", timeout=30000)
    await page.wait_for_selector("#ClinicChange", state="attached", timeout=15000)
    raw = await page.evaluate(
        """(companyId) => {
            const sel = document.querySelector('#ClinicChange');
            if (!sel) return [];
            const prefix = companyId + ',';
            return Array.from(sel.options)
                .filter(o => o.value.startsWith(prefix))
                .map(o => {
                    const parts = o.value.split(',');
                    return {
                        company_id: parts[0],
                        facility_id: parts[1] || '',
                        name: (o.textContent || '').trim(),
                    };
                });
        }""",
        company_id,
    )
    clinics = [
        ClinicInfo(
            company_id=str(c["company_id"]),
            facility_id=str(c["facility_id"]),
            name=str(c["name"]),
        )
        for c in raw
        if c.get("facility_id")
    ]
    log.info("Found %d clinic(s) for company %s", len(clinics), company_id)
    return clinics


async def switch_clinic(
    page: Page,
    *,
    company_id: str,
    facility_id: str,
    user_id: str | None = None,
) -> None:
    """Switch active clinic via #ClinicChange dropdown.

    Raises ClinicSwitchError if the dropdown cannot be set to the target clinic.
    """
    await page.goto(DASHBOARD_URL, wait_until="domcontentloaded", timeout=90000)
    await _settle_app_page(page)

    if user_id is None:
        user_id = await page.evaluate(
            """() => {
                const m = document.cookie.match(/Marketing=([^;]+)/);
                if (!m) return null;
                try {
                    return String(JSON.parse(decodeURIComponent(m[1])).user.userId);
                } catch { return null; }
            }"""
        )

    target_value = f"{company_id},{facility_id}"
    log.info("Switching clinic to %s", target_value)

    changed = await page.evaluate(
        """([targetValue, userId]) => {
            const sel = document.querySelector('#ClinicChange');
            if (!sel) return false;
            const opt = Array.from(sel.options).find(o => o.value === targetValue);
            if (!opt) return false;
            sel.value = targetValue;
            sel.dispatchEvent(new Event('change', { bubbles: true }));
            if (typeof changeClinic !== 'undefined' && changeClinic.change && userId) {
                changeClinic.change(userId, targetValue);
            }
            return true;
        }""",
        [target_value, user_id or ""],
    )

    if not changed:
        log.warning("Clinic switch via JS failed — trying select_option")
        try:
            await page.wait_for_selector("#ClinicChange", state="attached", timeout=15000)
            await page.select_option("#ClinicChange", target_value, timeout=5000)
            await page.evaluate(
                "([uid, val]) => { if (typeof changeClinic !== 'undefined') changeClinic.change(uid, val); }",
                [user_id or "", target_value],
            )
        except Exception as exc:
            raise ClinicSwitchError(
                f"Could not switch clinic to {target_value}: {exc}"
            ) from exc

    await asyncio.sleep(2)
    try:
        await page.wait_for_load_state("networkidle", timeout=30000)
    except Exception:
        pass

    # changeClinic.change often navigates; wait for dropdown to reappear.
    actual = ""
    for _ in range(6):
        try:
            await page.wait_for_selector(
                "#ClinicChange", state="attached", timeout=10000
            )
        except Exception:
            await _settle_app_page(page)
        actual = await page.evaluate(
            """() => {
                const sel = document.querySelector('#ClinicChange');
                return sel ? String(sel.value || '') : '';
            }"""
        )
        if actual == target_value:
            break
        # Soft re-apply if page reloaded with empty/wrong clinic.
        await page.evaluate(
            """([targetValue, userId]) => {
                const sel = document.querySelector('#ClinicChange');
                if (!sel) return;
                const opt = Array.from(sel.options).find(o => o.value === targetValue);
                if (!opt) return;
                sel.value = targetValue;
                sel.dispatchEvent(new Event('change', { bubbles: true }));
                if (typeof changeClinic !== 'undefined' && changeClinic.change && userId) {
                    changeClinic.change(userId, targetValue);
                }
            }""",
            [target_value, user_id or ""],
        )
        await asyncio.sleep(1.5)
        try:
            await page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass

    if actual != target_value:
        raise ClinicSwitchError(
            f"Clinic switch did not stick: wanted {target_value}, got {actual!r}"
        )


async def switch_clinic_and_settle(
    page: Page,
    context: BrowserContext,
    config: WebPTConfig,
    *,
    company_id: str,
    facility_id: str,
    allow_oust: bool | None = None,
) -> SessionState:
    """Switch clinic, open scheduler, and refresh auth/CSRF so patientChart sticks."""
    await switch_clinic(
        page, company_id=company_id, facility_id=str(facility_id)
    )
    await page.goto(
        SCHEDULER_INDEX_URL,
        wait_until="domcontentloaded",
        timeout=45000,
    )
    return await ensure_page_authenticated(
        page, context, config, allow_oust=allow_oust
    )


def ajax_headers(csrf_token: str | None, referer: str) -> dict[str, str]:
    headers = {
        "Accept": "*/*",
        "Content-Type": "application/x-www-form-urlencoded",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": BASE_URL,
        "Referer": referer,
    }
    if csrf_token:
        headers["x-csrf-token"] = csrf_token
    return headers


PATIENT_EXT_DOC_PATTERN = re.compile(
    r"patientExtDoc\.php\?.*ID=(\d+).*CaseID=(\d+)",
    re.IGNORECASE,
)


def parse_patient_ext_doc_url(url: str) -> tuple[int, int] | None:
    match = PATIENT_EXT_DOC_PATTERN.search(url)
    if not match:
        parsed = urlparse(url)
        from urllib.parse import parse_qs

        qs = parse_qs(parsed.query)
        pid = (qs.get("ID") or qs.get("id") or [None])[0]
        case = (qs.get("CaseID") or qs.get("caseid") or [None])[0]
        if pid and case:
            return int(pid), int(case)
        return None
    return int(match.group(1)), int(match.group(2))
