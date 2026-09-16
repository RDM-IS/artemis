"""HARDEN-1 item 3 — no API key ever reaches the log output.

Run:
    python3.11 tests/test_log_redaction.py
"""

import io
import json
import logging
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

os.environ.setdefault("RDS_HOST", "test-host")
os.environ.setdefault("RDS_DB", "test-db")

import requests  # noqa: E402

from artemis import weather  # noqa: E402
from artemis.log_redaction import RedactingFilter, install, redact_secrets  # noqa: E402

KEY = "9f3b0c1d2e3f4a5b6c7d8e9f0a1b2c3d"
URL = ("https://api.openweathermap.org/data/3.0/onecall?lat=43.4&lon=-88.1"
       f"&appid={KEY}&units=imperial")


def _http_error(status=401):
    resp = requests.Response()
    resp.status_code = status
    resp.url = URL
    resp.reason = "Unauthorized"
    resp._content = b'{"cod":401}'
    try:
        resp.raise_for_status()
    except requests.HTTPError as exc:
        return exc
    raise AssertionError("raise_for_status did not raise")


class Capture:
    """A root-level stream handler with the process-wide install() applied."""

    def __enter__(self):
        self.buf = io.StringIO()
        self.handler = logging.StreamHandler(self.buf)
        self.handler.setFormatter(logging.Formatter("%(name)s %(levelname)s: %(message)s"))
        self.root = logging.getLogger()
        self.root.addHandler(self.handler)
        self._level = self.root.level
        self.root.setLevel(logging.DEBUG)
        install()
        return self

    def __exit__(self, *exc):
        self.root.removeHandler(self.handler)
        self.root.setLevel(self._level)

    @property
    def text(self):
        return self.buf.getvalue()


class TestRedactSecrets(unittest.TestCase):
    def test_query_parameters(self):
        out = redact_secrets(URL)
        self.assertNotIn(KEY, out)
        self.assertIn("appid=REDACTED", out)
        self.assertIn("lat=43.4", out)
        self.assertIn("units=imperial", out)

    def test_other_credential_names(self):
        for name in ("key", "api_key", "apikey", "API_KEY", "access_token", "token", "password"):
            with self.subTest(name=name):
                out = redact_secrets(f"https://x.example/p?{name}={KEY}&q=1")
                self.assertNotIn(KEY, out)
                self.assertIn("q=1", out)

    def test_words_ending_in_key_are_left_alone(self):
        self.assertEqual(redact_secrets("monkey=banana turnkey=yes"),
                         "monkey=banana turnkey=yes")

    def test_empty(self):
        self.assertEqual(redact_secrets(""), "")


class TestRaisedHttpErrorIsRedacted(unittest.TestCase):
    def test_the_real_requests_message_contains_the_key(self):
        # Guard the premise: without redaction this line WOULD leak.
        self.assertIn(KEY, str(_http_error()))

    def test_logger_exception_traceback_has_no_key(self):
        log = logging.getLogger("artemis.weather.test")
        with Capture() as cap:
            try:
                raise _http_error()
            except requests.HTTPError:
                log.exception("OpenWeatherMap daily fetch failed")
        self.assertIn("Traceback", cap.text)
        self.assertIn("401 Client Error", cap.text)
        self.assertNotIn(KEY, cap.text)
        self.assertIn("appid=REDACTED", cap.text)

    def test_formatted_message_args_are_redacted(self):
        log = logging.getLogger("artemis.any")
        with Capture() as cap:
            log.warning("fetch failed: %s", _http_error())
            log.error("url was %s", URL)
        self.assertNotIn(KEY, cap.text)

    def test_filter_is_attached_to_handlers_once(self):
        with Capture() as cap:
            install()
            install()
            n = sum(isinstance(f, RedactingFilter) for f in cap.handler.filters)
        self.assertEqual(n, 1)

    def test_child_logger_records_are_redacted(self):
        """Logger-level filters don't see propagated records — handler-level do."""
        with Capture() as cap:
            logging.getLogger("apscheduler.executors.default").error("job failed %s", URL)
        self.assertNotIn(KEY, cap.text)


class TestWeatherModuleNeverLogsTheKey(unittest.TestCase):
    """Defense in depth: weather.py itself must not emit the URL, even with
    the filter absent."""

    def _run_without_filter(self, fn):
        buf = io.StringIO()
        handler = logging.StreamHandler(buf)
        log = logging.getLogger("artemis.weather")
        log.addHandler(handler)
        old = log.level
        log.setLevel(logging.DEBUG)
        try:
            with patch.object(weather, "_get_api_key", return_value=KEY), \
                 patch("artemis.quiet_hours.get_timezone_override", return_value=None):
                fn()
        finally:
            log.removeHandler(handler)
            log.setLevel(old)
        return buf.getvalue()

    def test_http_401(self):
        def fake_get(url, params=None, timeout=None):
            r = requests.Response()
            r.status_code = 401
            r.url = f"{url}?appid={params['appid']}"
            r._content = json.dumps({"cod": 401}).encode()
            return r

        with patch.object(weather.requests, "get", fake_get):
            out = self._run_without_filter(lambda: (weather.get_today_forecast(),
                                                    weather.get_current_conditions()))
        self.assertIn("HTTP 401", out)
        self.assertNotIn(KEY, out)
        self.assertNotIn("appid", out)

    def test_connection_error_whose_text_contains_the_key(self):
        with patch.object(weather.requests, "get",
                          side_effect=requests.ConnectionError(f"Max retries exceeded url: {URL}")):
            out = self._run_without_filter(weather.get_today_forecast)
        self.assertIn("ConnectionError", out)
        self.assertNotIn(KEY, out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
