-- 021_email_index.sql
-- acos.email_index — a queryable MIRROR of the Gmail working set (Phase E1).
--
-- WHY THIS TABLE (docs/EMAIL_MODEL.md): Gmail is the source of truth; labels are
-- the state. This table exists for ONE reason — let Artemis see/list/match the
-- ENTIRE inbox in a single fast query instead of paginating the Gmail API 20 at
-- a time (the "Artemis only sees the last 5" blind spot, root-caused to
-- scheduler _MAX_FULL_FETCHES=5 + get_recent_messages(max_results=20)). It is a
-- mirror, NEVER acted through: Artemis decides from the index, acts against
-- Gmail, then corrects the index to match verified Gmail reality.
--
-- WORKING SET ONLY: holds the active queue — in-INBOX (Path 1) + snoozed (will
-- resurface) + proposed-awaiting-confirm. Terminal dispositions (archive/delete/
-- file/spam) DROP from this table — that history lives permanently in Gmail and
-- acos.audit_log, so the index stays small and means "currently active." See the
-- email_index.sync_from_gmail remove-stale rule (it never prunes snoozed/pending).
--
-- NOT inbox_threads: the older acos.inbox_threads state machine (migration 018)
-- is a separate, soon-to-be-retired concern (NEEDS_ACTION/WAITING/... per-thread
-- tracker). This is per-MESSAGE, keyed on Gmail message_id, mirroring Gmail's own
-- label state. The two do not share columns or indexes; 018 is left untouched in
-- this phase.
--
-- CT-ANCHORING (CLAUDE.md): there is NO "today"/due-date logic in this phase.
-- received_at and snooze_until are absolute TIMESTAMPTZ instants — zone-
-- independent, no America/Chicago anchoring needed. When snooze RESURFACING lands
-- (later phase) and compares snooze_until to "now", that comparison is also an
-- absolute instant comparison (now() vs a TIMESTAMPTZ) and stays tz-independent.
-- Only a bare-DATE "due today" comparison would need the CT anchor; none exists
-- here.
--
-- Idempotent; needs only the existing acos schema (migration 001).

CREATE TABLE IF NOT EXISTS acos.email_index (
    message_id      TEXT PRIMARY KEY,           -- Gmail message id (the working-set key)
    thread_id       TEXT,                        -- Gmail thread id
    sender          TEXT,                        -- raw From header ("Name <addr>")
    sender_domain   TEXT,                        -- lowercased domain of the From address
    subject         TEXT,
    snippet         TEXT,                        -- Gmail snippet (no body — bodies stay on-demand)
    received_at     TIMESTAMPTZ,                 -- from Gmail internalDate (absolute instant)
    is_unread       BOOLEAN,                     -- 'UNREAD' present in current_labels
    current_labels  TEXT[],                      -- Gmail labelIds as last observed
    path            SMALLINT,                    -- 1 = Ryan's attention (default), 2 = a playbook claimed it
    pb_match        TEXT,                        -- matched playbook id when path=2, else NULL
    state           TEXT CHECK (state IN ('inbox', 'snoozed', 'pending')),
    snooze_until    TIMESTAMPTZ,                 -- when a snoozed row should resurface (NULL unless snoozed)
    indexed_at      TIMESTAMPTZ DEFAULT now()    -- last time this row was synced from Gmail
);

-- Indexes for the read/match patterns:
--   state         — query_working_set / count_working_set filter (the active queue)
--   received_at   — listing order (ORDER BY received_at DESC)
--   sender_domain — frequency/"emails like this" matching (corpus + future rules)
--   snooze_until  — future snooze-resurfacing scan
CREATE INDEX IF NOT EXISTS idx_email_index_state         ON acos.email_index (state);
CREATE INDEX IF NOT EXISTS idx_email_index_received_at   ON acos.email_index (received_at);
CREATE INDEX IF NOT EXISTS idx_email_index_sender_domain ON acos.email_index (sender_domain);
CREATE INDEX IF NOT EXISTS idx_email_index_snooze_until  ON acos.email_index (snooze_until);
