"""Smoke test for rejection_details parsing using captured editor HTML."""

from pathlib import Path

from rejection_details import parse_rejection_details

CAPTURE = Path("output/explore_rejection/001_claims.zirmed.com_Editor_V5010_Professional_Main.aspx.txt")


def main() -> None:
    raw = CAPTURE.read_text(encoding="utf-8")
    body = raw.split("=" * 80, 1)[1]
    details = parse_rejection_details(body)
    print("count:", details["rejection_count"], "| found_grid:", details["found_grid"])
    for msg in details["messages"]:
        print("-" * 70)
        print("MSG:", msg["message"][:200])
        print("FIX:", msg["fix_slug"] or "(none)")
    print("=" * 70)
    print("ORIGINAL:")
    print(details["original_message"])


if __name__ == "__main__":
    main()
