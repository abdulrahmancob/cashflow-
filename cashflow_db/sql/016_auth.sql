-- Application auth (portal users / roles)
CREATE SCHEMA IF NOT EXISTS auth;

CREATE TABLE IF NOT EXISTS auth.role (
    role_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    role_key text NOT NULL UNIQUE
        CHECK (role_key IN ('super_admin', 'finance', 'posting_team')),
    display_name text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS auth.app_user (
    user_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    username text NOT NULL UNIQUE,
    email text,
    password_hash text NOT NULL,
    display_name text NOT NULL,
    is_active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    last_login_at timestamptz
);

CREATE TABLE IF NOT EXISTS auth.user_role (
    user_id uuid NOT NULL REFERENCES auth.app_user (user_id) ON DELETE CASCADE,
    role_id uuid NOT NULL REFERENCES auth.role (role_id) ON DELETE CASCADE,
    PRIMARY KEY (user_id, role_id)
);

CREATE INDEX IF NOT EXISTS ix_app_user_active ON auth.app_user (is_active);
CREATE INDEX IF NOT EXISTS ix_user_role_role ON auth.user_role (role_id);

INSERT INTO auth.role (role_key, display_name) VALUES
    ('super_admin', 'Super Admin'),
    ('finance', 'Finance'),
    ('posting_team', 'Posting Team')
ON CONFLICT (role_key) DO NOTHING;
