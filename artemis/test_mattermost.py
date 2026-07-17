"""Tests for the Mattermost websocket client.

MOCKED unit tests only — no network, no AWS/RDS. The client is built with
__new__ to skip __init__ (which reaches Secrets Manager), and the websocket
module is patched so nothing actually connects.

Run:
    python3 -m artemis.test_mattermost
    python3 artemis/test_mattermost.py
"""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

os.environ.setdefault("RDS_HOST", "test-host")
os.environ.setdefault("RDS_DB", "test-db")

from artemis.mattermost import MattermostClient  # noqa: E402


class TestWebsocketPing(unittest.TestCase):
    """run_forever must carry the heartbeat kwargs so a half-open connection is
    detected (ping every 30s, fail if no pong within 10s) instead of silently
    hanging until the next reconnect."""

    def test_run_forever_uses_ping_keepalive(self):
        # __new__ skips __init__ (which hits Secrets Manager); _connect_ws needs
        # no instance state.
        client = MattermostClient.__new__(MattermostClient)
        fake_ws = MagicMock()
        with patch("artemis.mattermost.websocket.WebSocketApp", return_value=fake_ws) as app:
            client._connect_ws(
                "wss://example/api/v4/websocket",
                MagicMock(), MagicMock(), MagicMock(), MagicMock(),
            )
        app.assert_called_once()
        fake_ws.run_forever.assert_called_once_with(ping_interval=30, ping_timeout=10)


if __name__ == "__main__":
    unittest.main(verbosity=2)
