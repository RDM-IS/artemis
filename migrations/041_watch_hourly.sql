-- 041_watch_hourly.sql
-- STEPS-HOURLY (2026-09-21): step_count and apple_exercise_time, stored as
-- HOURLY totals. Both were on the ingest's ignore list (counted and named,
-- never stored). The dietitian report's activity charts need them.
--
-- One row per (metric, local hour). `hour_start` is the instant the hour opens
-- in the ACTIVE timezone at ingest (so a half-hour-offset zone still gets real
-- local hours); `local_date` is that hour's local date, fixed at ingest like
-- health.watch_sample.
--
-- NEVER ADDITIVE. Pushes overlap and resend minutes, so adding an incoming
-- total to the stored one would double-count. The ENERGY-HOURLY design said a
-- push "replaces" the hour; a whole-hour replace is still wrong when a push
-- covers only part of the hour (it would shrink the total). So the row keeps
-- the hour's per-minute values in `minutes`, keyed "<minute ISO>|<device>",
-- and an ingest MERGES by key: a resent minute overwrites itself, a new minute
-- is added, nothing is summed twice. `value` is always recomputed from
-- `minutes`. At most 60 keys per device per hour.
--
-- Two devices (watch and iPhone) can both report steps for the same minute.
-- Health Auto Export's aggregated samples normally arrive as one sample per
-- minute tagged with the contributing device(s) ("RAW", "RIP", "RAW|RIP"). If
-- two SEPARATE samples ever arrive for one minute, `value` counts both, and
-- `overlap_minutes` says how many minutes that happened in. That is what the
-- one-day comparison against the Health app checks before any report uses
-- steps. The code does not pick a device on a guess.
--
-- Additive: a new table, no changes to existing rows.

CREATE TABLE IF NOT EXISTS health.watch_hourly (
    metric           TEXT NOT NULL,
    hour_start       TIMESTAMPTZ NOT NULL,
    local_date       DATE NOT NULL,
    value            NUMERIC NOT NULL,
    unit             TEXT,
    sample_count     INT NOT NULL,
    overlap_minutes  INT NOT NULL DEFAULT 0,
    first_at         TIMESTAMPTZ NOT NULL,
    last_at          TIMESTAMPTZ NOT NULL,
    devices          TEXT[] NOT NULL DEFAULT '{}',
    minutes          JSONB NOT NULL,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (metric, hour_start),
    CONSTRAINT watch_hourly_metric_known
        CHECK (metric IN ('step_count', 'apple_exercise_time'))
);

CREATE INDEX IF NOT EXISTS idx_watch_hourly_metric_day
    ON health.watch_hourly (metric, local_date);
