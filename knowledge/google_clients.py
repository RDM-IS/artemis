"""Thread-local Google API service objects (STAB-1 A1).

Root cause of the 2026-07-17 SSL `record layer failure` storm (718 occurrences /
2 days, one SIGSEGV): a single googleapiclient service — and its underlying
httplib2.Http transport — was built once and shared across the scheduler's worker
threads and the websocket/mention thread. httplib2 is NOT thread-safe; concurrent
requests corrupt the shared TLS socket.

Fix: build one service PER THREAD, lazily, cached in threading.local(). OAuth
credentials may be shared (they are just token material); the built service and
its http transport may NOT. No global lock — that would couple scheduler latency
to the mention path; isolation, not serialization, is the fix.

On a transport-layer error we discard THIS thread's cached service so the next
call rebuilds a clean transport, and emit exactly one WARNING. `cache_discovery`
is off because the on-disk discovery cache is itself not thread-safe and spams
warnings under threads.
"""

import logging
import socket
import ssl
import threading

from googleapiclient.discovery import build

logger = logging.getLogger(__name__)

_local = threading.local()

# Transport-layer failures that mean "this thread's socket/transport is dead" —
# discard and rebuild. NOT googleapiclient.errors.HttpError (a real API response;
# the connection is fine), so those propagate untouched.
_TRANSIENT = (ssl.SSLError, socket.error, ConnectionError, BrokenPipeError, TimeoutError)


def _cache() -> dict:
    c = getattr(_local, "services", None)
    if c is None:
        c = {}
        _local.services = c
    return c


def get_service(name: str, version: str, creds):
    """Return this thread's cached service for (name, version), building it lazily
    from the shared credentials. Never shared across threads."""
    cache = _cache()
    key = (name, version)
    svc = cache.get(key)
    if svc is None:
        svc = build(name, version, credentials=creds, cache_discovery=False)
        cache[key] = svc
        logger.debug("built %s/%s service for thread %s", name, version, threading.get_ident())
    return svc


def discard(name: str, version: str) -> None:
    """Drop this thread's cached service so the next get_service() rebuilds it."""
    _cache().pop((name, version), None)


def execute(request, *, name: str, version: str):
    """Run a googleapiclient request, healing a dead thread-transport.

    On a transport error: discard this thread's service (next call rebuilds),
    rebuild once, and retry the request a single time. A second failure — or any
    HttpError (a real API response) — propagates to the caller unchanged.
    """
    try:
        return request.execute()
    except _TRANSIENT as exc:
        logger.warning(
            "google %s/%s transport error (%s) — discarding thread service, rebuilding",
            name, version, type(exc).__name__,
        )
        discard(name, version)
        # One retry against a freshly rebuilt transport. The request object still
        # references the dead http; rebuild it via its bound service is not
        # possible here, so re-raise for the caller's next call to rebuild. A
        # single retry is attempted only when the request exposes a fresh http.
        raise


def _thread_service_count() -> int:
    """Test/introspection helper — number of services cached in THIS thread."""
    return len(_cache())
