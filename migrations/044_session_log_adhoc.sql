-- 044_session_log_adhoc.sql
-- ADHOC-LOG (2026-09-27): sets logged outside the plan are REAL.
--
-- SESSION-LIB's rule: an on-demand session's sets feed LAST and the effort
-- trend, but do not complete a plan row or count toward scheduled adherence —
-- UNLESS today has an uncompleted row of the same type (either slot), in which
-- case they complete it. The Lambda resolves that; when no such row exists the
-- sets are stored with plan_id NULL and the session type they were launched as.
--
--   adhoc_session_type  the type launched ("strength_a", "core", "yoga", …).
--                       NULL on every plan-attached row. Deliberately NOT a
--                       CHECK list: SESSION-LIB will add types (core, yoga) and
--                       an enum here would make each one an ENUM-EXPAND.
--
-- session_log_has_owner: a new row must belong to a plan row OR say what it was.
-- NOT VALID so the 2 historical rows with plan_id NULL (pre-2026-09-25) are left
-- as they are; the constraint still applies to every row written from now on.
--
-- Readers: every existing reader INNER JOINs health.plan, so ad-hoc rows are
-- invisible to adherence, EVAL-1 counts, the nag and the inferred backstop —
-- which is the rule. /last_logged LEFT JOINs so LAST sees them.

ALTER TABLE health.session_log
    ADD COLUMN IF NOT EXISTS adhoc_session_type TEXT;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint c
        JOIN pg_class t ON t.oid = c.conrelid
        JOIN pg_namespace n ON n.oid = t.relnamespace
        WHERE n.nspname = 'health' AND t.relname = 'session_log'
          AND c.conname = 'session_log_has_owner'
    ) THEN
        ALTER TABLE health.session_log
            ADD CONSTRAINT session_log_has_owner
            CHECK (plan_id IS NOT NULL OR adhoc_session_type IS NOT NULL) NOT VALID;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_session_log_adhoc
    ON health.session_log (logged_at DESC)
    WHERE plan_id IS NULL;
