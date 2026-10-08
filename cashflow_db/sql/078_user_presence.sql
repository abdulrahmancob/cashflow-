-- Live presence per person, fed by the portal tab and the desk extension.
-- Idempotent: migrate() re-runs every SQL file.

CREATE TABLE IF NOT EXISTS ops.user_presence (
    user_id uuid PRIMARY KEY REFERENCES auth.app_user (user_id) ON DELETE CASCADE,
    tab_state text,
    tab_state_since timestamptz,
    tab_last_at timestamptz,
    tab_page text,
    tab_open jsonb NOT NULL DEFAULT '{}'::jsonb,
    tab_closed_at timestamptz,
    tab_permission_at timestamptz,
    ext_state text,
    ext_state_since timestamptz,
    ext_last_at timestamptz,
    ext_version text,
    updated_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE ops.user_activity_slice
    ADD COLUMN IF NOT EXISTS seconds_unverified int NOT NULL DEFAULT 0
        CHECK (seconds_unverified >= 0);

ALTER TABLE ops.user_away
    ADD COLUMN IF NOT EXISTS auto_closed boolean NOT NULL DEFAULT false;

CREATE TABLE IF NOT EXISTS ops.presence_ping_log (
    ping_id bigserial PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES auth.app_user (user_id) ON DELETE CASCADE,
    at timestamptz NOT NULL DEFAULT now(),
    source text NOT NULL,
    state text,
    tab_id text,
    visible boolean,
    client_at timestamptz,
    booked text
);

CREATE INDEX IF NOT EXISTS ix_presence_ping_log_user_at
    ON ops.presence_ping_log (user_id, at DESC);
