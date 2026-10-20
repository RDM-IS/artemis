-- 056_session_log_row_dnf.sql
-- ROW-DNF (Ryan, 2026-10-20): "if I can't do all sets at their target, we
-- shouldn't advance." At 24 spm he held 60-75 s of a 4-minute round and needed
-- a break, and nothing recorded that the round was not finished.
--
--   target_spm  the stroke rate the round was PRESCRIBED
--   target_sec  the work length it was prescribed
--   dnf         TRUE = he did not finish this round at its target
--
-- Progression now starts from the last session's TARGETS, and advances only
-- when every round he is repeating was finished. stroke_rate (held) and
-- duration_sec (actually rowed) stay what happened.
--
-- Nullable and additive. Rows written before this keep NULL, read as
-- "target = what was held, finished" -- the rule that applied when they were
-- written.
--
-- Consumers grepped: api/app/routers/health.py post_log, BOTH insert bind
-- dicts, in the same change (the 043 incident). Nothing else names these.

ALTER TABLE health.session_log
    ADD COLUMN IF NOT EXISTS target_spm SMALLINT,
    ADD COLUMN IF NOT EXISTS target_sec INTEGER,
    ADD COLUMN IF NOT EXISTS dnf        BOOLEAN;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint c
        JOIN pg_class t ON t.oid = c.conrelid
        JOIN pg_namespace n ON n.oid = t.relnamespace
        WHERE n.nspname = 'health' AND t.relname = 'session_log'
          AND c.conname = 'session_log_target_spm_range'
    ) THEN
        ALTER TABLE health.session_log
            ADD CONSTRAINT session_log_target_spm_range
            CHECK (target_spm IS NULL OR target_spm BETWEEN 10 AND 60);
    END IF;
END $$;
