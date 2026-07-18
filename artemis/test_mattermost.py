"""Tests for the Mattermost websocket client (STAB-1 A2/A3 + the ping hotfix).

MOCKED unit tests only — no network, no AWS/RDS. The client is built with
__new__ to skip __init__ (which reaches Secrets Manager).

Run:
    python3 -m artemis.test_mattermost
"""

import os
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

os.environ.setdefault("RDS_HOST", "test-host")
os.environ.setdefault("RDS_DB", "test-db")

from artemis.mattermost import MattermostClient, _backoff_delay  # noqa: E402


def _bare_client():
    c = MattermostClient.__new__(MattermostClient)
    c._ws = None
    c._ws_should_run = True
    c._reconnect_attempt = 0
    c._disconnected_at = None
    c._last_event_ts = time.time()
    return c


class TestWebsocketPing(unittest.TestCase):
    def test_run_forever_uses_ping_keepalive(self):
        client = _bare_client()
        fake_ws = MagicMock()

        def stop_after(**kw):
            client._ws_should_run = False  # exit the reconnect loop after one pass

        fake_ws.run_forever.side_effect = stop_after
        with patch("artemis.mattermost.websocket.WebSocketApp", return_value=fake_ws):
            client._run_ws_loop("wss://x/ws", MagicMock(), MagicMock(),
                                MagicMock(), MagicMock(), MagicMock())
        fake_ws.run_forever.assert_called_once_with(ping_interval=30, ping_timeout=10)


class TestReconnectBackoff(unittest.TestCase):
    def test_backoff_schedule(self):
        self.assertEqual([_backoff_delay(i) for i in range(7)],
                         [5, 10, 20, 40, 60, 60, 60])

    def test_reconnect_attempt_increments_across_failures(self):
        client = _bare_client()
        calls = {"n": 0}
        fake_ws = MagicMock()

        def run_forever(**kw):
            calls["n"] += 1
            if calls["n"] >= 3:
                client._ws_should_run = False  # stop after 3 failed connects

        fake_ws.run_forever.side_effect = run_forever
        with patch("artemis.mattermost.websocket.WebSocketApp", return_value=fake_ws), \
             patch("artemis.mattermost.time.sleep"):  # don't actually wait
            client._run_ws_loop("wss://x", MagicMock(), MagicMock(),
                                MagicMock(), MagicMock(), MagicMock())
        # 3 connect attempts = 2 retries scheduled between them (the 3rd stops).
        self.assertEqual(client._reconnect_attempt, 2)


class TestWatchdog(unittest.TestCase):
    def test_forces_close_when_stale(self):
        client = _bare_client()
        client._ws = MagicMock()
        client._last_event_ts = time.time() - 200  # >90s stale
        self.assertTrue(client.watchdog_check())
        client._ws.close.assert_called_once()

    def test_noop_when_fresh(self):
        client = _bare_client()
        client._ws = MagicMock()
        client._last_event_ts = time.time()
        self.assertFalse(client.watchdog_check())
        client._ws.close.assert_not_called()

    def test_noop_when_shutting_down(self):
        client = _bare_client()
        client._ws = MagicMock()
        client._ws_should_run = False
        client._last_event_ts = time.time() - 999
        self.assertFalse(client.watchdog_check())


class TestClose(unittest.TestCase):
    def test_close_stops_loop_and_closes_socket(self):
        client = _bare_client()
        client._ws = MagicMock()
        client.close()
        self.assertFalse(client._ws_should_run)
        client._ws.close.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
