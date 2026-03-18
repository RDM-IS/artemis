"""Entry point — starts all schedulers and webhook listener."""

import json
import logging
import signal
import sys
import time
from datetime import datetime, timezone
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
from artemis.version import format_version_status, get_commit_hash, get_latest_github_version, get_version

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
_start_time: float = 0.0
_sched: ArtemisScheduler | None = None
_last_triage: str = "never"
_last_brief: str = "never"


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


@app.route("/health", methods=["GET"])
def health_check():
    """Health check endpoint for external monitoring."""
    gmail_status = "connected" if _gmail and _gmail.service else "error"
    calendar_status = "connected" if _calendar and _calendar.service else "error"
    mm_status = "connected" if _mm and _mm._bot_user_id else "error"
    job_count = len(_sched.scheduler.get_jobs()) if _sched else 0
    uptime = int(time.time() - _start_time) if _start_time else 0
    local_hash = get_commit_hash()
    latest_hash, _ = get_latest_github_version()

    return jsonify({
        "status": "ok",
        "version": get_version(),
        "latest_commit": latest_hash or "unknown",
        "up_to_date": bool(local_hash and latest_hash and latest_hash.startswith(local_hash)),
        "gmail": gmail_status,
        "calendar": calendar_status,
        "mattermost": mm_status,
        "scheduler_jobs": job_count,
        "uptime_seconds": uptime,
        "last_triage": _last_triage,
        "last_brief": _last_brief,
    })


def _build_mention_context(post: dict, gmail: GmailClient, calendar: CalendarClient) -> str:
    """Build data context for an @mention response."""
    parts = []

    # Time awareness
    now = datetime.now()
    day_name = now.strftime("%A")
    time_str = now.strftime("%I:%M %p")
    parts.append(f"**Current time:** {day_name}, {time_str}")

    # Recent emails
    try:
        messages = gmail.get_recent_messages(max_results=10)
        if messages:
            parts.append("\n**Recent emails (last 3 threads):**")
            for m in messages[:3]:
                parts.append(f"- From: {m['from']} | Subject: {m['subject']} | {m['snippet'][:100]}")
    except Exception:
        logger.exception("Failed to get emails for mention context")

    # Today's calendar
    try:
        events = calendar.get_today_events()
        if events:
            parts.append("\n**Today's calendar:**")
            for e in events:
                external = [a for a in e["attendees"] if not a.get("self")]
                if external:
                    attendee_str = ", ".join(a["name"] or a["email"] for a in external)
                else:
                    attendee_str = "(solo)"
                parts.append(f"- {e['summary']} at {e['start']} — {attendee_str}")
        else:
            parts.append("\n**Today's calendar:** No events scheduled.")
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

    # Version commands
    q_lower = question.lower().strip()
    if q_lower in ("version", "what version are you?", "what version", "update check"):
        channel_id = post.get("channel_id", "")
        root_id = post.get("root_id") or post["id"]
        reply = format_version_status()
        if _mm:
            _mm.post_to_channel_id(channel_id, reply, root_id=root_id)
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


def _connect_mattermost_with_retry(mm: MattermostClient) -> bool:
    """Try to connect to Mattermost with configurable retries."""
    for attempt in range(1, config.STARTUP_RETRY_COUNT + 1):
        try:
            mm.get_bot_user_id()
            logger.info("Mattermost connected on attempt %d (bot user: %s)", attempt, mm._bot_user_id)
            return True
        except Exception:
            logger.warning(
                "Mattermost connection attempt %d/%d failed — retrying in %ds",
                attempt, config.STARTUP_RETRY_COUNT, config.STARTUP_RETRY_DELAY,
            )
            if attempt < config.STARTUP_RETRY_COUNT:
                time.sleep(config.STARTUP_RETRY_DELAY)
    return False


def _post_startup_message(mm: MattermostClient, gmail: GmailClient, calendar: CalendarClient, sched: ArtemisScheduler):
    """Post startup status to #artemis-ops."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    gmail_status = "connected" if gmail and gmail.service else "disconnected"
    cal_status = "connected" if calendar and calendar.service else "disconnected"
    job_count = len(sched.scheduler.get_jobs())
    version = get_version()
    msg = (
        f"\u2705 Artemis online \u2014 {ts}\n"
        f"Version: {version}\n"
        f"Gmail: {gmail_status}. Calendar: {cal_status}. "
        f"Scheduler: {job_count} jobs running."
    )
    try:
        mm.post_message(config.CHANNEL_OPS, msg)
    except Exception:
        logger.exception("Failed to post startup message")


def _post_shutdown_message(mm: MattermostClient):
    """Post shutdown notice to #artemis-ops."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        mm.post_message(config.CHANNEL_OPS, f"\U0001f534 Artemis going offline \u2014 {ts}.")
    except Exception:
        logger.exception("Failed to post shutdown message")


def main():
    global _mm, _gmail, _calendar, _start_time, _sched

    _start_time = time.time()
    logger.info("Starting Artemis...")

    # Init database (commitments + inbox_threads tables)
    from artemis.inbox import get_db as init_inbox_db
    get_db()
    init_inbox_db()

    # Init Mattermost with retry loop
    _mm = MattermostClient()
    if not _connect_mattermost_with_retry(_mm):
        logger.error(
            "Failed to connect to Mattermost after %d attempts — giving up",
            config.STARTUP_RETRY_COUNT,
        )
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
    _sched = ArtemisScheduler(_mm, _gmail, _calendar)
    _sched.start()

    # Post startup message
    _post_startup_message(_mm, _gmail, _calendar, _sched)

    # Start Flask for uptime webhook + health check
    shutdown = Event()

    def signal_handler(sig, frame):
        logger.info("Shutting down...")
        _post_shutdown_message(_mm)
        _sched.stop()
        shutdown.set()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    logger.info("Artemis is running. Press Ctrl+C to stop.")
    app.run(host="0.0.0.0", port=5000, use_reloader=False)


if __name__ == "__main__":
    main()
