-- Login attempts, for lockout after repeated failures. Kept for 7 days.
-- Sign-in keeps working without this table; the lockout starts once it exists.
-- Idempotent: migrate() re-runs every SQL file.

CREATE TABLE IF NOT EXISTS auth.login_attempt (
    attempt_id bigserial PRIMARY KEY,
    username text NOT NULL,
    ip text,
    at timestamptz NOT NULL DEFAULT now(),
    ok boolean NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_login_attempt_username_at
    ON auth.login_attempt (username, at DESC);

CREATE INDEX IF NOT EXISTS ix_login_attempt_ip_at
    ON auth.login_attempt (ip, at DESC);
