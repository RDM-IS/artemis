#!/usr/bin/env python3.11
"""PREP-2 — seed `nutrition.recipe_step` from the source spec's step table.

The durations are the spec's own STARTING ESTIMATES, which it says the user will
correct. They are not measurements, and `prep_event` records planned-vs-actual from
the first session so they can eventually be learned rather than guessed.

IDEMPOTENT, and it does not fight the editor. A recipe that already has steps is
LEFT ALONE — the Prep tab's editor is the authority once Ryan has touched one, and
a seed that overwrote his edits every deploy would be worse than no seed. Pass
`--replace <slug>` to deliberately re-seed one recipe.

WHY THE SLUGS ARE IN THIS FILE. They are program content, like the exercise names
in health_office.py, rather than logged health data: no loads, no measurements, no
dates. PUBLIC-FIXTURES governs test fixtures and is satisfied — every test uses
Greek-letter stand-ins. If Ryan would rather his recipe names were not in a public
repository, the alternative is entering steps through the editor and deleting this
file; say so and it goes.

SHARED PREP: the `roast:veg` chain is attached to the three recipes the spec's own
merged-card example names ("Cut peppers, onion, zucchini · 900 g · tempeh bowl,
lentil bowl, egg bites"). The spec lists "Roasted veg" as a row with NO owning
recipe, so that example is the only evidence of who it feeds. Adjust in the editor.

`salmon roasted veg` deliberately gets NO steps: the spec says it is cooked fresh
on the day, not batch-prepped. Absent is the correct answer there, not empty.
"""

import argparse
import sys

sys.path.insert(0, ".")

HANDS, OVEN, STOVE, AIR, COUNTER, FRIDGE = (
    "hands", "oven", "stove", "air_fryer", "counter", "fridge")
ACTIVE, PASSIVE, UNATT = "active", "passive", "unattended"

#: (name, resource, mode, base_min, per_serving_min, temp_f, batch_key, shortcut,
#:  chain_key)
#:
#: CHAIN KEYS MATTER MORE THAN THEY LOOK. Steps sharing a key are sequential;
#: different keys run in PARALLEL; NULL is a barrier that waits for all of them.
#: Without them the Lentil tofu bowl's eleven steps were one queue and the board
#: said 125 minutes with a 25-minute idle gap — the lentils, the veg and the tofu
#: were being cooked one after another. The source spec lists those three as
#: separate rows for exactly this reason.
ROAST_VEG = [
    ("Wash veg", HANDS, ACTIVE, 5, 0, None, "roast:veg", "precut_veg", "veg"),
    ("Cut veg", HANDS, ACTIVE, 12, 0, None, "roast:veg", "precut_veg", "veg"),
    ("Roast veg", OVEN, PASSIVE, 25, 0, 425, "roast:veg", None, "veg"),
]

LENTIL_BASE = [
    ("Rinse lentils", HANDS, ACTIVE, 3, 0, None, None, "precooked_lentils", "lentil"),
    ("Simmer lentils", STOVE, PASSIVE, 20, 0, None, None, "precooked_lentils", "lentil"),
    ("Drain and cool", COUNTER, PASSIVE, 10, 0, None, None, "precooked_lentils", "lentil"),
]

STEPS: dict[str, list] = {
    "lentil egg white bowl": LENTIL_BASE + [
        ("Portion", HANDS, ACTIVE, 0, 2, None, None, None, None),
    ],
    "lentil tofu bowl plant based": LENTIL_BASE + ROAST_VEG + [
        ("Press tofu", HANDS, ACTIVE, 2, 0, None, None, None, "tofu"),
        ("Pressing", COUNTER, PASSIVE, 15, 0, None, None, None, "tofu"),
        ("Cube and season tofu", HANDS, ACTIVE, 4, 0, None, None, None, "tofu"),
        ("Bake tofu", OVEN, PASSIVE, 25, 0, 425, None, None, "tofu"),
        ("Portion", HANDS, ACTIVE, 0, 2, None, None, None, None),
    ],
    "tempeh black bean bowl plant based": ROAST_VEG + [
        ("Crumble and sear tempeh", STOVE, ACTIVE, 10, 0, None, None, None, "tempeh"),
        ("Portion with beans and veg", HANDS, ACTIVE, 0, 1.5, None, None, None, None),
    ],
    "chicken thigh bowl": [
        ("Season chicken", HANDS, ACTIVE, 4, 0, None, None, None, "chicken"),
        ("Air-fry chicken", AIR, PASSIVE, 22, 0, None, None, None, "chicken"),
        ("Portion", HANDS, ACTIVE, 0, 2, None, None, None, None),
    ],
    "overnight oats jar": [
        ("Mix jars", HANDS, ACTIVE, 0, 2, None, None, None, None),
        ("Chill overnight", FRIDGE, UNATT, 0, 0, None, None, None, None),
    ],
    "cottage cheese chocolate pb mousse": [
        ("Blend", HANDS, ACTIVE, 0, 1.5, None, None, None, None),
        ("Chill", FRIDGE, UNATT, 0, 0, None, None, None, None),
    ],
    "cottage cheese cinnamon cheesecake": [
        ("Blend", HANDS, ACTIVE, 0, 1.5, None, None, None, None),
        ("Chill", FRIDGE, UNATT, 0, 0, None, None, None, None),
    ],
    "travel egg white bites string cheese": ROAST_VEG + [
        ("Whisk and fill", HANDS, ACTIVE, 4, 0, None, None, None, "bites"),
        ("Bake bites", OVEN, PASSIVE, 18, 0, 350, None, None, "bites"),
        ("Cool uncovered", COUNTER, PASSIVE, 5, 0, None, None, None, "bites"),
    ],
}

#: Recipes that are deliberately stepless, and why. Recorded so a future reader
#: does not "fix" the omission.
NO_STEPS = {
    "salmon roasted veg": "cooked fresh on the day, not batch-prepped (spec)",
}

#: Alignment decision 12 (2026-09-29): eatable, never auto-planned.
PLAN_INELIGIBLE = ("protein pb cup dish",)

_INSERT = """
INSERT INTO nutrition.recipe_step
  (recipe_notion_id, step_no, name, resource, mode, base_min, per_serving_min,
   temp_f, batch_key, shortcut_key, chain_key)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true",
                    help="write. Without it this is a dry run.")
    ap.add_argument("--replace", metavar="SLUG", action="append", default=[],
                    help="re-seed this recipe even if it already has steps")
    args = ap.parse_args()

    from knowledge.db import get_connection

    replace = set(args.replace)
    wrote = skipped = missing = 0
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT slug, notion_id, name FROM nutrition.prep_recipe "
                        "WHERE deleted_at IS NULL")
            by_slug = {r[0]: (r[1], r[2]) for r in cur.fetchall()}

            for slug, steps in STEPS.items():
                hit = by_slug.get(slug)
                if hit is None:
                    print(f"  MISSING recipe {slug!r} — not in nutrition.prep_recipe")
                    missing += 1
                    continue
                notion_id, name = hit
                cur.execute("SELECT count(*) FROM nutrition.recipe_step "
                            "WHERE recipe_notion_id = %s", (notion_id,))
                have = cur.fetchone()[0]
                if have and slug not in replace:
                    print(f"  skip  {slug!r} — already has {have} step(s); "
                          f"the editor owns it now")
                    skipped += 1
                    continue
                if have:
                    cur.execute("DELETE FROM nutrition.recipe_step "
                                "WHERE recipe_notion_id = %s", (notion_id,))
                for i, (sname, resource, mode, base, per, temp, batch, short,
                        chain) in enumerate(steps, start=1):
                    cur.execute(_INSERT, (notion_id, i, sname, resource, mode,
                                          base, per, temp, batch, short, chain))
                print(f"  seed  {slug!r}: {len(steps)} step(s)")
                wrote += 1

            for slug, why in NO_STEPS.items():
                print(f"  none  {slug!r} — {why}")

            for slug in PLAN_INELIGIBLE:
                hit = by_slug.get(slug)
                if hit is None:
                    print(f"  MISSING recipe {slug!r} for plan_eligible")
                    continue
                cur.execute("UPDATE nutrition.prep_recipe SET plan_eligible = FALSE "
                            "WHERE notion_id = %s AND plan_eligible", (hit[0],))
                if cur.rowcount:
                    print(f"  flag  {slug!r} plan_eligible = FALSE")

            # READ BACK inside the transaction and assert what landed, not that the
            # statements ran. A seed that "succeeded" proves neither.
            cur.execute("""
                SELECT r.slug, count(s.id) n,
                       count(*) FILTER (WHERE s.resource = 'oven' AND s.temp_f IS NULL) bad_oven
                  FROM nutrition.prep_recipe r
                  JOIN nutrition.recipe_step s ON s.recipe_notion_id = r.notion_id
                 GROUP BY r.slug ORDER BY r.slug""")
            rows = cur.fetchall()
            print("\n  steps now in RDS:")
            for slug, n, bad in rows:
                print(f"    {slug:48s} {n} step(s)")
                if bad:
                    raise SystemExit(f"ABORT: {slug} has an oven step with no temp_f")
            if not args.commit:
                conn.rollback()
                print("\n[DRY-RUN] nothing written. Re-run with --commit.")
                return 0
    print(f"\n[OK] seeded {wrote}, skipped {skipped}, missing {missing}")
    return 1 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main())
