"""WAYSTAR_RECENT_MONTHS is forwarded as --months-back on the claims pull."""

from __future__ import annotations

from cashflow_ops.adapters.subprocess_runner import CmdResult
from cashflow_ops.adapters import waystar as waystar_mod


def _capture_download(monkeypatch, tmp_path, *, env_value: str | None):
    (tmp_path / "download_claims_listing.py").write_text("# dummy\n", encoding="utf-8")
    captured: dict[str, list[str]] = {}

    def fake_run(script, args, **kwargs):
        captured["args"] = list(args)
        return CmdResult(ok=True, returncode=0, stdout="ok", stderr="", skipped=False)

    if env_value is None:
        monkeypatch.delenv("WAYSTAR_RECENT_MONTHS", raising=False)
    else:
        monkeypatch.setenv("WAYSTAR_RECENT_MONTHS", env_value)
    monkeypatch.setattr(waystar_mod, "WAYSTAR_DIR", tmp_path)
    monkeypatch.setattr(waystar_mod, "WAYSTAR_OUTPUT", tmp_path / "out")
    monkeypatch.setattr(waystar_mod, "run_python_script", fake_run)
    result = waystar_mod.download_recent_claims()
    assert result.ok
    return captured["args"]


def test_download_recent_claims_default_months_back(monkeypatch, tmp_path):
    args = _capture_download(monkeypatch, tmp_path, env_value=None)
    assert "--recent" in args
    assert args[args.index("--months-back") + 1] == "2"


def test_download_recent_claims_env_months_back(monkeypatch, tmp_path):
    args = _capture_download(monkeypatch, tmp_path, env_value="3")
    assert args[args.index("--months-back") + 1] == "3"


def test_download_recent_claims_invalid_env_falls_back(monkeypatch, tmp_path):
    args = _capture_download(monkeypatch, tmp_path, env_value="abc")
    assert args[args.index("--months-back") + 1] == "2"
