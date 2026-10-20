-- 057_session_log_target_bpm.sql
-- QUICK-START (Ryan, 2026-10-20): Ride (Richfield) and Run (Brown Deer) are the
-- same ladder as the row, climbing a heart-rate target instead of a stroke
-- rate. The round's target goes here; what he held goes in the existing
-- hr_avg. Nullable, additive. Both /log insert bind dicts name it in the same
-- change (the 043 incident).

ALTER TABLE health.session_log
    ADD COLUMN IF NOT EXISTS target_bpm SMALLINT;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint c
        JOIN pg_class t ON t.oid = c.conrelid
        JOIN pg_namespace n ON n.oid = t.relnamespace
        WHERE n.nspname = 'health' AND t.relname = 'session_log'
          AND c.conname = 'session_log_target_bpm_range'
    ) THEN
        ALTER TABLE health.session_log
            ADD CONSTRAINT session_log_target_bpm_range
            CHECK (target_bpm IS NULL OR target_bpm BETWEEN 60 AND 220);
    END IF;
END $$;
