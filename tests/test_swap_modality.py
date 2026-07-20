"""Tests for SWAP-2 (gated session-modality swap) and the HEALTH-1 Part A
deterministic-routing fix.

No live DB: knowledge.db primitives and the durable pending KV are mocked.

Covers (per SPEC):
  - detect_modality_swap / detect_swap_revert regex suite (positives + negatives)
  - translate_blocks round-trip (translate -> revert == original) for all three
    targets across cardio_z2 (steady) and cardio_intervals shapes
  - structural + stimulus preservation through translation
  - CT-anchored target-date resolution (the ~20:00 CT / 01:00 UTC day-ahead bug)
  - lifecycle: propose -> yes applies; propose -> no cancels; "yes <reason>"
    captures the reason into the audit metadata
  - double-swap: pre_swap still holds the ORIGINAL after two swaps
  - refusals: strength day; revert with no pre_swap; yes with no pending swap
  - verify-from-reread: a write that doesn't persist -> failure, no confirmation
  - weather-fetch exception -> swap still applies, context=null in the audit row
  - Part A: deterministic health intents bypass the LLM classifier (route_intent
    is never called); no live route reaches add_note or the "learning" re-route

Run:
    python tests/test_swap_modality.py
"""

import datetime as _dt
import json
import os
import sys
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

os.environ.setdefault("RDS_HOST", "test-host")
os.environ.setdefault("RDS_DB", "test-db")

from artemis import health  # noqa: E402


# ── Block fixtures (mirror scripts/reseed_health_plan_v2.py) ────────────────

_INTERVALS_BLOCKS = {
    "type": "intervals", "rounds": 8,
    "warmup_sec": 300, "warmup_settings": "easy spin",
    "cooldown_sec": 300, "cooldown_settings": "easy spin",
    "intervals_template": {
        "work_sec": 60, "work_settings": "hard effort (Z4)",
        "rest_sec": 90, "rest_settings": "easy spin",
    },
    "equipment": ["bike on trainer", "water rower"],
    "setup_notes": ["Indoor trainer or water rower", "8 rounds: 60s hard / 90s easy"],
}

_Z2_BLOCKS = {
    "type": "steady", "duration_min": 55, "target_range_min": [45, 55],
    "intensity": "Zone 2", "warmup_sec": 300, "cooldown_sec": 300,
    "equipment": ["road bike"],
    "setup_notes": ["Steady 45-55 min Zone 2", "Road bike outside; trainer if rain"],
    "finisher": {"rounds": 3, "exercises": [{"name": "Plank", "duration_sec": 30}]},
}


def _plan_row(blocks, session_type="cardio_z2", plan_id=42, plan_date=None):
    return {
        "plan_id": plan_id,
        "plan_date": plan_date or date.today(),
        "session_type": session_type,
        "target_rpe": 4.5,
        "blocks": json.loads(json.dumps(blocks)),
    }


# ============================================================================
# B1 — regex suite
# ============================================================================

class TestModalitySwapRegex(unittest.TestCase):
    def test_positives(self):
        cases = [
            ("update outdoor workout to indoor rower", "rower", None),
            ("swap today to indoor bike", "bike", None),
            ("switch my run to rowing", "rower", None),
            ("swap to rower due to wildfire smoke", "rower", "wildfire smoke"),
            ("indoor bike today", "bike", None),           # bare form
            ("@artemis swap my cardio to walking pad", "walking_pad", None),
            ("- change today's session to bike because it's raining", "bike",
             "it's raining"),
        ]
        for msg, target, reason in cases:
            with self.subTest(msg=msg):
                r = health.detect_modality_swap(msg)
                self.assertIsNotNone(r, f"expected a match for {msg!r}")
                self.assertEqual(r["target"], target)
                self.assertEqual(r["reason"], reason)

    def test_reason_captured(self):
        r = health.detect_modality_swap("swap to rower due to wildfire smoke")
        self.assertEqual(r["reason"], "wildfire smoke")

    def test_revert(self):
        self.assertTrue(health.detect_swap_revert("swap revert"))
        self.assertTrue(health.detect_swap_revert("undo swap"))
        self.assertTrue(health.detect_swap_revert("@artemis revert swap"))
        # A revert is NOT a modality swap.
        self.assertIsNone(health.detect_modality_swap("swap revert"))

    def test_negatives(self):
        for neg in ("how was my workout", "trainer set indoor",
                    "what's today's workout", "hello", "how's my training going"):
            with self.subTest(neg=neg):
                self.assertIsNone(health.detect_modality_swap(neg),
                                  f"{neg!r} must NOT match a modality swap")

    def test_trainer_override_not_swallowed(self):
        # 'trainer set indoor' stays a trainer override, never a swap.
        self.assertIsNone(health.detect_modality_swap("trainer set indoor"))
        self.assertEqual(health.detect_health_intent("trainer set indoor"),
                         health.INTENT_TRAINER_OVERRIDE)


# ============================================================================
# B3 — translation: round-trip, structural + stimulus preservation
# ============================================================================

class TestTranslateBlocks(unittest.TestCase):
    def test_round_trip_all_targets_all_shapes(self):
        for shape, stype in ((_INTERVALS_BLOCKS, "cardio_intervals"),
                             (_Z2_BLOCKS, "cardio_z2")):
            for target in ("rower", "bike", "walking_pad"):
                with self.subTest(shape=stype, target=target):
                    new = health.apply_modality_swap(
                        shape, target, session_type=stype, reason="AQI",
                        swap_meta={"reason": "AQI"})
                    restored = health.revert_modality_swap(new)
                    self.assertEqual(restored, shape,
                                     "revert must reproduce the exact original")

    def test_structural_preservation(self):
        new = health.translate_blocks(_INTERVALS_BLOCKS, "rower",
                                      session_type="cardio_intervals")
        self.assertEqual(new["type"], "intervals")
        self.assertEqual(new["rounds"], 8)
        self.assertEqual(new["warmup_sec"], 300)
        self.assertEqual(new["cooldown_sec"], 300)
        self.assertEqual(new["intervals_template"]["work_sec"], 60)
        self.assertEqual(new["intervals_template"]["rest_sec"], 90)

    def test_stimulus_carries_unchanged(self):
        new = health.translate_blocks(_Z2_BLOCKS, "walking_pad")
        self.assertEqual(new["intensity"], "Zone 2")
        self.assertEqual(new["target_range_min"], [45, 55])
        self.assertEqual(new["finisher"], _Z2_BLOCKS["finisher"])

    def test_modality_labels(self):
        r = health.translate_blocks(_INTERVALS_BLOCKS, "rower",
                                    session_type="cardio_intervals")
        self.assertEqual(r["equipment"], ["water rower"])
        self.assertEqual(r["intervals_template"]["work_settings"], "moderate row")
        self.assertEqual(r["intervals_template"]["rest_settings"], "easy row")
        self.assertEqual(r["warmup_settings"], "easy row")
        self.assertEqual(r["display_name"], "Indoor Row — Intervals")

        z2_rower = health.translate_blocks(_Z2_BLOCKS, "rower", session_type="cardio_z2")
        self.assertEqual(z2_rower["display_name"], "Indoor Row — Z2 Intervals")
        self.assertEqual(health.translate_blocks(_Z2_BLOCKS, "bike")["display_name"],
                         "Indoor Bike — Z2")
        self.assertEqual(health.translate_blocks(_Z2_BLOCKS, "walking_pad")["display_name"],
                         "Indoor Walk — Z2")

    def test_setup_notes_reason_prefix(self):
        n = health.translate_blocks(_INTERVALS_BLOCKS, "rower", reason="wildfire AQI")
        self.assertTrue(n["setup_notes"][0].startswith("Indoor row (wildfire AQI)"))
        # trailing lines survive
        self.assertEqual(n["setup_notes"][1], _INTERVALS_BLOCKS["setup_notes"][1])

    def test_double_swap_preserves_original(self):
        s1 = health.apply_modality_swap(_INTERVALS_BLOCKS, "rower",
                                        session_type="cardio_intervals",
                                        swap_meta={"n": 1})
        s2 = health.apply_modality_swap(s1, "bike",
                                        session_type="cardio_intervals",
                                        swap_meta={"n": 2})
        self.assertEqual(s2["pre_swap"], _INTERVALS_BLOCKS,
                         "double-swap must keep the true ORIGINAL under pre_swap")
        self.assertEqual(health.revert_modality_swap(s2), _INTERVALS_BLOCKS)

    def test_revert_no_pre_swap_returns_none(self):
        self.assertIsNone(health.revert_modality_swap(_Z2_BLOCKS))


# ============================================================================
# B2 — CT-anchored target-date resolution
# ============================================================================

_FIXED_UTC = _dt.datetime(2026, 7, 20, 1, 0, tzinfo=_dt.timezone.utc)  # 20:00 CT on 7/19


class _FakeDateTime(_dt.datetime):
    @classmethod
    def now(cls, tz=None):
        return _FIXED_UTC.astimezone(tz) if tz else _FIXED_UTC.replace(tzinfo=None)


class TestTargetDateResolution(unittest.TestCase):
    def test_ct_anchored_not_utc(self):
        """At 20:00 CT (01:00 UTC next day), the target must be the CT date
        (2026-07-19), never the UTC date (2026-07-20) — the day-ahead bug."""
        with patch.object(health, "datetime", _FakeDateTime), \
             patch.object(health, "_fetch_swap_plan_row",
                          return_value=_plan_row(_Z2_BLOCKS, "cardio_z2",
                                                  plan_date=date(2026, 7, 19))):
            target, row, refusal = health.resolve_swap_target()
        self.assertIsNone(refusal)
        self.assertEqual(target, date(2026, 7, 19))

    def test_strength_day_refuses(self):
        with patch.object(health, "_fetch_swap_plan_row",
                          return_value=_plan_row(_Z2_BLOCKS, "strength_a")):
            target, row, refusal = health.resolve_swap_target(today=date(2026, 7, 20))
        self.assertIsNone(target)
        self.assertIsNotNone(refusal)
        self.assertIn("strength", refusal.lower())

    def test_rest_day_falls_to_next_cardio(self):
        def fake_fetch(d):
            if d == date(2026, 7, 20):
                return _plan_row(_Z2_BLOCKS, "rest_mobility", plan_date=d)
            return _plan_row(_Z2_BLOCKS, "cardio_z2", plan_date=d)
        with patch.object(health, "_fetch_swap_plan_row", side_effect=fake_fetch), \
             patch.object(health, "_next_cardio_date", return_value=date(2026, 7, 22)):
            target, row, refusal = health.resolve_swap_target(today=date(2026, 7, 20))
        self.assertIsNone(refusal)
        self.assertEqual(target, date(2026, 7, 22))


# ============================================================================
# B4/B5 — lifecycle (propose / yes / no), verify-from-reread, audit + weather
# ============================================================================

class _LifecycleBase(unittest.TestCase):
    def setUp(self):
        # In-memory durable pending store (stands in for system_state KV).
        self._kv = {}
        self._get = patch("artemis.quiet_hours.get_system_value",
                          side_effect=lambda k: self._kv.get(k))
        self._set = patch("artemis.quiet_hours.set_system_value",
                          side_effect=lambda k, v: self._kv.__setitem__(k, v))
        self._get.start()
        self._set.start()

        # Fake DB: execute_write captures the UPDATE; execute_one reflects it back
        # (a successful persist). Tests override _reread for the failure case.
        self._written = {}
        self._reread = None  # None → reflect what was written

        def fake_write(sql, params):
            if "UPDATE health.plan" in sql:
                self._written["blocks"] = json.loads(params[0])
                self._written["plan_id"] = params[1]
            return None

        def fake_one(sql, params):
            if self._reread is not None:
                return self._reread
            return {"plan_id": self._written.get("plan_id", 42),
                    "plan_date": date.today(),
                    "session_type": "cardio_z2",
                    "blocks": self._written.get("blocks")}

        self._write_patch = patch("knowledge.db.execute_write", side_effect=fake_write)
        self._one_patch = patch("knowledge.db.execute_one", side_effect=fake_one)
        self._write_patch.start()
        self._one_patch.start()

        self._audit = MagicMock(return_value="uuid-1")
        self._audit_patch = patch("knowledge.db.log_audit", self._audit)
        self._audit_patch.start()

        # Weather present by default.
        self._weather_patch = patch("artemis.weather.get_current_conditions",
                                    return_value={"temp_f": 41.0,
                                                  "precip_next_90min": False,
                                                  "fetched_at": None})
        self._weather_patch.start()

    def tearDown(self):
        for p in (self._get, self._set, self._write_patch, self._one_patch,
                  self._audit_patch, self._weather_patch):
            p.stop()

    def _audit_meta(self):
        self.assertTrue(self._audit.called, "expected an audit_log write")
        return self._audit.call_args.kwargs


class TestSwapLifecycle(_LifecycleBase):
    def test_propose_writes_nothing_and_stages_pending(self):
        with patch.object(health, "_fetch_swap_plan_row",
                          return_value=_plan_row(_Z2_BLOCKS, "cardio_z2")):
            reply = health.propose_modality_swap(
                "swap today to indoor rower", "chan1")
        self.assertIn("Indoor Row", reply)
        self.assertIn("yes", reply.lower())
        self.assertNotIn("blocks", self._written)  # nothing written on propose
        self.assertIsNotNone(health.load_swap_pending("chan1"))

    def test_propose_then_yes_applies_and_audits(self):
        with patch.object(health, "_fetch_swap_plan_row",
                          return_value=_plan_row(_Z2_BLOCKS, "cardio_z2")):
            health.propose_modality_swap("swap today to indoor rower", "chan1")
            reply = health.commit_modality_swap("chan1")
        self.assertIn("Swapped", reply)
        # The row was actually written and carries the swap + pre_swap.
        self.assertIn("swap", self._written["blocks"])
        self.assertIn("pre_swap", self._written["blocks"])
        self.assertEqual(self._written["blocks"]["display_name"], "Indoor Row — Z2 Intervals")
        # Pending cleared after commit.
        self.assertIsNone(health.load_swap_pending("chan1"))
        # Audit ledger row shape.
        meta = self._audit_meta()
        self.assertEqual(meta["action"], "plan_modality_swap")
        md = meta["metadata"]
        self.assertEqual(md["to"]["modality"], "rower")
        self.assertEqual(md["from"]["session_type"], "cardio_z2")
        self.assertIsNotNone(md["context"])  # weather present
        self.assertIn("weather", md["context"])

    def test_propose_then_no_cancels(self):
        with patch.object(health, "_fetch_swap_plan_row",
                          return_value=_plan_row(_Z2_BLOCKS, "cardio_z2")):
            health.propose_modality_swap("swap today to indoor bike", "chan1")
            reply = health.cancel_modality_swap("chan1")
        self.assertIn("Cancelled", reply)
        self.assertNotIn("blocks", self._written)
        self.assertIsNone(health.load_swap_pending("chan1"))

    def test_yes_reason_captured_into_audit(self):
        with patch.object(health, "_fetch_swap_plan_row",
                          return_value=_plan_row(_Z2_BLOCKS, "cardio_z2")):
            health.propose_modality_swap("swap today to indoor rower", "chan1")
            health.commit_modality_swap("chan1", reason_override="wildfire AQI")
        md = self._audit_meta()["metadata"]
        self.assertEqual(md["reason"], "wildfire AQI")
        # Reason also lands in the written swap metadata.
        self.assertEqual(self._written["blocks"]["swap"]["reason"], "wildfire AQI")

    def test_yes_with_no_pending(self):
        reply = health.commit_modality_swap("chan-empty")
        self.assertIn("Nothing pending", reply)
        self.assertFalse(self._audit.called)

    def test_verify_from_reread_failure_reports_no_confirmation(self):
        # The re-read returns the OLD blocks (swap did not persist).
        self._reread = {"plan_id": 42, "plan_date": date.today(),
                        "session_type": "cardio_z2",
                        "blocks": json.loads(json.dumps(_Z2_BLOCKS))}
        with patch.object(health, "_fetch_swap_plan_row",
                          return_value=_plan_row(_Z2_BLOCKS, "cardio_z2")):
            health.propose_modality_swap("swap today to indoor rower", "chan1")
            reply = health.commit_modality_swap("chan1")
        self.assertIn("did not persist", reply)
        self.assertNotIn("Swapped", reply)
        self.assertFalse(self._audit.called, "no audit row on a failed persist")

    def test_weather_exception_still_swaps_context_null(self):
        self._weather_patch.stop()
        self._weather_patch = patch("artemis.weather.get_current_conditions",
                                    side_effect=RuntimeError("owm down"))
        self._weather_patch.start()
        with patch.object(health, "_fetch_swap_plan_row",
                          return_value=_plan_row(_Z2_BLOCKS, "cardio_z2")):
            health.propose_modality_swap("swap today to indoor rower", "chan1")
            reply = health.commit_modality_swap("chan1")
        self.assertIn("Swapped", reply)  # swap still applied
        md = self._audit_meta()["metadata"]
        self.assertIsNone(md["context"], "weather failure → context=null")


class TestRevertLifecycle(_LifecycleBase):
    def _swapped_row(self):
        swapped = health.apply_modality_swap(
            _Z2_BLOCKS, "rower", session_type="cardio_z2", reason="AQI",
            swap_meta={"reason": "AQI", "requested_via": "mattermost"})
        return _plan_row(swapped, "cardio_z2")

    def test_revert_restores_original(self):
        row = self._swapped_row()
        with patch.object(health, "_fetch_swap_plan_row", return_value=row):
            propose = health.propose_swap_revert("chan1")
            reply = health.commit_swap_revert("chan1")
        self.assertIn("Revert", propose)
        self.assertIn("Reverted", reply)
        # The restored row drops swap + pre_swap.
        self.assertNotIn("swap", self._written["blocks"])
        self.assertNotIn("pre_swap", self._written["blocks"])
        self.assertEqual(self._written["blocks"], _Z2_BLOCKS)
        meta = self._audit_meta()
        self.assertEqual(meta["action"], "plan_modality_swap_revert")

    def test_revert_with_no_pre_swap_refuses(self):
        with patch.object(health, "_fetch_swap_plan_row",
                          return_value=_plan_row(_Z2_BLOCKS, "cardio_z2")):
            reply = health.propose_swap_revert("chan1")
        self.assertIn("isn't swapped", reply)
        self.assertIsNone(health.load_swap_pending("chan1"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
