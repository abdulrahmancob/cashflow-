#!/usr/bin/env python3
"""One-shot: authorize Gmail readonly and write gmail_token.json."""
from __future__ import annotations

import os
import sys

from config import RevFlowConfig
from gmail import GMAIL_SCOPES, _load_credentials
from google_auth_oauthlib.flow import InstalledAppFlow


def authorize_interactive(cfg) -> None:
    flow = InstalledAppFlow.from_client_secrets_file(
        str(cfg.gmail_credentials_path), GMAIL_SCOPES
    )
    use_console = os.getenv("GMAIL_OAUTH_CONSOLE", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }
    if use_console:
        auth_url, _ = flow.authorization_url(prompt="consent", access_type="offline")
        print("\nOpen this URL, authorize, paste the code from the redirect URL (?code=...):\n")
        print(auth_url)
        print()
        code = input("Enter authorization code: ").strip()
        flow.fetch_token(code=code)
        creds = flow.credentials
    else:
        port = int(os.getenv("GMAIL_OAUTH_PORT", "8765"))
        print(f"Starting local OAuth listener on localhost:{port} ...")
        print("A browser window should open — approve gmail.readonly for the RevFlow inbox.")
        # Prefer localhost (matches Google desktop client redirect_uris).
        creds = flow.run_local_server(
            port=port,
            open_browser=True,
            bind_addr="localhost",
            authorization_prompt_message="Waiting for authorization in browser...",
            success_message="Gmail authorized. You can close this tab.",
        )
    cfg.gmail_token_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.gmail_token_path.write_text(creds.to_json(), encoding="utf-8")
    print(f"wrote {cfg.gmail_token_path}")


def main() -> int:
    cfg = RevFlowConfig.from_env()
    print(f"credentials: {cfg.gmail_credentials_path}")
    print(f"token path:  {cfg.gmail_token_path}")
    print(
        "NOTE: Google Cloud OAuth client must be type Desktop "
        "(or Web with Authorized redirect URI http://localhost:8765/). "
        "Current client_id returns redirect_uri_mismatch if misconfigured."
    )
    if cfg.gmail_token_path.exists() and os.getenv("GMAIL_OAUTH_FORCE", "") != "1":
        creds = _load_credentials(cfg)
        print(f"token_ok={bool(creds and creds.valid)} refresh={bool(creds.refresh_token)}")
        return 0
    authorize_interactive(cfg)
    creds = _load_credentials(cfg)
    print(f"token_ok={bool(creds and creds.valid)} refresh={bool(creds.refresh_token)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
