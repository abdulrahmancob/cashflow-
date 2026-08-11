-- Raise Acquire SLA: case download + parallel scrapers routinely exceed 20 minutes.
UPDATE monitoring.sla_definition
SET max_seconds = 3600,
    notes = '60 min (case download + parallel scrapers)'
WHERE scope_type = 'stage'
  AND scope_key = 'acquire';
