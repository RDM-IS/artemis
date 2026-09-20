-- 038_watch_device.sql  — PROPOSED, NOT APPLIED
-- WATCH-1: store the DECODED device alongside the payload's own string.
--
-- Health Auto Export puts its own marker in every sample's "source" field.
-- Decoded (Ryan, 2026-09-20):
--     RAW      -> watch          (Ryan's Apple Watch)
--     RIP      -> iphone         (Ryan's iPhone)
--     RAW|RIP  -> watch+iphone   (both)
--
-- Both are kept: `device_raw` is what arrived, `device` is what it means. An
-- unrecognised marker stores the raw string with a NULL decode, so a new
-- device shows up as data rather than being guessed at.
--
-- Where a metric can come from either device the WATCH is preferred for
-- resting HR, HRV and sleep; weight comes from the scale via either and needs
-- no preference (knowledge/watch_payload.prefers_watch).
--
-- Idempotent: guarded ADD COLUMN. No row changes. Existing rows keep NULL
-- (their source string is still inside health.watch_sample.raw).

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_schema = 'health' AND table_name = 'watch_sample'
                     AND column_name = 'device') THEN
        ALTER TABLE health.watch_sample
            ADD COLUMN device      TEXT,
            ADD COLUMN device_raw  TEXT;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_schema = 'health' AND table_name = 'watch_heart_rate'
                     AND column_name = 'device_raw') THEN
        -- 037 shipped a single `device` column holding the RAW string; keep the
        -- raw value under its proper name and let `device` hold the decode.
        ALTER TABLE health.watch_heart_rate RENAME COLUMN device TO device_raw;
        ALTER TABLE health.watch_heart_rate ADD COLUMN device TEXT;
    END IF;
END $$;

COMMENT ON COLUMN health.watch_sample.device IS
    'WATCH-1: decoded source — watch | iphone | watch+iphone. NULL when the '
    'payload marker was not recognised; device_raw always keeps what arrived.';
