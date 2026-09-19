"""HARDEN-1 item 5 — weather on the free OWM 2.5 endpoints.

Recorded fixtures (tests/fixtures/owm_*_2_5.json, captured 2026-09-16 for
West Bend, WI; they contain no key). No live HTTP: requests.get is mocked.

Run:
    python3.11 tests/test_weather.py
"""

import os as _os; _os.environ["ARTEMIS_TEST_NO_DB"] = "1"  # TEST-DB-GUARD: never a real DB

import json
import os
import sys
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

os.environ.setdefault("RDS_HOST", "test-host")
os.environ.setdefault("RDS_DB", "test-db")

import requests  # noqa: E402

from artemis import weather  # noqa: E402

FIX = _REPO_ROOT / "tests" / "fixtures"
FORECAST = json.loads((FIX / "owm_forecast_2_5.json").read_text())
CURRENT = json.loads((FIX / "owm_weather_2_5.json").read_text())
CT = ZoneInfo("America/Chicago")
FAKE_KEY = "0123456789abcdef0123456789abcdef"


def _resp(payload, status=200, url="https://api.openweathermap.org/x"):
    r = requests.Response()
    r.status_code = status
    r._content = json.dumps(payload).encode()
    r.url = url
    r.headers["content-type"] = "application/json"
    return r


def _router(forecast=FORECAST, current=CURRENT, status=200):
    calls = []

    def fake_get(url, params=None, timeout=None):
        calls.append((url, dict(params or {})))
        if url.endswith("/data/2.5/forecast"):
            return _resp(forecast, status, url + "?appid=" + params["appid"])
        if url.endswith("/data/2.5/weather"):
            return _resp(current, status, url + "?appid=" + params["appid"])
        raise AssertionError(f"unexpected URL {url}")

    return fake_get, calls


class TestSummarizeToday(unittest.TestCase):
    """Pure reduction of the recorded forecast."""

    def test_dry_day(self):
        s = weather.summarize_today(FORECAST, CT, date(2026, 9, 16))
        self.assertEqual(s, {"available": True, "high_f": 68, "low_f": 60,
                             "precip_chance": 0, "precip_in": 0.0,
                             "summary": "overcast clouds"})

    def test_light_rain_below_threshold_keeps_the_common_summary(self):
        # 9/17 has rain.3h in several slots but max pop 0.22.
        s = weather.summarize_today(FORECAST, CT, date(2026, 9, 17))
        self.assertEqual((s["high_f"], s["low_f"]), (71, 61))
        self.assertEqual(s["precip_chance"], 22)
        self.assertEqual(s["precip_in"], 0.09)   # summed rain.3h, mm -> in
        self.assertEqual(s["summary"], "overcast clouds")

    def test_likely_rain_uses_the_wettest_slot(self):
        s = weather.summarize_today(FORECAST, CT, date(2026, 9, 18))
        self.assertEqual(s["precip_chance"], 100)
        self.assertEqual(s["summary"], "light rain")
        self.assertEqual((s["high_f"], s["low_f"]), (69, 56))

    def test_high_low_are_max_min_of_slot_extremes(self):
        today = date(2026, 9, 16)
        slots = [x for x in FORECAST["list"]
                 if datetime.fromtimestamp(x["dt"], CT).date() == today]
        s = weather.summarize_today(FORECAST, CT, today)
        self.assertEqual(s["high_f"], round(max(x["main"]["temp_max"] for x in slots)))
        self.assertEqual(s["low_f"], round(min(x["main"]["temp_min"] for x in slots)))

    def test_date_outside_the_forecast_is_unavailable(self):
        s = weather.summarize_today(FORECAST, CT, date(2026, 9, 30))
        self.assertFalse(s["available"])

    def test_local_date_follows_the_given_timezone(self):
        # One slot at 04:00 UTC on 9/17 = 23:00 CT 9/16 but 01:00 São Paulo 9/17.
        slot_dt = int(datetime(2026, 9, 17, 4, 0, tzinfo=timezone.utc).timestamp())
        fc = {"list": [{"dt": slot_dt, "main": {"temp_min": 40, "temp_max": 90},
                        "pop": 0, "weather": [{"description": "clear sky"}]}]}
        ct = weather.summarize_today(fc, CT, date(2026, 9, 16))
        sp = weather.summarize_today(fc, ZoneInfo("America/Sao_Paulo"), date(2026, 9, 16))
        self.assertTrue(ct["available"])
        self.assertFalse(sp["available"])

    def test_snow_counts_toward_precip(self):
        slot_dt = int(datetime(2026, 12, 1, 18, 0, tzinfo=CT).timestamp())
        fc = {"list": [{"dt": slot_dt, "main": {"temp_min": 28, "temp_max": 30},
                        "pop": 0.9, "snow": {"3h": 25.4},
                        "weather": [{"description": "snow"}]}]}
        s = weather.summarize_today(fc, CT, date(2026, 12, 1))
        self.assertEqual(s["precip_in"], 1.0)
        self.assertEqual(s["summary"], "snow")


class TestPrecipSoon(unittest.TestCase):
    NOW = datetime.fromtimestamp(CURRENT["dt"], timezone.utc)

    def test_recorded_morning_is_dry(self):
        self.assertFalse(weather.precip_soon(CURRENT, FORECAST, self.NOW))

    def test_raining_now(self):
        cur = dict(CURRENT, weather=[{"main": "Rain", "description": "light rain"}])
        self.assertTrue(weather.precip_soon(cur, None, self.NOW))

    def test_wet_slot_within_three_hours(self):
        wet = next(s for s in FORECAST["list"] if (s.get("pop") or 0) >= 0.5)
        at = datetime.fromtimestamp(wet["dt"], timezone.utc)
        from datetime import timedelta
        self.assertTrue(weather.precip_soon(CURRENT, FORECAST, at - timedelta(hours=2)))
        self.assertFalse(weather.precip_soon(CURRENT, FORECAST, at - timedelta(hours=4)))


class TestEndpointsAndFallbacks(unittest.TestCase):
    def setUp(self):
        self._p = [
            patch.object(weather, "_get_api_key", return_value=FAKE_KEY),
            patch.object(weather, "_local_tz", return_value=CT),
            patch("artemis.quiet_hours.local_today", return_value=date(2026, 9, 16)),
            patch("artemis.quiet_hours.get_timezone_override", return_value=None),
        ]
        for p in self._p:
            p.start()

    def tearDown(self):
        for p in self._p:
            p.stop()

    def test_forecast_uses_the_free_endpoint_at_home(self):
        fake_get, calls = _router()
        with patch.object(weather.requests, "get", fake_get):
            f = weather.get_today_forecast()
        self.assertTrue(f["available"])
        self.assertEqual(f["high_f"], 68)
        self.assertEqual(len(calls), 1)
        url, params = calls[0]
        self.assertEqual(url, "https://api.openweathermap.org/data/2.5/forecast")
        self.assertNotIn("3.0", url)
        self.assertEqual((params["lat"], params["lon"]), (weather.HOME_LAT, weather.HOME_LON))
        self.assertEqual(params["units"], "imperial")

    def test_forecast_uses_the_override_city(self):
        fake_get, calls = _router()
        with patch("artemis.quiet_hours.get_timezone_override",
                   return_value={"timezone": "Europe/Paris", "city_name": "paris"}), \
             patch.object(weather, "geocode", return_value=(48.85, 2.35)) as geo, \
             patch.object(weather.requests, "get", fake_get):
            weather.get_today_forecast()
        geo.assert_called_once_with("paris")
        self.assertEqual((calls[0][1]["lat"], calls[0][1]["lon"]), (48.85, 2.35))

    def test_override_city_that_cannot_be_geocoded_omits_weather(self):
        fake_get, calls = _router()
        with patch("artemis.quiet_hours.get_timezone_override",
                   return_value={"timezone": "America/Sao_Paulo", "city_name": "brazil"}), \
             patch.object(weather, "geocode", return_value=None), \
             patch.object(weather.requests, "get", fake_get):
            f = weather.get_today_forecast()
        self.assertFalse(f["available"])
        self.assertEqual(calls, [])

    def test_explicit_coordinates_win(self):
        fake_get, calls = _router()
        with patch.object(weather.requests, "get", fake_get):
            weather.get_today_forecast(1.5, 2.5)
        self.assertEqual((calls[0][1]["lat"], calls[0][1]["lon"]), (1.5, 2.5))

    def test_http_401_falls_back(self):
        fake_get, _ = _router(status=401)
        with patch.object(weather.requests, "get", fake_get):
            self.assertFalse(weather.get_today_forecast()["available"])
            self.assertEqual(weather.get_current_conditions()["temp_f"], 50.0)

    def test_network_error_falls_back(self):
        with patch.object(weather.requests, "get",
                          side_effect=requests.ConnectionError("boom appid=" + FAKE_KEY)):
            self.assertFalse(weather.get_today_forecast()["available"])

    def test_current_conditions_from_free_endpoints(self):
        fake_get, calls = _router()
        with patch.object(weather.requests, "get", fake_get):
            c = weather.get_current_conditions()
        self.assertAlmostEqual(c["temp_f"], 59.85)
        self.assertIsInstance(c["precip_next_90min"], bool)
        self.assertIsNotNone(c["fetched_at"])
        self.assertEqual([u for u, _ in calls],
                         ["https://api.openweathermap.org/data/2.5/weather",
                          "https://api.openweathermap.org/data/2.5/forecast"])

    def test_no_key_means_no_request(self):
        with patch.object(weather, "_get_api_key", return_value=None), \
             patch.object(weather.requests, "get") as get:
            self.assertFalse(weather.get_today_forecast()["available"])
            self.assertEqual(weather.get_current_conditions()["temp_f"], 50.0)
        get.assert_not_called()


class TestWakeWeatherLine(unittest.TestCase):
    def test_line_renders_high_low_summary_and_precip(self):
        from artemis import wake
        f = {"available": True, "high_f": 69, "low_f": 56, "precip_chance": 100,
             "precip_in": 0.15, "summary": "light rain"}
        with patch.object(weather, "get_today_forecast", return_value=f):
            self.assertEqual(wake._weather_line(),
                             "Weather: 69°/56°F · light rain · 100% precip (0.15 in)")

    def test_dry_day_omits_the_amount(self):
        from artemis import wake
        f = {"available": True, "high_f": 68, "low_f": 60, "precip_chance": 0,
             "precip_in": 0.0, "summary": "overcast clouds"}
        with patch.object(weather, "get_today_forecast", return_value=f):
            self.assertEqual(wake._weather_line(),
                             "Weather: 68°/60°F · overcast clouds · 0% precip")

    def test_unavailable_omits_the_line(self):
        from artemis import wake
        with patch.object(weather, "get_today_forecast", return_value={"available": False}):
            self.assertIsNone(wake._weather_line())


if __name__ == "__main__":
    unittest.main(verbosity=2)
