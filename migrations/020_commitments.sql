-- 020_commitments.sql
-- Standalone work-deadline commitment tracker, migrated from SQLite
-- (commitments, on the shared commitments.get_db hub — the LAST SQLite module)
-- to RDS. Hard cut: the SQLite commitments data is abandoned; artemis/commitments.py
-- is repointed here.
--
-- WHY a NEW acos table, not public.commitments:
--   public.commitments is the CRM API's externally-owned, contact-scoped store
--   (contact_id, deal_id, description, due_date, status='open') — read by
--   crm_query, the scheduler follow-up radar, and `crm status`. This tracker is a
--   different concern: standalone deadlines with effort-based start alerts and a
--   free-text client, status='active'. effort_days / free-text client have no
--   column there, its rows have no contact_id, and the status vocabulary clashes
--   ('active' vs 'open' — they'd be mutually invisible in one table). So this is a
--   separate store, NOT a duplicate of the API's. `crm status` keeps reading
--   public.commitments unchanged.
--
-- Columns mirror the SQLite DDL (incl. closed_at, which the old code ALTERed in
-- at runtime): id AUTOINCREMENT→BIGSERIAL; due_date TEXT→DATE; created_at /
-- closed_at TEXT datetime('now')→TIMESTAMPTZ; status default 'active' preserved.
--
-- Idempotent; needs only the existing acos schema (migration 001).

CREATE TABLE IF NOT EXISTS acos.commitments (
    id           BIGSERIAL PRIMARY KEY,
    title        TEXT NOT NULL,
    due_date     DATE NOT NULL,
    effort_days  INTEGER NOT NULL DEFAULT 1,
    client       TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'active',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    closed_at    TIMESTAMPTZ
);

-- list_commitments / get_due_soon / get_start_alerts filter by status and order
-- by due_date; get_commitments_for_client filters client.
CREATE INDEX IF NOT EXISTS idx_acos_commitments_status_due ON acos.commitments (status, due_date);
CREATE INDEX IF NOT EXISTS idx_acos_commitments_client     ON acos.commitments (client);
