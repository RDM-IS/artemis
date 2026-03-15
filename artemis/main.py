"""Entry point — starts all schedulers and webhook listener."""

import json
import logging
import signal
import sys
from threading import Event

from flask import Flask, request, jsonify

from artemis import config
from artemis.briefs import handle_mention
from artemis.calendar import CalendarClient
from artemis.commitments import get_db, list_commitments, get_commitments_for_client
from artemis.inbox import (
    format_inbox_status,
    format_snoozed_list,
    format_waiting_list,
    get_counts,
    list_by_state,
    mark_done,
    mark_noise,
    mark_snoozed,
    mark_waiting,
    parse_inbox_command,
    resolve_thread_id,
    NEEDS_ACTION,
    SNOOZED,
    WAITING,
)
from artemis.gmail import GmailClient
from artemis.mattermost import MattermostClient
from artemis.prompts import UNTRUSTED_PREFIX
from artemis.scheduler import ArtemisScheduler

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Global references set during startup
_mm: MattermostClient | None = None
_gmail: GmailClient | None = None
_calendar: CalendarClient | None = None


@app.route("/webhook/uptime", methods=["POST"])
def uptime_webhook():
    """Receive Uptime Robot webhook notifications."""
    data = request.json or {}
    monitor_name = data.get("monitorFriendlyName", data.get("monitor_name", "Unknown"))
    alert_type = data.get("alertType", data.get("alert_type", ""))
    url = data.get("monitorURL", data.get("monitor_url", ""))

    # alertType 1 = down, 2 = up (Uptime Robot convention)
    if str(alert_type) == "1":
        msg = f"\U0001f534 **{monitor_name}** is DOWN"
    elif str(alert_type) == "2":
        msg = f"\U0001f7e2 **{monitor_name}** recovered"
    else:
        msg = f"\u2139\ufe0f **{monitor_name}** alert (type={alert_type})"

    if url:
        msg += f" — {url}"

    if _mm:
        try:
            _mm.post_message(config.CHANNEL_OPS, msg)
        except Exception:
            logger.exception("Failed to post uptime alert")

    return jsonify({"status": "ok"}), 200


def _build_mention_context(post: dict, gmail: GmailClient, calendar: CalendarClient) -> str:
    """Build data context for an @mention response."""
    parts = []

    # Recent emails
    try:
        messages = gmail.get_recent_messages(max_results=10)
        if messages:
            parts.append("**Recent emails:**")
            for m in messages[:5]:
                parts.append(f"- From: {m['from']} | Subject: {m['subject']} | {m['snippet'][:100]}")
    except Exception:
        logger.exception("Failed to get emails for mention context")

    # Today's calendar
    try:
        events = calendar.get_today_events()
        if events:
            parts.append("\n**Today's calendar:**")
            for e in events:
                attendees = ", ".join(
                    a["name"] or a["email"] for a in e["attendees"] if not a.get("self")
                )
                parts.append(f"- {e['summary']} at {e['start']} — {attendees or 'no external attendees'}")
    except Exception:
        logger.exception("Failed to get calendar for mention context")

    # Open commitments
    try:
        commitments = list_commitments()
        if commitments:
            parts.append("\n**Open commitments:**")
            for c in commitments:
                parts.append(f"- {c['title']} (due {c['due_date']}, client: {c['client'] or 'n/a'})")
    except Exception:
        logger.exception("Failed to get commitments for mention context")

    # Inbox zero status
    try:
        counts = get_counts()
        na_count = counts.get(NEEDS_ACTION, 0)
        w_count = counts.get(WAITING, 0)
        if na_count or w_count:
            parts.append(f"\n**Inbox zero:** {na_count} need action, {w_count} waiting")
    except Exception:
        logger.exception("Failed to get inbox status for mention context")

    return UNTRUSTED_PREFIX + "\n".join(parts) if parts else "No context available."


def _handle_inbox_command(post: dict, question: str) -> bool:
    """Try to handle an inbox zero command. Returns True if handled."""
    parsed = parse_inbox_command(question)
    if not parsed:
        return False

    cmd, thread_id, extra = parsed
    channel_id = post.get("channel_id", "")
    root_id = post.get("root_id") or post["id"]
    reply = ""

    if cmd == "inbox":
        counts = get_counts()
        reply = format_inbox_status(counts)
        # Also include oldest NEEDS_ACTION
        na = list_by_state(NEEDS_ACTION)
        if na:
            oldest = na[-1]  # sorted DESC, so last is oldest
            reply += f"\n\nOldest unresolved: **{oldest['subject']}** from {oldest['sender']}"

    elif cmd == "waiting":
        if not thread_id:
            threads = list_by_state(WAITING)
            reply = format_waiting_list(threads)
        else:
            # Mark as waiting: "wait <id>"
            tid = resolve_thread_id(thread_id)
            if tid:
                mark_waiting(tid, waiting_on=extra or "")
                reply = f"Marked as WAITING" + (f" on {extra}" if extra else " — who are we waiting on?")
            else:
                reply = f"Thread not found: {thread_id}"

    elif cmd == "snoozed":
        threads = list_by_state(SNOOZED)
        reply = format_snoozed_list(threads)

    elif cmd == "done":
        if not thread_id:
            reply = "Usage: `done <thread_id>`"
        else:
            tid = resolve_thread_id(thread_id)
            if tid:
                mark_done(tid)
                reply = f"Marked as DONE"
            else:
                reply = f"Thread not found: {thread_id}"

    elif cmd == "noise":
        if not thread_id:
            reply = "Usage: `noise <thread_id>`"
        else:
            tid = resolve_thread_id(thread_id)
            if tid:
                mark_noise(tid)
                reply = f"Marked as NOISE — won't resurface"
            else:
                reply = f"Thread not found: {thread_id}"

    elif cmd == "snooze":
        if not thread_id:
            reply = "Usage: `snooze <thread_id> <1d|3d|1w|2w>`"
        else:
            tid = resolve_thread_id(thread_id)
            period = extra or "3d"
            if tid:
                if mark_snoozed(tid, period):
                    reply = f"Snoozed for {period}"
                else:
                    reply = f"Invalid snooze period: {period} (use 1d, 3d, 1w, 2w)"
            else:
                reply = f"Thread not found: {thread_id}"

    elif cmd == "wait":
        if not thread_id:
            reply = "Usage: `wait <thread_id>`"
        else:
            tid = resolve_thread_id(thread_id)
            if tid:
                mark_waiting(tid, waiting_on=extra or "")
                reply = f"Marked as WAITING" + (f" on {extra}" if extra else " — who are we waiting on?")
            else:
                reply = f"Thread not found: {thread_id}"

    else:
        return False

    if reply and _mm:
        _mm.post_to_channel_id(channel_id, reply, root_id=root_id)
    return True


def _handle_mention(post: dict, thread: list[dict]):
    """Handle an @artemis mention."""
    question = post.get("message", "").replace("@artemis", "").strip()
    if not question:
        return

    # Try inbox commands first (done, wait, snooze, noise, inbox, waiting, snoozed)
    if _handle_inbox_command(post, question):
        return

    thread_lines = []
    for p in thread[-10:]:
        thread_lines.append(f"{p.get('message', '')}")
    thread_context = "\n".join(thread_lines)

    data_context = _build_mention_context(post, _gmail, _calendar)

    response = handle_mention(question, thread_context, data_context)
    if response and _mm:
        channel_id = post.get("channel_id", "")
        root_id = post.get("root_id") or post["id"]
        _mm.post_to_channel_id(channel_id, response, root_id=root_id)


def main():
    global _mm, _gmail, _calendar

    logger.info("Starting Artemis...")

    # Init database (commitments + inbox_threads tables)
    from artemis.inbox import get_db as init_inbox_db
    get_db()
    init_inbox_db()

    # Init Mattermost
    _mm = MattermostClient()
    try:
        _mm.get_bot_user_id()
        logger.info("Mattermost connected (bot user: %s)", _mm._bot_user_id)
    except Exception:
        logger.error("Failed to connect to Mattermost — check MATTERMOST_URL and MATTERMOST_BOT_TOKEN")
        sys.exit(1)

    # Init Gmail
    _gmail = GmailClient()
    try:
        _gmail.authenticate()
    except Exception:
        logger.warning("Gmail authentication failed — email features disabled")

    # Init Calendar
    _calendar = CalendarClient()
    try:
        _calendar.authenticate()
    except Exception:
        logger.warning("Calendar authentication failed — calendar features disabled")

    # Register @mention handler
    _mm.on_mention(_handle_mention)
    _mm.start_websocket()

    # Start scheduler
    sched = ArtemisScheduler(_mm, _gmail, _calendar)
    sched.start()

    # Start Flask for uptime webhook
    shutdown = Event()

    def signal_handler(sig, frame):
        logger.info("Shutting down...")
        sched.stop()
        shutdown.set()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    logger.info("Artemis is running. Press Ctrl+C to stop.")
    app.run(host="0.0.0.0", port=5000, use_reloader=False)


if __name__ == "__main__":
    main()
