-- 027_org_profile.sql
-- PB-010d: Org Profiles.
--
-- Organizations get profiles parallel to person dossiers. Same wall as
-- everything else, split by authorship:
--   * Authored sections (overview / active_work / opportunities / display_name)
--     — Ryan writes them, propose-then-confirm. Artemis never edits these.
--   * Org notes — append-only, drafted by extraction WITH evidence + meeting
--     provenance, approved by Ryan. Facts with provenance, not prose merges.
--
-- Person-org links ride the existing rails: "led by X" is prose in active_work;
-- if X has a dossier + assignment the roster render already shows them.
--
-- Idempotent; needs acos.dossier_meeting (024). org keys match org_assignment.org.

CREATE TABLE IF NOT EXISTS acos.org_profile (
    org          TEXT PRIMARY KEY,            -- matches org_assignment.org values
    display_name TEXT,                        -- 'Farm Credit Administration — ODAE'
    overview     TEXT,                        -- Ryan-authored
    active_work  TEXT,                        -- Ryan-authored
    opportunities TEXT,                       -- Ryan-authored
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS acos.org_note (
    note_id     SERIAL PRIMARY KEY,
    org         TEXT NOT NULL REFERENCES acos.org_profile,   -- profile must exist first
    note_text   TEXT NOT NULL,
    meeting_id  INT REFERENCES acos.dossier_meeting,         -- provenance
    status      TEXT NOT NULL CHECK (status IN ('draft', 'approved')) DEFAULT 'draft',
    approved_at TIMESTAMPTZ,
    note_date   DATE NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_org_note_org_status ON acos.org_note (org, status);

-- Seed the fca-odae profile row (sections null — Ryan fills via `org set`). The
-- org_note FK requires a profile row before notes attach; extraction
-- auto-creates a bare profile (org key + display_name=org) for an unknown org.
INSERT INTO acos.org_profile (org, display_name)
    VALUES ('fca-odae', 'FCA — Office of Data Analytics & Economics')
    ON CONFLICT (org) DO NOTHING;
