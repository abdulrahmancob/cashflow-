-- Documents + OCR details
CREATE TABLE IF NOT EXISTS docs.document (
    document_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    patient_id uuid REFERENCES core.patient (patient_id),
    case_pk uuid REFERENCES core.patient_case (case_pk),
    document_type_id uuid REFERENCES ref.document_type (document_type_id),
    ext_doc_id text,
    filename text,
    storage_path text,
    source text
        CHECK (source IS NULL OR source IN (
            'edoc', 'chart_note', 'upload', 'mail', 'drive'
        )),
    status text,
    status_description text,
    error text,
    source_system text,
    source_natural_key text,
    etl_run_id uuid REFERENCES etl.etl_run (etl_run_id),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS docs.document_ocr (
    document_ocr_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id uuid NOT NULL UNIQUE REFERENCES docs.document (document_id),
    extraction_method text,
    text_chars int,
    icd_codes text,
    raw_text_ref text,
    ocr_name text,
    name_match boolean,
    ocr_patient_id text,
    id_match boolean,
    ocr_diagnosis text,
    diagnosis_match boolean,
    etl_run_id uuid REFERENCES etl.etl_run (etl_run_id)
);

CREATE TABLE IF NOT EXISTS docs.plan_of_care_detail (
    poc_detail_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id uuid NOT NULL REFERENCES docs.document (document_id),
    poc_id text,
    date_of_plan_of_care date,
    frequency text,
    duration text,
    plan_text text
);

CREATE TABLE IF NOT EXISTS docs.denial_letter_detail (
    denial_letter_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id uuid NOT NULL REFERENCES docs.document (document_id),
    denial_date date,
    insurance_name text,
    payer_guess text,
    reason_raw text,
    reason_class text
);

CREATE INDEX IF NOT EXISTS ix_document_patient ON docs.document (patient_id);
CREATE INDEX IF NOT EXISTS ix_document_ext ON docs.document (ext_doc_id);
