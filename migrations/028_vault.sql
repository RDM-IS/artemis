-- 028_vault.sql
-- PB-011: Vault / Second Brain Ingest (v1).
--
-- Ryan's Obsidian vault (RDM-IS/vault, private) is the canonical human-authored
-- knowledge store. Artemis ingests it into schema `vault` (Postgres), runs one
-- extraction pass per new note, and surfaces everything as proposals through the
-- existing approval gates. The vault FILE is canon; these tables are a rebuildable
-- projection.
--
-- THE WALL (statistics vs semantics): Artemis parses, counts, links, detects, and
-- PROPOSES. Nothing extraction produces auto-writes to any system-of-record table.
-- vault.extraction_proposal rows only become records when Ryan approves them, and
-- only ever through the existing creation code paths (dossier draft-approval,
-- commitment creation) so all prior guardrails hold.
--
-- Idempotent (IF NOT EXISTS guards, matching prior migrations). Reserves room for
-- embeddings / semantic links (built later) without carrying that weight now.

CREATE SCHEMA IF NOT EXISTS vault;

CREATE TABLE IF NOT EXISTS vault.notes (
  capture_id        text PRIMARY KEY,
  path              text NOT NULL UNIQUE,        -- repo-relative
  source            text NOT NULL,               -- meeting|dictation|thought|journal|legacy-notes|legacy-kl|...
  status            text NOT NULL DEFAULT 'bronze',
  created_at        timestamptz,                 -- frontmatter `created`
  frontmatter       jsonb NOT NULL DEFAULT '{}'::jsonb,
  raw_text          text NOT NULL,               -- body below frontmatter, byte-faithful
  cleaned_text      text,                        -- inferential readable rendering; DB-only
  content_hash      text NOT NULL,               -- sha256(normalized body)
  word_count        int,
  first_ingested_at timestamptz NOT NULL DEFAULT now(),
  last_ingested_at  timestamptz NOT NULL DEFAULT now(),
  deleted_at        timestamptz                  -- file gone from repo; row retained
);

CREATE TABLE IF NOT EXISTS vault.note_links (
  id                 bigserial PRIMARY KEY,
  source_capture_id  text NOT NULL REFERENCES vault.notes(capture_id) ON DELETE CASCADE,
  target_raw         text NOT NULL,              -- literal [[...]] target text
  target_capture_id  text REFERENCES vault.notes(capture_id),  -- resolved; NULL = dangling
  link_type          text NOT NULL DEFAULT 'wikilink'
                     CHECK (link_type IN ('wikilink','semantic')),  -- 'semantic' reserved
  computed_at        timestamptz NOT NULL DEFAULT now(),
  UNIQUE (source_capture_id, target_raw, link_type)
);

CREATE TABLE IF NOT EXISTS vault.note_metadata (
  id          bigserial PRIMARY KEY,
  capture_id  text NOT NULL REFERENCES vault.notes(capture_id) ON DELETE CASCADE,
  key         text NOT NULL,                     -- e.g. 'suggested_tags','summary'
  value       jsonb NOT NULL,
  provenance  text NOT NULL CHECK (provenance IN ('deterministic','inferential')),
  confidence  numeric,                           -- required when inferential
  model       text,                              -- model id when inferential
  created_at  timestamptz NOT NULL DEFAULT now(),
  UNIQUE (capture_id, key, provenance)
);

CREATE TABLE IF NOT EXISTS vault.extraction_proposal (
  id              bigserial PRIMARY KEY,
  capture_id      text NOT NULL REFERENCES vault.notes(capture_id),
  extraction_type text NOT NULL CHECK (extraction_type IN
                    ('action_item','commitment','dossier_entry','org_fact',
                     'decision_candidate','question')),
  payload         jsonb NOT NULL,                -- structured candidate
  context         text,                          -- 'fca' | client tag | NULL
  content_hash    text NOT NULL,                 -- sha256 of canonicalized payload core
  status          text NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending','approved','rejected','expired')),
  created_at      timestamptz NOT NULL DEFAULT now(),
  adjudicated_at  timestamptz,
  target_ref      text,                          -- written row ref on approval
  UNIQUE (capture_id, extraction_type, content_hash)
);

CREATE INDEX IF NOT EXISTS idx_extraction_proposal_status
    ON vault.extraction_proposal (status, created_at);

CREATE TABLE IF NOT EXISTS vault.ingest_state (
  key        text PRIMARY KEY,                   -- 'last_sha','last_run', counters
  value      jsonb NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now()
);

-- Context tag on the two creation targets an approved proposal writes through.
-- ('fca' | client tag | NULL). Verified against the live schema: both
-- acos.action_items and acos.commitments exist (CONTEXT.generated.md schema acos).
ALTER TABLE acos.action_items ADD COLUMN IF NOT EXISTS context text;
ALTER TABLE acos.commitments  ADD COLUMN IF NOT EXISTS context text;
