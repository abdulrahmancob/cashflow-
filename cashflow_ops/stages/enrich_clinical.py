"""Stage 3 — Clinical Enrichment (OCR / notes / CPT / POC / denials + retry queue)."""

from __future__ import annotations

import logging
from typing import Any

from cashflow_ops.adapters import case_pipeline
from cashflow_ops.adapters.subprocess_runner import run_python_script
from cashflow_ops.config import (
    CASE_PIPELINE_DIR,
    ENRICH_CRITICAL_FAIL_PCT,
    RETRY_DELAY_HOURS,
    WEBPT_DIR,
    WEBPT_LEGACY_OUTPUT,
)
from cashflow_ops.contracts import ArtifactSpec, FailurePolicy, RunContext, StageResult

log = logging.getLogger(__name__)


class EnrichClinicalStage:
    key = "enrich_clinical"
    requires = ["validate_sources"]
    produces = ["clinical_enriched", "ocr_complete"]
    on_failure = FailurePolicy.CONTINUE_WITH_ALERT
    max_attempts = 2

    def run(self, ctx: RunContext) -> StageResult:
        dry = ctx.dry_run
        skip = ctx.skip_scrapers
        outputs: dict[str, Any] = {}
        alerts: list[dict[str, Any]] = []
        retry_items: list[dict[str, Any]] = []
        warnings: list[str] = []

        enrich = case_pipeline.run_full_case_enrich(
            out_dir=CASE_PIPELINE_DIR, dry_run=dry, skip=skip
        )
        outputs["case_enrich"] = enrich.to_dict()
        if not enrich.ok and not enrich.skipped:
            warnings.append("case enrich reported failure")
            retry_items.append(
                {
                    "item_type": "ocr_batch",
                    "item_key": f"enrich:{ctx.as_of_date.isoformat()}",
                    "last_error": enrich.stderr[-500:] or "enrich failed",
                    "delay_hours": RETRY_DELAY_HOURS,
                    "max_attempts": 3,
                    "payload": {"out_dir": str(CASE_PIPELINE_DIR)},
                }
            )
            alerts.append(
                {
                    "severity": "warning",
                    "alert_key": "enrich_partial_failure",
                    "message": "Clinical enrich/OCR failed for some units; queued retry",
                }
            )

        ocr = case_pipeline.run_case_ocr_batch(
            out_dir=CASE_PIPELINE_DIR, dry_run=dry, skip=skip
        )
        outputs["ocr_batch"] = ocr.to_dict()
        if not ocr.ok and not ocr.skipped:
            retry_items.append(
                {
                    "item_type": "ocr",
                    "item_key": f"ocr:{ctx.as_of_date.isoformat()}",
                    "last_error": ocr.stderr[-500:] or "ocr failed",
                    "delay_hours": RETRY_DELAY_HOURS,
                    "payload": {},
                }
            )

        # POC / denial extract (legacy helpers — best effort)
        denial_script = WEBPT_DIR / "scripts" / "extract_denial_reasons.py"
        if denial_script.exists() and not skip:
            den = run_python_script(
                denial_script,
                [
                    "--edocs-dir",
                    str(WEBPT_LEGACY_OUTPUT / "edocs"),
                    "--output",
                    str(WEBPT_LEGACY_OUTPUT / "extracted" / "denial_reasons.csv"),
                ],
                cwd=WEBPT_DIR,
                dry_run=dry,
                timeout=2 * 3600,
            )
            outputs["denial_extract"] = den.to_dict()
        else:
            outputs["denial_extract"] = {"skipped": True}

        # Volume sanity: extracted notes present?
        notes = CASE_PIPELINE_DIR / "extracted" / "daily_notes.csv"
        cpt = CASE_PIPELINE_DIR / "extracted" / "cpt_codes.csv"
        notes_ok = notes.is_file() or dry or skip
        cpt_ok = cpt.is_file() or dry or skip
        outputs["notes_present"] = notes.is_file()
        outputs["cpt_present"] = cpt.is_file()

        # Estimate missing / OCR success for quality history
        from cashflow_ops import quality

        notes_missing = 0 if notes.is_file() else 1
        cpt_missing = 0 if cpt.is_file() else 1
        ocr_success_pct = 100.0 if (ocr.ok or ocr.skipped or dry) else 0.0
        outputs["notes_missing"] = notes_missing
        outputs["cpt_missing"] = cpt_missing
        outputs["ocr_success_pct"] = ocr_success_pct
        if not dry:
            quality.persist_from_metrics_dict(
                as_of_date=ctx.as_of_date,
                run_id=ctx.run_id,
                metrics={
                    "notes_missing": notes_missing,
                    "cpt_missing": cpt_missing,
                    "ocr_success_pct": ocr_success_pct,
                },
            )

        if not notes_ok or not cpt_ok:
            # Soft unless everything failed
            fail_ratio = 1.0 if (not enrich.ok and not ocr.ok) else 0.0
            if fail_ratio >= ENRICH_CRITICAL_FAIL_PCT and not dry and not skip:
                return StageResult.failed(
                    "Enrich clinical critical mass failure (notes/CPT missing and enrich/OCR failed)",
                    outputs=outputs,
                )
            alerts.append(
                {
                    "severity": "warning",
                    "alert_key": "clinical_extracts_thin",
                    "message": "Case daily_notes/cpt extracts missing or thin after enrich",
                }
            )

        return StageResult.success(
            outputs=outputs,
            artifacts=[
                ArtifactSpec(
                    key="clinical_enriched",
                    uri=str(CASE_PIPELINE_DIR / "extracted"),
                    payload={"notes": notes.is_file(), "cpt": cpt.is_file()},
                ),
                ArtifactSpec(
                    key="ocr_complete",
                    payload=outputs.get("ocr_batch", {}),
                ),
            ],
            alerts=alerts,
            retry_items=retry_items,
            warnings=warnings,
        )
