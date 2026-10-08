-- Desk tracker extensions paired to a person. Only a hash of each device token is kept.
-- Idempotent: migrate() re-runs every SQL file.

CREATE TABLE IF NOT EXISTS auth.desk_device (
    device_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES auth.app_user (user_id) ON DELETE CASCADE,
    token_hash text NOT NULL UNIQUE,
    label text,
    user_agent text,
    created_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz,
    revoked_at timestamptz
);

CREATE INDEX IF NOT EXISTS ix_desk_device_user
    ON auth.desk_device (user_id);
