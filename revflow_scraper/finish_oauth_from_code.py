#!/usr/bin/env python3
"""Finish Gmail OAuth when you paste ?code=... into oauth_code.txt or argv."""
from __future__ import annotations

import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from google_auth_oauthlib.flow import InstalledAppFlow

from gmail import GMAIL_SCOPES

CODE_FILE = Path("oauth_code.txt")
TOKEN_FILE = Path("gmail_token.json")


def extract_code(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("http"):
        qs = parse_qs(urlparse(raw).query)
        if "code" in qs:
            return qs["code"][0]
    return raw


def main() -> int:
    if len(sys.argv) > 1:
        code = extract_code(sys.argv[1])
    elif CODE_FILE.exists():
        code = extract_code(CODE_FILE.read_text(encoding="utf-8"))
    else:
        print(f"Usage: {sys.argv[0]} <code-or-redirect-url>")
        print(f"Or write the code/URL into {CODE_FILE}")
        return 2

    flow = InstalledAppFlow.from_client_secrets_file("credentials.json", GMAIL_SCOPES)
    # Must match the redirect used when the code was issued
    port = __import__("os").getenv("GMAIL_OAUTH_PORT", "8765")
    flow.redirect_uri = f"http://127.0.0.1:{port}/"
    flow.fetch_token(code=code)
    TOKEN_FILE.write_text(flow.credentials.to_json(), encoding="utf-8")
    print(f"wrote {TOKEN_FILE}")
    if CODE_FILE.exists():
        CODE_FILE.unlink()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
