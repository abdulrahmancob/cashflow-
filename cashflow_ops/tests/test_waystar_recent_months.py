"""WAYSTAR_TRANS_FROM is forwarded as --since; empty falls back to WAYSTAR_RECENT_MONTHS."""

from __future__ import annotations

from cashflow_ops.adapters.subprocess_runner import CmdResult
from cashflow_ops.adapters import waystar as waystar_mod


def _capture_download(
    monkeypatch,
    tmp_path,
    *,
    env_value: str | None = None,
    since_value: str | None = None,
):
    (tmp_path / "download_claims_listing.py").write_text("# dummy\n", encoding="utf-8")
    captured: dict[str, list[str]] = {}

    def fake_run(script, args, **kwargs):
        captured["args"] = list(args)
        return CmdResult(ok=True, returncode=0, stdout="ok", stderr="", skipped=False)

    if env_value is None:
        monkeypatch.delenv("WAYSTAR_RECENT_MONTHS", raising=False)
    else:
        monkeypatch.setenv("WAYSTAR_RECENT_MONTHS", env_value)
    if since_value is None:
        monkeypatch.delenv("WAYSTAR_TRANS_FROM", raising=False)
    else:
        monkeypatch.setenv("WAYSTAR_TRANS_FROM", since_value)
    monkeypatch.setattr(waystar_mod, "WAYSTAR_DIR", tmp_path)
    monkeypatch.setattr(waystar_mod, "WAYSTAR_OUTPUT", tmp_path / "out")
    monkeypatch.setattr(waystar_mod, "run_python_script", fake_run)
    result = waystar_mod.download_recent_claims()
    assert result.ok
    return captured["args"]


def test_download_recent_claims_defaults_to_start_of_year(monkeypatch, tmp_path):
    args = _capture_download(monkeypatch, tmp_path, env_value="2")
    assert "--recent" in args
    assert args[args.index("--since") + 1] == "2026-01-01"
    assert "--months-back" not in args


def test_download_recent_claims_env_since(monkeypatch, tmp_path):
    args = _capture_download(monkeypatch, tmp_path, since_value="2026-03-01")
    assert args[args.index("--since") + 1] == "2026-03-01"


def test_download_recent_claims_invalid_since_uses_default(monkeypatch, tmp_path):
    args = _capture_download(monkeypatch, tmp_path, since_value="01/01/2026")
    assert args[args.index("--since") + 1] == "2026-01-01"


def test_download_recent_claims_empty_since_default_months_back(monkeypatch, tmp_path):
    args = _capture_download(monkeypatch, tmp_path, since_value="")
    assert "--since" not in args
    assert args[args.index("--months-back") + 1] == "2"


def test_download_recent_claims_env_months_back(monkeypatch, tmp_path):
    args = _capture_download(monkeypatch, tmp_path, env_value="3", since_value="")
    assert args[args.index("--months-back") + 1] == "3"


def test_download_recent_claims_invalid_env_falls_back(monkeypatch, tmp_path):
    args = _capture_download(monkeypatch, tmp_path, env_value="abc", since_value="")
    assert args[args.index("--months-back") + 1] == "2"
