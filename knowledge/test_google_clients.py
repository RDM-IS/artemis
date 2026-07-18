"""Tests for thread-local Google service objects (STAB-1 A1).

MOCKED only — googleapiclient.discovery.build is patched, no network/OAuth.

Run:
    python3 -m knowledge.test_google_clients
"""

import socket
import ssl
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from knowledge import google_clients  # noqa: E402


class TestThreadLocalServices(unittest.TestCase):
    def setUp(self):
        # Each build() call returns a distinct object so identity reveals sharing.
        self._counter = 0

        def fake_build(name, version, credentials=None, cache_discovery=None):
            self._counter += 1
            m = MagicMock(name=f"svc-{self._counter}")
            m._svc_id = self._counter
            return m

        self._patch = patch.object(google_clients, "build", side_effect=fake_build)
        self._patch.start()
        # Clear this thread's cache.
        google_clients._local.__dict__.clear()

    def tearDown(self):
        self._patch.stop()

    def test_same_thread_stable(self):
        a = google_clients.get_service("gmail", "v1", "creds")
        b = google_clients.get_service("gmail", "v1", "creds")
        self.assertIs(a, b)  # cached — one build per thread

    def test_two_threads_distinct(self):
        results = {}

        def grab(tag):
            results[tag] = google_clients.get_service("gmail", "v1", "creds")

        t1 = threading.Thread(target=grab, args=("a",))
        t2 = threading.Thread(target=grab, args=("b",))
        t1.start(); t2.start(); t1.join(); t2.join()
        self.assertIsNot(results["a"], results["b"])  # NEVER shared across threads
        self.assertNotEqual(results["a"]._svc_id, results["b"]._svc_id)

    def test_discard_forces_rebuild(self):
        a = google_clients.get_service("gmail", "v1", "creds")
        google_clients.discard("gmail", "v1")
        b = google_clients.get_service("gmail", "v1", "creds")
        self.assertIsNot(a, b)  # rebuilt after discard

    def test_distinct_apis_cached_separately(self):
        g = google_clients.get_service("gmail", "v1", "creds")
        c = google_clients.get_service("calendar", "v3", "creds")
        self.assertIsNot(g, c)
        self.assertEqual(google_clients._thread_service_count(), 2)


class TestExecuteHealing(unittest.TestCase):
    def setUp(self):
        self._patch = patch.object(
            google_clients, "build",
            side_effect=lambda *a, **k: MagicMock())
        self._patch.start()
        google_clients._local.__dict__.clear()

    def tearDown(self):
        self._patch.stop()

    def test_success_passthrough(self):
        req = MagicMock()
        req.execute.return_value = {"ok": True}
        self.assertEqual(
            google_clients.execute(req, name="gmail", version="v1"), {"ok": True})

    def test_ssl_error_discards_thread_service_and_reraises(self):
        google_clients.get_service("gmail", "v1", "creds")  # populate cache
        self.assertEqual(google_clients._thread_service_count(), 1)
        req = MagicMock()
        req.execute.side_effect = ssl.SSLError("record layer failure")
        with self.assertRaises(ssl.SSLError):
            google_clients.execute(req, name="gmail", version="v1")
        # discarded → next call rebuilds
        self.assertEqual(google_clients._thread_service_count(), 0)

    def test_socket_error_also_discards(self):
        google_clients.get_service("calendar", "v3", "creds")
        req = MagicMock()
        req.execute.side_effect = socket.error("broken")
        with self.assertRaises(socket.error):
            google_clients.execute(req, name="calendar", version="v3")
        self.assertEqual(google_clients._thread_service_count(), 0)

    def test_http_error_does_not_discard(self):
        # A real API error (HttpError) is a response, not a dead transport —
        # the cached service must survive.
        from googleapiclient.errors import HttpError
        google_clients.get_service("gmail", "v1", "creds")
        req = MagicMock()
        resp = MagicMock(status=404)
        req.execute.side_effect = HttpError(resp, b"not found")
        with self.assertRaises(HttpError):
            google_clients.execute(req, name="gmail", version="v1")
        self.assertEqual(google_clients._thread_service_count(), 1)  # survives


class TestClientsAreThreadLocal(unittest.TestCase):
    """Regression guard: GmailClient/CalendarClient expose `.service` as a
    thread-local property, not a shared attribute set once at auth."""

    def test_gmail_service_is_property(self):
        from artemis.gmail import GmailClient
        self.assertIsInstance(type(GmailClient).__dict__.get("service", None) or
                              GmailClient.__dict__.get("service"), property)

    def test_calendar_service_is_property(self):
        from artemis.calendar import CalendarClient
        self.assertIsInstance(CalendarClient.__dict__.get("service"), property)

    def test_no_shared_build_in_client_sources(self):
        import artemis.gmail as g
        import artemis.calendar as c
        for mod in (g, c):
            src = Path(mod.__file__).read_text()
            self.assertNotIn("self.service = build(", src)
            self.assertNotIn('build("gmail"', src.replace("google_clients", ""))


if __name__ == "__main__":
    unittest.main(verbosity=2)
