-- Deduplicate ops.mail_work_item and enforce natural-key uniqueness so
-- load_mail re-runs upsert instead of piling duplicate rows.
DELETE FROM ops.mail_work_item a
USING ops.mail_work_item b
WHERE a.source_system = b.source_system
  AND a.source_natural_key = b.source_natural_key
  AND a.source_natural_key IS NOT NULL
  AND a.work_item_id > b.work_item_id;

CREATE UNIQUE INDEX IF NOT EXISTS uq_mail_work_item_natural
    ON ops.mail_work_item (source_system, source_natural_key)
    WHERE source_natural_key IS NOT NULL;
