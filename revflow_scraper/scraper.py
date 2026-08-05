"""CLI for RevFlow Billing Open 835s export automation."""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

from playwright.async_api import async_playwright

from auth import (
    AuthState,
    SessionExpiredError,
    _apply_token_to_state,
    close_context,
    create_context,
    ensure_authenticated,
    extract_bearer_token,
    login_page,
    reauthenticate,
    refresh_session,
    save_storage_state,
    verify_session,
)
from config import OUTPUT_DIR, RevFlowConfig
from export import export_eob_spreadsheet, selection_key
from logging_config import get_logger, setup_logging
from reports_api import ReportsClient, discover_eobs, load_selections, write_eob_catalog
from verify import (
    print_verify_summary,
    verify_exports,
    write_missing_selections,
    write_verify_report,
)

log = get_logger("scraper")

MANIFEST_FILENAME = "manifest.json"
CHECKPOINT_FILENAME = "checkpoint.json"
CATALOG_FILENAME = "eob_catalog.json"
SELECTIONS_ALL_FILENAME = "selections_all.json"


def _run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def _resolve_output_dir(output: str | None, run_id: str | None) -> Path:
    rid = run_id or _run_id()
    if output:
        base = Path(output)
        if base.name == rid or (base / CATALOG_FILENAME).exists() or (base / "exports").exists():
            return base
        return base
    return OUTPUT_DIR / rid


def _load_checkpoint(path: Path) -> set[str]:
    if not path.exists():
        return set()
    data = json.loads(path.read_text(encoding="utf-8"))
    return set(data.get("completed_keys") or [])


def _save_checkpoint(path: Path, completed: set[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"completed_keys": sorted(completed)}, indent=2),
        encoding="utf-8",
    )


def _load_manifest(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"exports": [], "updated_at": None}


def _save_manifest(path: Path, manifest: dict) -> None:
    manifest["updated_at"] = datetime.now(timezone.utc).isoformat()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def _append_manifest(manifest: dict, result: dict) -> None:
    exports = manifest.setdefault("exports", [])
    exports = [e for e in exports if e.get("key") != result.get("key")]
    exports.append(result)
    manifest["exports"] = exports


def _auth_credentials(state: AuthState, config: RevFlowConfig) -> tuple[str, str, str]:
    return (
        state.bearer_token or "",
        state.user_id or config.user_id,
        state.company_id or config.company_id,
    )


async def _maybe_refresh_session(
    page,
    context,
    config: RevFlowConfig,
    state: AuthState,
    exports_since_refresh: int,
) -> tuple[AuthState, int]:
    if (
        config.session_refresh_every_n <= 0
        or exports_since_refresh < config.session_refresh_every_n
    ):
        return state, exports_since_refresh

    log.info(
        "Proactive session refresh after %d export(s)",
        exports_since_refresh,
    )
    try:
        state = await refresh_session(page, context, config)
        await save_storage_state(context, config.storage_state_path)
        return state, 0
    except SessionExpiredError:
        log.warning("Proactive refresh found expired session — will re-auth on next export")
        return state, exports_since_refresh


async def _export_with_recovery(
    page,
    context,
    config: RevFlowConfig,
    state: AuthState,
    selection: dict,
    exports_dir: Path,
    *,
    skip_existing: bool,
) -> tuple[dict, AuthState]:
    bearer, user_id, company_id = _auth_credentials(state, config)
    last_exc: Exception | None = None

    for attempt in range(1, config.export_retry_max + 1):
        try:
            result = await export_eob_spreadsheet(
                page,
                context.request,
                config,
                bearer,
                user_id,
                company_id,
                selection,
                exports_dir,
                skip_existing=skip_existing,
            )
            return result, state
        except SessionExpiredError as exc:
            last_exc = exc
            if attempt >= config.export_retry_max:
                raise
            log.warning(
                "Session expired — re-authenticating (attempt %d/%d)",
                attempt,
                config.export_retry_max,
            )
            await asyncio.sleep(config.reauth_cooldown_sec)
            state = await reauthenticate(page, context, config)
            await save_storage_state(context, config.storage_state_path)
            bearer, user_id, company_id = _auth_credentials(state, config)

    if last_exc:
        raise last_exc
    raise RuntimeError("Export failed without exception")


async def cmd_login(config: RevFlowConfig, *, fresh: bool = False) -> int:
    async with async_playwright() as playwright:
        context = await create_context(playwright, config, reuse_session=not fresh)
        try:
            page, state = await login_page(
                context, config, reuse_session=not fresh
            )
            await save_storage_state(context, config.storage_state_path)

            if not state.bearer_token:
                state.bearer_token = await extract_bearer_token(page, context, config)
                _apply_token_to_state(state, config)

            if state.bearer_token:
                log.info(
                    "Login successful | user_id=%s company_id=%s token=yes",
                    state.user_id,
                    state.company_id,
                )
            else:
                log.warning(
                    "Session saved to %s but bearer token not captured — "
                    "discover-eobs will retry token extraction",
                    config.storage_state_path,
                )
            await page.close()
            return 0
        finally:
            await close_context(context)


async def cmd_list_session(config: RevFlowConfig) -> int:
    if not config.storage_state_path.exists():
        log.error("No saved session at %s", config.storage_state_path)
        return 1
    async with async_playwright() as playwright:
        context = await create_context(playwright, config, reuse_session=True)
        try:
            ok = await verify_session(context, config)
            if ok:
                log.info("Saved session is valid")
                return 0
            log.error("Saved session expired — run: python scraper.py login")
            return 1
        finally:
            await close_context(context)


async def cmd_discover_eobs(
    config: RevFlowConfig,
    *,
    from_date: str,
    to_date: str,
    output_dir: Path,
    fresh: bool = False,
) -> int:
    async with async_playwright() as playwright:
        context = await create_context(playwright, config, reuse_session=not fresh)
        try:
            page, state = await ensure_authenticated(
                context, config, reuse_session=not fresh
            )
            client = ReportsClient(context.request, state.bearer_token or "", config)
            entries = await discover_eobs(
                page,
                context,
                client,
                config,
                from_date=from_date,
                to_date=to_date,
            )
            catalog_path = output_dir / CATALOG_FILENAME
            write_eob_catalog(
                catalog_path,
                entries,
                meta={
                    "from_date": from_date,
                    "to_date": to_date,
                    "discovered_at": datetime.now(timezone.utc).isoformat(),
                },
            )

            selections_path = output_dir / "selections.json"
            if not selections_path.exists() and entries:
                selections_path.write_text(
                    json.dumps(
                        [entries[0].to_selection_dict()],
                        indent=2,
                    ),
                    encoding="utf-8",
                )
                log.info("Wrote sample selections.json (first EOB only) — edit before export")

            if entries:
                selections_all_path = output_dir / SELECTIONS_ALL_FILENAME
                selections_all_path.write_text(
                    json.dumps(
                        [entry.to_selection_dict() for entry in entries],
                        indent=2,
                    ),
                    encoding="utf-8",
                )
                log.info(
                    "Wrote %s (%d EOB(s)) — use export-all or export-selected with this file",
                    SELECTIONS_ALL_FILENAME,
                    len(entries),
                )

            await save_storage_state(context, config.storage_state_path)
            await page.close()
            log.info("Discovery complete: %d EOB(s) in catalog", len(entries))
            return 0
        finally:
            await close_context(context)


async def cmd_export_selected(
    config: RevFlowConfig,
    *,
    selections_path: Path,
    output_dir: Path,
    fresh: bool = False,
    skip_existing: bool = True,
) -> int:
    selections = load_selections(selections_path)
    if not selections:
        log.error("No selections found in %s", selections_path)
        return 1

    exports_dir = output_dir / "exports"
    checkpoint_path = output_dir / CHECKPOINT_FILENAME
    manifest_path = output_dir / MANIFEST_FILENAME
    completed = _load_checkpoint(checkpoint_path)
    manifest = _load_manifest(manifest_path)

    async with async_playwright() as playwright:
        context = await create_context(playwright, config, reuse_session=not fresh)
        try:
            page, state = await ensure_authenticated(
                context, config, reuse_session=not fresh
            )
            exports_since_refresh = 0

            ok_count = 0
            for selection in selections:
                key = selection_key(selection)
                if key in completed:
                    log.info("Checkpoint skip: %s", key)
                    continue

                state, exports_since_refresh = await _maybe_refresh_session(
                    page, context, config, state, exports_since_refresh
                )

                try:
                    result, state = await _export_with_recovery(
                        page,
                        context,
                        config,
                        state,
                        selection,
                        exports_dir,
                        skip_existing=skip_existing,
                    )
                    _append_manifest(manifest, result)
                    _save_manifest(manifest_path, manifest)
                    if result["status"] in {"ok", "skipped"}:
                        completed.add(key)
                        _save_checkpoint(checkpoint_path, completed)
                        ok_count += 1
                        if result["status"] == "ok":
                            exports_since_refresh += 1
                except SessionExpiredError as exc:
                    log.error("Export failed after re-auth retries for %s: %s", key, exc)
                    _append_manifest(
                        manifest,
                        {
                            "key": key,
                            "status": "error",
                            "error": str(exc),
                            "selection": selection,
                        },
                    )
                    _save_manifest(manifest_path, manifest)
                except Exception as exc:
                    log.error("Export failed for %s: %s", key, exc)
                    _append_manifest(
                        manifest,
                        {"key": key, "status": "error", "error": str(exc), "selection": selection},
                    )
                    _save_manifest(manifest_path, manifest)

            await save_storage_state(context, config.storage_state_path)
            await page.close()
            log.info("Export batch complete: %d/%d succeeded or skipped", ok_count, len(selections))
            return 0 if ok_count == len(selections) else 1
        finally:
            await close_context(context)


async def cmd_export_all(
    config: RevFlowConfig,
    *,
    output_dir: Path,
    fresh: bool = False,
    skip_existing: bool = True,
) -> int:
    catalog_path = output_dir / CATALOG_FILENAME
    if not catalog_path.exists():
        log.error(
            "No catalog at %s — run discover-eobs first",
            catalog_path,
        )
        return 1

    selections = load_selections(catalog_path)
    log.info("Exporting all %d EOB(s) from %s", len(selections), catalog_path)
    return await cmd_export_selected(
        config,
        selections_path=catalog_path,
        output_dir=output_dir,
        fresh=fresh,
        skip_existing=skip_existing,
    )


def cmd_verify_exports(
    *,
    output_dir: Path,
    catalog_paths: list[Path],
    manifest_path: Path | None = None,
    write_missing: Path | None = None,
) -> int:
    for catalog_path in catalog_paths:
        if not catalog_path.exists():
            log.error("Catalog not found: %s", catalog_path)
            return 1

    report = verify_exports(
        output_dir,
        catalog_paths,
        manifest_path=manifest_path,
    )
    report_path = write_verify_report(report, output_dir)
    print_verify_summary(report)
    log.info("Wrote verify report to %s", report_path)

    if write_missing is not None:
        count = write_missing_selections(report, write_missing)
        log.info("Wrote %d selection(s) needing export to %s", count, write_missing)

    summary = report["summary"]
    if summary["missing"] or summary["collision_missing"]:
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    global_args = argparse.ArgumentParser(add_help=False)
    global_args.add_argument("--headless", action="store_true", help="Run browser headless")
    global_args.add_argument("--fresh-login", action="store_true", help="Ignore saved session")
    global_args.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )

    parser = argparse.ArgumentParser(description="RevFlow Billing Open 835s export scraper")
    parser.add_argument("--headless", action="store_true", help="Run browser headless")
    parser.add_argument("--fresh-login", action="store_true", help="Ignore saved session")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser(
        "login",
        parents=[global_args],
        help="Login and save session (includes Gmail IP registration)",
    )

    sub.add_parser(
        "list-session",
        parents=[global_args],
        help="Verify saved session is still valid",
    )

    discover = sub.add_parser(
        "discover-eobs",
        parents=[global_args],
        help="Build EOB catalog for a date range",
    )
    discover.add_argument("--from-date", required=True, help="MM/DD/YYYY")
    discover.add_argument("--to-date", required=True, help="MM/DD/YYYY")
    discover.add_argument("--output", required=True, help="Output directory")
    discover.add_argument("--run-id", default=None, help="Optional run id suffix")

    export_cmd = sub.add_parser(
        "export-selected",
        parents=[global_args],
        help="Export EOBs from selections file",
    )
    export_cmd.add_argument("--selections", required=True, help="Path to selections.json")
    export_cmd.add_argument("--output", required=True, help="Output directory")
    export_cmd.add_argument("--run-id", default=None)
    export_cmd.add_argument(
        "--no-skip-existing",
        action="store_true",
        help="Re-download even if file exists",
    )

    export_all_cmd = sub.add_parser(
        "export-all",
        parents=[global_args],
        help="Export all EOBs from eob_catalog.json in the output directory",
    )
    export_all_cmd.add_argument("--output", required=True, help="Output directory")
    export_all_cmd.add_argument("--run-id", default=None)
    export_all_cmd.add_argument(
        "--no-skip-existing",
        action="store_true",
        help="Re-download even if file exists",
    )

    verify_cmd = sub.add_parser(
        "verify-exports",
        parents=[global_args],
        help="Verify exports against EOB catalog(s)",
    )
    verify_cmd.add_argument("--output", required=True, help="Output directory with exports/")
    verify_cmd.add_argument(
        "--catalog",
        action="append",
        required=True,
        help="Path to eob_catalog.json (repeat for multiple catalogs)",
    )
    verify_cmd.add_argument(
        "--manifest",
        default=None,
        help="Optional manifest.json to check for export errors",
    )
    verify_cmd.add_argument(
        "--write-missing",
        default=None,
        help="Write missing/collision selections to this JSON file",
    )

    return parser


async def run_cli(args: argparse.Namespace) -> int:
    config = RevFlowConfig.from_env()
    if args.headless:
        config.headless = True

    if args.command == "login":
        return await cmd_login(config, fresh=args.fresh_login)

    if args.command == "list-session":
        return await cmd_list_session(config)

    if args.command == "discover-eobs":
        output_dir = _resolve_output_dir(args.output, args.run_id)
        output_dir.mkdir(parents=True, exist_ok=True)
        return await cmd_discover_eobs(
            config,
            from_date=args.from_date,
            to_date=args.to_date,
            output_dir=output_dir,
            fresh=args.fresh_login,
        )

    if args.command == "export-selected":
        output_dir = _resolve_output_dir(args.output, args.run_id)
        output_dir.mkdir(parents=True, exist_ok=True)
        return await cmd_export_selected(
            config,
            selections_path=Path(args.selections),
            output_dir=output_dir,
            fresh=args.fresh_login,
            skip_existing=not args.no_skip_existing,
        )

    if args.command == "export-all":
        output_dir = _resolve_output_dir(args.output, args.run_id)
        output_dir.mkdir(parents=True, exist_ok=True)
        return await cmd_export_all(
            config,
            output_dir=output_dir,
            fresh=args.fresh_login,
            skip_existing=not args.no_skip_existing,
        )

    if args.command == "verify-exports":
        output_dir = _resolve_output_dir(args.output, None)
        catalog_paths = [Path(p) for p in args.catalog]
        manifest_path = Path(args.manifest) if args.manifest else None
        write_missing = Path(args.write_missing) if args.write_missing else None
        return cmd_verify_exports(
            output_dir=output_dir,
            catalog_paths=catalog_paths,
            manifest_path=manifest_path,
            write_missing=write_missing,
        )

    raise ValueError(f"Unknown command: {args.command}")


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    setup_logging(args.log_level)
    try:
        raise SystemExit(asyncio.run(run_cli(args)))
    except ValueError as exc:
        log.error("Configuration error: %s", exc)
        raise SystemExit(1) from exc
    except KeyboardInterrupt:
        log.warning("Interrupted")
        raise SystemExit(130)


if __name__ == "__main__":
    main()
