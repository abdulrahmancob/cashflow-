"""deploy_release.sh end to end, with docker/curl/sudo stubbed and a temp git repo as the push."""

from __future__ import annotations

import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "deploy_release.sh"


def _bash() -> str | None:
    for candidate in (r"C:\Program Files\Git\bin\bash.exe", shutil.which("bash")):
        if candidate and Path(candidate).exists():
            try:
                done = subprocess.run([candidate, "-c", "echo ok"], capture_output=True, text=True, timeout=30)
            except (OSError, subprocess.TimeoutExpired):
                continue
            if done.stdout.strip() == "ok":
                return candidate
    return None


BASH = _bash()
pytestmark = pytest.mark.skipif(BASH is None, reason="bash is not available")

DOCKER_STUB = r"""#!/usr/bin/env bash
echo "docker $*" >> "$STUB_LOG"
case "$*" in
  "ps --format"*) printf '%s\n' ${STUB_BUSY:-} ;;
  "inspect -f"*) echo "${STUB_BUSY_CMD:-python -m cashflow_ops run}" ;;
  "image inspect"*) exit 0 ;;
  *deploy_preflight*) [ "${STUB_PREFLIGHT:-ok}" = ok ] || { echo "NEW missing x"; exit 1; } ;;
  *"cashflow_db migrate"*) [ "${STUB_MIGRATE:-ok}" = ok ] || exit 1 ;;
  *"--entrypoint nginx nginx -t"*) [ "${STUB_NGINX_T:-ok}" = ok ] || exit 1 ;;
esac
exit 0
"""

CURL_STUB = r"""#!/usr/bin/env bash
echo "curl $*" >> "$STUB_LOG"
url=""; want_code=0
for a in "$@"; do
  case "$a" in http*) url="$a" ;; -w) want_code=1 ;; esac
done
code=200; body="ok"
case "$url" in
  */ready)
    n=$(cat "$STUB_STATE/ready_calls" 2>/dev/null || echo 0); n=$((n + 1)); echo "$n" > "$STUB_STATE/ready_calls"
    if [ "$n" -le "${STUB_READY_FAILS:-0}" ]; then code=503; fi
    body='{"status":"ready"}' ;;
  */api/auth/login) code=405 ;;
  */api/auth/me) code="${STUB_ME_CODE:-401}" ;;
  */api/eligibility/items/export-job) code=401 ;;
  http://127.0.0.1/) body='<html><script src="/export-guard.js"></script><script src="/assets/index-abc.js"></script></html>' ;;
esac
if [ "$want_code" = 1 ]; then printf '%s' "$code"; exit 0; fi
if [ "$code" -ge 400 ]; then exit 22; fi
printf '%s\n' "$body"
"""

PASS_THROUGH = '#!/usr/bin/env bash\nexec "$@"\n'
NOOP = "#!/usr/bin/env bash\nexit 0\n"
SYSTEMCTL_STUB = '#!/usr/bin/env bash\necho "systemctl $*" >> "$STUB_LOG"\n'


def _posix(path: Path) -> str:
    """C:/x/y -> /c/x/y for bash on Windows; unchanged elsewhere."""
    text = path.as_posix()
    if len(text) > 1 and text[1] == ":":
        return f"/{text[0].lower()}{text[2:]}"
    return text


def _git(repo: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x"}
    return subprocess.run(["git", "-c", "core.autocrlf=false", *args], cwd=repo, env=env,
                          capture_output=True, text=True, check=True).stdout.strip()


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, body in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8", newline="\n")


class World:
    """A pushed repo, a live tree that matches the commit before the push, and stubs."""

    def __init__(self, tmp: Path, history: list[dict[str, str]], message: str = "push") -> None:
        self.tmp = tmp
        self.repo = tmp / "repo"
        self.dest = tmp / "dest"
        self.state = tmp / "state"
        self.bin = tmp / "bin"
        self.log = tmp / "stub.log"
        for folder in (self.repo, self.dest, self.state, self.bin):
            folder.mkdir(parents=True)
        _git(self.repo, "init", "-q")
        shas = []
        for index, files in enumerate(history):
            _write(self.repo, files)
            _git(self.repo, "add", "-A")
            _git(self.repo, "commit", "-q", "-m", message if index == len(history) - 1 else f"c{index}")
            shas.append(_git(self.repo, "rev-parse", "HEAD"))
        self.before, self.sha = shas[-2], shas[-1]
        tree_before = {}
        for files in history[:-1]:
            tree_before.update(files)
        _write(self.dest, tree_before)
        (self.dest / "deploy").mkdir(exist_ok=True)
        (self.dest / "deploy" / "docker-compose.yml").write_text(
            "      - ./nginx/export-guard.js:/etc/nginx/export-guard.js:ro\n", encoding="utf-8"
        )
        stubs = {"docker": DOCKER_STUB, "curl": CURL_STUB, "sudo": PASS_THROUGH, "flock": NOOP, "sleep": NOOP,
                 "systemctl": SYSTEMCTL_STUB}
        for name, body in stubs.items():
            stub = self.bin / name
            stub.write_text(body, encoding="utf-8", newline="\n")
            stub.chmod(0o755)

    def run(self, **stub_env: str) -> subprocess.CompletedProcess:
        env = {
            **os.environ,
            "DEPLOY_SHA": self.sha,
            "DEPLOY_BEFORE": self.before,
            "GITHUB_WORKSPACE": self.repo.as_posix(),
            "CASHFLOW_DEST": self.dest.as_posix(),
            "DEPLOY_STATE_ROOT": self.state.as_posix(),
            "DEPLOY_PORTAL_DIR": (self.state / "portal").as_posix(),
            "DEPLOY_SYSTEMD_DIR": (self.state / "systemd").as_posix(),
            "TMPDIR": self.tmp.as_posix(),
            "DEPLOY_BUSY_WAIT_SECONDS": "2",
            "DEPLOY_BUSY_STEP_SECONDS": "1",
            "DEPLOY_READY_TRIES": "2",
            "DEPLOY_READY_SLEEP": "0",
            "MIGRATE_ATTEMPTS": "1",
            "STUB_LOG": self.log.as_posix(),
            "STUB_STATE": self.tmp.as_posix(),
            **stub_env,
        }
        # Git Bash's launcher puts /mingw64/bin first, so prepend the stubs from inside bash.
        command = f'export PATH="{_posix(self.bin)}:$PATH"; exec bash "{_posix(SCRIPT)}"'
        return subprocess.run([BASH, "-c", command], cwd=self.repo, env=env, capture_output=True, text=True, timeout=300)

    def live(self, rel: str) -> str | None:
        path = self.dest / rel
        return path.read_text(encoding="utf-8") if path.exists() else None

    def calls(self) -> str:
        return self.log.read_text(encoding="utf-8") if self.log.exists() else ""

    def backups(self) -> list[Path]:
        root = self.state / "deploy-backups"
        return sorted(root.iterdir()) if root.exists() else []


BASE = {"cashflow_db/__init__.py": "", "cashflow_db/a.py": "A = 1\n"}
PUSH = {"cashflow_db/a.py": "A = 2\n", "cashflow_db/new.py": "N = 1\n"}


def test_success_copies_backs_up_and_prunes(tmp_path):
    world = World(tmp_path, [BASE, PUSH])
    old_root = world.state / "deploy-backups"
    for day in range(16):
        (old_root / f"20200101T0000{day:02d}Z-old").mkdir(parents=True)
    done = world.run()
    assert done.returncode == 0, done.stdout + done.stderr
    assert "DEPLOY_RELEASE_DONE" in done.stdout
    assert world.live("cashflow_db/a.py") == "A = 2\n"
    assert world.live("cashflow_db/new.py") == "N = 1\n"
    assert "deploy_preflight" in world.calls()
    assert "--force-recreate api" in world.calls()
    backups = world.backups()
    assert len(backups) == 15
    newest = backups[-1]
    assert (newest / "existing.txt").read_text().split() == ["cashflow_db/a.py"]
    assert (newest / "created.txt").read_text().split() == ["cashflow_db/new.py"]
    assert (newest / "files" / "cashflow_db" / "a.py").read_text() == "A = 1\n"
    assert not list((world.state / "deploy-stage").glob("run.*"))


def test_busy_worker_blocks_before_any_copy(tmp_path):
    world = World(tmp_path, [BASE, PUSH])
    done = world.run(STUB_BUSY="cashflow-nightly-worker")
    assert done.returncode == 1
    assert "still run the mounted code" in done.stderr
    assert world.live("cashflow_db/a.py") == "A = 1\n"
    assert world.live("cashflow_db/new.py") is None
    assert "deploy_preflight" not in world.calls()


def test_busy_job_that_does_not_use_the_code_is_ignored(tmp_path):
    world = World(tmp_path, [BASE, PUSH])
    done = world.run(STUB_BUSY="cashflow-scraper-run-1", STUB_BUSY_CMD="python -u /app/deploy/scripts/intake_census_eval.py")
    assert done.returncode == 0, done.stdout + done.stderr


def test_deploy_now_skips_the_busy_wait(tmp_path):
    world = World(tmp_path, [BASE, PUSH], message="urgent fix [deploy-now]")
    done = world.run(STUB_BUSY="cashflow-nightly-worker")
    assert done.returncode == 0, done.stdout + done.stderr
    assert "busy guard: skipped" in done.stdout


def test_drift_blocks_and_override_replaces(tmp_path):
    world = World(tmp_path, [BASE, PUSH])
    (world.dest / "cashflow_db" / "a.py").write_text("A = 99  # uploaded by hand\n", encoding="utf-8")
    done = world.run()
    assert done.returncode == 1
    assert "drift guard" in done.stderr and "cashflow_db/a.py" in done.stderr
    assert world.live("cashflow_db/a.py") == "A = 99  # uploaded by hand\n"
    assert not world.backups()

    world2 = World(tmp_path / "again", [BASE, PUSH], message="replace it [deploy-overwrite]")
    (world2.dest / "cashflow_db" / "a.py").write_text("A = 99  # uploaded by hand\n", encoding="utf-8")
    done = world2.run()
    assert done.returncode == 0, done.stdout + done.stderr
    assert world2.live("cashflow_db/a.py") == "A = 2\n"
    backup = world2.backups()[-1] / "files" / "cashflow_db" / "a.py"
    assert backup.read_text() == "A = 99  # uploaded by hand\n"


def test_an_older_committed_version_is_not_drift(tmp_path):
    world = World(tmp_path, [BASE, {"cashflow_db/a.py": "A = 1.5\n"}, PUSH])
    (world.dest / "cashflow_db" / "a.py").write_text("A = 1\n", encoding="utf-8")
    done = world.run()
    assert done.returncode == 0, done.stdout + done.stderr
    assert "drift guard: none" in done.stdout


def test_crlf_only_difference_is_not_drift(tmp_path):
    world = World(tmp_path, [BASE, PUSH])
    (world.dest / "cashflow_db" / "a.py").write_bytes(b"A = 1\r\n")
    done = world.run()
    assert done.returncode == 0, done.stdout + done.stderr


def test_failed_preflight_leaves_the_live_tree_alone(tmp_path):
    world = World(tmp_path, [BASE, PUSH])
    done = world.run(STUB_PREFLIGHT="fail")
    assert done.returncode == 1
    assert world.live("cashflow_db/a.py") == "A = 1\n"
    assert world.live("cashflow_db/new.py") is None
    assert not list((world.state / "deploy-stage").glob("run.*"))
    assert "DEPLOY_ROLLED_BACK" not in done.stderr


def test_failed_migration_restores_files_and_removes_new_ones(tmp_path):
    base = {**BASE, "cashflow_db/sql/001.sql": "SELECT 1;\n"}
    push = {**PUSH, "cashflow_db/sql/001.sql": "SELECT 2;\n"}
    world = World(tmp_path, [base, push])
    done = world.run(STUB_MIGRATE="fail")
    assert done.returncode == 1
    assert "DEPLOY_ROLLED_BACK" in done.stderr and "step=migrations" in done.stderr
    assert world.live("cashflow_db/a.py") == "A = 1\n"
    assert world.live("cashflow_db/sql/001.sql") == "SELECT 1;\n"
    assert world.live("cashflow_db/new.py") is None
    assert "--force-recreate api" not in world.calls()


def test_api_not_ready_rolls_back_and_restarts_the_api(tmp_path):
    world = World(tmp_path, [BASE, PUSH])
    done = world.run(STUB_READY_FAILS="2")
    assert done.returncode == 1
    assert "DEPLOY_ROLLED_BACK" in done.stderr and "step=api" in done.stderr
    assert world.live("cashflow_db/a.py") == "A = 1\n"
    assert world.live("cashflow_db/new.py") is None
    assert world.calls().count("--force-recreate api") == 2


def test_signed_out_auth_me_must_be_401(tmp_path):
    world = World(tmp_path, [BASE, PUSH])
    done = world.run(STUB_ME_CODE="500")
    assert done.returncode == 1
    assert "DEPLOY_ROLLED_BACK" in done.stderr
    assert world.live("cashflow_db/a.py") == "A = 1\n"


def test_bad_nginx_config_never_reaches_the_running_nginx(tmp_path):
    base = {**BASE, "deploy/nginx/site.conf": "server {}\n"}
    push = {"deploy/nginx/site.conf": "server { broken\n"}
    world = World(tmp_path, [base, push])
    done = world.run(STUB_NGINX_T="fail")
    assert done.returncode == 1
    assert "DEPLOY_ROLLED_BACK" in done.stderr
    assert world.live("deploy/nginx/site.conf") == "server {}\n"
    assert "--force-recreate nginx" not in world.calls()
    assert "deploy_preflight" not in world.calls()


def test_systemd_units_are_installed_and_the_timer_enabled(tmp_path):
    push = {
        "deploy/systemd/runner-watchdog.service": "[Service]\nType=oneshot\n",
        "deploy/systemd/runner-watchdog.timer": "[Timer]\nOnUnitActiveSec=5min\n",
    }
    world = World(tmp_path, [BASE, push])
    done = world.run()
    assert done.returncode == 0, done.stdout + done.stderr
    installed = sorted(path.name for path in (world.state / "systemd").iterdir())
    assert installed == ["runner-watchdog.service", "runner-watchdog.timer"]
    assert "systemctl daemon-reload" in world.calls()
    assert "systemctl enable --now runner-watchdog.timer" in world.calls()
    assert "deploy_preflight" not in world.calls()
