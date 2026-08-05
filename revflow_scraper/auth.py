"""Playwright authentication for WebPT Billing (RevFlow)."""

from __future__ import annotations

import asyncio
import base64
import json
import time
from dataclasses import dataclass
from pathlib import Path

from playwright.async_api import BrowserContext, Page

from config import BILLING_BASE_URL, LOGIN_URL, RevFlowConfig
from logging_config import get_logger, mask_secret

log = get_logger("auth")


class SessionExpiredError(Exception):
    """Saved session is no longer valid."""


@dataclass
class AuthState:
    bearer_token: str | None = None
    user_id: str | None = None
    company_id: str | None = None


def decode_jwt_payload(token: str) -> dict:
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1]
    padding = "=" * (-len(payload) % 4)
    raw = base64.urlsafe_b64decode(payload + padding)
    return json.loads(raw.decode("utf-8"))


def _is_login_url(url: str) -> bool:
    u = (url or "").lower()
    return "billing.revflow.com/login" in u or "/login" in u


def _is_authenticated_url(url: str) -> bool:
    u = (url or "").lower()
    if _is_login_url(u):
        return False
    return "billing.revflow.com" in u


SESSION_EXPIRED_BODY_MARKERS = (
    "server error",
    "session expired",
    "session has expired",
    "please sign in",
    "sign in again",
    "logged out",
    "unauthorized",
)


async def _page_has_login_form(page: Page) -> bool:
    sign_in = page.locator(
        'button:has-text("SIGN IN"), button:has-text("Sign in"), '
        'input[type="submit"][value*="Sign" i]'
    )
    username = page.locator(
        'input[name="username"], input[name="Username"], input#username'
    )
    try:
        if await sign_in.count() > 0 and await username.count() > 0:
            return await sign_in.first.is_visible()
    except Exception:
        pass
    return False


async def is_session_expired_page(page: Page) -> bool:
    if _is_login_url(page.url):
        return True
    if await _page_has_login_form(page):
        return True
    try:
        body = (await page.locator("body").inner_text(timeout=3000)).lower()
    except Exception:
        body = ""
    return any(marker in body for marker in SESSION_EXPIRED_BODY_MARKERS)


async def assert_authenticated_page(page: Page) -> None:
    if await is_session_expired_page(page):
        raise SessionExpiredError(
            f"RevFlow session expired or server error — login required | {await _page_snapshot(page)}"
        )


DASHBOARD_MARKERS = (
    'input[placeholder*="patient" i]',
    'input[placeholder*="Search" i]',
    "#applicationFooterSticky",
    "#export_report_button",
    'nav :text("Dashboard")',
    'nav :text("Payments")',
    'nav :text("Reports")',
    '[class*="sidebar"] :text("Dashboard")',
    'a:has-text("Dashboard")',
)


async def _wait_for_authenticated_app(
    page: Page,
    config: RevFlowConfig,
    *,
    timeout_ms: int = 30_000,
) -> None:
    """Wait for RevFlow app shell — never use networkidle (SPA polls forever)."""
    deadline = time.monotonic() + timeout_ms / 1000

    while time.monotonic() < deadline:
        if _is_authenticated_url(page.url) and not _is_login_url(page.url):
            for selector in DASHBOARD_MARKERS:
                locator = page.locator(selector).first
                try:
                    if await locator.count() > 0 and await locator.is_visible():
                        log.debug("Authenticated app ready (matched %s)", selector)
                        return
                except Exception:
                    continue
            # URL is authenticated; allow short settle even if markers not found yet
            if "/dashboard" in page.url.lower():
                await asyncio.sleep(config.action_delay_sec)
                return
        await asyncio.sleep(0.3)

    raise RuntimeError(
        f"Timed out waiting for RevFlow dashboard | {await _page_snapshot(page)}"
    )


def _extract_bearer_from_headers(headers: dict) -> str | None:
    auth = headers.get("authorization") or headers.get("Authorization")
    if auth and auth.lower().startswith("bearer "):
        return auth.split(" ", 1)[1]
    return None


async def _wait_for_bearer_token(
    page: Page,
    *,
    timeout_sec: float = 30.0,
    navigate_url: str | None = None,
) -> str | None:
    """Capture Bearer token from r6prodgoldna API requests."""
    captured: dict[str, str | None] = {"token": None}
    token_event = asyncio.Event()

    async def on_request(request) -> None:
        if "r6prodgoldna.revflow.com" not in request.url:
            return
        token = _extract_bearer_from_headers(request.headers)
        if token:
            captured["token"] = token
            token_event.set()

    page.on("request", on_request)
    try:
        if navigate_url:
            await page.goto(navigate_url, wait_until="domcontentloaded", timeout=90_000)
        try:
            await asyncio.wait_for(token_event.wait(), timeout=timeout_sec)
        except asyncio.TimeoutError:
            pass
        if not captured["token"]:
            await asyncio.sleep(2)
    finally:
        page.remove_listener("request", on_request)

    return captured["token"]


async def _page_snapshot(page: Page) -> str:
    try:
        title = await page.title()
    except Exception:
        title = "(unavailable)"
    return f"url={page.url!r} title={title!r}"


async def _login_error_on_page(page: Page) -> str | None:
    selectors = [
        ".error",
        ".alert-danger",
        ".validation-summary-errors",
        ".field-validation-error",
        '[role="alert"]',
    ]
    for selector in selectors:
        locator = page.locator(selector)
        if await locator.count() == 0:
            continue
        text = " ".join((await locator.first.inner_text()).split())
        if text:
            return text
    return None


async def _fill_login_form(page: Page, config: RevFlowConfig) -> None:
    username_input = page.locator(
        'input[name="username"], input[name="Username"], '
        'input[id="username"], input[type="text"]'
    ).first
    password_input = page.locator(
        'input[name="password"], input[name="Password"], '
        'input[id="password"], input[type="password"]'
    ).first

    await username_input.wait_for(state="visible", timeout=20_000)
    await password_input.wait_for(state="visible", timeout=20_000)
    await username_input.fill(config.username)
    await asyncio.sleep(config.action_delay_sec)
    await password_input.fill(config.password)


async def submit_login(page: Page, config: RevFlowConfig) -> None:
    log.info("Submitting login for user %s", mask_secret(config.username))
    await _fill_login_form(page, config)

    submit = page.locator(
        'button[type="submit"], input[type="submit"], '
        'button:has-text("Log in"), button:has-text("Login"), '
        'button:has-text("Sign in")'
    ).first
    await submit.click()
    await asyncio.sleep(config.action_delay_sec * 2)


async def _needs_ip_registration(page: Page) -> bool:
    body = ""
    try:
        body = (await page.locator("body").inner_text(timeout=5000)).lower()
    except Exception:
        pass
    url = page.url.lower()
    markers = [
        "ip registration",
        "register this ip",
        "email has been sent",
        "check your email",
        "verify your ip",
        "ipregistration",
    ]
    return any(m in body for m in markers) or "ipregistration" in url


async def complete_ip_registration(page: Page, config: RevFlowConfig) -> None:
    from gmail import poll_ip_registration_link

    log.info("Waiting for ipRegistration email link...")
    link = await asyncio.to_thread(poll_ip_registration_link, config)
    log.info("Opening ipRegistration link in browser")
    await page.goto(link, wait_until="domcontentloaded", timeout=90_000)
    await asyncio.sleep(config.action_delay_sec * 2)

    if _is_login_url(page.url):
        log.info("ipRegistration redirected to login — signing in again")
        await submit_login(page, config)
        await page.wait_for_url(
            lambda url: _is_authenticated_url(url) and not _is_login_url(url),
            timeout=45_000,
        )
        await _wait_for_authenticated_app(page, config)


async def login_page(
    context: BrowserContext,
    config: RevFlowConfig,
    *,
    reuse_session: bool = True,
) -> tuple[Page, AuthState]:
    state = AuthState()
    captured_token: dict[str, str | None] = {"token": None}

    async def capture_auth_header(request) -> None:
        if "r6prodgoldna.revflow.com" not in request.url:
            return
        auth = request.headers.get("authorization") or request.headers.get("Authorization")
        if auth and auth.lower().startswith("bearer "):
            captured_token["token"] = auth.split(" ", 1)[1]

    page = await context.new_page()
    page.on("request", capture_auth_header)

    if reuse_session and config.storage_state_path.exists():
        log.info("Trying saved session from %s", config.storage_state_path)
        await page.goto(
            f"{BILLING_BASE_URL}/dashboard",
            wait_until="domcontentloaded",
            timeout=60_000,
        )
        if _is_authenticated_url(page.url) and not _is_login_url(page.url):
            await _wait_for_authenticated_app(page, config)
            log.info("Reused session OK | %s", await _page_snapshot(page))
            state.bearer_token = captured_token["token"]
            _apply_token_to_state(state, config)
            return page, state
        log.warning("Saved session expired — performing fresh login")

    log.info("Opening login page: %s", LOGIN_URL)
    await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=90_000)
    await submit_login(page, config)

    try:
        await page.wait_for_url(
            lambda url: _is_authenticated_url(url) or _needs_ip_registration_sync(url),
            timeout=45_000,
        )
    except Exception:
        inline_error = await _login_error_on_page(page)
        if inline_error:
            raise RuntimeError(f"Login failed: {inline_error} | {await _page_snapshot(page)}")
        if _is_login_url(page.url) and not await _needs_ip_registration(page):
            raise RuntimeError(f"Login did not redirect | {await _page_snapshot(page)}")

    if await _needs_ip_registration(page):
        await complete_ip_registration(page, config)

    if _is_login_url(page.url):
        await page.goto(
            f"{BILLING_BASE_URL}/dashboard",
            wait_until="domcontentloaded",
            timeout=60_000,
        )

    await _wait_for_authenticated_app(page, config)
    log.info("Login complete | %s", await _page_snapshot(page))

    state.bearer_token = captured_token["token"]
    _apply_token_to_state(state, config)
    return page, state


def _needs_ip_registration_sync(url: str) -> bool:
    return "ipregistration" in (url or "").lower()


def _apply_token_to_state(state: AuthState, config: RevFlowConfig) -> None:
    if state.bearer_token:
        payload = decode_jwt_payload(state.bearer_token)
        state.user_id = str(payload.get("UserID") or payload.get("sub") or config.user_id or "")
        state.company_id = str(payload.get("CompanyID") or config.company_id or "")
    else:
        state.user_id = config.user_id or None
        state.company_id = config.company_id or None


async def _finalize_auth_state(
    page: Page,
    context: BrowserContext,
    config: RevFlowConfig,
    state: AuthState,
    captured_token: str | None = None,
) -> AuthState:
    if captured_token:
        state.bearer_token = captured_token
    if not state.bearer_token:
        state.bearer_token = await extract_bearer_token(page, context, config)
    _apply_token_to_state(state, config)
    if not state.bearer_token:
        raise RuntimeError("Could not capture RevFlow bearer token after login")
    return state


async def reauthenticate(
    page: Page,
    context: BrowserContext,
    config: RevFlowConfig,
) -> AuthState:
    """Re-login on an existing page after mid-batch session expiry."""
    captured_token: dict[str, str | None] = {"token": None}

    async def capture_auth_header(request) -> None:
        if "r6prodgoldna.revflow.com" not in request.url:
            return
        auth = request.headers.get("authorization") or request.headers.get("Authorization")
        if auth and auth.lower().startswith("bearer "):
            captured_token["token"] = auth.split(" ", 1)[1]

    page.on("request", capture_auth_header)
    try:
        log.info("Re-authenticating on %s", LOGIN_URL)
        await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=90_000)
        await submit_login(page, config)

        try:
            await page.wait_for_url(
                lambda url: _is_authenticated_url(url) or _needs_ip_registration_sync(url),
                timeout=45_000,
            )
        except Exception:
            inline_error = await _login_error_on_page(page)
            if inline_error:
                raise RuntimeError(
                    f"Re-login failed: {inline_error} | {await _page_snapshot(page)}"
                ) from None
            if _is_login_url(page.url):
                raise RuntimeError(
                    f"Re-login did not redirect | {await _page_snapshot(page)}"
                )

        if await _needs_ip_registration(page):
            await complete_ip_registration(page, config)

        if _is_login_url(page.url):
            await page.goto(
                f"{BILLING_BASE_URL}/dashboard",
                wait_until="domcontentloaded",
                timeout=60_000,
            )

        await _wait_for_authenticated_app(page, config)
        log.info("Re-authentication complete | %s", await _page_snapshot(page))
        state = AuthState()
        return await _finalize_auth_state(
            page, context, config, state, captured_token=captured_token["token"]
        )
    finally:
        page.remove_listener("request", capture_auth_header)


async def refresh_session(
    page: Page,
    context: BrowserContext,
    config: RevFlowConfig,
) -> AuthState:
    """Proactive session keepalive during long export batches."""
    captured_token: dict[str, str | None] = {"token": None}

    async def capture_auth_header(request) -> None:
        if "r6prodgoldna.revflow.com" not in request.url:
            return
        auth = request.headers.get("authorization") or request.headers.get("Authorization")
        if auth and auth.lower().startswith("bearer "):
            captured_token["token"] = auth.split(" ", 1)[1]

    page.on("request", capture_auth_header)
    try:
        await page.goto(
            f"{BILLING_BASE_URL}/dashboard",
            wait_until="domcontentloaded",
            timeout=60_000,
        )
        await assert_authenticated_page(page)
        await _wait_for_authenticated_app(page, config, timeout_ms=15_000)
        if not captured_token["token"]:
            captured_token["token"] = await _wait_for_bearer_token(
                page, timeout_sec=10.0, navigate_url=None
            )
        state = AuthState()
        return await _finalize_auth_state(
            page, context, config, state, captured_token=captured_token["token"]
        )
    finally:
        page.remove_listener("request", capture_auth_header)


async def ensure_authenticated(
    context: BrowserContext,
    config: RevFlowConfig,
    *,
    reuse_session: bool = True,
) -> tuple[Page, AuthState]:
    page, state = await login_page(context, config, reuse_session=reuse_session)
    await _finalize_auth_state(page, context, config, state, captured_token=state.bearer_token)
    return page, state


async def extract_bearer_token(
    page: Page,
    context: BrowserContext,
    config: RevFlowConfig,
) -> str | None:
    token = await _wait_for_bearer_token(
        page,
        timeout_sec=30.0,
        navigate_url=f"{BILLING_BASE_URL}/report/report_data",
    )
    if token:
        return token

    token = await _wait_for_bearer_token(
        page,
        timeout_sec=15.0,
        navigate_url=f"{BILLING_BASE_URL}/dashboard",
    )
    if token:
        return token

    for key in ("token", "authToken", "access_token", "jwt", "bearerToken"):
        try:
            value = await page.evaluate(
                "(k) => localStorage.getItem(k) || sessionStorage.getItem(k)",
                key,
            )
            if value and len(value) > 20:
                return value
        except Exception:
            pass

    return None


async def save_storage_state(context: BrowserContext, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    await context.storage_state(path=str(path))
    log.info("Saved session to %s", path)


async def create_context(
    playwright,
    config: RevFlowConfig,
    *,
    reuse_session: bool = True,
) -> BrowserContext:
    browser = await playwright.chromium.launch(headless=config.headless)
    context_kwargs: dict = {
        "accept_downloads": True,
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "viewport": {"width": 1366, "height": 768},
    }
    if reuse_session and config.storage_state_path.exists():
        context_kwargs["storage_state"] = str(config.storage_state_path)
        log.info("Reusing saved session from %s", config.storage_state_path)
    context = await browser.new_context(**context_kwargs)
    context._revflow_browser = browser  # type: ignore[attr-defined]
    return context


async def close_context(context: BrowserContext) -> None:
    browser = getattr(context, "_revflow_browser", None)
    try:
        await context.close()
    except Exception as exc:
        log.warning("Error closing browser context: %s", exc)
    if browser:
        try:
            await browser.close()
        except Exception as exc:
            log.warning("Error closing browser: %s", exc)


async def verify_session(context: BrowserContext, config: RevFlowConfig) -> bool:
    page = await context.new_page()
    try:
        await page.goto(
            f"{BILLING_BASE_URL}/dashboard",
            wait_until="domcontentloaded",
            timeout=60_000,
        )
        try:
            await _wait_for_authenticated_app(page, config, timeout_ms=15_000)
            return True
        except RuntimeError:
            return False
    finally:
        await page.close()
