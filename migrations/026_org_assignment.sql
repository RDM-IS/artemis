-- 026_org_assignment.sql
-- PB-010c: Org Assignments & Org Chart.
--
-- People are org-INDEPENDENT; employment is the org-scoped fact. The dossier
-- stays the person (name + five sections); this table carries who employs them,
-- as what, reporting to whom — effective-dated, so reorgs and job changes accrue
-- HISTORY instead of overwriting. The org tag moves off acos.dossier onto here.
--
-- THE WALL is unchanged: reporting edges are FACTS Ryan approves, never
-- inferences the LLM asserts. The org chart renders ONLY approved current rows;
-- §1 prose is never parsed for structure.
--
-- Idempotent; needs acos.dossier (024) present.

CREATE TABLE IF NOT EXISTS acos.org_assignment (
    assignment_id SERIAL PRIMARY KEY,
    dossier_id    INT NOT NULL REFERENCES acos.dossier,
    org           TEXT NOT NULL,                       -- 'fca-odae', 'fdic', 'client:acme'
    title         TEXT,
    reports_to    INT REFERENCES acos.dossier,         -- null = unknown
    is_root       BOOLEAN NOT NULL DEFAULT FALSE,       -- true = known top of this org's tree
                                                        -- (distinguishes known-top from unknown)
    status        TEXT NOT NULL CHECK (status IN ('draft', 'approved')) DEFAULT 'approved',
    evidence      TEXT,                                -- draft org_signal provenance quote (§3.3).
                                                        -- NOT in the original spec DDL; added so a
                                                        -- draft review item can render its evidence
                                                        -- quote from the written row.
    valid_from    DATE NOT NULL,
    valid_to      DATE,                                -- null = current
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Exactly one current APPROVED assignment per person per org. The spec's index
-- was `where valid_to is null`, but §3.3 draft rows also carry valid_to IS NULL
-- and would collide with the approved current row — so the partial index is
-- scoped to status='approved'. Drafts (status='draft') coexist freely and are
-- invisible to every org-chart render.
CREATE UNIQUE INDEX IF NOT EXISTS one_current_assignment
    ON acos.org_assignment (dossier_id, org)
    WHERE valid_to IS NULL AND status = 'approved';

CREATE INDEX IF NOT EXISTS idx_org_assignment_org
    ON acos.org_assignment (org) WHERE valid_to IS NULL AND status = 'approved';
CREATE INDEX IF NOT EXISTS idx_org_assignment_reports_to
    ON acos.org_assignment (reports_to) WHERE valid_to IS NULL AND status = 'approved';

-- Migrate the org tag off dossier: backfill a current approved assignment for
-- every dossier that has an org, then drop the column. valid_from = current_date
-- is an honest "known since" for backfilled rows — employment start dates are NOT
-- invented (title/reports_to stay null; Ryan populates via `dossier set`).
INSERT INTO acos.org_assignment (dossier_id, org, valid_from)
    SELECT dossier_id, org, current_date FROM acos.dossier WHERE org IS NOT NULL
    ON CONFLICT DO NOTHING;

ALTER TABLE acos.dossier DROP COLUMN IF EXISTS org;
