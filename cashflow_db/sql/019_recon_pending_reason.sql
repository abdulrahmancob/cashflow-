-- Diagnostic pending_reason on visit agg (does NOT change visit_status semantics).
ALTER TABLE billing.reconciliation_visit_agg
    ADD COLUMN IF NOT EXISTS pending_reason text;

COMMENT ON COLUMN billing.reconciliation_visit_agg.pending_reason IS
    'Secondary diagnostic when visit_status=pending: zero_pay_rollup | patient_responsibility | secondary_pending | awaiting_payment | mixed | empty when not pending';
