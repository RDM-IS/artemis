import os as _os; _os.environ["ARTEMIS_TEST_NO_DB"] = "1"  # TEST-DB-GUARD: never a real DB
"""Synthetic data for the dietitian report tests (PUBLIC-FIXTURES: 2027 dates,
round made-up numbers, never real logs)."""
from datetime import date, timedelta


def month_data(start=date(2027, 1, 1), n=31, *, entries=True, watch=True):
    days = [start + timedelta(days=i) for i in range(n)]
    nd, es = [], []
    for i, d in enumerate(days):
        if i % 7 in (5, 6):
            nd.append({"day_date": d, "status": "assumed", "day_type": "wi",
                       "prefill_outcome": "no_plan", "prefill_note": None})
            continue
        nd.append({"day_date": d, "status": "corrected" if i % 3 == 0 else "assumed",
                   "day_type": "msp_work", "prefill_outcome": "planned", "prefill_note": None})
        if entries:
            for slot, k in (("breakfast", 400), ("lunch", 600), ("dinner", 700)):
                es.append({"day_date": d, "slot": slot, "description": f"Test {slot}",
                           "quantity": 1, "kcal": k, "protein_g": 40, "carb_g": 50,
                           "fat_g": 15, "fiber_g": 8, "source": "notion",
                           "confidence": "exact", "status": "assumed",
                           "is_placeholder": slot == "dinner", "source_detail": "label",
                           "portion": None})
    return {
        "start": days[0], "end": days[-1],
        "day_types": {d: ("wi" if i % 7 in (5, 6) else "msp_work") for i, d in enumerate(days)},
        "nutrition_days": nd, "entries": es,
        "target": {"kcal": 2000, "protein_g": 150, "set_by": "ryan", "provisional": True,
                   "effective_from": date(2026, 12, 1)},
        "weights_watch": ([{"local_date": d, "value": 300 - i * 0.2, "unit": "lb"}
                           for i, d in enumerate(days) if i % 2 == 0] if watch else []),
        "weights_checkin": [],
        "hourly": ([{"metric": "step_count", "local_date": d, "value": 8000 + i * 10}
                    for i, d in enumerate(days) if i != 4]
                   + [{"metric": "apple_exercise_time", "local_date": d, "value": 30 + i}
                      for i, d in enumerate(days)]) if watch else [],
        "active_energy": [{"local_date": d, "value": 500} for d in days] if watch else [],
        "sessions": {"planned": 20, "done": 12, "due": 16},
        "generated": date(2027, 2, 1),
    }
