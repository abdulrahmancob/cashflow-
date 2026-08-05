"""Shared subprocess runner for CLI adapters."""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from cashflow_ops.config import PYTHON, REPO_ROOT

log = logging.getLogger(__name__)


@dataclass
class CmdResult:
    ok: bool
    returncode: int
    stdout: str
    stderr: str
    command: list[str] = field(default_factory=list)
    skipped: bool = False
    dry_run: bool = False

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "returncode": self.returncode,
            "stdout_tail": self.stdout[-4000:],
            "stderr_tail": self.stderr[-4000:],
            "command": self.command,
            "skipped": self.skipped,
            "dry_run": self.dry_run,
        }


def run_cmd(
    args: Sequence[str],
    *,
    cwd: Path | None = None,
    dry_run: bool = False,
    timeout: int | None = None,
    env: dict[str, str] | None = None,
) -> CmdResult:
    cmd = list(args)
    log.info("exec%s: %s", " [dry-run]" if dry_run else "", " ".join(cmd))
    if dry_run:
        return CmdResult(
            ok=True,
            returncode=0,
            stdout="dry-run",
            stderr="",
            command=cmd,
            dry_run=True,
        )
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd or REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return CmdResult(
            ok=False,
            returncode=124,
            stdout=exc.stdout or "",
            stderr=f"timeout after {timeout}s",
            command=cmd,
        )
    except FileNotFoundError as exc:
        return CmdResult(
            ok=False,
            returncode=127,
            stdout="",
            stderr=str(exc),
            command=cmd,
        )
    return CmdResult(
        ok=proc.returncode == 0,
        returncode=proc.returncode,
        stdout=proc.stdout or "",
        stderr=proc.stderr or "",
        command=cmd,
    )


def run_python_module(
    module: str,
    module_args: Sequence[str] | None = None,
    *,
    cwd: Path | None = None,
    dry_run: bool = False,
    timeout: int | None = None,
) -> CmdResult:
    args = [PYTHON, "-m", module, *(module_args or [])]
    return run_cmd(args, cwd=cwd, dry_run=dry_run, timeout=timeout)


def run_python_script(
    script: Path,
    script_args: Sequence[str] | None = None,
    *,
    cwd: Path | None = None,
    dry_run: bool = False,
    timeout: int | None = None,
) -> CmdResult:
    args = [PYTHON, str(script), *(script_args or [])]
    return run_cmd(args, cwd=cwd, dry_run=dry_run, timeout=timeout)
