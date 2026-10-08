"""HEALTH-EXPORT-1 — settled nutrition days to Apple Health, exactly once.

No database and no network: `build_plan` and the config parser are pure, and
the routes are exercised with the store functions patched. The SQL itself was
run against a scratch Postgres with migrations 040 + 054 applied before this
was committed (see the PR description for what that run covered); TEST-DB-GUARD
keeps a real database out of this suite.

PUBLIC-FIXTURES: every value is synthetic. Dates are in 2031; a slot is 1,000+
kcal, which no recipe in the plan is and which the code handles as ordinarily
as any other integer.
"""
import os as _os; _os.environ["ARTEMIS_TEST_NO_DB"] = "1"  # TEST-DB-GUARD

import os
import re
import sys
import unittest
from datetime import date, datetime, time
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("RDS_HOST", "test-host")
os.environ.setdefault("RDS_DB", "test-db")

from knowledge import health_export as hx  # noqa: E402

TZ = "America/Denver"            # not the home zone, so nothing passes by default
TODAY = date(2031, 3, 20)
TIMES = {"breakfast": "06:30", "lunch": "11:30", "snacks": "15:00", "dinner": "18:30"}


def cfg(**over):
    raw = {"enabled": True, "start_date": "2031-03-01",
           "include_unconfirmed": False, "slot_times": dict(TIMES)}
    raw.update(over)
    return hx.parse_config(raw)


def entry(d, slot, kcal=1100, protein=71.0, carb=82.0, fat=31.0, fiber=21.0,
          status="corrected"):
    return {"day_date": d, "day_status": status, "slot": slot, "kcal": kcal,
            "protein_g": protein, "carb_g": carb, "fat_g": fat, "fiber_g": fiber}


def plan(entries, exported=(), **kw):
    c = kw.pop("config", None) or cfg()
    return hx.build_plan(c, today=kw.pop("today", TODAY), tz=TZ, entries=entries,
                         exported_days=set(exported), **kw)


class TestOneFactTwoPlaces(unittest.TestCase):
    """knowledge/ cannot import artemis/ (PACKAGE-IDENTITY), so the slot list
    and the correction window are written twice. This is what stops the copies
    drifting apart silently."""

    def test_slots_and_window_match_artemis_nutrition(self):
        from artemis import nutrition
        self.assertEqual(hx.SLOTS, nutrition.SLOTS)
        self.assertEqual(hx.CORRECTION_WINDOW_HOURS, nutrition.CORRECTION_WINDOW_HOURS)
        self.assertEqual(hx.DAY_CORRECTED, nutrition.DAY_CORRECTED)

    def test_last_closed_day_agrees_with_the_real_deadline(self):
        """The export's "closed" must be the tracker's "locked": day d is
        offered exactly when artemis.nutrition would refuse a correction to it.
        Checked a minute either side of the boundary, and across both DST
        changes, where "48 hours" and "two midnights" stop being the same."""
        from datetime import timedelta
        from zoneinfo import ZoneInfo
        from artemis import nutrition
        zone = ZoneInfo("America/Chicago")
        for d in (date(2031, 3, 7), date(2031, 3, 8),      # spring forward 3/9
                  date(2031, 10, 31), date(2031, 11, 1),   # fall back 11/2
                  date(2031, 6, 10)):
            deadline = datetime.combine(d + timedelta(days=3), time(0, 0), tzinfo=zone)
            with patch("artemis.quiet_hours.local_tz", return_value=zone):
                for now, closed in ((deadline - timedelta(minutes=1), False),
                                    (deadline + timedelta(minutes=1), True)):
                    with self.subTest(day=d, closed=closed):
                        self.assertEqual(not nutrition.is_open(d, now=now), closed)
                        self.assertEqual(d <= hx.last_closed_day(now.date()), closed)

    def test_migration_checks_name_the_module_constants(self):
        sql = (ROOT / "migrations" / "054_health_export.sql").read_text()
        slots = re.search(r"health_export_slot_known\s+CHECK \(slot IN \(([^)]*)\)", sql)
        states = re.search(r"health_export_state_known\s+CHECK \(state IN \(([^)]*)\)", sql)
        self.assertEqual(set(re.findall(r"'(\w+)'", slots.group(1))), set(hx.SLOTS))
        self.assertEqual(set(re.findall(r"'(\w+)'", states.group(1))),
                         {hx.STATE_CLAIMED, hx.STATE_SENT})

    def test_migration_ships_the_export_switched_off(self):
        sql = (ROOT / "migrations" / "054_health_export.sql").read_text()
        self.assertIn("""('health_export', '{"enabled": false}'::jsonb)""", sql)


class TestConfigFailsClosed(unittest.TestCase):
    def test_no_row_is_off_and_not_a_problem(self):
        c = hx.parse_config(None)
        self.assertFalse(c.enabled)
        self.assertEqual(c.problems, [])
        self.assertFalse(c.usable)

    def test_only_json_true_enables(self):
        for v in ("true", "on", "yes", 1, "1", None):
            with self.subTest(v=v):
                self.assertFalse(hx.parse_config({"enabled": v}).enabled)
        self.assertTrue(hx.parse_config({"enabled": True}).enabled)

    def test_enabled_alone_is_not_usable(self):
        """No default start date and no default meal times: both are Ryan's."""
        c = hx.parse_config({"enabled": True})
        self.assertFalse(c.usable)
        self.assertEqual(len(c.problems), 2)

    def test_every_slot_needs_a_time(self):
        t = dict(TIMES); del t["snacks"]
        c = cfg(slot_times=t)
        self.assertFalse(c.usable)
        self.assertIn("slot_times.snacks", c.problems[0])

    def test_bad_times_and_dates_are_problems(self):
        for bad in ("7:00", "0700", "25:00", "noon", 700, ""):
            with self.subTest(time=bad):
                self.assertFalse(cfg(slot_times=dict(TIMES, lunch=bad)).usable)
        for bad in ("03/01/2031", "2031-13-01", 20310301, None):
            with self.subTest(date=bad):
                self.assertFalse(cfg(start_date=bad).usable)

    def test_a_misspelt_slot_is_a_problem_not_an_ignored_setting(self):
        c = cfg(slot_times=dict(TIMES, snack="15:00"))
        self.assertFalse(c.usable)
        self.assertIn("unknown slots: snack", c.problems[0])

    def test_include_unconfirmed_defaults_off_and_must_be_boolean(self):
        raw = {"enabled": True, "start_date": "2031-03-01", "slot_times": dict(TIMES)}
        self.assertFalse(hx.parse_config(raw).include_unconfirmed)
        self.assertFalse(cfg(include_unconfirmed="yes").usable)

    def test_an_unusable_config_plans_nothing(self):
        p = plan([entry(date(2031, 3, 10), "lunch")],
                 config=hx.parse_config({"enabled": True}))
        self.assertEqual((p.items, p.withheld), ([], []))


class TestOnlySettledDays(unittest.TestCase):
    def test_last_closed_day_is_three_days_back(self):
        self.assertEqual(hx.last_closed_day(date(2031, 3, 20)), date(2031, 3, 17))

    def test_a_day_inside_the_window_is_not_offered(self):
        days = [date(2031, 3, d) for d in (16, 17, 18, 19, 20)]
        p = plan([entry(d, "lunch") for d in days])
        self.assertEqual(p.days, [date(2031, 3, 16), date(2031, 3, 17)])

    def test_nothing_before_the_start_date(self):
        p = plan([entry(date(2031, 2, 28), "lunch"), entry(date(2031, 3, 1), "lunch")])
        self.assertEqual(p.days, [date(2031, 3, 1)])


class TestWholeDayOrNothing(unittest.TestCase):
    def test_a_null_macro_withholds_the_whole_day(self):
        d = date(2031, 3, 10)
        p = plan([entry(d, "breakfast"), entry(d, "lunch", fiber=None),
                  entry(d, "dinner", fat=None)])
        self.assertEqual(p.items, [])
        self.assertEqual(p.withheld[0]["reason"], "missing_macros")
        self.assertIn("fat_g, fiber_g", p.withheld[0]["detail"])

    def test_zero_is_a_value_and_null_is_not(self):
        d = date(2031, 3, 10)
        p = plan([entry(d, "lunch", fiber=0)])
        self.assertEqual(len(p.items), 1)
        self.assertEqual(p.items[0].fiber_g, 0.0)

    def test_an_unconfirmed_day_is_withheld_unless_asked_for(self):
        d = date(2031, 3, 10)
        for status in ("locked_unconfirmed", "assumed"):
            with self.subTest(status=status):
                rows = [entry(d, "lunch", status=status)]
                p = plan(rows)
                self.assertEqual(p.items, [])
                self.assertEqual(p.withheld[0]["reason"], "unconfirmed")
                p = plan(rows, config=cfg(include_unconfirmed=True))
                self.assertEqual(len(p.items), 1)
                self.assertEqual(p.items[0].day_status, status)

    def test_an_exported_day_is_never_offered_again_even_in_part(self):
        """One slot of the day is in the ledger; a different slot has entries.
        Offering that slot would add to a day Health already holds."""
        d = date(2031, 3, 10)
        p = plan([entry(d, "lunch"), entry(d, "dinner")], exported=[d])
        self.assertEqual((p.items, p.withheld), ([], []))

    def test_an_unknown_slot_withholds_the_day(self):
        p = plan([entry(date(2031, 3, 10), "dessert")])
        self.assertEqual(p.items, [])
        self.assertEqual(p.withheld[0]["reason"], "unknown_slot")


class TestTheItems(unittest.TestCase):
    def test_a_slot_is_the_sum_of_its_entries(self):
        d = date(2031, 3, 10)
        p = plan([entry(d, "snacks", 1111, 71.25, 82.5, 31.1, 21),
                  entry(d, "snacks", 1222, 33, 4.04, 2, 0)])
        (it,) = p.items
        self.assertEqual((it.kcal, it.protein_g, it.carb_g, it.fat_g, it.fiber_g,
                          it.n_entries), (2333, 104.3, 86.5, 33.1, 21.0, 2))

    def test_grams_round_half_up_not_half_even(self):
        (it,) = plan([entry(date(2031, 3, 10), "lunch", protein=104.25, carb=104.35)]).items
        self.assertEqual((it.protein_g, it.carb_g), (104.3, 104.4))

    def test_an_all_zero_slot_is_dropped_but_does_not_withhold_the_day(self):
        d = date(2031, 3, 10)
        p = plan([entry(d, "breakfast", 0, 0, 0, 0, 0), entry(d, "lunch")])
        self.assertEqual([i.slot for i in p.items], ["lunch"])
        self.assertEqual(p.withheld, [])

    def test_the_timestamp_is_the_slot_time_in_the_active_zone(self):
        (it,) = plan([entry(date(2031, 3, 10), "dinner")]).items
        self.assertEqual(it.as_json()["at"], "2031-03-10T18:30:00-06:00")   # MDT

    def test_the_offset_follows_the_date_not_today(self):
        """3/5 is before the 2031 spring-forward and 3/10 after it."""
        p = plan([entry(date(2031, 3, 5), "lunch"), entry(date(2031, 3, 10), "lunch")])
        self.assertEqual([i.as_json()["at"][-6:] for i in p.items], ["-07:00", "-06:00"])

    def test_oldest_day_first_and_slots_in_a_stable_order(self):
        a, b = date(2031, 3, 9), date(2031, 3, 10)
        p = plan([entry(b, "dinner"), entry(a, "snacks"), entry(b, "breakfast"),
                  entry(a, "lunch")])
        self.assertEqual([(i.day, i.slot) for i in p.items],
                         [(a, "lunch"), (a, "snacks"), (b, "breakfast"), (b, "dinner")])

    def test_json_carries_no_food_names_and_no_status(self):
        (it,) = plan([entry(date(2031, 3, 10), "lunch")]).items
        self.assertEqual(set(it.as_json()),
                         {"day", "slot", "at", "kcal", "protein_g", "carb_g",
                          "fat_g", "fiber_g"})


class TestBatching(unittest.TestCase):
    def test_max_days_takes_the_oldest_and_counts_the_rest(self):
        days = [date(2031, 3, d) for d in range(2, 12)]
        p = plan([entry(d, "lunch") for d in days], max_days=4)
        self.assertEqual(p.days, days[:4])
        self.assertEqual(p.remaining_days, 6)

    def test_a_withheld_day_does_not_use_up_the_batch(self):
        days = [date(2031, 3, d) for d in range(2, 8)]
        rows = [entry(d, "lunch", status="assumed" if d.day < 5 else "corrected")
                for d in days]
        p = plan(rows, max_days=3)
        self.assertEqual(p.days, days[3:])
        self.assertEqual(len(p.withheld), 3)
        self.assertEqual(p.remaining_days, 0)


class _Fetch:
    """Records statements and answers them from a queue."""
    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, sql, params=()):
        self.calls.append((" ".join(sql.split()), tuple(params)))
        return self.answers.pop(0) if self.answers else []


class TestTheLedger(unittest.TestCase):
    def test_claim_refuses_while_a_batch_is_open(self):
        f = _Fetch([{"batch_id": "b", "day_date": date(2031, 3, 9), "slot": "lunch",
                     "claimed_at": datetime(2031, 3, 12, 8, 0)}])
        p = plan([entry(date(2031, 3, 10), "lunch")])
        with self.assertRaises(hx.ExportError):
            hx.claim(f, p)
        self.assertEqual(len(f.calls), 1)          # the read only — no INSERT

    def test_claim_writes_one_claimed_row_per_item_under_one_batch(self):
        f = _Fetch([])
        d = date(2031, 3, 10)
        p = plan([entry(d, "lunch"), entry(d, "dinner")])
        batch = hx.claim(f, p)
        inserts = [c for c in f.calls if c[0].startswith("INSERT")]
        self.assertEqual(len(inserts), 2)
        self.assertEqual({c[1][-2] for c in inserts}, {batch})
        self.assertEqual({c[1][-1] for c in inserts}, {hx.STATE_CLAIMED})

    def test_an_empty_plan_claims_nothing(self):
        f = _Fetch()
        self.assertIsNone(hx.claim(f, hx.Plan()))
        self.assertEqual(f.calls, [])

    def test_ack_is_idempotent(self):
        self.assertEqual(hx.ack(_Fetch([{"id": 1}, {"id": 2}]), "b"),
                         {"acked": 2, "already": False})
        self.assertEqual(hx.ack(_Fetch([], [{"n": 2}]), "b"),
                         {"acked": 2, "already": True})
        with self.assertRaises(hx.ExportError):
            hx.ack(_Fetch([], [{"n": 0}]), "b")

    def test_release_only_touches_claimed_rows(self):
        f = _Fetch([{"id": 1}])
        self.assertEqual(hx.release(f, "b"), 1)
        self.assertIn("DELETE", f.calls[0][0])
        self.assertEqual(f.calls[0][1], ("b", hx.STATE_CLAIMED))

    def test_a_sent_batch_cannot_be_released(self):
        with self.assertRaisesRegex(hx.ExportError, "already confirmed"):
            hx.release(_Fetch([], [{"n": 3}]), "b")

    def test_no_statement_interpolates_a_value(self):
        """`%s` + params, never string-built SQL (CLAUDE.md)."""
        src = (ROOT / "knowledge" / "health_export.py").read_text()
        self.assertNotRegex(src, r'fetch\(\s*f"')
        self.assertNotIn("% (", src)
        self.assertNotIn(".format(", src)


class TestRoutes(unittest.TestCase):
    KEY = "test-export-key"

    @classmethod
    def setUpClass(cls):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from api.app.database import get_db
        from api.app.routers import nutrition_export as nx
        cls.nx = nx

        class _Db:
            def commit(self): cls.commits += 1
            def rollback(self): pass
            def connection(self): raise AssertionError("no database in this test")

        app = FastAPI()
        app.include_router(nx.router, prefix="/api/nutrition/export")
        app.dependency_overrides[get_db] = lambda: _Db()
        cls.client = TestClient(app)

    def setUp(self):
        type(self).commits = 0
        self.nx._EXPORT_KEY = self.KEY
        self.h = {"X-API-Key": self.KEY}
        p = patch.object(self.nx, "_today_and_tz", return_value=(TODAY, TZ))
        p.start(); self.addCleanup(p.stop)

    def claim(self, config, *, open_batch=None, the_plan=None):
        with patch.object(hx, "load_config", return_value=config), \
             patch.object(hx, "open_batch", return_value=open_batch), \
             patch.object(hx, "plan_for", return_value=the_plan or hx.Plan()), \
             patch.object(hx, "claim", return_value="11111111-1111-4111-8111-111111111111") as c:
            r = self.client.post("/api/nutrition/export/claim", headers=self.h)
        return r, c

    def test_every_route_needs_the_export_key(self):
        for method, path in (("post", "claim"), ("post", "ack"), ("post", "release"),
                             ("get", "status")):
            for headers in ({}, {"X-API-Key": "the-display-key"}):
                with self.subTest(path=path, headers=headers):
                    r = getattr(self.client, method)(f"/api/nutrition/export/{path}",
                                                     headers=headers)
                    self.assertEqual(r.status_code, 401)

    def test_the_export_key_is_its_own_secret(self):
        """Not the display key and not the watch key (HARDEN-1)."""
        src = (ROOT / "api" / "app" / "routers" / "nutrition_export.py").read_text()
        self.assertIn("get_health_export_key", src)
        self.assertNotIn("get_health_api_key", src)
        self.assertNotIn("get_watch_ingest_key", src)
        self.assertNotIn("verify_health_api_key", src)
        secrets = (ROOT / "knowledge" / "secrets.py").read_text()
        self.assertIn('get_secret("rdmis/dev/health-export-key")', secrets)

    def test_disabled(self):
        r, c = self.claim(hx.parse_config({"enabled": False}))
        self.assertEqual(r.json()["status"], "disabled")
        c.assert_not_called()

    def test_misconfigured_names_the_problems(self):
        r, c = self.claim(hx.parse_config({"enabled": True}))
        body = r.json()
        self.assertEqual(body["status"], "misconfigured")
        self.assertEqual(len(body["problems"]), 2)
        c.assert_not_called()

    def test_stuck_hands_out_nothing_new(self):
        ob = {"batch_id": "22222222-2222-4222-8222-222222222222",
              "days": ["2031-03-09", "2031-03-10"], "items": 5}
        r, c = self.claim(cfg(), open_batch=ob,
                          the_plan=plan([entry(date(2031, 3, 11), "lunch")]))
        body = r.json()
        self.assertEqual((body["status"], body["items"], body["batch_id"]),
                         ("stuck", [], ob["batch_id"]))
        self.assertIn("2031-03-09 to 2031-03-10 (2 days)", body["message"])
        c.assert_not_called()
        self.assertEqual(self.commits, 0)

    def test_empty_reports_what_is_held_back(self):
        p = plan([entry(date(2031, 3, 10), "lunch", status="assumed")])
        r, c = self.claim(cfg(), the_plan=p)
        body = r.json()
        self.assertEqual(body["status"], "empty")
        self.assertEqual(body["withheld"][0]["reason"], "unconfirmed")
        self.assertIn("1 settled day(s) are being held back", body["message"])
        c.assert_not_called()

    def test_ok_returns_items_and_commits_the_claim(self):
        p = plan([entry(date(2031, 3, 10), "lunch")])
        r, c = self.claim(cfg(), the_plan=p)
        body = r.json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["items"][0]["at"], "2031-03-10T11:30:00-06:00")
        self.assertEqual(body["batch_id"], "11111111-1111-4111-8111-111111111111")
        c.assert_called_once()
        self.assertEqual(self.commits, 1)

    def test_the_claim_message_never_says_sent(self):
        """Nothing is in Health until the Shortcut writes it and acks."""
        r, _ = self.claim(cfg(), the_plan=plan([entry(date(2031, 3, 10), "lunch")]))
        self.assertNotRegex(r.json()["message"].lower(), r"\bsent\b|\bwritten\b|\bsaved\b")

    def test_every_claim_reply_has_a_status_and_a_message(self):
        """The Shortcut branches on one and shows the other; neither may be absent."""
        ob = {"batch_id": "x" * 36, "days": ["2031-03-09"], "items": 1}
        for config, kw in ((hx.parse_config(None), {}),
                           (hx.parse_config({"enabled": True}), {}),
                           (cfg(), {"open_batch": ob}), (cfg(), {}),
                           (cfg(), {"the_plan": plan([entry(date(2031, 3, 10), "lunch")])})):
            body = self.claim(config, **kw)[0].json()
            with self.subTest(status=body.get("status")):
                self.assertIn(body["status"],
                              ("ok", "empty", "stuck", "disabled", "misconfigured"))
                self.assertTrue(body["message"])
                self.assertIsInstance(body["items"], list)

    def test_a_malformed_batch_id_is_refused_before_the_database(self):
        for path in ("ack", "release"):
            r = self.client.post(f"/api/nutrition/export/{path}", headers=self.h,
                                 json={"batch_id": "not-a-uuid"})
            self.assertEqual(r.status_code, 422)

    def test_ack_and_release_map_refusals(self):
        b = {"batch_id": "33333333-3333-4333-8333-333333333333"}
        with patch.object(hx, "ack", side_effect=hx.ExportError("no such batch"), __name__="ack"):
            self.assertEqual(self.client.post("/api/nutrition/export/ack",
                                              headers=self.h, json=b).status_code, 404)
        with patch.object(hx, "release", __name__="release", side_effect=hx.ExportError(
                "that batch was already confirmed as written; it cannot be released")):
            self.assertEqual(self.client.post("/api/nutrition/export/release",
                                              headers=self.h, json=b).status_code, 409)


if __name__ == "__main__":
    unittest.main()
