-- Red team: agents log breaks on My day; the leader sees the Red agents' away board.
-- Catalog is auth.role rows. Do not CHECK role_key (migrate() replays every file).
INSERT INTO auth.role (role_key, display_name) VALUES
    ('red_agent', 'Red Agent'),
    ('redteam_leader', 'Red Team Leader')
ON CONFLICT (role_key) DO NOTHING;

-- Red team logins. Roles go only to rows created here, so replays never re-grant a removed role.
WITH seed (username, password_hash, display_name, role_key) AS (
    VALUES
        ('mohamed.samir@ptofthecity.com', 'pbkdf2_sha256$260000$a0T2Nn2pJ+c+NFWi1waWtw==$cPc5nWGqIf7HEXJ1h5L/JmJnW3c2dhHzUlZ8dBZG038=', 'Mohamed Samir', 'red_agent'),
        ('abdallah.abdelkhaleq@ptofthecity.com', 'pbkdf2_sha256$260000$A4W5g/N1SZkMzuCL1KIgow==$lS86xmF+GOf7JVbQf7NDY08YkdRom1HhFvT8afpXH0c=', 'Abdullah Fenon', 'red_agent'),
        ('ahmed.zaky@ptofthecity.com', 'pbkdf2_sha256$260000$tJQBcax8v0qFelmAPnJAaw==$AgPbOjSfHVfympQ2B8VvjcMVaub+0/EtybQd+hSAfYI=', 'Ahmed Zaky', 'red_agent'),
        ('ahmed.abdelgawad@cobsolution.com', 'pbkdf2_sha256$260000$QlSXGkW4v9zMyzflzxIR7g==$pmu/3TdEUvYGUm+3SlrD38MoaBMXB6GuEkWSrzuGHtQ=', 'Ahmed Abdelgawad', 'red_agent'),
        ('omar.elhadidi@cobsolution.com', 'pbkdf2_sha256$260000$ZEuVB6J7hpfR5hCZKJKIMw==$r1oZUgyT1/vpEO+J9IUYLDVI1N4QAb6CcfJgwxySlD4=', 'Omar el hadeedy', 'red_agent'),
        ('mohamed.essam.ali@cobsolution.com', 'pbkdf2_sha256$260000$GU7b7hS2DuTaBaIo4Re5TQ==$/BGsGfV9/SpBhv2jIDUag85S11mtryM5XWNiRYkJs74=', 'Mohamed Essam', 'red_agent'),
        ('amr.nasser@ptofthecity.com', 'pbkdf2_sha256$260000$S4x6uGzg6PRjQlBXkzyl9Q==$tsg6ijhAFW3Im2y/rBo08zgyA65qb8XU/lv3lMnVqtk=', 'Amr Nasser', 'red_agent'),
        ('ahmednasser@ptofthecity.com', 'pbkdf2_sha256$260000$dg494PxReCgO2E6Rsxzf5w==$GdwIWuaLBtOKiv2g7V0ffSwTO70Gq5CFzYnv2VsgTeU=', 'Ahmed Nassar', 'red_agent'),
        ('mohamed.osama.abdelgelel@ptofthecity.com', 'pbkdf2_sha256$260000$WyaF8tRMAedpye4Pc3Hjxw==$j7s1N/4UAXeCN2PBiVmNDNNEmkKeXtG7xEBQlI452fk=', 'Mohamed Osama', 'red_agent'),
        ('mohamed.shalaby@cobsolution.com', 'pbkdf2_sha256$260000$o4FNY5wBTbeAzHCSD75S6w==$0N9lTr/qEzStTb77Mu5egEstTywZ/1/Mfz2EQ8IGg1s=', 'Mohamed Shalaby', 'redteam_leader')
),
new_users AS (
    INSERT INTO auth.app_user (username, email, password_hash, display_name)
    SELECT username, username, password_hash, display_name FROM seed
    ON CONFLICT (username) DO NOTHING
    RETURNING user_id, username
)
INSERT INTO auth.user_role (user_id, role_id)
SELECT n.user_id, r.role_id
FROM new_users n
JOIN seed s ON s.username = n.username
JOIN auth.role r ON r.role_key = s.role_key
ON CONFLICT DO NOTHING;
