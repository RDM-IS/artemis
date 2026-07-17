# Dossier seed data (PB-010)

Place one markdown file per person here (e.g. `jennifer.md`, `jeremy.md`,
`dennis.md`). `scripts/seed_dossiers.py` parses them into the `acos.dossier*`
tables. The seed is idempotent — a slug that already exists is skipped whole.

## File format

Sections are matched loosely by their number or a keyword in the `##` header, so
`## 1. Position & terrain`, `## Position`, and `## Terrain` all work.

```markdown
# Jennifer Xu
slug: jennifer            # optional — defaults to the lowercased first name
date: 2026-07-01          # optional interview date — else pass --interview-date

## 1. Position & terrain
Free-text. Becomes dossier.position_terrain.

## 2. What they need from me
Free-text, their words preserved. Becomes dossier.needs_from_me.

## 3. Interaction log
Free-text. Stored verbatim as one dossier_meeting (topic='interview') AND as one
BLESSED dossier_entry dated to the interview date.

## 4. Open loops
- one watch-item per bullet → dossier_loop status='open' (blessed)
- another loop

## 5. Idea bank
- one idea per bullet → dossier_idea status='active'
- From Jeremy's dossier: a shared idea → gets source_dossier_id (cross-pollination)
```

## Running

```bash
set -a; [ -f .env ] && . ./.env; set +a
PYTHONPATH="$PWD" python3.11 scripts/seed_dossiers.py --interview-date 2026-07-01
```

The interview date is **never invented** — supply `--interview-date YYYY-MM-DD`
(applied to files without a `date:` line) or add a per-file `date:` line, or the
script refuses. Row counts per table are printed for verification against the
source files.
