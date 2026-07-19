-- 029_engagement.sql
-- OPS-2: Engagement Ops UI (ops.rdm.is rehab).
--
-- One operating model: engagements. The operator is entering a federal role (FCA)
-- as effectively a fractional chief data architect; later the same UI serves
-- multiple client engagements. This migration introduces the engagement registry
-- and the one new adjudication capability (edit-then-approve).
--
-- Idempotent (IF NOT EXISTS guards, matching prior migrations). No data is
-- destroyed; the parked RDMIS pipeline/survival panels keep their tables untouched.

-- ---------------------------------------------------------------------------
-- acos.engagement — the portfolio registry. One row per engagement.
--
-- Scoping rule for every ops panel: an item is scoped to an engagement when its
-- `context` column equals this slug (proposals: vault.extraction_proposal.context,
-- commitments: acos.commitments.context — both added in migration 028). Items with
-- NULL context are NOT hidden: they surface in the engagement page's "unscoped"
-- section and on the portfolio pending badge (silent filtering loses items).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS acos.engagement (
  slug                 text PRIMARY KEY,
  display_name         text NOT NULL,
  next_hard_date       date,
  next_hard_date_label text,
  active               boolean NOT NULL DEFAULT true,
  archived             boolean NOT NULL DEFAULT false,
  created_at           timestamptz NOT NULL DEFAULT now()
);

-- Seed the one live engagement (FCA). ON CONFLICT DO NOTHING keeps re-runs safe
-- and never clobbers an operator edit to the date/label made after the first apply.
INSERT INTO acos.engagement
  (slug, display_name, next_hard_date, next_hard_date_label, active, archived)
VALUES
  ('fca', 'FCA', '2026-08-08', 'DCAA release', true, false)
ON CONFLICT (slug) DO NOTHING;

-- ---------------------------------------------------------------------------
-- Edit-then-approve (OPS-2, the one new capability).
--
-- In the approval queue the operator may edit a proposal's payload fields (title,
-- due date, direction, body text) before approving. BOTH versions are retained:
-- the original `payload` (migration 028) stays byte-for-byte untouched, and the
-- edited version lands in `payload_final`. Approval writes through the existing
-- creation path using payload_final WHEN PRESENT, else payload. This preserves
-- proposed-vs-blessed as future rule-promotion training data. `target_ref`
-- discipline (migration 028) is unchanged.
-- ---------------------------------------------------------------------------
ALTER TABLE vault.extraction_proposal ADD COLUMN IF NOT EXISTS payload_final jsonb;
