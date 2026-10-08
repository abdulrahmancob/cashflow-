-- Latest Chrome idle-detection state reported by the portal heartbeat.

ALTER TABLE auth.app_user
    ADD COLUMN IF NOT EXISTS desk_permission text;

ALTER TABLE auth.app_user
    DROP CONSTRAINT IF EXISTS app_user_desk_permission_check;

ALTER TABLE auth.app_user
    ADD CONSTRAINT app_user_desk_permission_check
    CHECK (
        desk_permission IS NULL
        OR desk_permission IN (
            'watching', 'prompt', 'denied', 'unsupported',
            'granted_not_watching', 'error'
        )
    );
