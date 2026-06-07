-- 014_calendar_audit.sql
-- Calendar write audit trail — restores guard #3 of the Brad Spaits incident
-- (2026-03-18), lost in the AWS migration. Every calendar WRITE (create /
-- update / delete) records a queryable row here, AFTER the write succeeds.
-- Blocked duplicates write nothing; a dup_override create records dup_override=true.

CREATE TABLE IF NOT EXISTS acos.calendar_audit (
    id            BIGSERIAL PRIMARY KEY,
    action        TEXT NOT NULL,                       -- create | update | delete
    event_id      TEXT,
    title         TEXT,
    start_ts      TIMESTAMPTZ,
    attendees     JSONB NOT NULL DEFAULT '[]'::jsonb,  -- list of attendee emails
    has_external  BOOLEAN NOT NULL DEFAULT false,      -- any non-rdm.is attendee
    approved_by   TEXT,                                -- who approved (external sends / overrides)
    dup_override  BOOLEAN NOT NULL DEFAULT false,
    actor         TEXT NOT NULL DEFAULT 'artemis',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_calendar_audit_event   ON acos.calendar_audit (event_id);
CREATE INDEX IF NOT EXISTS idx_calendar_audit_created ON acos.calendar_audit (created_at DESC);
