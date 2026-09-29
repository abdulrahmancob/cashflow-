-- Paid PR-3 visits leave Denied for their own Collection tab.

ALTER TABLE analytics.collection_queue_member
    DROP CONSTRAINT IF EXISTS collection_queue_member_bucket_check;

ALTER TABLE analytics.collection_queue_member
    ADD CONSTRAINT collection_queue_member_bucket_check
    CHECK (bucket IN (
        'denied', 'overdue', 'collection', 'arbitration', 'action', 'at_risk',
        'paid_patient_responsibility'
    ));
