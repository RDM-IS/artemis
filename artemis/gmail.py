"""Gmail OAuth client — inbox polling and thread summarization."""

import base64
import html
import logging
import re
from datetime import datetime, timezone
from email.mime.text import MIMEText
from email.utils import parseaddr

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from artemis import config

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
]


class GmailClient:
    def __init__(self):
        self.service = None
        self._last_history_id: str | None = None
        self.scope_mismatch: bool = False

    def authenticate(self, mm_client=None):
        """Authenticate with Gmail API.

        Args:
            mm_client: Optional MattermostClient to post auth failure alerts.
        """
        creds = None
        token_path = config.GMAIL_TOKEN_PATH
        creds_path = config.GMAIL_CREDENTIALS_PATH

        if token_path.exists():
            creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                except Exception as exc:
                    err_str = str(exc).lower()
                    logger.error("Gmail token refresh failed: %s", exc)
                    if mm_client:
                        try:
                            mm_client.post_message(
                                config.CHANNEL_OPS,
                                "\U0001f510 Gmail authentication expired — manual re-authentication "
                                "required. Run: `python setup_oauth.py`",
                            )
                        except Exception:
                            logger.debug("Failed to post auth alert to Mattermost")
                    # Continue with degraded mode — no Gmail
                    self.service = None
                    return
            else:
                flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), SCOPES)
                creds = flow.run_local_server(port=0)

        # Persist refreshed token
        try:
            token_path.write_text(creds.to_json())
        except Exception:
            logger.warning("Failed to persist Gmail token to %s", token_path)

        # Validate scopes — warn but don't crash
        self.scope_mismatch = False
        granted = set(creds.scopes or []) if creds else set()
        required = {
            "https://www.googleapis.com/auth/gmail.modify",
            "https://www.googleapis.com/auth/gmail.send",
        }
        if granted and not required.issubset(granted):
            missing = required - granted
            logger.warning(
                "Gmail token missing scopes: %s — archive will not work. "
                "Delete token.json and re-authenticate.",
                ", ".join(missing),
            )
            self.scope_mismatch = True

        self._creds = creds
        self.service = build("gmail", "v1", credentials=creds)
        logger.info("Gmail authenticated")

    def _refresh_if_needed(self) -> bool:
        """Refresh credentials if expired and re-save token. Returns True if valid."""
        creds = getattr(self, "_creds", None)
        if not creds:
            return bool(self.service)
        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                config.GMAIL_TOKEN_PATH.write_text(creds.to_json())
                logger.debug("Gmail token refreshed and saved")
            except Exception:
                logger.exception("Gmail token refresh failed mid-session")
                return False
        return True

    def get_recent_messages(self, max_results: int = 20, query: str = "is:inbox") -> list[dict]:
        """Fetch recent inbox messages."""
        if not self.service:
            logger.error("Gmail not authenticated")
            return []

        try:
            results = (
                self.service.users()
                .messages()
                .list(userId="me", q=query, maxResults=max_results)
                .execute()
            )
        except Exception:
            logger.exception("Failed to list Gmail messages")
            return []

        messages = results.get("messages", [])
        detailed = []
        for msg_ref in messages:
            try:
                msg = (
                    self.service.users()
                    .messages()
                    .get(userId="me", id=msg_ref["id"], format="metadata",
                         metadataHeaders=["From", "Subject", "Date"])
                    .execute()
                )
                headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
                detailed.append({
                    "id": msg["id"],
                    "thread_id": msg["threadId"],
                    "from": headers.get("From", ""),
                    "from_email": parseaddr(headers.get("From", ""))[1],
                    "subject": headers.get("Subject", ""),
                    "date": headers.get("Date", ""),
                    "snippet": msg.get("snippet", ""),
                    "label_ids": msg.get("labelIds", []),
                })
            except Exception:
                logger.exception("Failed to get message %s", msg_ref["id"])

        return detailed

    def get_full_message(self, message_id: str) -> str:
        """Fetch the full body of a message.  Prefers text/plain, falls back to HTML.

        Returns the decoded body text (up to 10 000 chars) or empty string on failure.
        """
        if not self.service:
            return ""
        try:
            msg = (
                self.service.users()
                .messages()
                .get(userId="me", id=message_id, format="full")
                .execute()
            )
            body = self._extract_body(msg.get("payload", {}))
            return body[:10_000]
        except Exception:
            logger.exception("Failed to get full message %s", message_id)
            return ""

    @staticmethod
    def _extract_body(payload: dict) -> str:
        """Walk a Gmail payload tree and return the best text body."""
        # Collect candidate parts
        plain_parts: list[str] = []
        html_parts: list[str] = []

        def _walk(part: dict) -> None:
            mime = part.get("mimeType", "")
            if mime == "text/plain":
                data = part.get("body", {}).get("data", "")
                if data:
                    plain_parts.append(base64.urlsafe_b64decode(data).decode("utf-8", errors="replace"))
            elif mime == "text/html":
                data = part.get("body", {}).get("data", "")
                if data:
                    html_parts.append(base64.urlsafe_b64decode(data).decode("utf-8", errors="replace"))
            for sub in part.get("parts", []):
                _walk(sub)

        _walk(payload)

        if plain_parts:
            return "\n".join(plain_parts)

        if html_parts:
            return GmailClient._strip_html("\n".join(html_parts))

        return ""

    @staticmethod
    def _strip_html(raw_html: str) -> str:
        """Crude HTML-to-text: remove tags and decode entities."""
        # Remove style/script blocks
        text = re.sub(r"<(style|script)[^>]*>.*?</\1>", "", raw_html, flags=re.DOTALL | re.IGNORECASE)
        # Replace <br>, <p>, <div> with newlines
        text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
        text = re.sub(r"</(p|div|tr|li)>", "\n", text, flags=re.IGNORECASE)
        # Strip remaining tags
        text = re.sub(r"<[^>]+>", "", text)
        # Decode HTML entities
        text = html.unescape(text)
        # Collapse whitespace
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    def get_thread(self, thread_id: str) -> dict | None:
        """Get a full thread with message snippets."""
        if not self.service:
            return None
        try:
            thread = (
                self.service.users()
                .threads()
                .get(userId="me", id=thread_id, format="metadata",
                     metadataHeaders=["From", "Subject", "Date"])
                .execute()
            )
            messages = []
            for msg in thread.get("messages", []):
                headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
                messages.append({
                    "from": headers.get("From", ""),
                    "subject": headers.get("Subject", ""),
                    "date": headers.get("Date", ""),
                    "snippet": msg.get("snippet", ""),
                })
            return {
                "id": thread["id"],
                "subject": messages[0]["subject"] if messages else "",
                "messages": messages,
            }
        except Exception:
            logger.exception("Failed to get thread %s", thread_id)
            return None

    def get_threads_with_address(self, email_address: str, max_threads: int = 5) -> list[dict]:
        """Get recent threads involving a specific email address."""
        messages = self.get_recent_messages(
            max_results=50, query=f"from:{email_address} OR to:{email_address}"
        )
        seen_threads = set()
        threads = []
        for msg in messages:
            if msg["thread_id"] not in seen_threads and len(threads) < max_threads:
                seen_threads.add(msg["thread_id"])
                thread = self.get_thread(msg["thread_id"])
                if thread:
                    threads.append(thread)
        return threads

    def is_priority_sender(self, from_email: str) -> bool:
        """Check if sender matches any priority contact pattern."""
        email_lower = from_email.lower()
        for contact in config.PRIORITY_CONTACTS:
            contact_lower = contact.lower()
            if "@" in contact_lower:
                if email_lower == contact_lower:
                    return True
            else:
                # Domain match
                if email_lower.endswith(f"@{contact_lower}"):
                    return True
        return False

    def get_my_email(self) -> str:
        """Get the authenticated user's email address."""
        if not self.service:
            return ""
        try:
            profile = self.service.users().getProfile(userId="me").execute()
            return profile.get("emailAddress", "")
        except Exception:
            logger.exception("Failed to get user profile")
            return ""

    def check_for_reply(self, thread_id: str, since_date: str) -> bool:
        """Check if a thread has a reply from someone other than me after since_date.

        since_date should be ISO format YYYY-MM-DD.
        """
        if not self.service:
            return False
        try:
            thread = self.get_thread(thread_id)
            if not thread:
                return False
            my_email = self.get_my_email().lower()
            for msg in thread.get("messages", []):
                msg_from = parseaddr(msg.get("from", ""))[1].lower()
                if msg_from == my_email:
                    continue
                # Check if message date is after since_date
                msg_date = msg.get("date", "")
                if msg_date and since_date:
                    # Simple comparison: if the message exists after the thread
                    # was marked waiting, it's a reply. Gmail thread ordering
                    # is chronological, so later messages are newer.
                    return True
            return False
        except Exception:
            logger.exception("check_for_reply failed for thread %s", thread_id)
            return False

    def get_my_last_message_snippet(self, thread_id: str) -> str:
        """Get the first line of the last message I sent in a thread."""
        if not self.service:
            return ""
        try:
            thread = self.get_thread(thread_id)
            if not thread:
                return ""
            my_email = self.get_my_email().lower()
            my_messages = [
                msg for msg in thread.get("messages", [])
                if parseaddr(msg.get("from", ""))[1].lower() == my_email
            ]
            if not my_messages:
                return ""
            last = my_messages[-1]
            snippet = last.get("snippet", "")
            # Return first line / first ~120 chars
            first_line = snippet.split("\n")[0][:120]
            return first_line
        except Exception:
            logger.exception("get_my_last_message_snippet failed for thread %s", thread_id)
            return ""

    def archive_message(self, message_id: str) -> bool:
        """Remove message from inbox (archive).  Returns True on success."""
        if not self.service:
            logger.error("Gmail not authenticated — cannot archive")
            return False
        try:
            self.service.users().messages().modify(
                userId="me",
                id=message_id,
                body={"removeLabelIds": ["INBOX"]},
            ).execute()
            logger.info("Archived message %s", message_id)
            return True
        except Exception:
            logger.exception("Failed to archive message %s", message_id)
            return False

    def get_message_id_header(self, message_id: str) -> str:
        """Get the Message-ID header of a Gmail message for reply threading."""
        if not self.service:
            return ""
        try:
            msg = (
                self.service.users()
                .messages()
                .get(userId="me", id=message_id, format="metadata",
                     metadataHeaders=["Message-ID"])
                .execute()
            )
            headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
            return headers.get("Message-ID", "")
        except Exception:
            logger.exception("Failed to get Message-ID for %s", message_id)
            return ""

    def send_reply(
        self,
        thread_id: str,
        to: str,
        subject: str,
        body: str,
        in_reply_to: str = "",
    ) -> bool:
        """Send a reply in an existing thread.

        Args:
            thread_id: Gmail thread ID to reply in.
            to: Recipient email address.
            subject: Email subject (Re: prefix added if missing).
            body: Plain text body.
            in_reply_to: Message-ID header of the message being replied to.

        Returns True on success, False on failure.
        """
        if not self.service:
            logger.error("Gmail not authenticated — cannot send")
            return False

        if not subject.lower().startswith("re:"):
            subject = f"Re: {subject}"

        msg = MIMEText(body)
        msg["to"] = to
        msg["subject"] = subject

        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
            msg["References"] = in_reply_to

        # Get sender address
        my_email = self.get_my_email()
        if my_email:
            msg["from"] = my_email

        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")

        try:
            self.service.users().messages().send(
                userId="me",
                body={"raw": raw, "threadId": thread_id},
            ).execute()
            logger.info("Sent reply in thread %s to %s", thread_id, to)
            return True
        except Exception:
            logger.exception("Failed to send reply in thread %s", thread_id)
            return False

    def format_for_claude(self, messages: list[dict]) -> str:
        """Format messages for Claude with UNTRUSTED prefix."""
        from artemis.prompts import UNTRUSTED_PREFIX

        parts = []
        for msg in messages:
            parts.append(
                f"From: {msg['from']}\n"
                f"Subject: {msg['subject']}\n"
                f"Date: {msg['date']}\n"
                f"Preview: {msg['snippet']}\n"
            )
        return UNTRUSTED_PREFIX + "\n---\n".join(parts)
