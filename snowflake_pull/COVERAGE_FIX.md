# SF vs REC coverage recovery

Production control plane for closing Snowflake vs reconciliation visit gaps.

## Quick start (offline)

```bash
cd D:\cashflow\code
python snowflake_pull/scripts/run_init.py
# note run_id from JSON

python snowflake_pull/scripts/rebuild_root_cause.py --run-id <RUN_ID>
python snowflake_pull/scripts/fix_recon_missed.py --run-id <RUN_ID>
python snowflake_pull/scripts/validate_coverage_hypotheses.py --run-id <RUN_ID>
python snowflake_pull/scripts/build_sf_note_gap_list.py --run-id <RUN_ID> --enqueue --max-units 100
python snowflake_pull/scripts/run_clinic_rediscover.py --run-id <RUN_ID>          # blocked until P3 pass
python snowflake_pull/scripts/run_gap_batches.py --run-id <RUN_ID> --simulate    # FSM resume drill
python snowflake_pull/scripts/promote_rec.py --run-id <RUN_ID>                   # dry-run by default
python -m snowflake_pull.compare_visits --dual-key
```

## Online gates (browser)

1. **P3** — run schedule export for Brownsville `28029`, then set `summaries/gates/P3.json` `pass=true` with measured `schedule_coverage_pct`.
2. **P2a/P2b** — note-index then PDF pilot; unlock Track C only if P2b `pass=true`.
3. Clinic rediscover: `--execute` after P3 pass.
4. Gap batches: `--execute` after P2b pass.

## Workspace

`webpt_edco_scraper/output/jun_jul_2026/coverage_fix/runs/<run_id>/`

- `logs/*.jsonl` — structured decisions/errors
- `monitoring/heartbeat.json` — liveness
- `state/units.sqlite` — unit FSM
- `summaries/gates/*.json` — pass/fail gates
- `artifacts/` — classification, gap queues, side-by-side outputs
- `baseline/` — frozen live REC copy
- `RUN_LOCK` / `PROMOTE_LOCK` — single-flight

## Safety

- Live `reconciliation_visits.csv` is never overwritten unless `promote_rec.py --apply`.
- Rollback: `rollback_promote.py --apply` using `promote_manifest.json`.
- Home Care / Sensory Freeway are `out_of_scope` until mapped (P4).
