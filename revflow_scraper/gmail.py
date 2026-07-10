"""Gmail OAuth helper to fetch RevFlow IP registration links."""

from __future__ import annotations

import base64
import re
import time
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from config import IP_REGISTRATION_LINK_RE, RevFlowConfig
from logging_config import get_logger

log = get_logger("gmail")

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
IP_LINK_PATTERN = re.compile(IP_REGISTRATION_LINK_RE, re.IGNORECASE)


def _load_credentials(config: RevFlowConfig) -> Credentials:
    creds: Credentials | None = None
    token_path = config.gmail_token_path
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), GMAIL_SCOPES)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        token_path.write_text(creds.to_json(), encoding="utf-8")
        return creds

    if not config.gmail_credentials_path.exists():
        raise FileNotFoundError(
            f"Gmail credentials not found at {config.gmail_credentials_path}. "
            "Download OAuth desktop credentials from Google Cloud Console."
        )

    flow = InstalledAppFlow.from_client_secrets_file(
        str(config.gmail_credentials_path), GMAIL_SCOPES
    )
    creds = flow.run_local_server(port=0)
    token_path.write_text(creds.to_json(), encoding="utf-8")
    log.info("Gmail OAuth token saved to %s", token_path)
    return creds


def _extract_links_from_message(payload: dict) -> list[str]:
    links: list[str] = []

    def walk(part: dict) -> None:
        body = part.get("body", {})
        data = body.get("data")
        if data:
            try:
                text = base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
            except Exception:
                text = ""
            links.extend(IP_LINK_PATTERN.findall(text))
        for child in part.get("parts") or []:
            walk(child)

    walk(payload)
    return links


def _search_registration_link(service, query: str) -> str | None:
    result = (
        service.users()
        .messages()
        .list(userId="me", q=query, maxResults=5)
        .execute()
    )
    for item in result.get("messages") or []:
        msg = (
            service.users()
            .messages()
            .get(userId="me", id=item["id"], format="full")
            .execute()
        )
        links = _extract_links_from_message(msg.get("payload", {}))
        if links:
            return links[0]
    return None


def poll_ip_registration_link(config: RevFlowConfig) -> str:
    """Poll Gmail until an ipRegistration link appears."""
    creds = _load_credentials(config)
    service = build("gmail", "v1", credentials=creds, cache_discovery=False)

    queries = [
        "ipRegistration newer_than:1d",
        "revflow ipRegistration newer_than:1d",
        "billing.revflow.com ipRegistration newer_than:1d",
        "from:revflow newer_than:1d",
        "from:webpt newer_than:1d",
    ]

    deadline = time.monotonic() + config.gmail_poll_timeout_sec
    log.info(
        "Polling Gmail for ipRegistration link (timeout=%ss)...",
        config.gmail_poll_timeout_sec,
    )

    while time.monotonic() < deadline:
        for query in queries:
            link = _search_registration_link(service, query)
            if link:
                log.info("Found ipRegistration link in Gmail")
                return link
        time.sleep(config.gmail_poll_interval_sec)

    raise TimeoutError(
        f"No ipRegistration link found in Gmail within {config.gmail_poll_timeout_sec}s"
    )
