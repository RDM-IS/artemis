#!/usr/bin/env python3.11
"""PREP-1 — record Ryan's own nutrition targets (alignment decision 4).

His numbers, from the Prep Board spec of 2026-09-29:

    <= 2,100 kcal · >= 185 g protein · <= 185 g carbs · <= 70 g fat
    >= 38 g fiber · <= 25 g sugar · >= 1 plant-based meal/day

set_by = 'ryan', provisional = TRUE. ARTEMIS DOES NOT INVENT A TARGET, and it does
not promote one either: `provisional` is what makes the dietitian report render
"set by patient, pending dietitian review" instead of presenting these as clinical
advice. Only the dietitian clears that flag.

The open row before this script runs is the 2026-09-22 one: 2,100 kcal and 175 g
protein, with carbs, fat and fiber unset. This supersedes it rather than editing
it — `nutrition.target` is a history, and overwriting the row would destroy the
record of what he was aiming at in September.

`one_open_nutrition_target` is a unique index on (effective_to IS NULL), so the
prior row MUST be closed in the same transaction as the insert or the insert is
rejected. That is the whole reason this is a transaction and not two statements.

Idempotent: a second run finds the target already recorded and changes nothing.
"""

import sys
from datetime import date, timedelta

sys.path.insert(0, ".")

TARGET = {
    "kcal": 2100, "protein_g": 185, "carb_g": 185, "fat_g": 70,
    "fiber_g": 38, "sugar_g": 25, "plant_meals_min": 1,
}
EFFECTIVE_FROM = date(2026, 9, 29)
NOTES = ("Ryan's own targets, Prep Board spec 2026-09-29. Provisional: awaiting "
         "VA dietitian review. Sugar and plant-meal targets have no data source "
         "yet — Notion recipes carry neither, so Prep reports them as no-data "
         "rather than as satisfied.")


def main() -> int:
    from knowledge.db import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, effective_from, kcal, protein_g, carb_g, fat_g, "
                "       fiber_g, sugar_g, plant_meals_min "
                "  FROM nutrition.target WHERE effective_to IS NULL")
            open_rows = cur.fetchall()
            if len(open_rows) > 1:
                print("ABORT: %d open targets; expected at most one" % len(open_rows))
                return 1

            if open_rows:
                row = open_rows[0]
                current = {"kcal": row[2], "protein_g": row[3], "carb_g": row[4],
                           "fat_g": row[5], "fiber_g": row[6], "sugar_g": row[7],
                           "plant_meals_min": row[8]}
                current = {k: (int(v) if v is not None else None)
                           for k, v in current.items()}
                if current == TARGET and row[1] == EFFECTIVE_FROM:
                    print("Already recorded: target #%s from %s — nothing to do."
                          % (row[0], row[1]))
                    return 0
                if row[1] >= EFFECTIVE_FROM:
                    # Closing it would give it an end date before its start. This
                    # is health.py's TargetBackdated condition, checked here for
                    # the same reason.
                    print("ABORT: open target #%s starts %s, on or after %s. "
                          "Closing it would end it before it began."
                          % (row[0], row[1], EFFECTIVE_FROM))
                    return 1
                cur.execute(
                    "UPDATE nutrition.target SET effective_to = %s WHERE id = %s",
                    (EFFECTIVE_FROM - timedelta(days=1), row[0]))
                print("Closed target #%s at %s"
                      % (row[0], EFFECTIVE_FROM - timedelta(days=1)))

            cur.execute(
                """INSERT INTO nutrition.target
                     (effective_from, kcal, protein_g, carb_g, fat_g, fiber_g,
                      sugar_g, plant_meals_min, set_by, provisional, notes)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'ryan', TRUE, %s)
                   RETURNING id""",
                (EFFECTIVE_FROM, TARGET["kcal"], TARGET["protein_g"],
                 TARGET["carb_g"], TARGET["fat_g"], TARGET["fiber_g"],
                 TARGET["sugar_g"], TARGET["plant_meals_min"], NOTES))
            new_id = cur.fetchone()[0]

            # READ IT BACK IN THE SAME TRANSACTION and assert every value. An
            # insert that "succeeded" proves the statement ran, not that the
            # numbers landed — the distinction that let migration 039 pass a smoke
            # test it could not fail.
            cur.execute(
                "SELECT kcal, protein_g, carb_g, fat_g, fiber_g, sugar_g, "
                "       plant_meals_min, set_by, provisional, effective_to "
                "  FROM nutrition.target WHERE id = %s", (new_id,))
            k, p, c, f, fi, su, pl, by, prov, eff_to = cur.fetchone()
            got = {"kcal": int(k), "protein_g": int(p), "carb_g": int(c),
                   "fat_g": int(f), "fiber_g": int(fi), "sugar_g": int(su),
                   "plant_meals_min": int(pl)}
            if got != TARGET:
                raise SystemExit("ABORT: read-back mismatch %r != %r" % (got, TARGET))
            if by != "ryan" or not prov:
                raise SystemExit(
                    "ABORT: set_by=%r provisional=%r — must be ryan/True, or the "
                    "dietitian report would present his own numbers as clinical"
                    % (by, prov))
            if eff_to is not None:
                raise SystemExit("ABORT: the new target is not open (effective_to=%s)"
                                 % eff_to)

            cur.execute("SELECT count(*) FROM nutrition.target WHERE effective_to IS NULL")
            n_open = cur.fetchone()[0]
            if n_open != 1:
                raise SystemExit("ABORT: %d open targets after the write" % n_open)

    print("Target #%s live from %s: %s kcal, %s g protein, %s g carbs, %s g fat, "
          "%s g fiber, %s g sugar, >=%s plant meal/day (ryan, provisional)"
          % (new_id, EFFECTIVE_FROM, TARGET["kcal"], TARGET["protein_g"],
             TARGET["carb_g"], TARGET["fat_g"], TARGET["fiber_g"],
             TARGET["sugar_g"], TARGET["plant_meals_min"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
