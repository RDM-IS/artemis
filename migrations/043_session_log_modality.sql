-- 043_session_log_modality.sql
-- CARDIO-LOC (2026-09-25): what a cardio session was actually done ON.
--
-- Until now a cardio log's modality was free text in `exercise` — the session's
-- display name, or "Run 1" from the debrief parser. Rowing could only be told
-- from a substitute by matching a string, which is the same failure shape as
-- the exercise-name rules LOCATION-1 deleted.
--
-- It matters because rowing carries the progression: a bike or treadmill
-- session runs the same target but must NOT advance the rowing baseline, and
-- that baseline is a query — `WHERE modality = 'row'` — not a second store.
--
--   modality  row | bike | treadmill | elliptical   (one progression each)
--   device    the variant: water, indoor trainer, upright, recumbent, …
--             Recumbent is a DEVICE, not a modality (Ryan, 2026-09-25): one
--             progression per modality, the device captured so the heart-rate
--             data can revisit that once there are a few weeks of both.
--
-- ENUM-EXPAND: a fifth modality is a breaking change for every consumer that
-- reads this column. They are named in the CARDIO-LOC entry of
-- docs/ARTEMIS_STATE.md; add the value there in the same change.
--
-- Additive and nullable: every existing row keeps NULL, which reads as
-- "unrecorded" rather than as a guess about which machine it was.

ALTER TABLE health.session_log
    ADD COLUMN IF NOT EXISTS modality TEXT,
    ADD COLUMN IF NOT EXISTS device   TEXT;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint c
        JOIN pg_class t ON t.oid = c.conrelid
        JOIN pg_namespace n ON n.oid = t.relnamespace
        WHERE n.nspname = 'health' AND t.relname = 'session_log'
          AND c.conname = 'session_log_modality_known'
    ) THEN
        ALTER TABLE health.session_log
            ADD CONSTRAINT session_log_modality_known
            CHECK (modality IS NULL
                   OR modality IN ('row', 'bike', 'treadmill', 'elliptical'));
    END IF;
END $$;

-- Only cardio rows carry one, so the index covers only those.
CREATE INDEX IF NOT EXISTS idx_session_log_modality
    ON health.session_log (modality, logged_at DESC)
    WHERE modality IS NOT NULL;
