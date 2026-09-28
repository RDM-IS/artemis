-- 045_cognition_bronze.sql
-- COGNITION-1 slice 1: bronze — the decision ledger's three columns.
--
-- WHY HERE AND NOT IN A NEW TABLE (docs/ARTEMIS_STATE.md §6 COGNITION-1): §2's
-- sketch called for acos.cognition_log. acos.audit_log already holds the rows,
-- already carries confidence / outcome / metadata / verified, and its writers
-- already sit at four of the five decision sites. A second table would mean two
-- rows per decision and two writers per site, which is the two-store seam CRM-2
-- and COMMIT-1 both record as a mistake. One system of record.
--
-- The difference between an audit row and a decision row is not the columns: an
-- audit row means "this action was taken" and is never reopened, while a
-- decision row is REVISITED when its outcome is known. These columns are what
-- make that lifecycle expressible.
--
-- Additive only — every existing column, row and writer is untouched, and every
-- INSERT in the codebase names its columns explicitly (13 sites, verified
-- 2026-09-28; there are no SELECTs from audit_log at all). Migration 022 added
-- eleven columns to this table the same way.
--
-- Outcomes are APPENDED as a second row referencing the first
-- (metadata.decides = the original row's id), never written by UPDATE (Ryan,
-- 2026-09-28) — so nothing here needs to be mutable.

ALTER TABLE acos.audit_log
    ADD COLUMN IF NOT EXISTS assumptions jsonb,   -- what the decision believed at the time
    ADD COLUMN IF NOT EXISTS correction  jsonb,   -- what Ryan did instead
    ADD COLUMN IF NOT EXISTS manual_gap  boolean; -- set by RULE only, never by an LLM

-- Only decisions carry assumptions, so the index covers only those. This is
-- what keeps the decision ledger cheap to read while it shares a table with
-- 1,568 rows that are not decisions.
CREATE INDEX IF NOT EXISTS idx_audit_log_decisions
    ON acos.audit_log (domain, action, created_at DESC)
    WHERE assumptions IS NOT NULL;

-- The gap list: "Ryan did this by hand and Artemis had no path to do it."
CREATE INDEX IF NOT EXISTS idx_audit_log_manual_gap
    ON acos.audit_log (created_at DESC) WHERE manual_gap;
