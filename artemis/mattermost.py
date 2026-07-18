"""Mattermost bot client — post messages and listen for @mentions."""

import json
import logging
import random
import time
from threading import Thread

import requests
import websocket

from artemis import config

logger = logging.getLogger(__name__)

# STAB-1 A2: watchdog fires if no websocket event/pong arrives within this many
# seconds (server-close → client-select-forever was the 2026-07-17 half-open death).
WS_STALE_SECONDS = 90


def _backoff_delay(attempt: int, base: float = 5.0, cap: float = 60.0) -> float:
    """Exponential reconnect backoff: 5, 10, 20, 40, 60, 60, … (capped). Jitter is
    added by the caller; this bare schedule is what the tests pin."""
    return min(cap, base * (2 ** attempt))


class MattermostClient:
    TEAM_NAME = "rdmis"

    def __init__(self):
        from knowledge.secrets import get_mattermost_credentials, get_mattermost_url
        mm_creds = get_mattermost_credentials()
        self.url = get_mattermost_url().rstrip("/")
        self.token = mm_creds.get("token", "")
        self.headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }
        self._team_id: str | None = None
        self._channel_ids: dict[str, str] = {}
        self._bot_user_id: str | None = None
        # STAB-1 A2 websocket lifecycle state.
        self._ws = None
        self._ws_should_run: bool = True
        self._last_event_ts: float = time.time()
        self._disconnected_at: float | None = None
        self._reconnect_attempt: int = 0
        self._mention_handler = None

    @property
    def team_id(self) -> str:
        """Resolve team ID from the Mattermost API on first access."""
        if not self._team_id:
            resp = self._api("GET", f"/teams/name/{self.TEAM_NAME}")
            self._team_id = resp.json()["id"]
            logger.debug("Resolved team '%s' → %s", self.TEAM_NAME, self._team_id)
        return self._team_id

    def _api(self, method: str, path: str, **kwargs) -> requests.Response:
        resp = requests.request(
            method, f"{self.url}/api/v4{path}", headers=self.headers, **kwargs
        )
        resp.raise_for_status()
        return resp

    def get_bot_user_id(self) -> str:
        if not self._bot_user_id:
            resp = self._api("GET", "/users/me")
            self._bot_user_id = resp.json()["id"]
        return self._bot_user_id

    def get_channel_id(self, channel_name: str) -> str:
        if channel_name not in self._channel_ids:
            resp = self._api(
                "GET", f"/teams/{self.team_id}/channels/name/{channel_name}"
            )
            self._channel_ids[channel_name] = resp.json()["id"]
        return self._channel_ids[channel_name]

    def post_message(
        self, channel_name: str, message: str, root_id: str = ""
    ) -> dict:
        channel_id = self.get_channel_id(channel_name)
        payload = {"channel_id": channel_id, "message": message}
        if root_id:
            payload["root_id"] = root_id
        resp = self._api("POST", "/posts", json=payload)
        return resp.json()

    def post_to_channel_id(
        self, channel_id: str, message: str, root_id: str = ""
    ) -> dict:
        payload = {"channel_id": channel_id, "message": message}
        if root_id:
            payload["root_id"] = root_id
        resp = self._api("POST", "/posts", json=payload)
        return resp.json()

    def get_file_metadata(self, file_id: str) -> dict:
        """Metadata for an uploaded file (name, extension, size, mime_type)."""
        resp = self._api("GET", f"/files/{file_id}/info")
        return resp.json()

    def get_file_content(self, file_id: str) -> bytes:
        """Fetch the raw bytes of an uploaded file by id. Callers decode/guard the
        content-type themselves (PB-010 dossier capture accepts text formats only,
        rejects binaries)."""
        resp = self._api("GET", f"/files/{file_id}")
        return resp.content

    def get_thread_posts(self, post_id: str, limit: int = 10) -> list[dict]:
        resp = self._api("GET", f"/posts/{post_id}/thread")
        data = resp.json()
        posts = sorted(data["posts"].values(), key=lambda p: p["create_at"])
        return posts[-limit:]

    def on_mention(self, handler):
        """Register a handler for @mentions: handler(post_data, thread_context)."""
        self._mention_handler = handler

    def start_websocket(self):
        """Connect to Mattermost websocket and listen for mentions."""
        from knowledge.secrets import get_mattermost_ws_url
        ws_url = get_mattermost_ws_url().rstrip("/") + "/api/v4/websocket"
        bot_id = self.get_bot_user_id()

        # PB-009: Artemis always-listens in the configured DM channel
        # (#artemis-ryan). Every message there is routed to the mention handler
        # even without an @artemis mention. Resolve the channel ID once here
        # (cached) rather than hardcoding a literal; if it can't resolve we fall
        # back to mention-gating only.
        try:
            always_listen_channel_id = self.get_channel_id(config.CHANNEL_OPS)
            logger.info(
                "Always-listen enabled for channel '%s' (%s)",
                config.CHANNEL_OPS, always_listen_channel_id,
            )
        except Exception:
            logger.exception(
                "Could not resolve always-listen channel '%s'; mention-gating only",
                config.CHANNEL_OPS,
            )
            always_listen_channel_id = None

        def on_message(ws, raw):
            self._last_event_ts = time.time()  # A2: liveness for the watchdog
            try:
                event = json.loads(raw)
                # A4: every received event at DEBUG (full event-flow visibility).
                logger.debug("ws event: %s", event.get("event"))
                if event.get("event") != "posted":
                    return
                post = json.loads(event["data"]["post"])
                # Hard guard: never respond to our own messages (no self-reply loops).
                if post["user_id"] == bot_id:
                    return
                # Always-listen channel: route everything (no mention required).
                channel_id = post.get("channel_id", "")
                always_listen = (
                    always_listen_channel_id is not None
                    and channel_id == always_listen_channel_id
                )
                # Check for @mention or active thread participation
                message = post.get("message", "")
                has_mention = (
                    "@artemis" in message.lower()
                    or bot_id in post.get("props", {}).get("mentioned_user_ids", [])
                )
                in_active_thread = bool(post.get("root_id"))
                if not always_listen and not has_mention and not in_active_thread:
                    return
                if not always_listen and not has_mention and in_active_thread:
                    thread_id = post["root_id"]
                    try:
                        thread_posts = self.get_thread_posts(thread_id, limit=50)
                        bot_participated = any(p["user_id"] == bot_id for p in thread_posts)
                        if not bot_participated:
                            return
                    except Exception:
                        return
                if self._mention_handler:
                    # A4: every dispatched posted event at INFO (post_id, channel,
                    # sender, first 60 chars) — the instrument for the bare-message
                    # routing diagnosis. Silence is never a failure mode.
                    logger.info(
                        "posted → dispatch: post=%s ch=%s user=%s msg=%r",
                        post.get("id"), channel_id, post.get("user_id"),
                        (post.get("message", "") or "")[:60],
                    )
                    thread_id = post.get("root_id") or post["id"]
                    thread = self.get_thread_posts(thread_id)
                    try:
                        self._mention_handler(post, thread)
                    except Exception:
                        # A4: full traceback to journal + one-line channel reply.
                        logger.exception("mention handler raised for post %s", post.get("id"))
                        try:
                            self.post_to_channel_id(
                                channel_id, "⚠️ error handling that — logged.",
                                root_id=post.get("root_id") or post["id"],
                            )
                        except Exception:
                            logger.debug("error-reply post failed", exc_info=True)
            except Exception:
                logger.exception("Error processing websocket message")

        def on_open(ws):
            auth = json.dumps(
                {"seq": 1, "action": "authentication_challenge", "data": {"token": self.token}}
            )
            ws.send(auth)
            now = time.time()
            self._last_event_ts = now
            if self._reconnect_attempt > 0:
                logger.info(
                    "Mattermost websocket reconnected (attempt %d)", self._reconnect_attempt
                )
                gap = now - (self._disconnected_at or now)
                # Announce only if the outage was long enough to matter (>5 min).
                if gap > 300:
                    try:
                        self.post_message(
                            config.CHANNEL_OPS,
                            f"✅ Artemis online — websocket reconnected after "
                            f"{int(gap // 60)}m offline.",
                        )
                    except Exception:
                        logger.debug("reconnect announce failed", exc_info=True)
            else:
                logger.info("Mattermost websocket connected")
            self._reconnect_attempt = 0
            self._disconnected_at = None

        def on_pong(ws, _payload):
            self._last_event_ts = time.time()  # A2: pong keeps the connection live

        def on_error(ws, error):
            logger.error("Websocket error: %s", error)

        def on_close(ws, code, msg):
            if self._disconnected_at is None:
                self._disconnected_at = time.time()
            logger.warning("Mattermost websocket closed (code=%s)", code)

        self._ws_should_run = True
        thread = Thread(
            target=self._run_ws_loop,
            args=(ws_url, on_message, on_open, on_pong, on_error, on_close),
            daemon=True,
        )
        thread.start()

    def _run_ws_loop(self, url, on_message, on_open, on_pong, on_error, on_close):
        """Reconnect loop (STAB-1 A2). Replaces the old fixed sleep(5) + recursive
        self-call in on_close, which leaked stack across many flaps. Exponential
        backoff with jitter, infinite retries, until close() flips the flag."""
        while self._ws_should_run:
            self._ws = websocket.WebSocketApp(
                url,
                on_message=on_message,
                on_open=on_open,
                on_pong=on_pong,
                on_error=on_error,
                on_close=on_close,
            )
            try:
                self._ws.run_forever(ping_interval=30, ping_timeout=10)
            except Exception:
                logger.exception("websocket run_forever crashed")
            if not self._ws_should_run:
                break
            delay = _backoff_delay(self._reconnect_attempt)
            delay += random.uniform(0, delay * 0.3)  # jitter
            self._reconnect_attempt += 1
            logger.warning(
                "Mattermost websocket down — reconnect attempt %d in %.1fs",
                self._reconnect_attempt, delay,
            )
            time.sleep(delay)

    def watchdog_check(self) -> bool:
        """STAB-1 A2: if no event/pong for >90s the connection is half-open —
        force-close so the reconnect loop fires. Returns True if it force-closed.
        Called by a 60s scheduler job."""
        if not self._ws_should_run:
            return False
        stale = time.time() - self._last_event_ts
        if stale > WS_STALE_SECONDS:
            logger.warning(
                "Mattermost websocket stale %.0fs (>%ds) — forcing close to reconnect",
                stale, WS_STALE_SECONDS,
            )
            try:
                if self._ws:
                    self._ws.close()
            except Exception:
                logger.debug("watchdog force-close failed", exc_info=True)
            return True
        return False

    def close(self) -> None:
        """Stop the reconnect loop and close the socket (STAB-1 A3 shutdown)."""
        self._ws_should_run = False
        try:
            if self._ws:
                self._ws.close()
        except Exception:
            logger.debug("websocket close failed", exc_info=True)
