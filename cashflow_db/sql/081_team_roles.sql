-- PIU, Client Success and Product Owner teams. PIU views the work pages read-only;
-- the other two see My day only. Catalog is auth.role rows (migrate() replays every file).
DO $$
DECLARE
  rec record;
BEGIN
  FOR rec IN
    SELECT conname
    FROM pg_constraint
    WHERE conrelid = 'auth.role'::regclass
      AND contype = 'c'
      AND pg_get_constraintdef(oid) ILIKE '%role_key%'
  LOOP
    EXECUTE format('ALTER TABLE auth.role DROP CONSTRAINT %I', rec.conname);
  END LOOP;
END $$;

INSERT INTO auth.role (role_key, display_name) VALUES
    ('piu', 'PIU'),
    ('client_success', 'Client Success'),
    ('product_owner', 'Product Owner')
ON CONFLICT (role_key) DO NOTHING;

-- Team logins. Roles go only to rows created here, so replays never re-grant a removed role.
WITH seed (username, password_hash, display_name, role_key) AS (
    VALUES
        ('donia@cobsolution.com', 'pbkdf2_sha256$260000$QQzijRbTI7FuXz7eVYTotw==$tqqrdeeDkhj+pMTmGGzh0gaVD/+gvtbgAAJkGtP9e+o=', 'Donia', 'piu'),
        ('finance7@cobsolution.com', 'pbkdf2_sha256$260000$6OamgYjPN5cejrjxUKLTMA==$GPSN8Fyc+nYRCqQxbFvrL5dUKnxYfWAj0lNB1+ojfag=', 'Abdelrahman Nabi', 'piu'),
        ('basma@cobsolution.com', 'pbkdf2_sha256$260000$0G5dh/Ne/DkwPcOO7z5cqQ==$dGrWAjwP35aZ9k3ceggAjaHk937bd8iZfoF+UXE5U30=', 'Basma Aref', 'piu'),
        ('ahmed.emad@cobsolution.com', 'pbkdf2_sha256$260000$F0DVpD8X0xyA2+uueHCGIg==$rHX2GgOmLQrsY0EznoKZeE/d3MOokfuXS0InjZ4+kM0=', 'Ahmed Emad', 'client_success'),
        ('nour.galal@cobsolution.com', 'pbkdf2_sha256$260000$iiybk3KMreypYeY1pDOUOQ==$GU3ahQc1vYLYnVH7uYfSGw04L+CYf8DUAJx2qTbqOZc=', 'Nour Galal', 'client_success'),
        ('amina.sayed@cobsolution.com', 'pbkdf2_sha256$260000$OpNb4Mwz8zqgTajcIH8+iA==$DTJL7cYuoFj7MO6ofG/wPhLbEPJw67VKGpBZc3eCueE=', 'Amina Sayed', 'client_success'),
        ('mohamed.galal@cobsolution.com', 'pbkdf2_sha256$260000$KoPTuMu3eoqPD4uxLKG1qA==$g5L2x4850ChZq+BpVO2SLFpVZ9CUN/NmVhaVYzC81xc=', 'Mohamed Galal', 'product_owner'),
        ('reem.mohamed@cobsolution.com', 'pbkdf2_sha256$260000$GLmUI9P5kVmA52/Rhfcpig==$+54cFSsU62esd8OXQm9AAWgXMpAdbapAG2b5GieXwbQ=', 'Reem Mohamed', 'product_owner'),
        ('sara.omar@cobsolution.com', 'pbkdf2_sha256$260000$EG3HvKrZ/uvsCJmU1cp+jQ==$INF9OXhCBJ6tGBtnAn/jxLuB2jyYv1qP1TPe4C1gZ+0=', 'Sara Omar', 'product_owner')
),
new_users AS (
    INSERT INTO auth.app_user (username, email, password_hash, display_name)
    SELECT s.username, s.username, s.password_hash, s.display_name
    FROM seed s
    WHERE NOT EXISTS (
        SELECT 1 FROM auth.app_user u WHERE lower(u.username) = lower(s.username)
    )
    ON CONFLICT (username) DO NOTHING
    RETURNING user_id, username
)
INSERT INTO auth.user_role (user_id, role_id)
SELECT n.user_id, r.role_id
FROM new_users n
JOIN seed s ON s.username = n.username
JOIN auth.role r ON r.role_key = s.role_key
ON CONFLICT DO NOTHING;
