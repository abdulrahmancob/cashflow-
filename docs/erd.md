# Cashflow Database ERD

PostgreSQL · 3NF · typed Mermaid diagrams for all schemas.

Apply schema: python -m cashflow_db migrate

# Cashflow Database ERD (Mermaid)

PostgreSQL · 3NF · كل جدول بأعمدة وأنواع. الرسمة مقسومة domains عشان تفضل واضحة (رسمة واحدة بكل الأعمدة بتبقى غير مقروءة).

**اصطلاحات الأنواع:** `uuid` PK surrogate · `timestamptz` · `numeric(14,2)` فلوس · `jsonb` payloads · `text[]` قوائم بسيطة.

**أعمدة lineage مشتركة على كل Fact كبير:** `source_system text`, `source_natural_key text`, `etl_run_id uuid FK`, `created_at timestamptz` (مذكورة صراحة في الجداول الأساسية؛ ضمناً على الباقي).

**Case-centric identity:** clinical `VISIT` = `(case_pk, service_date)` · first-class `SCHEDULE_APPOINTMENT` = `(case_pk, appointment_at)` · Snowflake KPI = `(emr_id, date_of_service)` only.

**Operational spine (013):** `billing.reconciliation_run` / `reconciliation_line` / `reconciliation_visit_agg` · `analytics.payor_behavior_summary` / `checks_timeline` / `forecast_feature` · forecast_run lineage (`reconciliation_run_id`, `rules_version`, `source_etl_run_ids`). Consumers use `cashflow_db.repository` — see [`deprecation_plan.md`](deprecation_plan.md).

**Pipeline control (014):** `ops.pipeline_run` / `stage_run` / `stage_artifact` / `retry_item` / `alert_event` / `daily_snapshot` / `forecast_accuracy_day` — workflow engine state for the daily RCM Processing Platform (`cashflow_ops`). See [`rcm_platform.md`](rcm_platform.md).

**Monitoring / platform qualities (015):** schema `monitoring` — typed `pipeline_metric`, `pipeline_event`, `job_runtime`, `quality_metric`/`quality_rule`, `sla_definition`, `system_config`/`system_health`, `backfill_run`/`backfill_day`. Feature store: `analytics.feature_definition` + `feature_snapshot`. `dataset_version` on pipeline/recon/forecast/snapshot.

---

## 0) خريطة عالية المستوى

```mermaid
flowchart LR
  subgraph refLayer [ref]
    Payer[payer hierarchy]
    Codes[CPT ICD rules]
    Routes[submission_route]
  end
  subgraph coreLayer [core]
    Patient[patient SCD]
    Case[patient_case]
    Visit[visit]
    Note[clinical_note]
    Auth[authorization]
  end
  subgraph billLayer [billing]
    Claim[claim]
    Lines[claim_line]
    Events[claim_event]
    EOB[eob_check / eob_line]
    Bank[bank_deposit]
  end
  subgraph other [docs ops analytics gov etl]
    Docs[document]
    Forecast[forecast_run]
    AuditLog[system_audit_log]
  end
  refLayer --> coreLayer --> billLayer
  billLayer --> Forecast
  Docs --> coreLayer
  AuditLog --> billLayer
```

---

## 1) `ref` — Reference data

```mermaid
erDiagram
  PAYER_ORG {
    uuid payer_org_id PK
    text name
    text short_code
    boolean is_active
    timestamptz created_at
  }

  INSURANCE_PRODUCT {
    uuid insurance_product_id PK
    uuid payer_org_id FK
    text name
    text product_class
    boolean is_active
  }

  INSURANCE_PLAN {
    uuid insurance_plan_id PK
    uuid insurance_product_id FK
    text name
    text plan_type
    text reimbursement_model
    numeric flat_fee_amount
    text auth_pattern
    int direct_access_visit_limit
    int default_sla_days
    int high_season_sla_days
    boolean is_active
  }

  SUBMISSION_ROUTE {
    uuid submission_route_id PK
    text code
    text name
    text description
  }

  FACILITY {
    uuid facility_id PK
    text webpt_facility_id UK
    text name
    text clinic_cluster
    boolean is_active
  }

  CPT_CODE {
    text cpt_code PK
    text description
    boolean is_timed
    boolean is_active
  }

  ICD10_CODE {
    text icd10_code PK
    text description
    boolean is_billable
    boolean is_header
    date effective_from
    date effective_to
  }

  DOCUMENT_TYPE {
    uuid document_type_id PK
    text code
    text name
  }

  DENIAL_REASON_TAXONOMY {
    uuid reason_taxonomy_id PK
    text code
    text category
    text default_error_owner
    text description
  }

  INSURANCE_ALIAS {
    uuid alias_id PK
    text source_system
    text raw_name
    uuid payer_org_id FK
    uuid insurance_plan_id FK
    text revflow_payor
    int match_count
    boolean is_mapped
  }

  PAYER_CPT_RULE {
    uuid rule_id PK
    uuid insurance_plan_id FK
    text rule_kind
    text expected_value
    text severity
    text detail
  }

  ICD_DENIAL_RULE {
    uuid rule_id PK
    text category
    text description
    text examples
    text correct_approach
    text severity
  }

  PAYER_ORG ||--o{ INSURANCE_PRODUCT : has
  INSURANCE_PRODUCT ||--o{ INSURANCE_PLAN : has
  PAYER_ORG ||--o{ INSURANCE_ALIAS : aliases
  INSURANCE_PLAN ||--o{ INSURANCE_ALIAS : aliases
  INSURANCE_PLAN ||--o{ PAYER_CPT_RULE : rules
```

`product_class`: `commercial|medicaid|medicare|workers_comp|no_fault|other`  
`reimbursement_model`: `percent_of_charge|flat_per_visit`  
`auth_pattern`: `pattern_based|pre_service|none_dummy`  
`submission_route.code`: `waystar|zaya|manual`  
`document_type.code`: `referral|poc|prescription|denial|appeal|mri|lab|daily_note|remittance|other`

---

## 2) `etl` + `gov`

```mermaid
erDiagram
  ETL_RUN {
    uuid etl_run_id PK
    text source_system
    text source_uri
    timestamptz started_at
    timestamptz finished_at
    text status
    int row_count
    text checksum
    text notes
  }

  SYSTEM_AUDIT_LOG {
    uuid audit_log_id PK
    text user_id
    text action
    text entity_type
    uuid entity_id
    jsonb payload
    timestamptz at
    uuid etl_run_id FK
  }

  ETL_RUN ||--o{ SYSTEM_AUDIT_LOG : may_link
```

`source_system`: `webpt|revflow|tracker|mail|rules|waystar|zaya|snowflake|manual`  
`status`: `running|success|failed|partial`

---

## 3) `core` — Patient, Case, Coverage, Auth, Visit, Note

```mermaid
erDiagram
  PATIENT {
    uuid patient_id PK
    text webpt_patient_id UK
    text revflow_patient_id
    text name_key
    boolean is_active
    timestamptz created_at
    uuid etl_run_id FK
  }

  PATIENT_HISTORY {
    uuid patient_history_id PK
    uuid patient_id FK
    text patient_name
    date dob
    text mobile_phone
    text home_phone
    text work_phone
    text email
    text best_phone
    timestamptz valid_from
    timestamptz valid_to
    boolean is_current
  }

  PATIENT_CASE {
    uuid case_pk PK
    text webpt_case_id UK
    uuid patient_id FK
    uuid facility_id FK
    text assigned_therapist
    text diagnosis_raw
    text status
    text discharge_reason
    date opened_at
    date closed_at
    uuid etl_run_id FK
  }

  PATIENT_COVERAGE {
    uuid coverage_id PK
    uuid patient_id FK
    uuid case_pk FK
    uuid insurance_plan_id FK
    text member_id
    text payer_id_external
    boolean is_network_eligible
    numeric deductible
    numeric copay
    int limit_per_year
    boolean referral_required
    date effective_from
    date effective_to
    boolean is_primary
    text raw_insurance_name
    uuid etl_run_id FK
  }

  LEAD_INTAKE {
    uuid lead_id PK
    uuid patient_id FK
    text lead_source
    text campaign
    numeric cost_per_lead
    timestamptz captured_at
    text status
  }

  AUTHORIZATION {
    uuid auth_id PK
    uuid case_pk FK
    uuid coverage_id FK
    text auth_kind
    text auth_number
    int visits_authorized
    int visits_used
    date start_date
    date end_date
    text status
    int non_ev_tat_days
    text hold_reason
    uuid etl_run_id FK
  }

  VISIT {
    uuid visit_id PK
    uuid case_pk FK
    uuid patient_id FK
    uuid facility_id FK
    uuid coverage_id FK
    date service_date
    timestamptz appointment_at
    text visit_type
    int visit_no
    text status
    timestamptz check_in_at
    timestamptz check_out_at
    text insurance_name_raw
    text webpt_appointment_id
    text confirmation_response
    int green_board_units
    text source_system
    text source_natural_key
    uuid etl_run_id FK
    timestamptz created_at
  }

  SCHEDULE_APPOINTMENT {
    uuid schedule_appointment_id PK
    uuid case_pk FK
    uuid patient_id FK
    uuid facility_id FK
    uuid visit_id FK
    date service_date
    timestamptz appointment_at
    text webpt_appointment_id
    text visit_status_raw
    text status
    timestamptz check_in_at
    timestamptz check_out_at
    text insurance_name_raw
    boolean is_selected_clinical
    text source_system
    text source_natural_key
    uuid etl_run_id FK
  }

  AUTHORIZATION_VISIT {
    uuid auth_visit_id PK
    uuid auth_id FK
    uuid visit_id FK
    int units_consumed
    timestamptz consumed_at
  }

  CLINICAL_NOTE {
    uuid note_id PK
    uuid visit_id FK
    text external_daily_note_id UK
    text note_kind
    date note_date
    int version_no
    text note_file
    text referring_physician
    text diagnosis_raw
    text diagnosis_icd_codes
    text treatment_diagnosis_icd_codes
    text insurance_name_raw
    text extraction_method
    timestamptz signed_at
    timestamptz finalized_at
    int sla_hours_target
    boolean sla_breached
    text error
    uuid etl_run_id FK
  }

  VISIT_SERVICE_LINE {
    uuid service_line_id PK
    uuid visit_id FK
    text cpt_code FK
    text modifiers
    int units
    text description
    text billing_modifier_suffix
    uuid etl_run_id FK
  }

  PATIENT ||--o{ PATIENT_HISTORY : versions
  PATIENT ||--o{ PATIENT_CASE : has
  PATIENT ||--o{ PATIENT_COVERAGE : enrolled
  PATIENT ||--o{ LEAD_INTAKE : sourced_as
  PATIENT_CASE ||--o{ PATIENT_COVERAGE : may_scope
  PATIENT_CASE ||--o{ AUTHORIZATION : authorizes
  PATIENT_COVERAGE ||--o{ AUTHORIZATION : under
  PATIENT_CASE ||--o{ VISIT : includes
  PATIENT_CASE ||--o{ SCHEDULE_APPOINTMENT : schedules
  VISIT ||--o{ SCHEDULE_APPOINTMENT : selected_from
  PATIENT_COVERAGE ||--o{ VISIT : covers
  PATIENT ||--o{ VISIT : attends
  AUTHORIZATION ||--o{ AUTHORIZATION_VISIT : consumes
  VISIT ||--o{ AUTHORIZATION_VISIT : uses
  VISIT ||--o{ CLINICAL_NOTE : documented_by
  VISIT ||--o{ VISIT_SERVICE_LINE : bills
```

**Enums مهمة**

| عمود | قيم |
|------|-----|
| `patient_case.status` | `active\|partial_discharge\|full_discharge\|closed` |
| `authorization.auth_kind` | `hard\|dummy` |
| `authorization.status` | `approved\|non_ev\|on_hold\|expired\|exhausted` |
| `authorization.hold_reason` | `cob\|missing_referral\|insurance_change\|md_pending\|other` |
| `visit.visit_type` | `follow_up\|initial\|re_examination` |
| `visit.status` | `scheduled\|confirmed\|completed\|cancelled\|no_show\|unchecked_out` |
| `visit.confirmation_response` | `C\|R\|X\|none` |
| `clinical_note.note_kind` | `initial\|daily\|correction\|addendum\|poc\|recert` |
| `lead_source` | `phone\|zocdoc\|website\|zoho\|google_ads\|other` |

**قاعدة أعمال:** `authorization.end_date` يغلب `visits_authorized - visits_used`. Dummy `auth_number = '0'` ≠ صفر زيارات.

---

## 4) `billing` — Claim lifecycle + money

```mermaid
erDiagram
  CLAIM {
    uuid claim_id PK
    text claim_number
    uuid case_pk FK
    uuid patient_id FK
    uuid coverage_id FK
    uuid insurance_plan_id FK
    uuid submission_route_id FK
    text payer_sequence
    uuid parent_claim_id FK
    boolean force_rejected_from_waystar
    date submit_date
    date service_date_from
    date service_date_to
    text status_current
    numeric billed_total
    text source_system
    text source_natural_key
    uuid etl_run_id FK
    timestamptz created_at
  }

  CLAIM_LINE {
    uuid claim_line_id PK
    uuid claim_id FK
    uuid visit_id FK
    uuid service_line_id FK
    int line_no
    text cpt_code
    text modifiers
    int units
    numeric billed_amount
    numeric allowed_amount
    numeric expected_amount
    uuid etl_run_id FK
  }

  CLAIM_EVENT {
    uuid claim_event_id PK
    uuid claim_id FK
    uuid claim_line_id FK
    text event_type
    timestamptz event_at
    text actor_user_id
    jsonb payload
    text source_system
    uuid etl_run_id FK
  }

  DENIAL_RECORD {
    uuid denial_id PK
    uuid claim_id FK
    uuid claim_line_id FK
    uuid facility_id FK
    uuid reason_taxonomy_id FK
    text reason_code
    text error_owner
    boolean is_partial_denial
    text source
    numeric denied_amount
    date denial_date
    uuid etl_run_id FK
  }

  AUDIT_FINDING {
    uuid finding_id PK
    uuid visit_id FK
    uuid note_id FK
    uuid patient_id FK
    text finding_kind
    text rule_id
    text severity
    jsonb detail
    uuid etl_run_id FK
  }

  EOB_CHECK {
    uuid eob_check_id PK
    text eob_key
    text company_id
    text check_eft_num
    text payor_raw
    date check_date
    date eob_date
    date report_from
    date report_to
    numeric paid_amount_sum
    text source_file
    text source_system
    text source_natural_key
    uuid etl_run_id FK
  }

  EOB_LINE {
    uuid eob_line_id PK
    uuid eob_check_id FK
    text revflow_patient_id
    uuid patient_id FK
    uuid claim_line_id FK
    date date_of_service
    text cpt_code
    text modifiers
    int units
    numeric billed_amount
    numeric allowed_amount
    numeric paid_amount
    numeric adjustment_amount
    numeric deductible_amount
    text carcs
    text pr_oa_codes
    text source_system
    uuid etl_run_id FK
  }

  EOB_CARC_RAW {
    uuid eob_carc_id PK
    uuid eob_check_id FK
    text revflow_patient_id
    date date_of_service
    text cpt_code
    text carc_code
    numeric adjustment_amount
    uuid etl_run_id FK
  }

  BANK_DEPOSIT {
    uuid deposit_id PK
    text payment_id_external UK
    text channel
    date check_date_recognized
    date bank_posting_date
    numeric amount
    text transaction_type
    text bank_name
    text description
    text billing_status
    text collector
    boolean posted
    text notes
    text eft_1
    text eft_2
    text eft_last4
    text source_system
    uuid etl_run_id FK
  }

  DEPOSIT_CHECK_ALLOCATION {
    uuid allocation_id PK
    uuid deposit_id FK
    uuid eob_check_id FK
    numeric allocated_amount
    text match_method
    numeric confidence
    uuid etl_run_id FK
  }

  PATIENT_PAYMENT {
    uuid patient_payment_id PK
    uuid patient_id FK
    uuid case_pk FK
    uuid visit_id FK
    uuid facility_id FK
    date service_date
    date transaction_date
    text payment_category
    text payment_type
    text description
    numeric amount_due
    numeric amount_paid
    text paid_method
    text credit_type
    text source_system
    text source_natural_key
    uuid etl_run_id FK
  }

  CLAIM ||--o{ CLAIM : secondary_of
  CLAIM ||--o{ CLAIM_LINE : contains
  CLAIM ||--o{ CLAIM_EVENT : timeline
  CLAIM_LINE ||--o{ CLAIM_EVENT : line_events
  CLAIM ||--o{ DENIAL_RECORD : denied_as
  CLAIM_LINE ||--o{ DENIAL_RECORD : line_denied
  EOB_CHECK ||--o{ EOB_LINE : pays
  EOB_CHECK ||--o{ EOB_CARC_RAW : raw_carcs
  CLAIM_LINE ||--o{ EOB_LINE : allocated_from
  BANK_DEPOSIT ||--o{ DEPOSIT_CHECK_ALLOCATION : splits
  EOB_CHECK ||--o{ DEPOSIT_CHECK_ALLOCATION : matched
  PATIENT ||--o{ PATIENT_PAYMENT : office_pay
  VISIT ||--o{ PATIENT_PAYMENT : for_visit
```

`patient_payment.payment_category`: `Copay|Other|Wellness|Deductible|Supplies|Internal Payment`

**Enums مهمة**

| عمود | قيم |
|------|-----|
| `claim.payer_sequence` | `primary\|secondary` |
| `claim.status_current` | آخر `event_type` / ملخص حالة |
| `claim_event.event_type` | `created\|internal_hold\|ready_for_submission\|submitted\|force_rejected_waystar\|submitted_zaya\|dropped_unsubmitted\|clearinghouse_rejected\|accepted\|payer_denied\|era_received\|appealed\|adjusted\|patient_responsibility\|self_pay_converted\|deposit_posted\|closed\|merged\|relinked` |
| `denial_record.error_owner` | `front_desk\|medical_audit\|authorization\|coding\|payer_behavior` |
| `audit_finding.finding_kind` | `cpt_rule\|icd_rule` |
| `bank_deposit.channel` | `eft\|mail_check\|v_card\|direct_deposit\|ach\|other` |
| `allocation.match_method` | `eft1\|eft2\|manual\|mail_sheet\|last4` |
| `pr_oa_codes` | `PR1\|PR2\|PR3\|OA23` + CARCs |

**سلسلة المال:** `VISIT_SERVICE_LINE → CLAIM_LINE → EOB_LINE` و `BANK_DEPOSIT ↔ EOB_CHECK` عبر allocation (M:N).  
**Dual dates:** ledger = `eob_check.check_date` · liquidity = `bank_deposit.bank_posting_date`.

---

## 5) `docs` — Documents + OCR

```mermaid
erDiagram
  DOCUMENT {
    uuid document_id PK
    uuid patient_id FK
    uuid case_pk FK
    uuid document_type_id FK
    text ext_doc_id
    text filename
    text storage_path
    text source
    text status
    text status_description
    text error
    uuid etl_run_id FK
    timestamptz created_at
  }

  DOCUMENT_OCR {
    uuid document_ocr_id PK
    uuid document_id FK
    text extraction_method
    int text_chars
    text icd_codes
    text raw_text_ref
    text ocr_name
    boolean name_match
    text ocr_patient_id
    boolean id_match
    text ocr_diagnosis
    boolean diagnosis_match
    uuid etl_run_id FK
  }

  PLAN_OF_CARE_DETAIL {
    uuid poc_detail_id PK
    uuid document_id FK
    text poc_id
    date date_of_plan_of_care
    text frequency
    text duration
    text plan_text
  }

  DENIAL_LETTER_DETAIL {
    uuid denial_letter_id PK
    uuid document_id FK
    date denial_date
    text insurance_name
    text payer_guess
    text reason_raw
    text reason_class
  }

  DOCUMENT ||--o| DOCUMENT_OCR : extracted
  DOCUMENT ||--o| PLAN_OF_CARE_DETAIL : poc
  DOCUMENT ||--o| DENIAL_LETTER_DETAIL : denial_letter
```

`document.source`: `edoc|chart_note|upload|mail|drive`

---

## 6) `ops` — Mail / CC / issues

```mermaid
erDiagram
  MAIL_WORK_ITEM {
    uuid work_item_id PK
    text payer_label
    text collector
    text item_type
    text notes
    boolean previously_posted_flag
    uuid linked_eob_check_id FK
    uuid linked_deposit_id FK
    text status
    uuid etl_run_id FK
  }

  NOTE_ISSUE {
    uuid note_issue_id PK
    uuid visit_id FK
    uuid note_id FK
    text issue_type
    text severity
    text therapist
    text status
    timestamptz opened_at
    timestamptz resolved_at
  }

  OUTREACH_EVENT {
    uuid outreach_id PK
    uuid patient_id FK
    uuid visit_id FK
    text channel
    text campaign_type
    text response
    text disposition
    text agent_id
    timestamptz sent_at
    timestamptz responded_at
  }

  COVERAGE_HOLD_ACTION {
    uuid hold_action_id PK
    uuid coverage_id FK
    uuid auth_id FK
    text action
    int appointments_cancelled
    text agent_id
    timestamptz acted_at
  }
```

`mail item_type`: `eob|check|eob_and_check` · `outreach.channel`: `sms|call|email` · `campaign_type`: `next_day_confirm|no_upcoming|auth_approval|self_discharge`

`NOTE_ISSUE` / `OUTREACH_EVENT` = Phase 2 جداول محجوزة في الـ ERD.

---

## 7) `analytics` — Versioned forecast + Snowflake KPI staging

```mermaid
erDiagram
  SNOWFLAKE_VISIT_KPI {
    uuid snowflake_visit_kpi_id PK
    text emr_id
    date date_of_service
    uuid patient_id FK
    text patient_name
    text insurance
    text clinic
    text status
    numeric charged_amount
    numeric insurance_payment
    numeric client_payment
    text sf_visit_id
    text sf_billing_id
    jsonb payload
    text source_system
    uuid etl_run_id FK
  }

  FORECAST_RUN {
    uuid forecast_run_id PK
    text algorithm_version
    jsonb params
    date as_of_date
    text status
    uuid etl_run_id FK
    timestamptz created_at
  }

  FORECAST_PREDICTION {
    uuid prediction_id PK
    uuid forecast_run_id FK
    uuid claim_line_id FK
    uuid visit_id FK
    text outcome_stage
    numeric expected_amount
    date expected_pay_date
    int overdue_days
    numeric denied_amount
    text denial_category
    int sla_lag_days
    int forecast_shift_days
    jsonb risk_flags
    numeric risk_score
  }

  FORECAST_RUN ||--o{ FORECAST_PREDICTION : produces
```

`outcome_stage`: `paid|on_track|overdue|rejected|denied|zero_pay`  
`params` يسجّل: first_pass_target, denial_shift_cycles, medical_audit_delay_pct, auth_delay_pct, high_season_sla — **ممنوع overwrite لتوقعات run قديم**.

---

## 8) `finance` — Phase 3 stubs فقط

```mermaid
erDiagram
  MASTER_BUDGET {
    uuid budget_id PK
    int fiscal_year
    int fiscal_month
    uuid facility_id FK
    text department
    numeric revenue_target
    numeric expense_budget
  }

  COGS_ENTRY {
    uuid cogs_id PK
    uuid facility_id FK
    date period_date
    text component
    numeric amount
    text allocation_split
  }

  DEPARTMENT_SCORECARD {
    uuid scorecard_id PK
    text department
    date period_date
    numeric budget_adherence
    numeric kpi_score
    numeric utilization_score
    numeric combined_score
  }
```

`cogs.component`: `labor|supplies|direct_utilities|documentation`

---

## 9) علاقات عابرة للـ schemas (الملخص)

```mermaid
erDiagram
  FACILITY ||--o{ PATIENT_CASE : hosts
  INSURANCE_PLAN ||--o{ PATIENT_COVERAGE : covers
  INSURANCE_PLAN ||--o{ CLAIM : billed_to
  SUBMISSION_ROUTE ||--o{ CLAIM : routes
  PATIENT_CASE ||--o{ CLAIM : submits
  VISIT_SERVICE_LINE ||--o| CLAIM_LINE : becomes
  CLINICAL_NOTE ||--o{ AUDIT_FINDING : flags
  VISIT ||--o{ AUDIT_FINDING : flagged_on
  PATIENT ||--o{ DOCUMENT : owns
  DOCUMENT_TYPE ||--o{ DOCUMENT : classifies
  CLAIM_LINE ||--o{ FORECAST_PREDICTION : scored
  ETL_RUN ||--o{ CLAIM : loads
  ETL_RUN ||--o{ EOB_LINE : loads
  ETL_RUN ||--o{ BANK_DEPOSIT : loads
  SYSTEM_AUDIT_LOG }o--|| CLAIM : may_audit
```

---

## 10) قائمة الجداول الكاملة (36 + 3 finance stubs)

| Schema | Tables |
|--------|--------|
| `ref` | payer_org, insurance_product, insurance_plan, submission_route, facility, cpt_code, icd10_code, document_type, denial_reason_taxonomy, insurance_alias, payer_cpt_rule, icd_denial_rule |
| `etl` | etl_run |
| `gov` | system_audit_log |
| `core` | patient, patient_history, patient_case, patient_coverage, lead_intake, authorization, authorization_visit, visit, clinical_note, visit_service_line |
| `billing` | claim, claim_line, claim_event, denial_record, audit_finding, eob_check, eob_line, eob_carc_raw, bank_deposit, deposit_check_allocation |
| `docs` | document, document_ocr, plan_of_care_detail, denial_letter_detail |
| `ops` | mail_work_item, note_issue, outreach_event, coverage_hold_action, pipeline_run, stage_run, stage_artifact, retry_item, alert_event, daily_snapshot, forecast_accuracy_day |
| `monitoring` | pipeline_metric, quality_metric, quality_rule, job_runtime, system_health, pipeline_event, sla_definition, system_config, backfill_run, backfill_day |
| `analytics` (+015) | feature_definition, feature_snapshot (plus forecast_*) |
| `analytics` | forecast_run, forecast_prediction |
| `finance` | master_budget, cogs_entry, department_scorecard |

**Phase 1 للتنفيذ الفعلي:** كل `ref` + `etl` + `gov` + `core` (بدون lead_intake إن حابب) + `billing` + `docs` + `ops.mail_work_item` + `analytics`.  
**Phase 2:** lead_intake, note_issue, outreach_event, coverage_hold_action.  
**Phase 3:** finance stubs.

---

## 11) Pipeline control (`ops` — migration 014)

```mermaid
erDiagram
  PIPELINE_RUN ||--o{ STAGE_RUN : contains
  PIPELINE_RUN ||--o{ STAGE_ARTIFACT : produces
  PIPELINE_RUN ||--o{ RETRY_ITEM : queues
  PIPELINE_RUN ||--o{ ALERT_EVENT : raises
  PIPELINE_RUN ||--o| DAILY_SNAPSHOT : freezes
  PIPELINE_RUN ||--o| FORECAST_ACCURACY_DAY : scores

  PIPELINE_RUN {
    uuid run_id PK
    date as_of_date
    text status
    text trigger_source
    int lookback_days
    timestamptz started_at
    timestamptz finished_at
    jsonb meta
  }
  STAGE_RUN {
    uuid stage_run_id PK
    uuid run_id FK
    text stage_key
    text status
    int attempt
    text on_failure
    jsonb inputs
    jsonb outputs
  }
  STAGE_ARTIFACT {
    uuid artifact_id PK
    uuid run_id FK
    text artifact_key
    text uri
    bigint row_count
    jsonb payload
  }
  RETRY_ITEM {
    uuid retry_id PK
    text item_type
    text item_key
    text status
    timestamptz next_attempt_at
    jsonb payload
  }
  ALERT_EVENT {
    uuid alert_id PK
    text severity
    text alert_key
    text message
    jsonb payload
  }
  DAILY_SNAPSHOT {
    uuid snapshot_id PK
    date as_of_date UK
    jsonb summary
    jsonb volumes
    jsonb stage_statuses
  }
  FORECAST_ACCURACY_DAY {
    uuid accuracy_id PK
    date as_of_date UK
    numeric mape
    numeric bias
    jsonb per_insurance
  }
```

---

## ملاحظات العرض

- Mermaid `erDiagram` يعرض الأنواع بجانب الأعمدة؛ في Cursor/GitHub اضغط Preview على كل بلوك.
- لو عايز ملف واحد للطباعة لاحقاً عند التنفيذ: `docs/erd.md` بنفس المحتوى + اختياري تصدير PNG عبر mermaid-cli.
- الـ CSV المكررة (`patients_export_*`, `projected_cash_*`, …) **مش جداول** — Views فوق الـ ERD ده.
- Daily orchestration: [`rcm_platform.md`](rcm_platform.md) (`python -m cashflow_ops`).
