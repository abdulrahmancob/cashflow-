-- Example pull query. Replace with the tables Adham grants you access to.
-- Usage:
--   python -m snowflake_pull --sql-file snowflake_pull/queries/example.sql -o snowflake_pull/output/example.csv

SELECT
    CURRENT_TIMESTAMP() AS pulled_at,
    CURRENT_USER() AS snowflake_user,
    CURRENT_ACCOUNT() AS snowflake_account,
    CURRENT_ROLE() AS snowflake_role,
    CURRENT_WAREHOUSE() AS snowflake_warehouse,
    CURRENT_DATABASE() AS snowflake_database,
    CURRENT_SCHEMA() AS snowflake_schema
;
