"""runner_watchdog.sh restarts an idle runner when a deploy sits queued, and never a busy one."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from test_deploy_release import BASH, _posix

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "runner_watchdog.sh"
pytestmark = pytest.mark.skipif(BASH is None, reason="bash is not available")


class Host:
    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.bin = tmp / "bin"
        self.bin.mkdir()
        self.calls = tmp / "calls.log"
        self.log = tmp / "watchdog.log"
        self.state = tmp / "last"
        self.body = tmp / "runs.json"
        python = _posix(Path(sys.executable))
        stubs = {
            "curl": '#!/usr/bin/env bash\n[ -n "${STUB_CURL_FAIL:-}" ] && exit 7\ncat "$STUB_BODY"\n',
            "pgrep": '#!/usr/bin/env bash\n[ -n "${STUB_BUSY:-}" ]\n',
            "systemctl": (
                '#!/usr/bin/env bash\necho "systemctl $*" >> "$STUB_CALLS"\n'
                'if [ "$1" = list-units ]; then echo "actions.runner.org-repo.box.service loaded active running GitHub runner"; fi\n'
            ),
            "python3": f'#!/usr/bin/env bash\nexec "{python}" "$@"\n',
        }
        for name, body in stubs.items():
            path = self.bin / name
            path.write_text(body, encoding="utf-8", newline="\n")
            path.chmod(0o755)

    def runs(self, *ages_minutes: float, name: str = "Deploy") -> None:
        now = datetime.now(timezone.utc)
        runs = [
            {
                "name": name,
                "status": "queued",
                "created_at": (now - timedelta(minutes=age)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
            for age in ages_minutes
        ]
        self.body.write_text(json.dumps({"workflow_runs": runs}), encoding="utf-8")

    def run(self, **env: str) -> subprocess.CompletedProcess:
        full = {
            **os.environ,
            "WATCHDOG_STATE": self.state.as_posix(),
            "WATCHDOG_LOG": self.log.as_posix(),
            "STUB_BODY": self.body.as_posix(),
            "STUB_CALLS": self.calls.as_posix(),
            **env,
        }
        command = f'export PATH="{_posix(self.bin)}:$PATH"; exec bash "{_posix(SCRIPT)}"'
        return subprocess.run([BASH, "-c", command], env=full, capture_output=True, text=True, timeout=120)

    def restarts(self) -> int:
        if not self.calls.exists():
            return 0
        return self.calls.read_text(encoding="utf-8").count("systemctl restart")

    def logged(self) -> str:
        return self.log.read_text(encoding="utf-8") if self.log.exists() else ""


def test_no_queued_deploy_does_nothing(tmp_path):
    host = Host(tmp_path)
    host.runs()
    assert host.run().returncode == 0
    assert host.restarts() == 0 and host.logged() == ""


def test_short_queue_or_other_workflows_are_left_alone(tmp_path):
    host = Host(tmp_path)
    host.runs(3)
    assert host.run().returncode == 0 and host.restarts() == 0
    host.runs(45, name="Intake reader eval")
    assert host.run().returncode == 0 and host.restarts() == 0


def test_idle_runner_with_a_stuck_deploy_is_restarted_once(tmp_path):
    host = Host(tmp_path)
    host.runs(15)
    done = host.run()
    assert done.returncode == 0, done.stderr
    assert host.restarts() == 1
    assert "restarted actions.runner.org-repo.box.service" in host.logged()
    assert host.state.exists()
    host.run()
    assert host.restarts() == 1
    assert "waiting" in host.logged()


def test_busy_runner_is_never_restarted(tmp_path):
    host = Host(tmp_path)
    host.runs(120)
    assert host.run(STUB_BUSY="1").returncode == 0
    assert host.restarts() == 0
    assert "busy with another job" in host.logged()


def test_github_unreachable_is_logged_not_fatal(tmp_path):
    host = Host(tmp_path)
    assert host.run(STUB_CURL_FAIL="1").returncode == 0
    assert host.restarts() == 0
    assert "unreachable" in host.logged()
