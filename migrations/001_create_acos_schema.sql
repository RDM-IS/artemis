-- 001: Create the acos schema
-- Does NOT touch crm schema.

CREATE SCHEMA IF NOT EXISTS acos;

-- Migration tracking table lives in the acos schema
CREATE TABLE IF NOT EXISTS acos.schema_migrations (
    migration_name VARCHAR(255) PRIMARY KEY,
    applied_at     TIMESTAMPTZ DEFAULT now()
);
