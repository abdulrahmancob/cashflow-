# Register Snowflake RSA public key (admin)

Send **only** the public key to Adham / Snowflake admin. Never send `snowflake_key.p8` or the passphrase.

## Files to send

- `keys/snowflake_key.pub` — full PEM public key, **or**
- `keys/snowflake_key_for_admin.txt` — same key as a single line (no headers), ready for `ALTER USER`

## Admin SQL

Replace `<YOUR_USERNAME>` and paste the single-line body from `snowflake_key_for_admin.txt`:

```sql
ALTER USER <YOUR_USERNAME> SET RSA_PUBLIC_KEY='MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAw8JTjhqFY/eEyXR7PTbeextprAW++l+JOPJiTYO4bZMHGKcbAqShjlqrimRc4ubj6ijISZU94V9vUbNZffudigdsmi3EqUi2gVWi8bMwgcAbCg7tNX37kGHDV8IX0fDfZPoU7WYUIMn9PcnGDa6nP6srC+u5hu1UViq/oOVOL24Nx10rh6u7BxaKCLP0VQfggu+ttpw0HWkSFbMouKs0ad76XYXTpgFf2igX3wSebSDTEWxGcNDo7D+tUVOKoOimvORAfjHhy3iY2Yqf01fcnC2i49XS3pbYp8zzJVK4NyjBO0BRPw/IfsLgKLPZ4/y5YK+UGPWzIxpCYf41PKhrIQIDAQAB';
```

Verify:

```sql
DESC USER <YOUR_USERNAME>;
-- look for RSA_PUBLIC_KEY_FP (fingerprint should be set)
```

## After registration

1. Copy `.env.example` → `.env` and fill `SNOWFLAKE_ACCOUNT`, `SNOWFLAKE_USER`, warehouse/database/schema.
2. Set `SNOWFLAKE_PRIVATE_KEY_PASSPHRASE` from `keys/.passphrase` (or leave blank to auto-read that file).
3. Smoke test:

```bash
cd D:\cashflow\code
pip install -r snowflake_pull/requirements.txt
python -m snowflake_pull --dry-run
python -m snowflake_pull -o snowflake_pull/output/smoke.csv
```

4. Pull real data:

```bash
python -m snowflake_pull --sql "SELECT * FROM MY_DB.MY_SCHEMA.MY_TABLE LIMIT 1000" -o snowflake_pull/output/my_table.csv
# or
python -m snowflake_pull --sql-file path/to/query.sql -o snowflake_pull/output/result.csv
```

## Security

- `keys/` is gitignored (private key, passphrase, admin body).
- Do not commit `.env` or `*.p8`.
