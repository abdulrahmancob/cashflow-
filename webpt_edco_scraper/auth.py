import asyncio
import base64
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
    STORAGE_STATE_PATH,
    WebPTConfig,
)
from logging_config import get_logger, mask_secret

log = get_logger("auth")

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


@dataclass
class SessionState:
    csrf_token: str | None = None
    vega_user_id: str | None = None


@dataclass
class ClinicInfo:
    company_id: str
    facility_id: str
    name: str


def _is_auth_redirect_url(url: str) -> bool:
    u = (url or "").lower()
    return (
        "login.webpt.com" in u
        or "auth.webpt.com" in u
        or "auth0" in u
    )


def _is_login_url(url: str) -> bool:
    return _is_auth_redirect_url(url)


def _oust_yes_button(page: Page):
    return (
        page.get_by_role("button", name=re.compile(r"yes.*oust", re.I))
        .or_(page.locator('input[type="submit"][value*="oust" i]'))
        .or_(page.get_by_text(re.compile(r"yes,\s*oust them", re.I)))
        .or_(page.locator('button:has-text("oust them"), a:has-text("oust them")'))
    ).first


async def dismiss_already_signed_in_prompt(page: Page) -> bool:
    """Click 'Yes, oust them!' if single-session conflict dialog is shown."""
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
        await asyncio.sleep(1)
        log.info("Dismissed 'already signed in' prompt")
        return True
    except Exception as exc:
        log.warning("Failed to dismiss 'already signed in' prompt: %s", exc)
        return False


async def _settle_app_page(page: Page) -> None:
    await dismiss_already_signed_in_prompt(page)


def _page_needs_auth(page: Page) -> bool:
    return _is_auth_redirect_url(page.url)


async def ensure_page_authenticated(
    page: Page,
    context: BrowserContext,
    config: WebPTConfig,
) -> SessionState:
    """Re-auth if the visible page landed on SSO/login after navigation."""
    if _page_needs_auth(page):
        log.warning("Page on auth redirect (%s) — re-authenticating", page.url)
        return await ensure_authenticated(page, context, config)
    await _settle_app_page(page)
    if _page_needs_auth(page):
        log.warning("Auth redirect after oust prompt (%s) — re-authenticating", page.url)
        return await ensure_authenticated(page, context, config)
    return await refresh_csrf(context, page)


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

    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        if "app.webpt.com" in page.url and not _is_auth_redirect_url(page.url):
            await _settle_app_page(page)
            log.info("Auth0: redirected to app (%s)", page.url)
            return True
        await asyncio.sleep(0.5)

    if not _is_auth_redirect_url(page.url):
        return True
    log.warning("Auth0: login timed out (still on %s)", page.url)
    return False


async def _try_automated_auth0_login(page: Page, config: WebPTConfig) -> bool:
    """Fill Auth0 login form (identifier + password, or password-only step)."""
    if not _is_auth_redirect_url(page.url) and "auth0" not in page.url.lower():
        log.debug("Auth0: not on login page (%s)", page.url)
        return False

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
            await username_input.wait_for(state="visible", timeout=8000)
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


async def login(
    page: Page,
    context: BrowserContext,
    config: WebPTConfig,
    *,
    fresh: bool = False,
) -> None:
    if fresh:
        await context.clear_cookies()
        log.info("Cleared browser cookies for fresh login")

    log.info("Opening WebPT (entry: %s)", LOGIN_ENTRY_URL)
    await page.goto(LOGIN_ENTRY_URL, wait_until="domcontentloaded", timeout=90000)
    await asyncio.sleep(2)

    if not fresh and await _session_ready(page, context):
        log.info("Already authenticated")
        return

    if _is_auth_redirect_url(page.url):
        log.info("Auth0 login page detected (%s)", page.url)
    elif await _has_session_cookies(context):
        log.warning(
            "Session cookies present but login incomplete (url=%s) — retrying Auth0",
            page.url,
        )

    if await _try_automated_auth0_login(page, config):
        log.info("Automated Auth0 login succeeded")
        await _settle_app_page(page)
        return

    if config.headless:
        raise SessionExpiredError(
            "Automated login failed and headless mode is on. "
            "Run without --headless to log in manually, then retry."
        )

    await wait_for_manual_login(page, context, timeout_sec=300)


async def ensure_authenticated(
    page: Page,
    context: BrowserContext,
    config: WebPTConfig,
    *,
    fresh_login: bool = False,
) -> SessionState:
    if not fresh_login and await is_session_valid(page, context):
        log.info("Existing session is valid")
        state = await refresh_csrf(context, page)
        if state.csrf_token:
            return state
        log.warning("Saved session missing CSRF — performing login")

    log.info("Session invalid or missing — performing login")
    await login(page, context, config, fresh=fresh_login)
    await page.goto(DASHBOARD_URL, wait_until="networkidle", timeout=90000)
    await _settle_app_page(page)
    try:
        await page.wait_for_function(
            "() => localStorage.getItem('vega_auth_csrf') || document.cookie.includes('vega_emr_auth')",
            timeout=60000,
        )
    except Exception:
        log.warning("Timed out waiting for CSRF markers after login")
    if not await _has_session_cookies(context):
        raise SessionExpiredError("Login completed but session cookies are missing")
    if not await _session_ready(page, context):
        raise SessionExpiredError(
            "Login completed but session is not valid (CSRF token missing)"
        )
    await save_storage_state(context)
    return await refresh_csrf(context, page)


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
    """Switch active clinic via #ClinicChange dropdown."""
    await page.goto(DASHBOARD_URL, wait_until="domcontentloaded", timeout=30000)
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
            log.warning("Could not switch clinic (continuing): %s", exc)
            return

    await asyncio.sleep(2)
    await page.wait_for_load_state("networkidle", timeout=30000)


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
