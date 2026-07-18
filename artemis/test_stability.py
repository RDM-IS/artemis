"""STAB-1 Part A orchestration tests that live in artemis.main (SIGTERM cleanup).

Skips when artemis.main can't import (flask absent locally; present on the box/CI).

Run:
    python3 -m artemis.test_stability
"""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("RDS_HOST", "test-host")
os.environ.setdefault("RDS_DB", "test-db")

try:
    import artemis.main as main
    _MAIN_OK = True
except Exception:
    main = None
    _MAIN_OK = False


@unittest.skipUnless(_MAIN_OK, "artemis.main (flask) unavailable")
class TestShutdownCleanup(unittest.TestCase):
    """A3: shutdown must never wait on running jobs and must close ws + DB pool."""

    def test_cleanup_orchestration(self):
        sched = MagicMock()
        mm = MagicMock()
        with patch.object(main, "_sched", sched), \
             patch.object(main, "_mm", mm), \
             patch.object(main, "_post_shutdown_message", MagicMock()), \
             patch("artemis.quiet_hours.set_system_value", MagicMock()), \
             patch("knowledge.db.close_pool", MagicMock()) as close_pool:
            main._shutdown_cleanup()
        sched.scheduler.shutdown.assert_called_once_with(wait=False)  # never blocks
        mm.close.assert_called_once()
        close_pool.assert_called_once()

    def test_cleanup_survives_component_failures(self):
        # A failing scheduler shutdown must not stop the ws/db cleanup.
        sched = MagicMock()
        sched.scheduler.shutdown.side_effect = RuntimeError("boom")
        mm = MagicMock()
        with patch.object(main, "_sched", sched), \
             patch.object(main, "_mm", mm), \
             patch.object(main, "_post_shutdown_message", MagicMock()), \
             patch("artemis.quiet_hours.set_system_value", MagicMock()), \
             patch("knowledge.db.close_pool", MagicMock()) as close_pool:
            main._shutdown_cleanup()  # must not raise
        mm.close.assert_called_once()
        close_pool.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
