-- 017_grocery_to_acos.sql
-- Move the grocery list off SQLite into Postgres (acos schema), mirroring the
-- existing SQLite `grocery_list` schema exactly so behavior is identical and
-- only the backend changes.
--
-- SQLite source columns: item, category, quantity, store, added_at,
-- purchased_at, is_purchased, notes. is_purchased was INTEGER 0/1 in SQLite;
-- here it is a proper BOOLEAN. Existing rows are copied over by the one-time
-- data-migration script migrations/migrate_grocery_sqlite_to_postgres.py.

CREATE SCHEMA IF NOT EXISTS acos;

CREATE TABLE IF NOT EXISTS acos.grocery_list (
    id           SERIAL PRIMARY KEY,
    item         TEXT NOT NULL,
    category     TEXT,
    quantity     TEXT,
    store        TEXT,
    added_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    purchased_at TIMESTAMPTZ,
    is_purchased BOOLEAN NOT NULL DEFAULT FALSE,
    notes        TEXT
);

-- The hot read is "everything still on the list" (is_purchased = false),
-- ordered by category then item — mirror that access pattern.
CREATE INDEX IF NOT EXISTS idx_acos_grocery_active
    ON acos.grocery_list(is_purchased, category, item);
