"""Seed the initial FCA colleague dossiers (PB-010). One-off, idempotent.

Parses three authored markdown dossiers from scripts/seed_data/{slug}.md (Ryan
places them there) into the acos.dossier* tables:

  §1 Position & terrain   → dossier.position_terrain
  §2 What they need       → dossier.needs_from_me
  §3 Interaction log      → one dossier_meeting (topic='interview', raw_notes=§3)
                            + one BLESSED dossier_entry
  §4 Open loops (bullets) → dossier_loop status='open' (blessed — Ryan-authored)
  §5 Idea bank (bullets)  → dossier_idea status='active'; a bullet that cites
                            another dossier ("From Jeremy's dossier: …") gets
                            source_dossier_id (cross-pollination provenance)

Idempotent: a dossier whose slug already exists is SKIPPED whole.

The interview date is NOT invented. Supply it with --interview-date YYYY-MM-DD
(applies to all three) or a per-file `date: YYYY-MM-DD` line; absent both, the
script refuses rather than guessing.

Markdown shape (headers matched loosely by section number or keyword):

    # Jennifer Xu
    slug: jennifer            (optional; defaults to the first name)
    date: 2026-07-01          (optional; else --interview-date)

    ## 1. Position & terrain
    <free text>

    ## 2. What they need from me
    <free text>

    ## 3. Interaction log
    <free text>

    ## 4. Open loops
    - loop one
    - loop two

    ## 5. Idea bank
    - idea one
    - From Jeremy's dossier: shared idea

Usage:
    set -a; [ -f .env ] && . ./.env; set +a
    PYTHONPATH="$PWD" python3.11 scripts/seed_dossiers.py --interview-date 2026-07-01
"""

import argparse
import glob
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from knowledge.db import execute_one, execute_query, execute_write  # noqa: E402

_SEED_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seed_data")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_CROSS_RE = re.compile(r"^from\s+(.+?)'?s?\s+dossier\s*:\s*(.+)$", re.IGNORECASE)


def _slugify(text: str) -> str:
    s = re.sub(r"[^\w\s-]", "", (text or "").strip().lower())
    s = re.sub(r"[\s_]+", "-", s).strip("-")
    return s[:40]


def _parse_markdown(path: str) -> dict:
    """Parse one dossier markdown file into a structured dict."""
    text = open(path, encoding="utf-8").read()
    lines = text.splitlines()

    full_name, slug, file_date = None, None, None
    sections: dict[str, list[str]] = {}
    current = None

    for line in lines:
        h1 = re.match(r"^#\s+(.+)$", line)
        if h1 and full_name is None:
            full_name = h1.group(1).strip()
            continue
        mslug = re.match(r"^slug:\s*(.+)$", line, re.IGNORECASE)
        if mslug:
            slug = _slugify(mslug.group(1))
            continue
        mdate = re.match(r"^date:\s*(.+)$", line, re.IGNORECASE)
        if mdate:
            file_date = mdate.group(1).strip()
            continue
        h2 = re.match(r"^##\s+(.+)$", line)
        if h2:
            current = _classify_section(h2.group(1))
            sections.setdefault(current, [])
            continue
        if current:
            sections[current].append(line)

    return {
        "full_name": full_name,
        "slug": slug or (_slugify(full_name.split(" ")[0]) if full_name else None),
        "date": file_date,
        "position": _joined(sections.get("position")),
        "needs": _joined(sections.get("needs")),
        "log": _joined(sections.get("log")),
        "loops": _bullets(sections.get("loops")),
        "ideas": _bullets(sections.get("ideas")),
    }


def _classify_section(header: str) -> str | None:
    h = header.lower()
    if "1" in h or "position" in h or "terrain" in h:
        return "position"
    if "2" in h or "need" in h:
        return "needs"
    if "3" in h or "interaction" in h or "log" in h:
        return "log"
    if "4" in h or "loop" in h:
        return "loops"
    if "5" in h or "idea" in h:
        return "ideas"
    return None


def _joined(lines: list[str] | None) -> str:
    return "\n".join(lines).strip() if lines else ""


def _bullets(lines: list[str] | None) -> list[str]:
    out = []
    for line in lines or []:
        m = re.match(r"^\s*[-*]\s+(.+)$", line)
        if m:
            out.append(m.group(1).strip())
    return out


def _resolve_date(rec: dict, cli_date: str | None) -> str:
    d = rec.get("date") or cli_date
    if not d or not _DATE_RE.match(d):
        raise SystemExit(
            f"ERROR: no valid interview date for {rec['slug']} — pass "
            f"--interview-date YYYY-MM-DD or add a `date:` line. Refusing to invent one."
        )
    return d


def seed_one(rec: dict, interview_date: str) -> dict:
    """Insert one dossier and its children. Returns per-table counts.
    Cross-pollination is resolved in a second pass (see main)."""
    dossier = execute_one(
        "INSERT INTO acos.dossier (slug, full_name, position_terrain, needs_from_me) "
        "VALUES (%s, %s, %s, %s) RETURNING dossier_id",
        (rec["slug"], rec["full_name"], rec["position"] or None, rec["needs"] or None),
    )
    did = dossier["dossier_id"]
    counts = {"dossier": 1, "meeting": 0, "entry": 0, "loop": 0, "idea": 0}

    if rec["log"]:
        meeting = execute_one(
            "INSERT INTO acos.dossier_meeting (occurred_on, topic, raw_notes) "
            "VALUES (%s, 'interview', %s) RETURNING meeting_id",
            (interview_date, rec["log"]),
        )
        mid = meeting["meeting_id"]
        execute_write(
            "INSERT INTO acos.dossier_meeting_attendee (meeting_id, dossier_id) VALUES (%s, %s)",
            (mid, did),
        )
        execute_write(
            "INSERT INTO acos.dossier_entry (dossier_id, meeting_id, entry_date, entry_text, status, blessed_at) "
            "VALUES (%s, %s, %s, %s, 'blessed', now())",
            (did, mid, interview_date, rec["log"]),
        )
        counts["meeting"] = 1
        counts["entry"] = 1

    for loop in rec["loops"]:
        execute_write(
            "INSERT INTO acos.dossier_loop (dossier_id, loop_text, status) VALUES (%s, %s, 'open')",
            (did, loop),
        )
        counts["loop"] += 1

    return counts, did


def seed_ideas(did: int, ideas: list[str]) -> int:
    """Second pass: ideas with cross-pollination resolved against seeded slugs."""
    n = 0
    for idea in ideas:
        m = _CROSS_RE.match(idea)
        source_id = None
        text = idea
        if m:
            src = execute_one(
                "SELECT dossier_id FROM acos.dossier "
                "WHERE lower(split_part(full_name,' ',1)) = lower(%s) OR lower(slug) = lower(%s)",
                (_slugify(m.group(1)), _slugify(m.group(1))),
            )
            if src:
                source_id = src["dossier_id"]
            text = m.group(2).strip()
        execute_write(
            "INSERT INTO acos.dossier_idea (dossier_id, source_dossier_id, idea_text, status) "
            "VALUES (%s, %s, %s, 'active')",
            (did, source_id, text),
        )
        n += 1
    return n


def main():
    ap = argparse.ArgumentParser(description="Seed FCA colleague dossiers (PB-010)")
    ap.add_argument("--interview-date", help="YYYY-MM-DD applied to files lacking a `date:` line")
    ap.add_argument("--seed-dir", default=_SEED_DIR)
    args = ap.parse_args()

    files = [f for f in sorted(glob.glob(os.path.join(args.seed_dir, "*.md")))
             if not os.path.basename(f).lower().startswith(("readme", "_"))]
    if not files:
        print(f"No markdown files in {args.seed_dir} — nothing to seed.")
        return

    totals = {"dossier": 0, "meeting": 0, "entry": 0, "loop": 0, "idea": 0}
    skipped = []
    seeded = []  # (rec, did) for the idea second pass

    for path in files:
        rec = _parse_markdown(path)
        if not rec["full_name"] or not rec["slug"]:
            print(f"  [SKIP] {os.path.basename(path)} — missing name/slug")
            continue
        if execute_query("SELECT 1 FROM acos.dossier WHERE lower(slug) = lower(%s)", (rec["slug"],)):
            skipped.append(rec["slug"])
            print(f"  [SKIP] {rec['slug']} — already exists")
            continue
        interview_date = _resolve_date(rec, args.interview_date)
        counts, did = seed_one(rec, interview_date)
        for k in ("dossier", "meeting", "entry", "loop"):
            totals[k] += counts[k]
        seeded.append((rec, did))
        print(f"  [OK]   {rec['slug']} — meeting {counts['meeting']}, "
              f"entry {counts['entry']}, loops {counts['loop']}")

    # Second pass: ideas (cross-pollination needs all dossiers present first).
    for rec, did in seeded:
        n = seed_ideas(did, rec["ideas"])
        totals["idea"] += n

    print("\nRow counts written:")
    for k, v in totals.items():
        print(f"  {k:8} {v}")
    if skipped:
        print(f"Skipped (already present): {', '.join(skipped)}")


if __name__ == "__main__":
    main()
