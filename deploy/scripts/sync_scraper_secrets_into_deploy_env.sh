#!/usr/bin/env bash
# Merge scraper/module secrets into deploy/.env WITHOUT printing secret values.
# Sources (in priority order, first non-empty wins): existing deploy/.env, then module .env files.
set -euo pipefail

DEPLOY_ENV="${1:-/opt/cashflow/deploy/.env}"
# Prefer explicit root; fall back to repo relative to this script when installed under deploy/scripts.
ROOT="${2:-}"
if [[ -z "${ROOT}" ]]; then
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  if [[ -d "${SCRIPT_DIR}/../../cashflow_db" ]]; then
    ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
  else
    ROOT="/opt/cashflow"
  fi
fi

python3 - <<'PY' "$DEPLOY_ENV" "$ROOT"
import re
import sys
from pathlib import Path

deploy_env = Path(sys.argv[1])
root = Path(sys.argv[2])

KEYS = [
    "REVFLOW_USERNAME", "REVFLOW_PASSWORD", "REVFLOW_COMPANY_ID", "REVFLOW_USER_ID",
    "REVFLOW_CLINIC_CODE", "REVFLOW_HEADLESS",
    "WAYSTAR_USER", "WAYSTAR_PASS", "WAYSTAR_CUST_ID",
    "WAYSTAR_SECURITY_ANSWERS", "WAYSTAR_SECURITY_ANSWER",
    "WEBPT_USERNAME", "WEBPT_PASSWORD", "WEBPT_STORAGE_STATE", "WEBPT_HEADLESS",
    "SNOWFLAKE_ACCOUNT", "SNOWFLAKE_USER", "SNOWFLAKE_PASSWORD",
    "SNOWFLAKE_WAREHOUSE", "SNOWFLAKE_DATABASE", "SNOWFLAKE_SCHEMA",
]

SOURCES = [
    root / "revflow_scraper" / ".env",
    root / "waystar_scraper" / ".env",
    root / "webpt_edco_scraper" / ".env",
    root / "snowflake_pull" / ".env",
]

def parse_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip()
        if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
            v = v[1:-1]
        out[k] = v
    return out

def needs_quotes(v: str) -> bool:
    return any(ch in v for ch in ' \t#"\'') or v == ""

current = parse_env(deploy_env)
merged = dict(current)
filled = []
for src in SOURCES:
    src_map = parse_env(src)
    for k in KEYS:
        if k not in src_map:
            continue
        val = src_map[k]
        if not val:
            continue
        cur = (merged.get(k) or "").strip()
        if not cur:
            merged[k] = val
            filled.append(f"{k}<-{src.name}")

# Rewrite deploy .env preserving unknown keys/order; update or append KEYS
if not deploy_env.is_file():
    raise SystemExit(f"missing {deploy_env}")

lines = deploy_env.read_text(encoding="utf-8", errors="replace").splitlines()
seen = set()
out_lines = []
for line in lines:
    stripped = line.strip()
    if stripped and not stripped.startswith("#") and "=" in stripped:
        k = stripped.split("=", 1)[0].strip()
        if k in KEYS and k in merged:
            v = merged[k]
            out_lines.append(f'{k}="{v}"' if needs_quotes(v) else f"{k}={v}")
            seen.add(k)
            continue
    out_lines.append(line)

missing = [k for k in KEYS if k in merged and k not in seen]
if missing:
    out_lines.append("")
    out_lines.append("# Synced from module .env files")
    for k in missing:
        v = merged[k]
        out_lines.append(f'{k}="{v}"' if needs_quotes(v) else f"{k}={v}")

bak = deploy_env.with_suffix(".env.bak_sync")
bak.write_text(deploy_env.read_text(encoding="utf-8", errors="replace"), encoding="utf-8")
deploy_env.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
print(f"backup={bak}")
print(f"filled_count={len(filled)}")
for item in filled:
    print(f"  {item}")
# Verify non-empty after sync (no values)
after = parse_env(deploy_env)
for k in ("REVFLOW_USERNAME", "REVFLOW_PASSWORD", "WAYSTAR_USER", "WAYSTAR_PASS", "WEBPT_USERNAME"):
    print(f"check {k}={'SET' if (after.get(k) or '').strip() else 'EMPTY'}")
PY
