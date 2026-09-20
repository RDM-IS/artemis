-- 037_watch_heart_rate.sql  — PROPOSED, NOT YET APPROVED
-- WATCH-2: a compact home for minute-level heart rate.
--
-- Why it is NOT in health.watch_sample: HR arrives ~1,100 samples/day, about a
-- third of everything the phone sends. In watch_sample each row carries its
-- raw JSON (~92 B) plus metric text and a unit, so three months of HR would be
-- ~100k rows dominating a table whose other metrics total a few hundred rows a
-- week. Here a row is a timestamp and three smallints.
--
-- Why keep it at all: it is the only way to see TIME IN ZONE. The Z2 sessions
-- have a target zone, and the workout record alone gives one average — it
-- cannot show whether the session was actually spent in zone 2 or drifted.
-- avg/min/max per minute is enough for that, and enough for a session HR
-- curve, without storing a raw sample per beat.
--
-- Idempotent: CREATE IF NOT EXISTS only. No row changes.

CREATE TABLE IF NOT EXISTS health.watch_heart_rate (
    measured_at  TIMESTAMPTZ PRIMARY KEY,   -- the natural key; a re-send is a no-op
    local_date   DATE NOT NULL,
    bpm          SMALLINT NOT NULL,         -- the sample's Avg
    bpm_min      SMALLINT,
    bpm_max      SMALLINT,
    device       TEXT                       -- the payload's own "source" ("RAW", "RAW|RIP")
);

CREATE INDEX IF NOT EXISTS watch_heart_rate_day_idx
    ON health.watch_heart_rate (local_date);

COMMENT ON TABLE health.watch_heart_rate IS
    'WATCH-2: minute-level heart rate, kept apart from health.watch_sample '
    'because of its volume (~1,100 rows/day). Supports time-in-zone for the '
    'Z2 sessions and a per-session HR curve. No raw JSON: the payload shape '
    'for this metric is simple and fixed (Avg/Min/Max + date + source).';
