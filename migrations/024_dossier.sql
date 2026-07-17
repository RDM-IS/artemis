-- 024_dossier.sql
-- PB-010: Meeting Intelligence / Colleague Dossiers.
--
-- Portable meeting-intelligence store. One dossier per person, five sections:
--   1. Position & terrain      (Ryan-authored → dossier.position_terrain)
--   2. What they need from me   (Ryan-authored → dossier.needs_from_me)
--   3. Interaction log          (append-only, draft→bless → dossier_entry)
--   4. Open loops               (undated watch-items dossier_loop + dated
--                                commitments carrying a dossier_id)
--   5. Idea bank + cross-poll   (provenance-tracked → dossier_idea)
--
-- Lifecycle: raw capture (autonomous, immutable) → draft extraction (autonomous,
-- status='draft'/'proposed') → bless (Ryan) → surfaces (pre-brief, to-do queries).
-- The statistics/semantics wall: Artemis extracts/drafts; nothing becomes the
-- record until Ryan blesses. Confirmations always render from written rows.
--
-- Org-agnostic on purpose (FCA is the first tenant; post-fed: clients).
-- Idempotent; needs only the existing acos schema (migration 001).
--
-- NOTE ON MIGRATION NUMBER: the build spec called this "022", but 022 and 023
-- already exist in the tree (audit-log corpus; playbook rules). Live repo wins on
-- facts (CLAUDE.md context precedence) → this is 024.

CREATE TABLE IF NOT EXISTS acos.dossier (
    dossier_id       SERIAL PRIMARY KEY,
    slug             TEXT UNIQUE NOT NULL,
    full_name        TEXT NOT NULL,
    org              TEXT NOT NULL DEFAULT 'fca-odae',
    position_terrain TEXT,
    needs_from_me    TEXT,
    person_id        INT,                        -- soft link to public.persons; NO FK by design
    active           BOOLEAN NOT NULL DEFAULT TRUE,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS acos.dossier_meeting (
    meeting_id      SERIAL PRIMARY KEY,
    occurred_on     DATE NOT NULL,               -- CT-anchored when derived from "today"
    topic           TEXT,
    raw_notes       TEXT NOT NULL,               -- verbatim, immutable, never LLM-touched
    source_filename TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS acos.dossier_meeting_attendee (
    meeting_id INT NOT NULL REFERENCES acos.dossier_meeting ON DELETE CASCADE,
    dossier_id INT NOT NULL REFERENCES acos.dossier,
    PRIMARY KEY (meeting_id, dossier_id)
);

CREATE TABLE IF NOT EXISTS acos.dossier_entry (
    entry_id    SERIAL PRIMARY KEY,
    dossier_id  INT NOT NULL REFERENCES acos.dossier,
    meeting_id  INT REFERENCES acos.dossier_meeting,
    entry_date  DATE NOT NULL,
    entry_text  TEXT NOT NULL,
    status      TEXT NOT NULL CHECK (status IN ('draft', 'blessed')) DEFAULT 'draft',
    blessed_at  TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Undated watch-items only. A "proposed closure" of an existing open loop is
-- represented WITHOUT a new row: status stays 'open', closed_entry_id points at
-- the draft entry that proposed it, and closed_at IS NULL. Blessing the closure
-- flips status='closed' + stamps closed_at. So:
--   proposed new loop → status='proposed'
--   proposed closure  → status='open'  AND closed_entry_id NOT NULL AND closed_at IS NULL
--   executed closure  → status='closed' AND closed_at NOT NULL
CREATE TABLE IF NOT EXISTS acos.dossier_loop (
    loop_id         SERIAL PRIMARY KEY,
    dossier_id      INT NOT NULL REFERENCES acos.dossier,
    loop_text       TEXT NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('proposed', 'open', 'closed')) DEFAULT 'proposed',
    opened_entry_id INT REFERENCES acos.dossier_entry,
    closed_entry_id INT REFERENCES acos.dossier_entry,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    closed_at       TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS acos.dossier_idea (
    idea_id          SERIAL PRIMARY KEY,
    dossier_id       INT NOT NULL REFERENCES acos.dossier,
    source_dossier_id INT REFERENCES acos.dossier,  -- cross-pollination provenance
    idea_text        TEXT NOT NULL,
    status           TEXT NOT NULL CHECK (status IN ('proposed', 'active', 'used', 'retired')) DEFAULT 'proposed',
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_dossier_active           ON acos.dossier (active);
CREATE INDEX IF NOT EXISTS idx_dossier_entry_dossier    ON acos.dossier_entry (dossier_id, status);
CREATE INDEX IF NOT EXISTS idx_dossier_loop_dossier     ON acos.dossier_loop (dossier_id, status);
CREATE INDEX IF NOT EXISTS idx_dossier_idea_dossier     ON acos.dossier_idea (dossier_id, status);
CREATE INDEX IF NOT EXISTS idx_dossier_attendee_dossier ON acos.dossier_meeting_attendee (dossier_id);

-- Provenance on the existing personal-tracker commitments store (migration 020),
-- the canonical to-do home (PB-010 coverage decision). Nullable; zero impact on
-- existing rows. Dossier-linked to-dos carry the person (and the meeting they
-- came from) so `brief`/`what's on the to dos` can attribute them.
ALTER TABLE acos.commitments
    ADD COLUMN IF NOT EXISTS dossier_id INT REFERENCES acos.dossier,
    ADD COLUMN IF NOT EXISTS meeting_id INT REFERENCES acos.dossier_meeting;

-- acos.commitments.status is already free-text 'TEXT NOT NULL DEFAULT active'
-- with NO CHECK constraint (migration 020), so a draft-capable status needs no
-- change — 'draft' is a legal value today. (Per the spec's conditional: extend
-- allowed values via the module's pattern, do NOT loosen a non-existent CHECK.)
--
-- COVERAGE SEAM (surfaced, not silently handled): due_date is NOT NULL in 020,
-- but PB-010 §3.2 requires draft action items with a null due date. Making it
-- nullable is zero-impact — every existing row is dated, and get_due_soon /
-- get_start_alerts filter `due_date <= …` / `due_date - … <= …`, which never
-- match NULL (an undated to-do correctly is not "due soon"). Reversible.
ALTER TABLE acos.commitments ALTER COLUMN due_date DROP NOT NULL;
