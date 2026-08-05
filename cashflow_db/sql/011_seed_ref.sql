-- Seed reference enums / business-doc alignment
INSERT INTO ref.submission_route (code, name, description)
VALUES
    ('waystar', 'Waystar', 'Primary automated clearinghouse (~90% of claims)'),
    ('zaya', 'Zaya', 'High-yield manual portal; requires force-reject from Waystar'),
    ('manual', 'Manual / Specialty', 'Medicaid, Workers Comp, No-Fault document-based path')
ON CONFLICT (code) DO UPDATE
SET name = EXCLUDED.name, description = EXCLUDED.description;

INSERT INTO ref.document_type (code, name)
VALUES
    ('referral', 'Referral'),
    ('poc', 'Plan of Care'),
    ('prescription', 'Prescription'),
    ('denial', 'Denial Letter'),
    ('appeal', 'Appeal'),
    ('mri', 'MRI'),
    ('lab', 'Lab'),
    ('daily_note', 'Daily Note / Chart Note'),
    ('remittance', 'Remittance Advice / EOB Scan'),
    ('other', 'Other')
ON CONFLICT (code) DO UPDATE SET name = EXCLUDED.name;

INSERT INTO ref.denial_reason_taxonomy (code, category, default_error_owner, description)
VALUES
    ('wrong_case', 'Front Desk', 'front_desk', 'Visit scheduled under wrong WebPT case / body part'),
    ('bad_demographics', 'Front Desk', 'front_desk', 'Incorrect patient name, DOB, or member ID'),
    ('expired_auth', 'Authorization', 'authorization', 'Service date after authorization end_date'),
    ('missing_auth', 'Authorization', 'authorization', 'Hard auth required but missing'),
    ('dummy_auth_misread', 'Authorization', 'authorization', 'Dummy auth_number 0 misinterpreted as zero visits'),
    ('missing_referral', 'Authorization', 'authorization', 'Direct Access limit exceeded without physician referral'),
    ('cpt_mismatch', 'Coding', 'coding', 'CPT / modifier not allowed for payer'),
    ('icd_conflict', 'Medical Audit', 'medical_audit', 'ICD redundancy or non-billable/header code'),
    ('unsigned_note', 'Medical Audit', 'medical_audit', 'Daily note not finalized within 24h SLA'),
    ('medical_necessity', 'Medical Audit', 'medical_audit', 'Goal-met trap / functional scale disconnect'),
    ('partial_denial', 'Payer Behavior', 'payer_behavior', 'Partial visit authorization (e.g. HealthFirst / Fidelis)'),
    ('payer_delay', 'Payer Behavior', 'payer_behavior', 'Clean claim delayed or denied without cause'),
    ('force_reject_drop', 'Coding', 'coding', 'Force-rejected in Waystar but never submitted to Zaya'),
    ('self_pay_mask', 'Front Desk', 'front_desk', 'Insurance claim converted to Self-Pay to hide miss')
ON CONFLICT (code) DO UPDATE
SET category = EXCLUDED.category,
    default_error_owner = EXCLUDED.default_error_owner,
    description = EXCLUDED.description;
