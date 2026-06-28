-- 018_inbox_threads.sql
-- Inbox-zero thread state machine — migrated from SQLite (inbox_threads) to RDS.
-- Hard cut: the SQLite inbox data is abandoned; artemis/inbox.py is repointed to
-- this table. Columns mirror the SQLite DDL one-for-one (TEXT→TEXT, SQLite
-- DATE→DATE, SQLite TIMESTAMP→TIMESTAMPTZ).
--
-- STATE: exactly the five values inbox.py writes (inbox.VALID_STATES /
-- set_state) — NEEDS_ACTION, WAITING, SNOOZED, DONE, NOISE. The CHECK enumerates
-- those and nothing more; default matches the SQLite default ('NEEDS_ACTION').
--
-- CT-ANCHORING: snoozed_until / waiting_since / due_date are DATEs compared
-- against "today" by the query functions. This box runs UTC, which is a day
-- AHEAD of America/Chicago after ~19:00 CT — the confirmed UTC-vs-CT split. So
-- inbox.py anchors every "today" to (now() AT TIME ZONE 'America/Chicago')::date,
-- NOT bare current_date. The timestamp DEFAULTs here are NOW() (TIMESTAMPTZ),
-- which are absolute instants and need no zone anchoring; the CT logic lives in
-- the queries (get_due_today / get_snoozed_due / get_stale_waiting) and in the
-- date values inbox.py stores (mark_waiting / mark_snoozed), not in defaults.

CREATE TABLE IF NOT EXISTS acos.inbox_threads (
    id                  TEXT PRIMARY KEY,
    subject             TEXT,
    sender              TEXT,
    sender_domain       TEXT,
    state               TEXT NOT NULL DEFAULT 'NEEDS_ACTION'
                            CHECK (state IN ('NEEDS_ACTION', 'WAITING', 'SNOOZED', 'DONE', 'NOISE')),
    snoozed_until       DATE,
    waiting_on          TEXT,
    waiting_since       DATE,
    due_date            DATE,
    client              TEXT,
    notes               TEXT,
    first_seen_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_nudged_at      TIMESTAMPTZ,
    mattermost_post_id  TEXT
);

-- Indexes for the query functions' filters:
--   state         — list_by_state, get_stale_*, get_due_today, get_snoozed_due
--   last_updated  — get_stale_needs_action (and the list_by_state ORDER BY)
--   waiting_since — get_stale_waiting
--   snoozed_until — get_snoozed_due
--   due_date      — get_due_today
--   sender_domain — not filtered today; indexed per spec for future triage reads
CREATE INDEX IF NOT EXISTS idx_inbox_threads_state         ON acos.inbox_threads (state);
CREATE INDEX IF NOT EXISTS idx_inbox_threads_last_updated  ON acos.inbox_threads (last_updated_at);
CREATE INDEX IF NOT EXISTS idx_inbox_threads_waiting_since ON acos.inbox_threads (waiting_since);
CREATE INDEX IF NOT EXISTS idx_inbox_threads_snoozed_until ON acos.inbox_threads (snoozed_until);
CREATE INDEX IF NOT EXISTS idx_inbox_threads_due_date      ON acos.inbox_threads (due_date);
CREATE INDEX IF NOT EXISTS idx_inbox_threads_sender_domain ON acos.inbox_threads (sender_domain);
