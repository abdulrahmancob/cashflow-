# Data Coverage Audit → Architecture Certification

**Date:** 2026-08-04  
**Scope:** `cashflow_db` vs active sources under `webpt_edco_scraper`, `revflow_scraper`, `snowflake_pull`  
**Runtime load verified:** **false** (re-run `migrate && load-all && validate` on a live DB to flip)  
**Certification result:** **IMPLEMENTATION IN PLACE — PENDING RUNTIME VERIFY** — product paths now support DB SoT (`repository`, `--from-db`, spine migration `013`); close with green `validate` + reconcile/forecast `--from-db` on full data (see [`deprecation_plan.md`](deprecation_plan.md))

---

## 0) Legend

| Code | Meaning |
|------|---------|
| **LEAK-S** | Needs schema |
| **LEAK-L** | Schema exists; loader missing/dropped |
| **LEAK-M** | Column exists; mapping incomplete/wrong |
| **LEAK-U** | Landed in DB but unused by consumers |
| **Critical / Important / Optional** | Business column tier |
| **Row / Column / Semantic** | Coverage dimensions |

Architectural decisions (documented):
- Clinical `visit` = one encounter per `(case_pk, service_date)`, not every scheduler slot
- `schedule_appointment` = first-class scheduler grain
- Case-aware notes/CPT only (legacy jun_jul extracts without `case_id` intentionally blocked)
- Snowflake = KPI staging (EMR+DOS), never selects `case_id`
- Reconciliation CSV rollups are **not** warehouse SoT; marts should derive from normalized facts

---

## 1) Executive lifecycle matrix

| Domain | Source (rows) | Loader | Table(s) | Used By | Row Cov | Col Cov (C+I) | Semantic | Status |
|--------|---------------|--------|----------|---------|---------|---------------|----------|--------|
| Schedule | `schedule_visits` 234,479 | `load_schedule` | `schedule_appointment`, `visit` | marts (`v_schedule_*`, recon views); forecast **not yet** | ~100% (0 blank `case_id`) | 12/13 Critical+Important → **~92%** | full (clinical selection documented) | ✅ mapping |
| Patients | `patients_export_jan_aug` 21,177 | `load_webpt` | `patient`, `patient_history` | marts, joins | ~100% w/ `case_id` | identity Critical OK; phones Optional omit | full for identity | ✅ / Optional omit |
| Cases | same export | `load_webpt` / schedule | `patient_case` | marts, visits | ~100% | `case_label` missing → **LEAK-S** Optional/Important | full | ⚠️ |
| Daily Notes | case extract **82** | `load_webpt` | `clinical_note` | marts; forecast via CSV today | **82 / ~233k DOS units ≪1%** | Critical note fields OK when present | full when loaded | ⚠️ volume P0 ops |
| CPT | case extract **113** | `load_webpt` | `visit_service_line` | marts; forecast via CSV | same volume gap | C+I OK | full when loaded | ⚠️ volume |
| Legacy notes/CPT | jun_jul 73k / 305k | **none** (by design) | — | recon/forecast CSV path | N/A | N/A | intentional outside case-DB | intentional |
| Coverage | schedule/patients `ins_name`, copay, ded | `load_schedule` / `load_webpt` | `patient_coverage`, `visit.coverage_id` | marts | high | Critical landed | **partial** (no within-case timeline) | ⚠️ semantic |
| Authorizations | `auth_ins_visits` | `load_webpt` | `authorization` | mart patient auth | high | field landed | **partial** (remaining vs authorized) | ⚠️ semantic |
| Patient Payments | 44,807 | `load_patient_payments` | `patient_payment` | **no mart/forecast consumer yet** | ~100% | ~100% C+I | full | ⚠️ **LEAK-U** |
| RevFlow EOB | 12,889 files | `load_revflow` | `eob_*`, claim bootstrap | marts unmatched/payor; forecast uses CSV recon today | mapping OK | C+I OK | full for ERA lines | ⚠️ non-idempotent; LEAK-U vs forecast |
| Tracker | Tracker XLSX | `load_tracker` | `bank_deposit`, allocations | mart cash daily; forecast scripts use tracker/CSV | mapping OK | C+I OK | full | ✅ mapping |
| Mail | mail checks CSV | `load_mail` | deposits / `mail_work_item` | ops | mapping OK | OK | full | ✅ |
| Snowflake KPI | billing CSV 168,589 | `load_snowflake_kpi` | `snowflake_visit_kpi` | mart `v_sf_vs_case_coverage`; SF scripts still use CSV | ~100% EMR+DOS | core KPI cols; 3rd/4th checks in `payload` | staging-only **full** | ✅ staging |
| EDocs | manifest 26,068 (`chart_note`,`edoc`) | `load_webpt` | `docs.document` | limited | high for manifest rows | type taxonomy partial | partial (only 2 sources) | ⚠️ |
| POC / denial letters | plans_of_care 15,900; denial_reasons 387 | **dropped from `load_webpt`** | `plan_of_care_detail`, `denial_letter_detail` exist | none | 0% | — | — | **P0 LEAK-L** |
| POC goals / referral ICD / OCR all | 143k / 15k / 164k | none | no dedicated facts | none | 0% | — | — | **LEAK-S** (Important) |
| Audit findings | jun_jul `audit/*` | `load_webpt` | `audit_finding` | limited | if files present | OK | full | ✅ mapping |
| Forecast outcomes | `outcome_stages` 454,880 | `load_forecast` | `forecast_prediction` | mart outcome views; **forecast package still reads CSV** | mapping OK | loaded subset | partial vs full feature set | ⚠️ dual path |
| Forecast other CSVs | 48 files (SLA, cash, risk_flags, …) | mostly **none** | some marts only | `cashflow_forecast` CSV SoT | N/A | — | features outside DB | **P0/P1 LEAK** for certification of forecast-in-DB |
| Recon rollups | `reconciliation_*.csv` etc. | none → marts | mart views | `cashflow_forecast` / reconcile **CSV SoT** | N/A | — | intentional not fact; consumers not on DB | ⚠️ certification blocker for DoD#6 |
| Case pipeline ops | sqlite, reports, checkpoints | none | — | pipeline only | N/A | — | intentional outside warehouse | intentional |

---

## 2) Source inventory (counts)

| Source key | Path | Rows / files |
|------------|------|--------------|
| schedule_visits | `webpt_edco_scraper/output/jan_aug_2026/schedule_visits_2026-01-01_2026-08-30.csv` | 234,479 |
| patients | `.../patients_export_jan_aug_2026.csv` | 21,177 |
| patient_payments | `.../patient_payments_202601_202608.csv` | 44,807 |
| case daily_notes | `snowflake_pull/artifacts/side_by_side_case/extracted/daily_notes.csv` | **82** |
| case cpt | `.../extracted/cpt_codes.csv` | **113** |
| legacy daily_notes | `jun_jul_2026/extracted/daily_notes.csv` | 73,253 (no `case_id`) |
| legacy cpt | `jun_jul_2026/extracted/cpt_codes.csv` | 305,182 |
| plans_of_care | jun_jul extracted | 15,900 |
| denial_reasons | jun_jul extracted | 387 |
| poc_goals | jun_jul extracted | 143,342 |
| referral_icd | jun_jul extracted | 14,921 |
| ocr_all_files | jun_jul extracted | 164,408 |
| edocs_manifest | latest jun_jul parallel | 26,068 (`chart_note` 15,542; `edoc` 10,526) |
| revflow exports | `revflow_scraper/output/jan_jul_2026/exports` | 12,889 files |
| sf billing | `snowflake_pull/output/billing_2026-01-01_to_2026-07-30.csv` | 168,589 |
| outcome_stages | jun_jul forecast | 454,880 |
| forecast CSV pack | jun_jul/forecast | **48** files |
| recon_lines | jun_jul reconciliation | 257,908 |

Raw inventory JSON: [`docs/_audit_inventory.json`](_audit_inventory.json).

---

## 3) Column maps (selected domains)

### 3.1 Schedule → `schedule_appointment` / `visit`

| Column | Tier | Landing | Notes |
|--------|------|---------|-------|
| facility_id | Critical | `ref.facility` + FKs | |
| case_id | Critical | `patient_case.webpt_case_id` | fail-closed if blank |
| patient_id | Critical | `patient.webpt_patient_id` | |
| appointment_at | Critical | both tables | clinical winner copied to visit |
| visit_status | Critical | `status` / `visit_status_raw` | `"1"`→scheduled |
| checkin/checkout | Important | `check_in_at` / `check_out_at` | am/pm parse |
| ins_name | Critical | coverage + `insurance_name_raw` | |
| auth_ins_visits | Important | not on appointment; auth from patients export | Semantic via auth table |
| copay/deductible | Important | coverage when from patients; schedule values unused on appt | **LEAK-M** schedule copay/ded → not stored on appointment |
| case_label | Important | — | **LEAK-S** |

**Row coverage:** 234,479/234,479 blank-case 0.  
**Column coverage (C+I):** ~92%.  
**Semantic:** full for clinical DOS selection.

### 3.2 Patients export

| Column | Tier | Landing |
|--------|------|---------|
| patient_id, name, dob | Critical | patient + history |
| case_id, facility_*, therapist, diagnosis | Critical/Important | patient_case |
| ins_name, ded, copay, limit, referral | Critical/Important | patient_coverage |
| auth_ins_visits | Important | authorization (semantic partial) |
| appointment_* lists / counts | Optional | omit (schedule is SoT) |
| edoc_* / chart_notes_* / ocr_* | Optional | omit (manifest/OCR paths) |
| additional_info_raw, cancel_no_show, visits_in_case | Optional/Important | mostly omit — visits_in_case **LEAK-M** |

### 3.3 Case daily_notes / CPT

Critical landed when present: `daily_note_id`, DOS, diagnosis ICDs, insurance, CPT/units/modifiers.  
Optional omit: facility address/fax, source_url, empty `appointment_id`.  
**Row coverage vs schedule clinical universe: critical gap** (pipeline incomplete, not schema).

### 3.4 Patient payments

All Critical/Important columns map to `billing.patient_payment` including `payment_category`.  
**LEAK-U:** no mart view / forecast path reads this table yet.

### 3.5 RevFlow

ERA line Critical fields → `eob_check` / `eob_line` / `eob_carc_raw`.  
**Loader:** insert-only per file run → **not idempotent** (re-run duplicates).

### 3.6 Snowflake KPI

EMR_ID, DOS, status, payments, primary/secondary checks → typed columns; remaining CSV fields → `payload` jsonb.  
Semantic: staging/validation only — **full for that role**.

### 3.7 EDocs / clinical extracts

| Source | Schema | Loader | Verdict |
|--------|--------|--------|---------|
| edocs_manifest | `docs.document` | yes | Partial types |
| plans_of_care | `docs.plan_of_care_detail` | **no** | **P0 LEAK-L** |
| denial_reasons | `docs.denial_letter_detail` | **no** | **P0 LEAK-L** |
| poc_goals | — | no | LEAK-S Important |
| referral_icd | — | no | LEAK-S Important |
| ocr_all_files | `document_ocr` partial | no | LEAK-L/S |

---

## 4) Reverse audit (DB → Source)

| Table | Canonical source | Status |
|-------|------------------|--------|
| `core.patient` | patients_export + schedule + revflow ids | OK |
| `core.patient_history` | patients_export / notes | OK |
| `core.patient_case` | patients_export / schedule | OK |
| `core.patient_coverage` | schedule/patients ins | OK |
| `core.authorization` | patients `auth_ins_visits` | OK (semantic partial) |
| `core.authorization_visit` | — | **Dead / unused** (no loader) |
| `core.visit` | schedule (clinical) | OK |
| `core.schedule_appointment` | schedule_visits | OK |
| `core.clinical_note` | case daily_notes | OK (volume) |
| `core.visit_service_line` | case cpt | OK (volume) |
| `core.lead_intake` | — | **Phase-2 stub / Dead** |
| `billing.claim` / `claim_line` / `claim_event` | RevFlow bootstrap | OK partial |
| `billing.denial_record` | — | **Dead** (no loader; denials via docs/audit) |
| `billing.audit_finding` | audit CSVs | OK |
| `billing.eob_*` | RevFlow | OK |
| `billing.bank_deposit` / allocation | Tracker / mail | OK |
| `billing.patient_payment` | patient_payments CSV | OK land; **LEAK-U** |
| `docs.document` | edocs_manifest | OK |
| `docs.document_ocr` | — | **Dead** (no loader) |
| `docs.plan_of_care_detail` | plans_of_care | **LEAK-L** |
| `docs.denial_letter_detail` | denial_reasons | **LEAK-L** |
| `ops.mail_work_item` | mail | OK |
| `ops.note_issue` / `outreach_event` / `coverage_hold_action` | — | **Phase-2 stubs / Dead** |
| `analytics.forecast_*` | outcome_stages | OK land; dual CSV path |
| `analytics.snowflake_visit_kpi` | SF billing CSV | OK staging |
| `finance.*` | — | **Phase-3 stubs / Dead** |
| `ref.*` | seeds + rules XLSX/CSV | OK |
| `etl.etl_run` / `gov.system_audit_log` | loaders / unused audit log | etl OK; gov log **LEAK-U/Dead** |
| mart views | derived from facts | OK design; consumers often still on CSV |

---

## 5) Forecast feature audit

### 5.1 What `cashflow_forecast` actually reads (CSV SoT today)

| Input | Role | DB landing? |
|-------|------|-------------|
| `reconciliation/reconciliation_lines.csv` | primary classify input | **No fact load** — marts approximate |
| `reconciliation/payments_unified.csv` | payments / calibration | via RevFlow facts possible; **not used by forecast package** |
| `reconciliation/reconciliation_visits.csv` | visits / SF overrides | marts only |
| `forecast/outcome_stages.csv` | API + many scripts | `load_forecast` → `forecast_prediction` |
| `forecast/payer_sla.csv` | SLA features | **CSV-only** |
| `forecast/risk_flags.csv` | risk | **CSV-only** (partially in prediction `risk_flags` jsonb if imported) |
| `forecast/actual_cash_*` / `projected_cash_*` | cash API | marts `v_actual_cash_daily` / `v_projected_cash_daily` exist; **API reads CSV** |
| Tracker / SF CSV | RCA / overrides scripts | Tracker→DB; SF→KPI table; scripts still CSV |
| Waystar claims/denials CSVs | rejection/denial stages in classify | **CSV-only** (`denial_record` unused) |
| `insurance_behavior/*.csv` | velocity, deposit cadence | **CSV-only** (mart thinner / unused) |

### 5.2 Verdict

- Forecast **runtime path is still file-based**, not `cashflow_db`.
- DoD #6 (**marts only, no intermediate CSV SoT**) is **failed** for live forecast/reconcile packages.
- Importing `outcome_stages` into DB does **not** mean forecast features are DB-native.

---

## 6) Loader checklists

| Loader | inserts | upsert/update | rejects | lineage | counts | idempotent | incremental |
|--------|---------|---------------|---------|---------|--------|------------|-------------|
| `load_schedule` | ✅ | ✅ visit/appt | ✅ blank case | ✅ | ✅ | ✅ (natural keys) | full-file reload |
| `load_webpt` | ✅ | notes ON CONFLICT; auth **re-inserts** | skips no case | ✅ | ✅ | ⚠️ auth/coverage may duplicate | full |
| `load_patient_payments` | ✅ | ✅ by natural key | skip bad category | ✅ | ✅ | ✅ | full |
| `load_revflow` | ✅ | ❌ always INSERT | empty file skip | ✅ | ✅ | **❌ duplicates** | full |
| `load_tracker` | ✅ | UNIQUE payment_id | — | ✅ | ✅ | ✅ if payment_id stable | full |
| `load_mail` | ✅ | partial | — | ✅ | ✅ | ⚠️ | full |
| `load_rules` | ✅ | ON CONFLICT rules | — | ✅ | ✅ | ✅ | full |
| `load_snowflake_kpi` | ✅ | ✅ emr+dos | skip bad | ✅ | ✅ | ✅ | full |
| `load_forecast` | ✅ new run each time | append predictions | missing file fail | ✅ | ✅ | ⚠️ new run each call (versioned OK) | full |

---

## 7) ERD coverage — who reads?

| Table / view | Mart | cashflow_forecast | cashflow_reconcile | snowflake_pull | Notes |
|--------------|------|-------------------|--------------------|----------------|-------|
| `visit` / `schedule_appointment` | ✅ | ❌ (CSV) | ❌ | schedule scripts (CSV) | |
| `clinical_note` / service_line | ✅ | ❌ CSV notes path | ✅ extracted CSV | case extracts | |
| `eob_*` / deposits | ✅ | ❌ CSV payments_unified | ✅ builds CSV | tracker/SF scripts | |
| `patient_payment` | ❌ | ❌ | ❌ | ❌ | **LEAK-U** |
| `snowflake_visit_kpi` | ✅ `v_sf_vs_case_coverage` | ❌ CSV | ❌ | ✅ CSV primary | |
| `forecast_prediction` | ✅ | ❌ reads outcome_stages.csv | ❌ | ❌ | dual path |
| `plan_of_care_detail` / denial_letter | ❌ | ❌ | ❌ | ❌ | empty without loader |
| finance / lead / outreach | ❌ | ❌ | ❌ | ❌ | stubs |

---

## 8) Gap backlog

### P0 (blocks certification)

1. **LEAK-L:** Restore loaders for `plans_of_care` → `docs.plan_of_care_detail` and `denial_reasons` → `docs.denial_letter_detail` (schema already exists).
2. **Forecast/Reconcile DoD#6:** Consumers still treat recon/forecast CSVs as SoT — either migrate readers to marts/facts or explicitly defer certification of “DB-aligned pipelines” until that migration.
3. **Notes/CPT row coverage:** Case-pipeline extracts (82 notes / 113 CPT) vs 234k schedule rows — clinical documentation path not production-complete (**ops/pipeline P0**, not schema).
4. **`load_revflow` idempotency:** re-run duplicates EOB rows (**Critical money path**).

### P1

5. **LEAK-U:** `patient_payment` — add mart(s) or wire cashflow consumers.  
6. **Semantic:** authorization remaining vs authorized; coverage effective dating timeline.  
7. **LEAK-M:** schedule `copay`/`deductible` not stored on appointment.  
8. **LEAK-S:** `case_label`; poc_goals / referral_icd / ocr_all (if still business-used).  
9. **LEAK-U/Dead:** `authorization_visit`, `document_ocr`, `denial_record`, Phase-2/3 stubs — document or remove from “active ERD”.  
10. Forecast feature landings: `payer_sla`, `risk_flags`, cash series beyond outcome_stages.  
11. **Waystar** rejection/denial CSVs still feed forecast classify — `billing.denial_record` unused (**LEAK-L** for denials path).  
12. **insurance_behavior** CSVs (`payor_behavior_summary`, `checks_timeline`) remain forecast SoT; mart `v_payor_behavior_summary` is thinner and unused by forecast.

### P2

11. Optional patient export OCR/edoc rollup columns — intentional omit OK.  
12. Facility address/fax on notes — Optional omit.

---

## 9) Architecture Certification vs Definition of Done

| # | Criterion | Result |
|---|-----------|--------|
| 1 | No P0 LEAK (Critical + S/L/M) | **FAIL** — POC/denial LEAK-L; RevFlow non-idempotent; notes volume; forecast CSV SoT |
| 2 | Business-Critical columns have landing | **PARTIAL** — schedule/patients/payments/SF/RevFlow OK; clinical notes volume fail |
| 3 | Business-Critical sources have Loader+Destination | **PARTIAL** — POC/denial fail; recon CSV intentional non-fact |
| 4 | Reverse Audit clean | **PARTIAL** — stubs/dead tables documented |
| 5 | No critical source without landing | **PARTIAL** |
| 6 | Marts only; no CSV intermediate SoT | **FAIL** — forecast/reconcile still CSV-native |
| 7 | Loaders idempotent | **FAIL** — RevFlow; auth insert duplication risk |
| 8 | Architectural decisions documented | **PASS** (this doc + README/erd) |

### Certification statement

> **Not certified.**  
> Case-centric **schema and loaders** are largely mapped for schedule, patients, cases, payments, RevFlow, tracker, and Snowflake KPI staging.  
> Certification is blocked until P0 items are resolved: restore POC/denial loaders, fix RevFlow idempotency, complete case-aware clinical extract coverage (or explicitly scope DB clinical notes as pilot-only), and either migrate forecast/reconcile onto marts/facts or narrow the certification claim to “warehouse mapping” excluding those packages.

When P0 is closed, this document should be re-run with `runtime_verified: true` after `python -m cashflow_db migrate && load-all`.

---

## 10) Success statement (current honesty)

For files under `webpt_edco_scraper`, `revflow_scraper`, and `snowflake_pull` we can now state for each major domain whether it enters the DB, where, and why — **including intentional outsides and known bugs**.  
We **cannot** yet claim full Architecture Certification against the DoD above.
