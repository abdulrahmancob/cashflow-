"""Smoke test for multi-question MFA answer resolution."""

from config import WaystarConfig, parse_security_answers_env, resolve_security_answer


def test_parse_security_answers_env() -> None:
    parsed = parse_security_answers_env("mother's maiden name=ahmed;first pet=katy")
    assert parsed["mother's maiden name"] == "ahmed"
    assert parsed["first pet"] == "katy"


def test_resolve_known_questions() -> None:
    cfg = WaystarConfig(username="u", password="p", security_answer="fallback")

    answer, key = resolve_security_answer("What is your mother's maiden name?", cfg)
    assert answer == "ahmed"
    assert key == "mother's maiden name"

    answer, key = resolve_security_answer("What was the name of your first pet?", cfg)
    assert answer == "katy"
    assert key == "first pet"


def test_resolve_fallback() -> None:
    cfg = WaystarConfig(username="u", password="p", security_answer="fallback")
    answer, key = resolve_security_answer("Unknown question?", cfg)
    assert answer == "fallback"
    assert key is None


def test_resolve_no_answer() -> None:
    cfg = WaystarConfig(username="u", password="p", security_answer=None)
    answer, key = resolve_security_answer("Unknown question?", cfg)
    assert answer is None
    assert key is None


if __name__ == "__main__":
    test_parse_security_answers_env()
    test_resolve_known_questions()
    test_resolve_fallback()
    test_resolve_no_answer()
    print("resolve_security_answer smoke test OK")
