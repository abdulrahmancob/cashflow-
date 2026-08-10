-- Indexes on FK columns used by deletes/joins (missing ones made bulk
-- deletes and reconciliation rebuilds pathologically slow).
CREATE INDEX IF NOT EXISTS ix_recon_line_eob_line
    ON billing.reconciliation_line (eob_line_id);
CREATE INDEX IF NOT EXISTS ix_recon_line_visit
    ON billing.reconciliation_line (visit_id);
CREATE INDEX IF NOT EXISTS ix_recon_line_service_line
    ON billing.reconciliation_line (service_line_id);
CREATE INDEX IF NOT EXISTS ix_recon_line_dos
    ON billing.reconciliation_line (date_of_service);
CREATE INDEX IF NOT EXISTS ix_eob_line_claim_line
    ON billing.eob_line (claim_line_id);
CREATE INDEX IF NOT EXISTS ix_eob_line_dos
    ON billing.eob_line (date_of_service);
CREATE INDEX IF NOT EXISTS ix_claim_line_visit
    ON billing.claim_line (visit_id);
CREATE INDEX IF NOT EXISTS ix_claim_line_service_line
    ON billing.claim_line (service_line_id);
CREATE INDEX IF NOT EXISTS ix_claim_event_claim_line
    ON billing.claim_event (claim_line_id);
CREATE INDEX IF NOT EXISTS ix_claim_event_claim
    ON billing.claim_event (claim_id);
CREATE INDEX IF NOT EXISTS ix_denial_record_claim_line
    ON billing.denial_record (claim_line_id);
CREATE INDEX IF NOT EXISTS ix_denial_record_claim
    ON billing.denial_record (claim_id);
CREATE INDEX IF NOT EXISTS ix_forecast_pred_visit
    ON analytics.forecast_prediction (visit_id);
CREATE INDEX IF NOT EXISTS ix_patient_payment_visit
    ON billing.patient_payment (visit_id);
CREATE INDEX IF NOT EXISTS ix_clinical_note_visit
    ON core.clinical_note (visit_id);
CREATE INDEX IF NOT EXISTS ix_service_line_visit
    ON core.visit_service_line (visit_id);
CREATE INDEX IF NOT EXISTS ix_authorization_visit_visit
    ON core.authorization_visit (visit_id);
CREATE INDEX IF NOT EXISTS ix_schedule_appointment_visit
    ON core.schedule_appointment (visit_id);
CREATE INDEX IF NOT EXISTS ix_audit_finding_visit
    ON billing.audit_finding (visit_id);
CREATE INDEX IF NOT EXISTS ix_audit_finding_note
    ON billing.audit_finding (note_id);
CREATE INDEX IF NOT EXISTS ix_note_issue_visit
    ON ops.note_issue (visit_id);
CREATE INDEX IF NOT EXISTS ix_note_issue_note
    ON ops.note_issue (note_id);
CREATE INDEX IF NOT EXISTS ix_outreach_event_visit
    ON ops.outreach_event (visit_id);
