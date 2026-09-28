"""Dietitian report (VA MOVE!) — DIET-1 spec tests. Synthetic data only."""
import os as _os; _os.environ["ARTEMIS_TEST_NO_DB"] = "1"  # TEST-DB-GUARD: never a real DB
import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures"))
# Kept out of tests/*.py: CI runs pytest on every file there, and a helper
# with no tests exits 5 ("no tests ran") and fails the job.
from dietitian_data import month_data  # noqa: E402

from artemis import dietitian_report as dr  # noqa: E402

try:
    import weasyprint  # noqa: F401
    HAVE_WEASY = True
except Exception:          # pango missing in CI is a skip, not a failure
    HAVE_WEASY = False


def _text(report, detail="full"):
    return dr.visible_text(dr.render_html(report, detail))


class TestLanguageRule(unittest.TestCase):
    def test_no_judgment_words_in_any_rendering(self):
        for data in (month_data(), month_data(n=7), month_data(entries=False, watch=False)):
            for detail in ("full", "summary"):
                with self.subTest(n=len(data["day_types"]), detail=detail):
                    self.assertEqual(dr.banned_words_in(_text(dr.build(data), detail)), [])

    def test_the_scanner_catches_them(self):
        self.assertEqual(dr.banned_words_in("intake was under target"), ["under"])
        self.assertEqual(dr.banned_words_in("on track"), ["on track"])
        self.assertEqual(dr.banned_words_in("undefined overview"), [])   # words, not substrings


class TestCompleteness(unittest.TestCase):
    def test_recorded_and_unrecorded_by_reason(self):
        r = dr.build(month_data())
        self.assertEqual(r.n_recorded, 23)                 # 31 days, 8 off days with no plan
        self.assertEqual(r.unrecorded_by_reason, {"no plan": 8})
        self.assertIn("Days with intake recorded: 23 of 31", _text(r))

    def test_old_not_work_day_rows_read_as_non_work(self):
        data = month_data(n=7)
        for row in data["nutrition_days"]:
            if row["prefill_outcome"] == "no_plan":
                row["prefill_outcome"] = "not_work_day"
        self.assertEqual(dr.build(data).unrecorded_by_reason,
                         {"non-work day (not pre-filled)": 2})

    def test_empty_period(self):
        r = dr.build(month_data(n=7, entries=False, watch=False))
        t = _text(r)
        self.assertEqual(r.n_recorded, 0)
        self.assertIn("No intake recorded in this period", t)
        self.assertIn("No weigh-ins recorded", t)
        self.assertEqual(r.target_diff, {})


class TestAverages(unittest.TestCase):
    def test_over_recorded_days_with_signed_difference(self):
        r = dr.build(month_data())
        self.assertEqual(r.averages["kcal"]["mean"], 1700)
        self.assertEqual(r.target_diff["kcal"], -300)
        self.assertEqual(r.target_diff["protein_g"], -30)
        self.assertNotIn("fiber_g", r.target_diff)          # no target -> "—"
        self.assertIn("−300 kcal", _text(r))

    def test_the_menu_marks_what_the_patient_changed(self):
        data = month_data(n=7)
        for e in data["entries"]:
            if e["day_date"] == date(2027, 1, 1) and e["slot"] == "lunch":
                e["status"] = "corrected"
                e["description"] = "Test salad"
        r = dr.build(data)
        day1 = r.days[0]
        self.assertEqual([m.slot for m in day1.menu], ["breakfast", "lunch", "dinner"])
        self.assertEqual([m.changed for m in day1.menu], [False, True, False])
        self.assertEqual(r.n_changed_days, 1)
        t = _text(r)
        self.assertIn("Daily menu", t)
        self.assertIn("Test salad †", t)
        self.assertIn("Day total", t)

    def test_no_recording_status_label_anywhere(self):
        t = _text(dr.build(month_data(n=7)))
        self.assertNotIn("no correction received", t)
        self.assertNotIn("Recording status", t)

    def test_amounts(self):
        self.assertEqual(dr._amount({"source": "usda", "quantity": 0.85}), "85 g")
        self.assertEqual(dr._amount({"source": "saved", "quantity": 2, "portion": "1 large egg"}),
                         "2 × 1 large egg")
        self.assertEqual(dr._amount({"source": "notion", "quantity": 1, "portion": None}), "1 portion")

    def test_summary_has_no_menu(self):
        self.assertNotIn("Daily menu", _text(dr.build(month_data(n=7)), "summary"))

class TestBasis(unittest.TestCase):
    def test_shares_sum_to_100(self):
        r = dr.build(month_data())
        self.assertAlmostEqual(sum(r.basis_kcal.values()), 100.0, places=6)
        self.assertAlmostEqual(sum(r.basis_protein.values()), 100.0, places=6)
        self.assertAlmostEqual(r.basis_kcal["placeholder"], 700 / 1700 * 100, places=6)

    def test_classification_never_guesses(self):
        self.assertEqual(dr.macro_basis({"source": "usda"}), "USDA")
        self.assertEqual(dr.macro_basis({"source": "off"}), "Open Food Facts")
        self.assertEqual(dr.macro_basis({"source": "notion", "is_placeholder": True}), "placeholder")
        self.assertEqual(dr.macro_basis({"source": "notion",
                                         "source_detail": "recipe: computed from ingredient labels"}),
                         "label")
        self.assertEqual(dr.macro_basis({"source": "notion", "source_detail": "grandma"}),
                         "unclassified")


class TestWeight(unittest.TestCase):
    def test_watch_first_checkin_fallback_with_source(self):
        data = month_data(n=7)
        data["weights_checkin"] = [{"local_date": date(2027, 1, 2), "value": 305.0},
                                   {"local_date": date(2027, 1, 1), "value": 999.0}]
        r = dr.build(data)
        by = {d.day: d.weight for d in r.days}
        self.assertEqual(by[date(2027, 1, 1)], (300.0, "watch"))       # watch wins
        self.assertEqual(by[date(2027, 1, 2)], (305.0, "check-in"))    # fallback

    def test_kg_is_converted(self):
        data = month_data(n=1)
        data["weights_watch"] = [{"local_date": date(2027, 1, 1), "value": 100, "unit": "kg"}]
        self.assertEqual(dr.build(data).days[0].weight, (220.5, "watch"))

    def test_fewer_than_three_weighins_are_listed_not_averaged(self):
        data = month_data(n=7)
        data["weights_watch"] = data["weights_watch"][:2]
        r = dr.build(data)
        self.assertNotIn("avg7", r.weight)
        self.assertIn("values listed instead of averaged", _text(r))

    def test_seven_day_average(self):
        r = dr.build(month_data(n=7))
        self.assertEqual(r.weight["avg7_n"], 4)


class TestActivity(unittest.TestCase):
    def test_no_data_day_is_an_empty_slot_not_zero(self):
        r = dr.build(month_data(n=7))
        self.assertIsNone(r.days[4].steps)
        svg = dr.bar_chart_svg([(d.day, d.steps) for d in r.days], title="Daily steps")
        self.assertIn("no data", svg)
        self.assertIn('fill="none"', svg)

    def test_intensity_is_never_estimated(self):
        self.assertIn("Intensity breakdown not yet available", _text(dr.build(month_data(n=7))))

    def test_no_watch_data(self):
        r = dr.build(month_data(n=7, watch=False))
        self.assertIsNone(r.activity["steps_avg"])
        self.assertEqual(r.activity["steps_days"], 0)
        self.assertIn("(0 days with data)", _text(r))

    def test_no_net_energy_line(self):
        self.assertNotIn("net", _text(dr.build(month_data())).lower().split())


class TestTargetLine(unittest.TestCase):
    def test_variants(self):
        self.assertEqual(dr._target_line(None), "Reference target: none recorded")
        line = dr._target_line({"kcal": 2100, "protein_g": 175, "set_by": "ryan",
                                "provisional": True, "effective_from": date(2027, 1, 3)})
        self.assertEqual(line, "Reference target: 2,100 kcal / 175 g protein, provisional, "
                               "set by patient 2027-01-03, pending dietitian review")
        line = dr._target_line({"kcal": 2000, "protein_g": 160, "set_by": "joy",
                                "provisional": False, "effective_from": date(2027, 1, 3)})
        self.assertNotIn("pending", line)
        self.assertIn("set by dietitian", line)


class TestPeriods(unittest.TestCase):
    def test_period(self):
        self.assertEqual(dr.period("month", date(2027, 2, 14)), (date(2027, 2, 1), date(2027, 2, 28)))
        self.assertEqual(dr.period("week", date(2027, 2, 14)), (date(2027, 2, 8), date(2027, 2, 14)))
        self.assertEqual(dr.period("fortnight", date(2027, 2, 14))[0], date(2027, 2, 1))


@unittest.skipUnless(HAVE_WEASY, "WeasyPrint not installed")
class TestPages(unittest.TestCase):
    def test_a_31_day_month_is_summary_detail_then_menu(self):
        # Pages 1–2 as specced, then the menu: 23 recorded days of 3 items.
        n = dr.page_count(dr.render_html(dr.build(month_data()), "full"))
        self.assertGreaterEqual(n, 3)
        self.assertLessEqual(n, 6)

    def test_summary_is_one_page(self):
        self.assertEqual(dr.page_count(dr.render_html(dr.build(month_data()), "summary")), 1)

    def test_a_week_is_three_pages(self):
        self.assertEqual(dr.page_count(dr.render_html(dr.build(month_data(n=7)), "full")), 3)

    # ── PAGE BUDGET (approved 2026-09-28) ──────────────────────────────────
    # The budget is on the SPECCED report: summary = 1 page, full = 2. The Daily
    # menu is an appendix of any length. `page_count` counts everything and is
    # deliberately NOT the budget — it stopped being a limit check when the menu
    # was added on 2026-09-27 and nothing noticed, which is what this restores.

    def test_the_specced_full_report_is_always_two_pages(self):
        for n in (1, 7, 14, 28, 31):
            with self.subTest(days=n):
                r = dr.build(month_data(n=n))
                self.assertEqual(dr.specced_page_count(r, "full"), 2,
                                 f"{n} days: the specced report must stay at two pages")

    def test_the_specced_summary_is_always_one_page(self):
        for n in (1, 7, 14, 31):
            with self.subTest(days=n):
                self.assertEqual(
                    dr.specced_page_count(dr.build(month_data(n=n)), "summary"), 1)

    def test_the_appendix_is_what_grows_and_the_budget_does_not(self):
        """A month has more recorded days than a week, so more appendix pages —
        and the same two specced pages."""
        wk, mo = dr.build(month_data(n=7)), dr.build(month_data(n=31))
        self.assertGreater(dr.page_count(dr.render_html(mo, "full")),
                           dr.page_count(dr.render_html(wk, "full")))
        self.assertEqual(dr.specced_page_count(wk, "full"),
                         dr.specced_page_count(mo, "full"))

    def test_the_budget_measure_excludes_the_menu(self):
        r = dr.build(month_data(n=31))
        self.assertLess(dr.specced_page_count(r, "full"),
                        dr.page_count(dr.render_html(r, "full")))

    def test_a_period_with_nothing_recorded_has_no_appendix(self):
        r = dr.build(month_data(n=14, entries=False))
        self.assertEqual(dr.page_count(dr.render_html(r, "full")),
                         dr.specced_page_count(r, "full"))


if __name__ == "__main__":
    unittest.main()


class TestMissingEngine(unittest.TestCase):
    def test_a_missing_weasyprint_names_the_fix(self):
        import builtins
        from unittest import mock
        real = builtins.__import__

        def no_weasy(name, *a, **k):
            if name == "weasyprint":
                raise ImportError("No module named 'weasyprint'")
            return real(name, *a, **k)
        with mock.patch("builtins.__import__", side_effect=no_weasy):
            with self.assertRaises(dr.PdfEngineMissing) as caught:
                dr.render_pdf("<p>x</p>")
        self.assertIn("pip install weasyprint", str(caught.exception))
        self.assertIn("--html", str(caught.exception))
