"""Mattermost bot client — post messages and listen for @mentions."""

import json
import logging
import time
from threading import Thread

import requests
import websocket

from artemis import config

logger = logging.getLogger(__name__)


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

        def on_message(ws, raw):
            try:
                event = json.loads(raw)
                if event.get("event") != "posted":
                    return
                post = json.loads(event["data"]["post"])
                # Ignore own messages
                if post["user_id"] == bot_id:
                    return
                # Check for @mention or active thread participation
                message = post.get("message", "")
                has_mention = (
                    "@artemis" in message.lower()
                    or bot_id in post.get("props", {}).get("mentioned_user_ids", [])
                )
                in_active_thread = bool(post.get("root_id"))
                if not has_mention and not in_active_thread:
                    return
                if not has_mention and in_active_thread:
                    thread_id = post["root_id"]
                    try:
                        thread_posts = self.get_thread_posts(thread_id, limit=50)
                        bot_participated = any(p["user_id"] == bot_id for p in thread_posts)
                        if not bot_participated:
                            return
                    except Exception:
                        return
                if self._mention_handler:
                    thread_id = post.get("root_id") or post["id"]
                    thread = self.get_thread_posts(thread_id)
                    self._mention_handler(post, thread)
            except Exception:
                logger.exception("Error processing websocket message")

        def on_open(ws):
            auth = json.dumps(
                {"seq": 1, "action": "authentication_challenge", "data": {"token": self.token}}
            )
            ws.send(auth)
            logger.info("Mattermost websocket connected")

        def on_error(ws, error):
            logger.error("Websocket error: %s", error)

        def on_close(ws, code, msg):
            logger.warning("Websocket closed (code=%s), reconnecting in 5s...", code)
            time.sleep(5)
            self._connect_ws(ws_url, on_message, on_open, on_error, on_close)

        thread = Thread(
            target=self._connect_ws,
            args=(ws_url, on_message, on_open, on_error, on_close),
            daemon=True,
        )
        thread.start()

    def _connect_ws(self, url, on_message, on_open, on_error, on_close):
        ws = websocket.WebSocketApp(
            url,
            on_message=on_message,
            on_open=on_open,
            on_error=on_error,
            on_close=on_close,
        )
        ws.run_forever()
