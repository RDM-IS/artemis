"""Entry point — starts all schedulers and webhook listener."""

import json
import logging
import re
import signal
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import Event
from zoneinfo import ZoneInfo

from flask import Flask, Response, request, jsonify

from artemis import config
from artemis.availability import (
    MODE_MEETING,
    MODE_WORK_BLOCK,
    format_slots_email,
    format_slots_mattermost,
    get_availability,
    has_avoid_day_slots,
    format_avoid_day_warning,
    parse_timeframe,
)
from artemis.briefs import handle_mention
from artemis.calendar import CalendarClient
from artemis.commitments import (
    add_commitment,
    close_commitment,
    format_close_result,
    format_commitments_list,
    get_db,
    list_commitments,
    get_commitments_for_client,
    log_calendar_action,
    parse_close_title,
)
from artemis.crm import format_contacts_list, init_db as init_crm_db, list_contacts
from artemis.crm_client import CRMClient
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
from artemis.life_ops import (
    get_db as init_life_ops_db,
    handle_grocery_command,
    handle_health_command,
    handle_workout_command,
    load_health_plan,
)
from artemis.mattermost import MattermostClient
from artemis.prompts import UNTRUSTED_PREFIX
from artemis.quiet_hours import (
    clear_timezone_override,
    enter_quiet,
    exit_quiet,
    extend_override,
    get_quiet_state,
    is_quiet,
    is_quiet_hours,
    quiet_hours_status,
    resolve_city_timezone,
    set_timezone_override,
    start_override,
    update_last_interaction,
)
from artemis.scheduler import ArtemisScheduler, get_playbook_text
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

# Pending confirmation actions keyed by channel_id
# Each value is a dict with "type", "data", and "timestamp"
_pending_confirms: dict[str, dict] = {}

# Pending availability reply flow keyed by channel_id
# Stores slots and email context for send/confirm/edit flow
_pending_availability: dict[str, dict] = {}


@app.route("/webhook/uptime", methods=["POST"])
def uptime_webhook():
    """Receive Uptime Robot webhook notifications."""
    data = request.json or {}
    monitor_name = data.get("monitorFriendlyName", data.get("monitor_name", "Unknown"))
    alert_type = data.get("alertType", data.get("alert_type", ""))
    url = data.get("monitorURL", data.get("monitor_url", ""))

    # alertType 1 = down, 2 = up (Uptime Robot convention)
    if str(alert_type) == "1":
        msg = f"\u26a0\ufe0f \U0001f534 **{monitor_name}** is DOWN"
    elif str(alert_type) == "2":
        msg = f"\u26a0\ufe0f \U0001f7e2 **{monitor_name}** recovered"
    else:
        msg = f"\u26a0\ufe0f **{monitor_name}** alert (type={alert_type})"

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


# ---------------------------------------------------------------------------
# Voice endpoint — Deepgram STT + ElevenLabs TTS
# ---------------------------------------------------------------------------

_voice_api_key = None


def _verify_voice_key():
    """Verify X-API-Key header. Returns error response or None if OK."""
    global _voice_api_key
    if _voice_api_key is None:
        try:
            from knowledge.secrets import get_crm_api_key
            _voice_api_key = get_crm_api_key()
        except Exception:
            logger.exception("Failed to load CRM API key for voice auth")
            return jsonify({"error": "Auth misconfigured"}), 500
    key = request.headers.get("X-API-Key", "")
    if key != _voice_api_key:
        return jsonify({"error": "Invalid API key"}), 403
    return None


@app.route("/voice", methods=["POST"])
def voice_endpoint():
    """Accept audio, transcribe, process, and return spoken response."""
    auth_err = _verify_voice_key()
    if auth_err:
        return auth_err

    if "audio" not in request.files:
        return jsonify({"error": "No 'audio' file in request"}), 400

    audio_file = request.files["audio"]
    audio_bytes = audio_file.read()
    mime_type = audio_file.content_type or "audio/webm"

    if len(audio_bytes) == 0:
        return jsonify({"error": "Empty audio file"}), 400

    logger.info("Voice request: %d bytes, mime=%s", len(audio_bytes), mime_type)

    try:
        from artemis.voice import process_voice_query

        response_text, audio_out = process_voice_query(
            audio_bytes=audio_bytes,
            mime_type=mime_type,
            mm_client=_mm,
            gmail_client=_gmail,
            calendar_client=_calendar,
        )

        return Response(
            audio_out,
            mimetype="audio/mpeg",
            headers={
                "X-Transcript": response_text[:500].replace("\n", " "),
            },
        )
    except Exception:
        logger.exception("Voice processing failed")
        return jsonify({"error": "Voice processing failed"}), 500


@app.route("/voice/health", methods=["GET"])
def voice_health():
    """Health check for voice subsystem."""
    return jsonify({"status": "ok", "stt": "deepgram", "tts": "elevenlabs"})


def _build_mention_context(post: dict, gmail: GmailClient, calendar: CalendarClient, question: str = "") -> str:
    """Build data context for an @mention response.

    If the question references a multi-day timeframe, includes events for that
    range in addition to today's calendar.
    """
    parts = []

    # Time awareness
    now = datetime.now()
    day_name = now.strftime("%A")
    time_str = now.strftime("%I:%M %p")
    parts.append(f"**Current time:** {day_name}, {time_str}")

    # Recent emails — fetch full bodies so Claude has real content
    try:
        messages = gmail.get_recent_messages(max_results=10)
        if messages:
            parts.append("\n**Recent emails (last 5 threads):**")
            for m in messages[:5]:
                body = gmail.get_full_message(m["id"])
                if body:
                    parts.append(
                        f"- From: {m['from']} | Subject: {m['subject']}\n"
                        f"  Body: {body[:1000]}"
                    )
                else:
                    parts.append(f"- From: {m['from']} | Subject: {m['subject']} | {m['snippet'][:200]}")
    except Exception:
        logger.exception("Failed to get emails for mention context")

    # Calendar from cache
    try:
        from artemis import calendar_cache
        from collections import defaultdict
        from datetime import datetime as dt

        events = calendar_cache.get_events()
        if events:
            parts.append(f"\n**Calendar ({calendar_cache.status()}):**")
            by_day: dict = defaultdict(list)
            for e in events:
                day_key = e["start"][:10]
                by_day[day_key].append(e)
            for day in sorted(by_day.keys()):
                label = dt.strptime(day, "%Y-%m-%d").strftime("%a %b %-d")
                parts.append(f"\n  {label}")
                for e in by_day[day]:
                    external = [a for a in e.get("attendees", []) if not a.get("self")]
                    attendee_str = ", ".join(a.get("name") or a.get("email", "") for a in external) if external else "(solo)"
                    time_str = e["start"][11:16] if "T" in e["start"] else "all-day"
                    parts.append(f"  - {e['summary']} at {time_str} — {attendee_str}")
        else:
            parts.append("\n**Calendar:** No events in window.")
    except Exception:
        logger.exception("Failed to build calendar context from cache")

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


def _process_calendar_events(response: str, channel_id: str = "") -> str:
    """Parse calendar_event blocks from Claude's response and create real events.

    Safety rules:
    - Events with external attendees are drafted, not created — requires confirmation.
    - Duplicate/conflict detection within ±2 hours — warns before creating.
    - All creations are audit-logged.
    """
    pattern = r"```calendar_event\s*\n(.*?)\n```"
    matches = list(re.finditer(pattern, response, re.DOTALL))
    if not matches:
        return response

    if not _calendar or not _calendar.service:
        return re.sub(
            pattern,
            "\n> :red_circle: Calendar not connected — event NOT created.\n",
            response,
            flags=re.DOTALL,
        )

    local_tz = ZoneInfo(config.TIMEZONE)

    for match in reversed(matches):
        try:
            data = json.loads(match.group(1))
            summary = data["summary"]
            date_str = data["date"]
            start_time = data["start_time"]
            end_time = data["end_time"]
            description = data.get("description")
            attendees = data.get("attendees") or []

            start_dt = datetime.strptime(f"{date_str} {start_time}", "%Y-%m-%d %H:%M")
            start_dt = start_dt.replace(tzinfo=local_tz)
            end_dt = datetime.strptime(f"{date_str} {end_time}", "%Y-%m-%d %H:%M")
            end_dt = end_dt.replace(tzinfo=local_tz)

            # ── Rule 1: External attendee gating ──
            if attendees:
                attendee_str = ", ".join(attendees)
                # Store as pending — don't create yet
                _pending_confirms[channel_id] = {
                    "type": "calendar_create_external",
                    "data": data,
                    "timestamp": time.time(),
                }
                replacement = (
                    f"\n> :calendar: **Proposed** calendar invite to {attendee_str} "
                    f"for **{summary}** on {date_str} {start_time}–{end_time}.\n"
                    f"> Reply `confirm` to send or `cancel` to discard.\n"
                )
                log_calendar_action(
                    action="draft",
                    event_id="pending",
                    summary=summary,
                    attendees=attendee_str,
                    user_approved=False,
                    notes="Awaiting user confirmation for external attendees",
                )
                response = response[:match.start()] + replacement + response[match.end():]
                continue

            # ── Rule 2: Duplicate / conflict detection ──
            nearby = _calendar.get_events_around(start_dt, window_hours=2)
            conflict = None
            for existing in nearby:
                # Same attendee overlap or similar name on same day
                if summary.lower() in existing["summary"].lower() or existing["summary"].lower() in summary.lower():
                    conflict = existing
                    break
                # Check time overlap
                try:
                    ex_start = datetime.fromisoformat(existing["start"])
                    if abs((ex_start - start_dt).total_seconds()) < 3600:  # within 1 hour
                        conflict = existing
                        break
                except (ValueError, TypeError):
                    pass

            if conflict:
                _pending_confirms[channel_id] = {
                    "type": "calendar_create_conflict",
                    "data": data,
                    "conflict": conflict,
                    "timestamp": time.time(),
                }
                replacement = (
                    f"\n> :warning: You already have **{conflict['summary']}** at {conflict['start']} on that day.\n"
                    f"> Create **{summary}** at {start_time} anyway? Reply `yes` to confirm.\n"
                )
                response = response[:match.start()] + replacement + response[match.end():]
                continue

            # ── No blockers — create directly ──
            event_id = _calendar.create_event(
                summary=summary,
                start_datetime=start_dt,
                end_datetime=end_dt,
                description=description,
            )

            if event_id:
                log_calendar_action(
                    action="create",
                    event_id=event_id,
                    summary=summary,
                    attendees="",
                    auto_created=True,
                    notes="Internal event, no attendees",
                )
                replacement = (
                    f"\n> :white_check_mark: Event created: **{summary}** on "
                    f"{date_str} {start_time}–{end_time} (ID: `{event_id}`)\n"
                )
            else:
                replacement = f"\n> :red_circle: Failed to create event: **{summary}** — check logs.\n"

        except (json.JSONDecodeError, KeyError, ValueError) as e:
            logger.warning("Failed to parse calendar_event block: %s", e)
            replacement = f"\n> :warning: Could not parse calendar event — {e}\n"

        response = response[:match.start()] + replacement + response[match.end():]

    return response


def _process_commitments(response: str, channel_id: str = "") -> str:
    """Parse commitment blocks from Claude's response and save to SQLite.

    Format: ```commitment\n{"title": "...", "due_date": "...", "client": "..."}\n```
    Returns the response with blocks replaced by confirmation messages.
    """
    pattern = r"```commitment\s*\n(.*?)\n```"
    matches = list(re.finditer(pattern, response, re.DOTALL))
    if not matches:
        return response

    for match in reversed(matches):
        try:
            data = json.loads(match.group(1))
            title = data.get("title", "").strip()
            due_date = data.get("due_date", "").strip()
            client = data.get("client", "").strip()

            if not title:
                logger.warning("Empty commitment title in Claude response — skipping")
                replacement = ""
            else:
                logger.debug("Saving commitment: %s (due=%s, client=%s)", title, due_date, client)
                try:
                    cid = add_commitment(
                        title=title,
                        due_date=due_date or "",
                        effort_days=1,
                        client=client,
                    )
                    logger.info("Commitment #%d saved: %s", cid, title)
                    due_str = f" (due {due_date})" if due_date else ""
                    client_str = f" [{client}]" if client else ""
                    replacement = f"\n> \U0001f4cc Commitment logged: **{title}**{due_str}{client_str}\n"
                except Exception:
                    logger.exception("Failed to save commitment: %s", title)
                    replacement = f"\n> \u26a0\ufe0f Failed to save commitment: {title} — check logs\n"

        except (json.JSONDecodeError, KeyError, ValueError) as e:
            logger.warning("Failed to parse commitment block: %s", e)
            replacement = f"\n> \u26a0\ufe0f Could not parse commitment — {e}\n"

        response = response[:match.start()] + replacement + response[match.end():]

    return response


def _handle_calendar_confirm(post: dict, question: str) -> bool:
    """Handle confirmation replies for pending calendar actions. Returns True if handled."""
    q_lower = question.lower().strip()
    channel_id = post.get("channel_id", "")
    root_id = post.get("root_id") or post["id"]

    if channel_id not in _pending_confirms:
        return False

    pending = _pending_confirms[channel_id]
    # Expire after 10 minutes
    if time.time() - pending["timestamp"] > 600:
        del _pending_confirms[channel_id]
        return False

    if q_lower not in ("confirm", "yes", "cancel", "no", "approve", "deny"):
        return False

    # Map approve/deny to yes/cancel for unified handling
    if q_lower == "approve":
        q_lower = "yes"
    elif q_lower == "deny":
        q_lower = "cancel"

    # Only handle calendar create types here; other types handled by their own handlers
    if pending.get("type") not in (None, "calendar_create", "calendar_create_external", "calendar_create_conflict"):
        return False

    local_tz = ZoneInfo(config.TIMEZONE)
    data = pending["data"]

    if q_lower in ("cancel", "no"):
        del _pending_confirms[channel_id]
        # Log guardrail denial if this was an external attendee block
        if pending.get("type") == "calendar_create_external":
            from artemis.guardrails import get_external_attendees, log_violation
            ext = get_external_attendees(data.get("attendees") or [])
            if ext:
                log_violation(data.get("summary", ""), ext, "denied")
        log_calendar_action(
            action="cancelled",
            event_id="pending",
            summary=data.get("summary", ""),
            notes="User cancelled/denied pending event",
        )
        if _mm:
            _mm.post_to_channel_id(channel_id, "Calendar event cancelled.", root_id=root_id)
        return True

    # confirm / yes
    if q_lower in ("confirm", "yes"):
        summary = data["summary"]
        date_str = data["date"]
        start_time_str = data["start_time"]
        end_time_str = data["end_time"]
        description = data.get("description")
        attendees = data.get("attendees") or []

        start_dt = datetime.strptime(f"{date_str} {start_time_str}", "%Y-%m-%d %H:%M").replace(tzinfo=local_tz)
        end_dt = datetime.strptime(f"{date_str} {end_time_str}", "%Y-%m-%d %H:%M").replace(tzinfo=local_tz)

        event_id = _calendar.create_event(
            summary=summary,
            start_datetime=start_dt,
            end_datetime=end_dt,
            description=description,
            attendees=attendees if attendees else None,
            _user_approved_external=True,  # User explicitly confirmed via Mattermost
        )

        attendee_str = ", ".join(attendees) if attendees else ""
        if event_id:
            log_calendar_action(
                action="create",
                event_id=event_id,
                summary=summary,
                attendees=attendee_str,
                user_approved=True,
                notes=f"Confirmed by user (type: {pending['type']})",
            )
            reply = (
                f":white_check_mark: Event created: **{summary}** on "
                f"{date_str} {start_time_str}–{end_time_str} (ID: `{event_id}`)"
            )
        else:
            reply = f":red_circle: Failed to create event: **{summary}** — check logs."

        del _pending_confirms[channel_id]
        if _mm:
            _mm.post_to_channel_id(channel_id, reply, root_id=root_id)
        return True

    return False


def _handle_delete_event(post: dict, question: str) -> bool:
    """Handle '@artemis delete event <id_or_name>'. Returns True if handled."""
    q_lower = question.lower().strip()
    if not q_lower.startswith("delete event "):
        return False

    channel_id = post.get("channel_id", "")
    root_id = post.get("root_id") or post["id"]
    identifier = question.strip()[len("delete event "):].strip()

    if not identifier:
        if _mm:
            _mm.post_to_channel_id(channel_id, "Usage: `delete event <event_id or event name>`", root_id=root_id)
        return True

    if not _calendar or not _calendar.service:
        if _mm:
            _mm.post_to_channel_id(channel_id, "Calendar not connected.", root_id=root_id)
        return True

    # Try as event ID first, then search by name
    event = _calendar.get_event(identifier)
    if not event:
        event = _calendar.find_event_by_name(identifier)

    if not event:
        if _mm:
            _mm.post_to_channel_id(channel_id, f"Event not found: {identifier}", root_id=root_id)
        return True

    # Store pending deletion — require confirmation
    _pending_confirms[channel_id] = {
        "type": "calendar_delete",
        "data": {"event_id": event["id"], "summary": event["summary"], "start": event["start"]},
        "timestamp": time.time(),
    }

    reply = (
        f"Delete **{event['summary']}** at {event['start']}?\n"
        f"Reply `yes` to confirm."
    )
    if _mm:
        _mm.post_to_channel_id(channel_id, reply, root_id=root_id)
    return True


def _handle_delete_confirm(post: dict, question: str) -> bool:
    """Handle 'yes' confirmation for pending event deletions."""
    q_lower = question.lower().strip()
    channel_id = post.get("channel_id", "")
    root_id = post.get("root_id") or post["id"]

    if channel_id not in _pending_confirms:
        return False

    pending = _pending_confirms[channel_id]
    if pending["type"] != "calendar_delete":
        return False

    if time.time() - pending["timestamp"] > 600:
        del _pending_confirms[channel_id]
        return False

    if q_lower not in ("yes", "no", "cancel"):
        return False

    data = pending["data"]
    del _pending_confirms[channel_id]

    if q_lower in ("no", "cancel"):
        if _mm:
            _mm.post_to_channel_id(channel_id, "Deletion cancelled.", root_id=root_id)
        return True

    if q_lower == "yes":
        success = _calendar.delete_event(data["event_id"])
        if success:
            log_calendar_action(
                action="delete",
                event_id=data["event_id"],
                summary=data["summary"],
                user_approved=True,
                notes="Deleted by user via @mention",
            )
            reply = f":white_check_mark: Deleted **{data['summary']}**."
        else:
            reply = f":red_circle: Failed to delete **{data['summary']}** — check logs."
        if _mm:
            _mm.post_to_channel_id(channel_id, reply, root_id=root_id)
        return True

    return False


# ---------- Bulk convert work sessions to tasks ----------

_CONVERT_PATTERNS = [
    re.compile(r"convert\s+(them|these|work\s*sessions?)\s+to\s+tasks?", re.I),
    re.compile(r"delete\s+(and|&|\+)\s+add\s+(them\s+)?as\s+tasks?", re.I),
    re.compile(r"convert\s+to\s+tasks?", re.I),
    re.compile(r"make\s+(them|these)\s+tasks?", re.I),
]


def _handle_convert_to_tasks(post: dict, question: str) -> bool:
    """Handle bulk convert-work-sessions-to-tasks flow. Returns True if handled."""
    q_lower = question.lower().strip()
    channel_id = post.get("channel_id", "")
    root_id = post.get("root_id") or post["id"]

    # ---- Phase 2: confirmation of a pending bulk_convert ----
    if channel_id in _pending_confirms:
        pending = _pending_confirms[channel_id]
        if pending["type"] == "bulk_convert_to_tasks":
            if time.time() - pending["timestamp"] > 600:
                del _pending_confirms[channel_id]
                return False
            if q_lower in ("yes", "confirm", "execute"):
                events = pending["events"]
                deleted = 0
                added = 0
                errors = []
                for ev in events:
                    ok = _calendar.delete_event(ev["event_id"])
                    if ok:
                        log_calendar_action(
                            action="delete",
                            event_id=ev["event_id"],
                            summary=ev["summary"],
                            user_approved=True,
                            notes="Bulk convert to task",
                        )
                        deleted += 1
                    else:
                        errors.append(f"Failed to delete: {ev['summary']}")
                    try:
                        logger.debug("Bulk convert: saving commitment '%s'", ev["summary"])
                        add_commitment(
                            title=ev["summary"],
                            due_date="",
                            effort_days=2,
                        )
                        added += 1
                    except Exception:
                        logger.exception("Failed to save commitment during bulk convert: %s", ev["summary"])
                        errors.append(f"Failed to save task: {ev['summary']}")
                del _pending_confirms[channel_id]
                parts = [f":white_check_mark: Deleted {deleted} event(s), added {added} task(s)."]
                if errors:
                    parts.append("\n".join(errors))
                if _mm:
                    _mm.post_to_channel_id(channel_id, "\n".join(parts), root_id=root_id)
                return True
            if q_lower in ("no", "cancel"):
                del _pending_confirms[channel_id]
                if _mm:
                    _mm.post_to_channel_id(channel_id, "Bulk convert cancelled.", root_id=root_id)
                return True
            # Not a yes/no — fall through so other handlers can try
            return False

    # ---- Phase 1: detect convert intent ----
    if not any(p.search(question) for p in _CONVERT_PATTERNS):
        return False

    if not _calendar or not _calendar.service:
        if _mm:
            _mm.post_to_channel_id(channel_id, "Calendar not connected.", root_id=root_id)
        return True

    # Extract event names from the thread context (look for quoted or listed items)
    # Also search for "work session" events in the next 14 days as a fallback
    summaries: list[str] = []

    # Try to pull names from the message (e.g., lines starting with bullet/dash/number)
    for line in question.split("\n"):
        line = line.strip().lstrip("-*•0123456789.) ").strip()
        if line and line.lower() not in (
            "convert them to tasks",
            "convert these to tasks",
            "convert to tasks",
            "delete and add as tasks",
        ):
            summaries.append(line)

    # Fallback: search for "work session" events in the next 14 days
    if not summaries:
        from datetime import date, timedelta
        start = date.today()
        end = start + timedelta(days=14)
        all_events = _calendar.get_events_in_range(start, end)
        for ev in all_events:
            if "work session" in ev["summary"].lower():
                summaries.append(ev["summary"])

    if not summaries:
        if _mm:
            _mm.post_to_channel_id(
                channel_id,
                "I didn't find any work session events to convert. "
                "List the event names or I'll look for events with 'Work Session' in the title.",
                root_id=root_id,
            )
        return True

    # Look up each event
    found_events = []
    not_found = []
    for name in summaries:
        ev = _calendar.find_event_by_name(name, days_ahead=14)
        if ev:
            found_events.append({"event_id": ev["id"], "summary": ev["summary"], "start": ev["start"]})
        else:
            not_found.append(name)

    if not found_events:
        msg = "No matching calendar events found for:\n" + "\n".join(f"- {n}" for n in not_found)
        if _mm:
            _mm.post_to_channel_id(channel_id, msg, root_id=root_id)
        return True

    # Store pending and show confirmation
    _pending_confirms[channel_id] = {
        "type": "bulk_convert_to_tasks",
        "events": found_events,
        "timestamp": time.time(),
    }

    lines = ["**Ready to execute:**"]
    lines.append(f"- Delete **{len(found_events)}** Work Session event(s) from calendar :white_check_mark:")
    lines.append(f"- Add **{len(found_events)}** commitment(s) to task list :white_check_mark:")
    if not_found:
        lines.append(f"\n:warning: Not found (skipped): {', '.join(not_found)}")
    lines.append("\nReply **yes** to confirm all.")

    if _mm:
        _mm.post_to_channel_id(channel_id, "\n".join(lines), root_id=root_id)
    return True


def _handle_availability_command(post: dict, question: str) -> bool:
    """Handle 'send', 'edit', 'cancel' for pending availability replies.

    Also handles 'confirm' for pending draft replies.
    Returns True if handled.
    """
    q_lower = question.lower().strip()
    channel_id = post.get("channel_id", "")
    root_id = post.get("root_id") or post["id"]

    if channel_id not in _pending_availability:
        return False

    pending = _pending_availability[channel_id]

    # Expire after 30 minutes
    if time.time() - pending.get("created_at", 0) > 1800:
        del _pending_availability[channel_id]
        return False

    # ── Phase 2: Draft confirmation ──
    if pending.get("phase") == "draft_review":
        if q_lower == "confirm":
            # Send the reply via Gmail
            if _gmail:
                in_reply_to = ""
                msg_id = pending.get("message_id", "")
                if msg_id:
                    in_reply_to = _gmail.get_message_id_header(msg_id)

                success = _gmail.send_reply(
                    thread_id=pending["thread_id"],
                    to=pending["sender_email"],
                    subject=pending["subject"],
                    body=pending["draft_body"],
                    in_reply_to=in_reply_to,
                )

                if success:
                    from artemis.inbox import mark_waiting
                    mark_waiting(pending["thread_id"], waiting_on=pending["sender_name"])
                    reply = (
                        f":white_check_mark: Reply sent to {pending['sender_email']}. "
                        f"Thread marked WAITING on {pending['sender_name']}."
                    )
                else:
                    reply = ":red_circle: Failed to send reply — check logs."
            else:
                reply = "Gmail not connected — cannot send."

            del _pending_availability[channel_id]
            if _mm:
                _mm.post_to_channel_id(channel_id, reply, root_id=root_id)
            return True

        elif q_lower in ("cancel", "no"):
            del _pending_availability[channel_id]
            if _mm:
                _mm.post_to_channel_id(channel_id, "Reply cancelled.", root_id=root_id)
            return True

        elif q_lower == "edit":
            # Show the raw draft for manual editing
            draft = pending.get("draft_body", "")
            if _mm:
                _mm.post_to_channel_id(
                    channel_id,
                    f"Current draft:\n```\n{draft}\n```\nPaste your edited version and I'll use that instead.",
                    root_id=root_id,
                )
            # Stay in draft_review phase — next non-command message will be treated as edited text
            return True

        else:
            # Treat any other text as an edited draft replacement
            pending["draft_body"] = question
            if _mm:
                _mm.post_to_channel_id(
                    channel_id,
                    f"Draft updated. Reply `confirm` to send or `cancel` to discard.",
                    root_id=root_id,
                )
            return True

    # ── Phase 1: Slot selection ──
    if q_lower in ("cancel", "no"):
        del _pending_availability[channel_id]
        if _mm:
            _mm.post_to_channel_id(channel_id, "Availability reply cancelled.", root_id=root_id)
        return True

    # Parse "send 1,3,5" or "send all"
    send_match = re.match(r"send\s+(.+)", q_lower)
    if not send_match:
        return False

    selection = send_match.group(1).strip()
    slots = pending.get("slots", [])

    if selection == "all":
        selected = slots
    else:
        # Parse comma-separated numbers
        try:
            indices = [int(x.strip()) for x in selection.split(",")]
            selected = [slots[i - 1] for i in indices if 0 < i <= len(slots)]
        except (ValueError, IndexError):
            if _mm:
                _mm.post_to_channel_id(
                    channel_id,
                    f"Invalid selection. Use `send 1,3,5` or `send all`.",
                    root_id=root_id,
                )
            return True

    if not selected:
        if _mm:
            _mm.post_to_channel_id(channel_id, "No valid slots selected.", root_id=root_id)
        return True

    # Generate draft directly from real calendar slots — no Claude rewrite.
    # format_slots_email produces the exact template with specific dates/times.
    sender_first = pending.get("sender_name", "").split()[0] if pending.get("sender_name", "").strip() else ""
    draft_body = format_slots_email(selected, sender_first_name=sender_first)

    # Move to Phase 2: draft review
    pending["phase"] = "draft_review"
    pending["draft_body"] = draft_body
    pending["selected_slots"] = selected
    pending["created_at"] = time.time()  # reset timer

    if _mm:
        _mm.post_to_channel_id(
            channel_id,
            f"**Draft reply to {pending.get('sender_email', '')}:**\n\n"
            f"```\n{draft_body}\n```\n\n"
            f"Reply `confirm` to send, `edit` to modify, or `cancel` to discard.",
            root_id=root_id,
        )
    return True


def _detect_availability_mode(text: str) -> str:
    """Detect whether user wants MEETING or WORK_BLOCK availability.

    WORK_BLOCK keywords: "work block", "focus time", "head down", "working session",
    "schedule time to work on", "block time", "SCORE prep", "development", "deep work"

    Everything else defaults to MEETING.
    """
    lower = text.lower()
    _WORK_BLOCK_KEYWORDS = [
        "work block", "focus time", "head down", "working session",
        "schedule time to work on", "block time", "score prep",
        "development time", "deep work", "focus session",
    ]
    for kw in _WORK_BLOCK_KEYWORDS:
        if kw in lower:
            return MODE_WORK_BLOCK
    return MODE_MEETING


def _handle_availability_mention(post: dict, question: str) -> bool:
    """Handle '@artemis availability [timeframe]' or '@artemis when am I free'.

    Direct availability check — no email context, just shows open slots.
    Detects MEETING vs WORK_BLOCK mode from keywords.
    """
    q_lower = question.lower().strip()

    # Match "availability ...", "when am i free ...", "when am I free ..."
    is_avail = q_lower.startswith("availability")
    is_free = "when am i free" in q_lower or "when are you free" in q_lower

    if not is_avail and not is_free:
        return False

    channel_id = post.get("channel_id", "")
    root_id = post.get("root_id") or post["id"]

    if not _calendar or not _calendar.service:
        if _mm:
            _mm.post_to_channel_id(channel_id, "Calendar not connected.", root_id=root_id)
        return True

    mode = _detect_availability_mode(question)
    start_date, end_date = parse_timeframe(question)

    slots = get_availability(_calendar, start_date, end_date, mode=mode)
    formatted = format_slots_mattermost(slots)

    if _mm:
        _mm.post_to_channel_id(channel_id, formatted, root_id=root_id)
    return True


def _handle_timezone_command(post: dict, question: str) -> bool:
    """Handle timezone override commands.

    Patterns:
      - "I'm in Paris" / "i'm in Tokyo this week"
      - "timezone Europe/Paris"
      - "I'm back home" / "I'm in Milwaukee" / "reset timezone"
    """
    q_lower = question.lower().strip()
    channel_id = post.get("channel_id", "")
    root_id = post.get("root_id") or post["id"]

    # Reset patterns
    if q_lower in ("i'm back home", "im back home", "i'm home", "im home", "reset timezone"):
        reply = clear_timezone_override()
        if _mm:
            _mm.post_to_channel_id(channel_id, reply, root_id=root_id)
        return True

    # "timezone Europe/Paris" — raw IANA
    if q_lower.startswith("timezone "):
        tz_input = question[len("timezone "):].strip()
        tz_name = resolve_city_timezone(tz_input)
        if tz_name:
            # Check if it's the home timezone
            if tz_name == config.HOME_TIMEZONE:
                reply = clear_timezone_override()
            else:
                reply = set_timezone_override(tz_name, city_name=tz_input)
            if _mm:
                _mm.post_to_channel_id(channel_id, reply, root_id=root_id)
            return True
        else:
            if _mm:
                _mm.post_to_channel_id(
                    channel_id,
                    f"I don't recognize that timezone: `{tz_input}`. "
                    f"Try a city name (e.g., Paris, Tokyo) or IANA timezone (e.g., Europe/Paris).",
                    root_id=root_id,
                )
            return True

    # "I'm in [city]" pattern
    im_in_match = re.match(r"i['\u2019]?m\s+in\s+(.+?)(?:\s+this\s+week|\s+for\s+\d+\s+days?)?$", q_lower)
    if im_in_match:
        city = im_in_match.group(1).strip()

        # Extract optional duration
        days = 7  # default
        duration_match = re.search(r"for\s+(\d+)\s+days?", q_lower)
        if duration_match:
            days = int(duration_match.group(1))

        tz_name = resolve_city_timezone(city)
        if tz_name:
            # Home city → reset
            if tz_name == config.HOME_TIMEZONE:
                reply = clear_timezone_override()
            else:
                reply = set_timezone_override(tz_name, city_name=city, days=days)
            if _mm:
                _mm.post_to_channel_id(channel_id, reply, root_id=root_id)
            return True
        else:
            if _mm:
                _mm.post_to_channel_id(
                    channel_id,
                    f"I don't recognize \"{city}\" as a city. "
                    f"Try `timezone Europe/Paris` with an IANA timezone name instead.",
                    root_id=root_id,
                )
            return True

    return False


def _handle_calendar_view_mention(post: dict, question: str) -> bool:
    """Handle requests to VIEW scheduled events (not find open slots).

    Patterns: "what's on my calendar", "show me my calendar", "events this week",
    "do you see my calendar", "calendar tomorrow", "show events through Friday", etc.

    Returns True if handled.
    """
    q_lower = question.lower().strip()

    # Calendar view intent patterns
    _VIEW_PATTERNS = [
        r"\b(show|see|view|display|pull up|check)\s+(me\s+)?(my\s+)?(calendar|events|schedule|sessions|meetings)",
        r"\bwhat.?s?\s+on\s+(my\s+)?(calendar|schedule)",
        r"\bdo\s+you\s+see\s+(my\s+)?(calendar|events|schedule|sessions|work\s+sessions|meetings)",
        r"^(calendar|events|meetings|schedule)\b",
        r"\b(my\s+)?(calendar|events|meetings)\s+(for|this|next|tomorrow|today)",
    ]

    is_view = False
    for pattern in _VIEW_PATTERNS:
        if re.search(pattern, q_lower):
            is_view = True
            break

    if not is_view:
        return False

    channel_id = post.get("channel_id", "")
    root_id = post.get("root_id") or post["id"]

    if not _calendar or not _calendar.service:
        if _mm:
            _mm.post_to_channel_id(channel_id, "Calendar not connected.", root_id=root_id)
        return True

    # Parse timeframe from the question (defaults to next 5 business days)
    start_date, end_date = parse_timeframe(question)

    # For bare "calendar" / "events" with no timeframe hint, default to today
    bare_match = re.match(r"^(calendar|events|meetings|schedule|my calendar|my events)$", q_lower)
    if bare_match:
        from datetime import date as _date
        start_date = _date.today()
        end_date = _date.today()

    events = _calendar.get_events_in_range(start_date, end_date)

    if not events:
        date_range_str = _format_date_range(start_date, end_date)
        reply = f":calendar: No events scheduled {date_range_str}."
        if _mm:
            _mm.post_to_channel_id(channel_id, reply, root_id=root_id)
        return True

    # Group events by day
    from collections import defaultdict
    by_day: dict[str, list[dict]] = defaultdict(list)
    for e in events:
        start_str = e.get("start", "")
        try:
            ev_start = datetime.fromisoformat(start_str)
            day_key = ev_start.strftime("%a %b %d")
            time_str = ev_start.strftime("%I:%M %p").lstrip("0")
        except (ValueError, TypeError):
            # All-day events — just a date string like "2026-03-20"
            try:
                from datetime import date as _date
                d = _date.fromisoformat(start_str)
                day_key = d.strftime("%a %b %d")
                time_str = "all day"
            except (ValueError, TypeError):
                day_key = "Unknown"
                time_str = ""

        by_day[day_key].append({
            "summary": e.get("summary", "(no title)"),
            "time": time_str,
            "attendees": e.get("attendees", []),
        })

    # Format response
    lines = [":calendar: **Scheduled events:**"]
    for day_label, day_events in by_day.items():
        lines.append(f"\n**{day_label}**")
        for ev in day_events:
            external = [a for a in ev["attendees"] if not a.get("self")]
            if external:
                attendee_str = " — " + ", ".join(
                    a.get("name") or a.get("email", "") for a in external
                )
            else:
                attendee_str = ""
            lines.append(f"- {ev['summary']} at {ev['time']}{attendee_str}")

    reply = "\n".join(lines)
    if _mm:
        _mm.post_to_channel_id(channel_id, reply, root_id=root_id)
    return True


def _format_date_range(start_date, end_date) -> str:
    """Format a date range for display (e.g., 'Thu Mar 20 – Fri Mar 21')."""
    if start_date == end_date:
        return f"on {start_date.strftime('%a %b %d')}"
    return f"{start_date.strftime('%a %b %d')} – {end_date.strftime('%a %b %d')}"


def _handle_scheduling_mention(post: dict, question: str) -> bool:
    """Detect scheduling/availability questions and respond with real calendar slots.

    Catches questions like "when can we meet", "schedule a call", "find time",
    "what's your availability", etc. that would otherwise fall through to
    Claude's freeform handler which might produce vague language.

    Returns True if handled.
    """
    q_lower = question.lower().strip()

    # Skip if already handled by the explicit "availability" command
    if q_lower.startswith("availability") or "when am i free" in q_lower:
        return False

    # Scheduling intent patterns
    _SCHEDULING_PATTERNS = [
        r"\b(schedule|set up|arrange|book)\s+(a\s+)?(call|meeting|chat|time|session)",
        r"\bwhen\s+(can|could|should)\s+(we|i|you)\s+(meet|talk|chat|connect|call)",
        r"\bfind\s+(a\s+)?time\s+(to|for)",
        r"\bwhat.?s?\s+(your|my)\s+(availability|schedule|calendar)",
        r"\b(free|available|open)\s+(time|slot|hour)",
        r"\blet.?s?\s+(meet|connect|chat|talk|hop on)",
        r"\bgrab\s+time",
        r"\bset\s+up\s+time",
        r"\bpick\s+(a\s+)?time",
    ]

    is_scheduling = False
    for pattern in _SCHEDULING_PATTERNS:
        if re.search(pattern, q_lower):
            is_scheduling = True
            break

    if not is_scheduling:
        return False

    channel_id = post.get("channel_id", "")
    root_id = post.get("root_id") or post["id"]

    if not _calendar or not _calendar.service:
        if _mm:
            _mm.post_to_channel_id(channel_id, "Calendar not connected.", root_id=root_id)
        return True

    # Detect mode and extract timeframe
    mode = _detect_availability_mode(question)
    start_date, end_date = parse_timeframe(question)
    slots = get_availability(_calendar, start_date, end_date, mode=mode)

    if not slots:
        reply = (
            "I checked your calendar but didn't find open slots in that timeframe.\n\n"
            f"Booking link: {config.BOOKING_LINK}" if config.BOOKING_LINK else
            "I checked your calendar but didn't find open slots in that timeframe."
        )
        if _mm:
            _mm.post_to_channel_id(channel_id, reply, root_id=root_id)
        return True

    # Format with real, specific times
    from artemis.quiet_hours import get_tz_abbrev
    tz_abbrev = get_tz_abbrev()

    lines = [":calendar: **Here are your next available slots:**", ""]
    for i, slot in enumerate(slots, 1):
        date_str = slot["date"].strftime("%A, %B %d")
        start_str = slot["start"].strftime("%I:%M %p").lstrip("0")
        lines.append(f"{i}. {date_str} — {start_str} {tz_abbrev}")

    if config.BOOKING_LINK:
        lines.append(f"\nBooking link: {config.BOOKING_LINK}")

    reply = "\n".join(lines)
    if _mm:
        _mm.post_to_channel_id(channel_id, reply, root_id=root_id)
    return True


def _handle_quiet_command(post: dict, question: str) -> bool:
    """Handle quiet hours session commands (goodnight, good morning, override, extend).

    Returns True if handled.
    """
    q_lower = question.lower().strip()
    channel_id = post.get("channel_id", "")
    root_id = post.get("root_id") or post["id"]

    # ── Goodnight ──
    if q_lower.startswith("goodnight") or q_lower.startswith("good night"):
        # Parse optional wake time: "goodnight, wake me at 6am" / "good night, wake me at 6:30"
        wake_time = None
        wake_match = re.search(r"wake\s+(?:me\s+)?at\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", q_lower)
        if wake_match:
            hour = int(wake_match.group(1))
            minute = int(wake_match.group(2) or 0)
            ampm = wake_match.group(3)
            if ampm == "pm" and hour != 12:
                hour += 12
            elif ampm == "am" and hour == 12:
                hour = 0
            wake_time = f"{hour:02d}:{minute:02d}"

        reply = enter_quiet(manual=True, wake_time=wake_time)
        if _mm:
            _mm.post_to_channel_id(channel_id, reply, root_id=root_id)
        return True

    # ── Good morning ──
    if q_lower in ("good morning", "morning", "gm", "goodmorning"):
        exit_quiet()

        # Build a quick overnight summary
        summary_parts = ["\u2600\ufe0f Good morning! Quiet hours ended."]

        # Today's calendar
        if _calendar and _calendar.service:
            try:
                events = _calendar.get_today_events()
                if events:
                    summary_parts.append(f"\n\U0001f4c5 **Today:** {len(events)} event(s)")
                    for e in events[:5]:
                        summary_parts.append(f"  \u2022 {e['summary']} at {e['start']}")
            except Exception:
                logger.debug("Failed to get calendar for morning summary")

        # Due commitments
        try:
            from artemis.commitments import get_due_soon
            due = get_due_soon(days=1)
            if due:
                summary_parts.append(f"\n\u2705 **Due today:** {len(due)} commitment(s)")
                for c in due:
                    summary_parts.append(f"  \u2022 {c['title']} ({c['client'] or 'n/a'})")
        except Exception:
            logger.debug("Failed to get commitments for morning summary")

        # Inbox status
        try:
            counts = get_counts()
            na = counts.get(NEEDS_ACTION, 0)
            if na:
                summary_parts.append(f"\n\U0001f4ec **Inbox:** {na} email(s) need action")
        except Exception:
            logger.debug("Failed to get inbox for morning summary")

        reply = "\n".join(summary_parts)
        if _mm:
            _mm.post_to_channel_id(channel_id, reply, root_id=root_id)
        return True

    # ── Override / working session ──
    if q_lower in ("override", "let's work", "lets work", "wake up", "override quiet hours"):
        reply = start_override()
        if _mm:
            _mm.post_to_channel_id(channel_id, reply, root_id=root_id)
        return True

    # "override until 10pm" / "override until 22:00"
    override_until_match = re.match(r"override\s+until\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", q_lower)
    if override_until_match:
        hour = int(override_until_match.group(1))
        minute = int(override_until_match.group(2) or 0)
        ampm = override_until_match.group(3)
        if ampm == "pm" and hour != 12:
            hour += 12
        elif ampm == "am" and hour == 12:
            hour = 0
        until_time = f"{hour:02d}:{minute:02d}"
        reply = start_override(until_time=until_time)
        if _mm:
            _mm.post_to_channel_id(channel_id, reply, root_id=root_id)
        return True

    # ── Extend timer ──
    if q_lower in ("extend", "extend timer", "more time", "keep going"):
        reply = extend_override()
        if _mm:
            _mm.post_to_channel_id(channel_id, reply, root_id=root_id)
        return True

    return False


def _try_life_ops(question: str) -> str | None:
    """Try workout, grocery, and health commands in order."""
    q = question.lower()
    if any(kw in q for kw in [
        "workout", "let's work out", "lets work out", "bench", "squat", "rdl",
        "sets", "reps", "lbs", "rest day", "skip today", "taking today off",
    ]):
        result = handle_workout_command(question)
        if result:
            return result
    if any(kw in q for kw in [
        "grocery", "shopping list", "add to list", "going to aldi",
        "heading to aldi", "need to get", "need ", "put ", "got ",
        "remove ", "done shopping", "finished shopping", "clear grocery",
        "what do i need", "aldi list", "shopping at",
    ]):
        result = handle_grocery_command(question)
        if result:
            return result
    if any(kw in q for kw in [
        "calories", "protein", "meal prep", "sunday prep",
        "weight goal", "daily targets", "what should i eat", "macros",
    ]):
        result = handle_health_command(question)
        if result:
            return result
    return None


def _handle_action_item_command(post: dict, question: str) -> bool:
    """Handle approve/skip/snooze sched <id_prefix> commands."""
    q_lower = question.lower().strip()
    parts = q_lower.split()

    # Match: approve|skip|snooze sched <id_prefix>
    if len(parts) != 3 or parts[1] != "sched":
        return False
    action = parts[0]
    if action not in ("approve", "skip", "snooze"):
        return False
    id_prefix = parts[2]

    channel_id = post.get("channel_id", "")
    root_id = post.get("root_id") or post["id"]

    try:
        from knowledge.db import execute_one, execute_write

        # Find the action item by ID prefix
        item = execute_one(
            "SELECT * FROM acos.action_items WHERE CAST(id AS TEXT) LIKE %s AND status = 'pending'",
            (f"{id_prefix}%",),
        )
        if not item:
            if _mm:
                _mm.post_to_channel_id(channel_id, f"No pending action item matching `{id_prefix}`.", root_id=root_id)
            return True

        import json as _json
        metadata = item["metadata"] if isinstance(item["metadata"], dict) else _json.loads(item["metadata"] or "{}")

        if action == "approve":
            # Send the draft email
            sent = False
            to_addr = metadata.get("to", "")
            subject = metadata.get("subject", "")
            body = metadata.get("body", "")
            thread_id = metadata.get("thread_id") or None

            if _gmail and _gmail.service and to_addr:
                logger.info("Approval handler: sending email to %s (thread=%s)", to_addr, thread_id)
                sent = _gmail.send_email(
                    to=to_addr,
                    subject=subject,
                    body=body,
                    thread_id=thread_id,
                )
                logger.info("Approval handler: send result=%s for %s", sent, to_addr)
            else:
                logger.warning(
                    "Approval handler: cannot send — gmail=%s, service=%s, to=%s",
                    bool(_gmail), bool(_gmail and _gmail.service), to_addr,
                )

            execute_write(
                """UPDATE acos.action_items
                   SET status = 'approved', resolved_at = now(),
                       resolved_by = 'ryan', updated_at = now()
                   WHERE id = %s""",
                (item["id"],),
            )

            if sent:
                sender_name = item.get("title", "").replace(f"Schedule {metadata.get('duration_minutes', '')}min with ", "")
                if _mm:
                    _mm.post_to_channel_id(
                        channel_id,
                        f"\u2705 Reply sent to {to_addr}",
                        root_id=root_id,
                    )
            else:
                # Fallback: show copy-paste draft so it's not lost
                fallback = (
                    f"\u26a0\ufe0f Email send failed — copy-paste draft below:\n\n"
                    f"**To:** {to_addr}\n"
                    f"**Subject:** {subject}\n"
                    f"```\n{body}\n```"
                )
                if _mm:
                    _mm.post_to_channel_id(channel_id, fallback, root_id=root_id)

        elif action == "skip":
            execute_write(
                """UPDATE acos.action_items
                   SET status = 'denied', resolved_at = now(),
                       resolved_by = 'ryan', updated_at = now()
                   WHERE id = %s""",
                (item["id"],),
            )
            if _mm:
                _mm.post_to_channel_id(
                    channel_id,
                    f"\u274c **Skipped:** {item['title']} — no email sent",
                    root_id=root_id,
                )

        elif action == "snooze":
            execute_write(
                """UPDATE acos.action_items
                   SET snoozed_until = now() + interval '4 hours', updated_at = now()
                   WHERE id = %s""",
                (item["id"],),
            )
            if _mm:
                _mm.post_to_channel_id(
                    channel_id,
                    f"\U0001f4a4 **Snoozed:** {item['title']} — will remind in 4 hours",
                    root_id=root_id,
                )

    except Exception:
        logger.exception("Action item command failed: %s", q_lower)
        if _mm:
            _mm.post_to_channel_id(channel_id, "\u26a0\ufe0f Action item command failed — check logs.", root_id=root_id)

    return True


# ---------------------------------------------------------------------------
# Correction / feedback learning
# ---------------------------------------------------------------------------

_CORRECTION_PHRASES = [
    "no,", "no ", "wrong", "actually", "i meant", "that's not right",
    "you should have", "next time", "correct action is", "not what i",
    "that was wrong", "try again", "redo", "should have been",
]

# Track last N Artemis responses for correction context: {post_id: {message, action_taken}}
_artemis_responses: dict[str, dict] = {}
_MAX_TRACKED_RESPONSES = 50


def _track_artemis_response(original_post: dict, response_text: str, intent: bool = False):
    """Store an Artemis response so corrections can reference it."""
    post_id = original_post.get("root_id") or original_post.get("id", "")
    if not post_id:
        return
    _artemis_responses[post_id] = {
        "original_message": original_post.get("message", "").replace("@artemis", "").strip(),
        "response": response_text[:500],
        "intent_routed": intent,
    }
    # Evict old entries
    if len(_artemis_responses) > _MAX_TRACKED_RESPONSES:
        oldest = list(_artemis_responses.keys())[0]
        del _artemis_responses[oldest]


@dataclass
class CorrectionResult:
    original_intent: str = ""
    correct_intent: str = ""
    learned_rule: str = ""
    confidence: float = 0.0


def classify_correction(
    original_message: str,
    artemis_response: str,
    correction_message: str,
) -> CorrectionResult:
    """Use Claude to understand what the user is correcting and what the right action was."""
    from knowledge.secrets import get_anthropic_key as _get_key
    import anthropic as _anthropic

    client = _anthropic.Anthropic(api_key=_get_key())
    system = (
        "The user is correcting an AI assistant called Artemis. "
        "Given the original message, Artemis's response, and the user's correction, determine:\n"
        "1. What action Artemis incorrectly took (original_intent)\n"
        "2. What action it should have taken (correct_intent, must be one of: "
        "add_contacts, query_crm, add_note, schedule, pipeline_update, general_reply)\n"
        "3. A short rule to remember for next time (under 100 chars)\n"
        "Return ONLY JSON: {\"original_intent\": \"...\", \"correct_intent\": \"...\", "
        "\"learned_rule\": \"...\", \"confidence\": 0.0-1.0}"
    )
    user = (
        f"Original message: {original_message}\n"
        f"Artemis response: {artemis_response}\n"
        f"User correction: {correction_message}"
    )

    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        text = resp.content[0].text.strip()
        text = re.sub(r"^```json\s*", "", text)
        text = re.sub(r"^```\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
        data = json.loads(text.strip())

        return CorrectionResult(
            original_intent=data.get("original_intent", ""),
            correct_intent=data.get("correct_intent", "general_reply"),
            learned_rule=data.get("learned_rule", "")[:100],
            confidence=float(data.get("confidence", 0.5)),
        )
    except Exception:
        logger.debug("Correction classification failed", exc_info=True)
        return CorrectionResult()


def _handle_correction(post: dict, question: str, thread: list[dict]) -> str | None:
    """Detect and handle correction messages in thread replies.

    Returns a response string if a correction was handled, None otherwise.
    """
    # Must be a thread reply
    root_id = post.get("root_id")
    if not root_id:
        return None

    # Check if message looks like a correction
    q_lower = question.lower()
    is_correction = any(phrase in q_lower for phrase in _CORRECTION_PHRASES)
    if not is_correction:
        return None

    # Find the original Artemis response in tracked history
    tracked = _artemis_responses.get(root_id)
    if not tracked:
        # Try to get context from thread
        if len(thread) < 2:
            return None
        # Find the last Artemis message in thread
        bot_msgs = [
            p for p in thread
            if p.get("user_id") == (_mm._bot_user_id if _mm else "")
        ]
        if not bot_msgs:
            return None
        tracked = {
            "original_message": thread[0].get("message", "").replace("@artemis", "").strip(),
            "response": bot_msgs[-1].get("message", "")[:500],
        }

    # Classify the correction
    correction = classify_correction(
        original_message=tracked["original_message"],
        artemis_response=tracked["response"],
        correction_message=question,
    )

    if not correction.learned_rule or correction.confidence < 0.4:
        return None

    # Store the learned rule
    try:
        from knowledge.db import execute_write as _db_write
        _db_write(
            """INSERT INTO acos.data_vault_satellites
               (entity_id, satellite_type, content, layer, crm_syncable, metadata)
               VALUES (
                   (SELECT id FROM acos.entities WHERE name = 'RDMIS' AND entity_type = 'Organization' LIMIT 1),
                   'intent_example',
                   %s,
                   'gold',
                   false,
                   '{}'
               )""",
            (json.dumps({
                "user_said": tracked["original_message"][:200],
                "correct_action": correction.correct_intent,
                "rule": correction.learned_rule,
                "learned_at": datetime.now(timezone.utc).isoformat(),
            }),),
        )
    except Exception:
        logger.exception("Failed to store learned intent rule")

    # Re-process the original message with the correction
    reprocess_result = None
    try:
        from artemis.intent import route_intent
        new_intent = route_intent(tracked["original_message"])
        logger.info(
            "Correction re-route: %s -> %s (was %s)",
            tracked["original_message"][:50],
            new_intent.primary_action,
            correction.original_intent,
        )
        # Execute the corrected action if it matches
        if new_intent.primary_action == correction.correct_intent or new_intent.confidence >= 0.6:
            reprocess_result = _handle_intent_routed(
                post, tracked["original_message"], thread
            )
    except Exception:
        logger.debug("Re-processing after correction failed", exc_info=True)

    response = f"\U0001f4a1 Got it \u2014 I've learned that \"{correction.learned_rule}\"."
    if reprocess_result:
        response += f"\n\nLet me try that again:\n\n{reprocess_result}"

    return response


def _handle_intent_routed(post: dict, question: str, thread: list[dict]) -> str | None:
    """Route message via intent classifier. Returns response string or None to fall through."""
    from artemis.intent import route_intent

    # Check for file attachments in the Mattermost post
    file_ids = post.get("file_ids") or []
    has_attachment = len(file_ids) > 0
    attachment_mime = None

    # Get file metadata if present
    file_info = None
    if has_attachment and _mm:
        try:
            resp = _mm._api("GET", f"/files/{file_ids[0]}/info")
            file_info = resp.json()
            attachment_mime = file_info.get("mime_type")
        except Exception:
            logger.debug("Could not fetch file info for %s", file_ids[0])

    intent = route_intent(question, has_attachment, attachment_mime)
    logger.info(
        "Intent: primary=%s, secondary=%s, confidence=%.2f, entities=%s",
        intent.primary_action, intent.secondary_actions, intent.confidence, intent.entities,
    )

    # Only act on high-confidence non-general intents
    if intent.primary_action == "general_reply" or intent.confidence < 0.6:
        return None

    channel_id = post.get("channel_id", "")
    root_id = post.get("root_id") or post["id"]

    # ── add_contacts ──
    if intent.primary_action == "add_contacts":
        from artemis.parser import parse_document
        from artemis.crm_writer import write_contacts

        contacts = []
        if has_attachment and _mm and file_ids:
            # Download file from Mattermost
            try:
                resp = _mm._api("GET", f"/files/{file_ids[0]}")
                file_bytes = resp.content
                mime = attachment_mime or "application/octet-stream"
                contacts = parse_document(file_bytes, mime, user_context=question)
            except Exception:
                logger.exception("Failed to download attachment %s", file_ids[0])
                return "\u26a0\ufe0f Failed to download the attachment. Try uploading again."

        if not contacts and intent.entities:
            # No attachment but entities mentioned — create minimal contacts
            from artemis.parser import ExtractedContact
            for name in intent.entities:
                contacts.append(ExtractedContact(
                    name=name,
                    notes=f"Added via Mattermost: {question[:200]}",
                    source_description="Mattermost message",
                ))

        if not contacts:
            return "\u26a0\ufe0f I couldn't extract any contacts. Try attaching a document or mentioning names."

        result = write_contacts(contacts, ryan_context=question)
        return f"\U0001f4c7 **Contacts imported**\n{result.summary}"

    # ── query_crm ──
    if intent.primary_action == "query_crm":
        from knowledge.db import execute_query

        parts = []
        for entity_name in intent.entities:
            # Search contacts
            rows = execute_query(
                """SELECT c.name, c.title, c.email, o.name AS org_name
                   FROM public.contacts c
                   LEFT JOIN public.organizations o ON c.org_id = o.id
                   WHERE LOWER(c.name) LIKE '%%' || LOWER(%s) || '%%'
                      OR LOWER(o.name) LIKE '%%' || LOWER(%s) || '%%'
                   LIMIT 5""",
                (entity_name, entity_name),
            )
            if rows:
                for r in rows:
                    org = f" at {r['org_name']}" if r.get("org_name") else ""
                    title = f" ({r['title']})" if r.get("title") else ""
                    parts.append(f"- **{r['name']}**{title}{org}")

            # Search knowledge graph
            entities = execute_query(
                """SELECT name, entity_type, content, layer, tags
                   FROM acos.entities
                   WHERE LOWER(name) LIKE '%%' || LOWER(%s) || '%%'
                   LIMIT 5""",
                (entity_name,),
            )
            for e in entities:
                tags = ", ".join(e.get("tags") or [])
                parts.append(
                    f"- \U0001f9e0 **{e['name']}** ({e['entity_type']}, {e['layer']})"
                    + (f" — {e['content'][:150]}" if e.get("content") else "")
                    + (f" [{tags}]" if tags else "")
                )

            # Search relationships
            rels = execute_query(
                """SELECT r.relationship_type, r.relationship_context,
                          s.name AS source_name, t.name AS target_name
                   FROM acos.relationships r
                   JOIN acos.entities s ON r.source_entity_id = s.id
                   JOIN acos.entities t ON r.target_entity_id = t.id
                   WHERE LOWER(s.name) LIKE '%%' || LOWER(%s) || '%%'
                      OR LOWER(t.name) LIKE '%%' || LOWER(%s) || '%%'
                   LIMIT 10""",
                (entity_name, entity_name),
            )
            for r in rels:
                parts.append(
                    f"  \u2192 {r['source_name']} **{r['relationship_type']}** {r['target_name']}"
                )

        if parts:
            return "\U0001f50d **CRM lookup**\n" + "\n".join(parts)
        return f"\U0001f50d No results found for: {', '.join(intent.entities)}"

    # ── add_note ──
    if intent.primary_action == "add_note":
        from knowledge.db import execute_write as db_write

        # Find entity to attach the note to
        entity_id = None
        entity_name = None
        if intent.entities:
            from artemis.crm_writer import _find_entity_by_name, _find_entity_by_name_fuzzy
            for name in intent.entities:
                ent = _find_entity_by_name(name) or _find_entity_by_name_fuzzy(name)
                if ent:
                    entity_id = str(ent["id"])
                    entity_name = ent["name"]
                    break

        if entity_id:
            db_write(
                """INSERT INTO acos.data_vault_satellites
                   (entity_id, satellite_type, content, layer, metadata)
                   VALUES (%s, 'business_context', %s, 'silver', '{}')""",
                (entity_id, question),
            )
            return f"\U0001f4dd Noted on **{entity_name}**: _{question[:200]}_"
        else:
            # No entity found — store as a general note on a generic entity
            db_write(
                """INSERT INTO acos.data_vault_satellites
                   (entity_id, satellite_type, content, layer, metadata)
                   VALUES (
                       (SELECT id FROM acos.entities WHERE name = 'RDMIS' AND entity_type = 'Organization' LIMIT 1),
                       'business_context', %s, 'silver', '{}'
                   )""",
                (question,),
            )
            return f"\U0001f4dd Noted: _{question[:200]}_"

    # ── pipeline_update ──
    if intent.primary_action == "pipeline_update":
        from knowledge.db import execute_one as db_one, execute_query as db_query

        for entity_name in intent.entities:
            deal = db_one(
                """SELECT d.id, d.name, d.gate, d.stage, o.name AS org_name
                   FROM public.deals d
                   JOIN public.organizations o ON d.org_id = o.id
                   WHERE LOWER(o.name) LIKE '%%' || LOWER(%s) || '%%'
                      OR LOWER(d.name) LIKE '%%' || LOWER(%s) || '%%'
                   LIMIT 1""",
                (entity_name, entity_name),
            )
            if deal:
                return (
                    f"\U0001f4ca **{deal['org_name']}** — {deal['name']}\n"
                    f"Gate: {deal['gate']} | Stage: {deal['stage'] or 'N/A'}\n\n"
                    f"_To update, use the CRM API or tell me specifically what changed._"
                )

        return "\U0001f4ca No matching deals found. Try mentioning the company name."

    # ── schedule — pass through to existing handlers ──
    if intent.primary_action == "schedule":
        return None  # let existing scheduling handlers pick it up

    return None


def _handle_mention(post: dict, thread: list[dict]):
    """Handle an @artemis mention."""
    question = post.get("message", "").replace("@artemis", "").strip()
    if not question:
        return

    # Track interaction for inactivity detection
    update_last_interaction()

    # Try confirmation flows first (yes/confirm/cancel for pending actions)
    if _handle_availability_command(post, question):
        return
    if _handle_calendar_confirm(post, question):
        return
    if _handle_delete_confirm(post, question):
        return

    # Try quiet hours session commands (goodnight, morning, override, extend)
    if _handle_quiet_command(post, question):
        return

    # Try inbox commands (done, wait, snooze, noise, inbox, waiting, snoozed)
    if _handle_inbox_command(post, question):
        return

    # Try action item commands (approve/skip/snooze sched)
    if _handle_action_item_command(post, question):
        return

    # Direct commands
    q_lower = question.lower().strip()
    channel_id = post.get("channel_id", "")
    root_id = post.get("root_id") or post["id"]

    if q_lower in ("version", "what version are you?", "what version", "update check"):
        reply = format_version_status()
        if _mm:
            _mm.post_to_channel_id(channel_id, reply, root_id=root_id)
        return

    if q_lower == "contacts":
        contacts = list_contacts()
        reply = format_contacts_list(contacts)
        if _mm:
            _mm.post_to_channel_id(channel_id, reply, root_id=root_id)
        return

    if q_lower == "leads":
        leads = list_contacts(status="lead")
        reply = format_contacts_list(leads)
        if _mm:
            _mm.post_to_channel_id(channel_id, reply, root_id=root_id)
        return

    if q_lower == "crm status":
        crm = CRMClient()
        if crm.is_available():
            try:
                reply = crm.format_status()
            except Exception:
                logger.exception("CRM status fetch failed")
                reply = "\u26a0\ufe0f CRM API error — check logs."
        else:
            reply = "CRM API not configured (CRM_API_URL / CRM_API_KEY not set)."
        if _mm:
            _mm.post_to_channel_id(channel_id, reply, root_id=root_id)
        return

    if q_lower in ("list commitments", "commitments", "open commitments"):
        open_items = list_commitments(status="active")
        reply = format_commitments_list(open_items)
        if _mm:
            _mm.post_to_channel_id(channel_id, reply, root_id=root_id)
        return

    if q_lower.startswith("close "):
        title = parse_close_title(question)
        if title:
            result = close_commitment(title)
            reply = format_close_result(result)
        else:
            reply = 'Usage: `close commitment "Title"` or `close "Title"`'
        if _mm:
            _mm.post_to_channel_id(channel_id, reply, root_id=root_id)
        return

    if q_lower == "playbooks":
        pb_text = get_playbook_text()
        reply = pb_text if pb_text else "No playbooks loaded."
        if _mm:
            _mm.post_to_channel_id(channel_id, reply, root_id=root_id)
        return

    if q_lower.startswith("archive "):
        short_id = q_lower.split("archive ", 1)[1].strip()
        tid = resolve_thread_id(short_id)
        if tid and _gmail:
            success = _gmail.archive_message(tid)
            if success:
                mark_done(tid)
                reply = f"Archived and marked DONE"
            else:
                reply = f"Failed to archive — check logs"
        elif not tid:
            reply = f"Thread not found: {short_id}"
        else:
            reply = "Gmail not connected"
        if _mm:
            _mm.post_to_channel_id(channel_id, reply, root_id=root_id)
        return

    # Quiet hours status
    if q_lower in ("quiet hours", "quiet hours status", "quiet"):
        reply = quiet_hours_status()
        if _mm:
            _mm.post_to_channel_id(channel_id, reply, root_id=root_id)
        return

    # Timezone override: "I'm in [city]" / "timezone [tz]"
    if _handle_timezone_command(post, question):
        return

    # Calendar view: "what's on my calendar", "show events", "calendar this week"
    if _handle_calendar_view_mention(post, question):
        return

    # Scheduling intent detection (real slots, never vague language)
    if _handle_scheduling_mention(post, question):
        return

    # Availability check command
    if _handle_availability_mention(post, question):
        return

    # Calendar delete command
    if _handle_delete_event(post, question):
        return

    # Bulk convert work sessions to tasks
    if _handle_convert_to_tasks(post, question):
        return


    # Life ops commands (workout, grocery, health)
    life_ops_response = _try_life_ops(question)
    if life_ops_response and _mm:
        _mm.post_to_channel_id(channel_id, life_ops_response, root_id=root_id)
        return

    # ── Correction / feedback detection ──
    correction_response = _handle_correction(post, question, thread)
    if correction_response:
        _mm.post_to_channel_id(channel_id, correction_response, root_id=root_id)
        return

    # ── Intent router: classify before generic Claude fallback ──
    intent_response = _handle_intent_routed(post, question, thread)
    if intent_response:
        _mm.post_to_channel_id(channel_id, intent_response, root_id=root_id)
        # Track this response for potential correction later
        _track_artemis_response(post, intent_response, intent=True)
        return

    thread_lines = []
    for p in thread[-10:]:
        thread_lines.append(f"{p.get('message', '')}")
    thread_context = "\n".join(thread_lines)

    data_context = _build_mention_context(post, _gmail, _calendar, question=question)

    response = handle_mention(question, thread_context, data_context)
    if response and _mm:
        channel_id = post.get("channel_id", "")
        root_id = post.get("root_id") or post["id"]

        # Check if Claude's response contains a calendar event to create
        response = _process_calendar_events(response, channel_id=channel_id)

        # Check if Claude's response contains commitments to save
        response = _process_commitments(response, channel_id=channel_id)

        # Append quiet/override status note
        state = get_quiet_state()
        if state.get("override_active"):
            response += "\n\n\u26a1 _Working session active. Inactivity timer running._"
        elif is_quiet():
            response += "\n\n\U0001f319 _Quiet hours active. Say `@artemis override` to start a working session._"

        _mm.post_to_channel_id(channel_id, response, root_id=root_id)
        _track_artemis_response(post, response)


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

    # Scope mismatch warnings (non-fatal)
    scope_warnings = []
    if gmail and getattr(gmail, "scope_mismatch", False):
        scope_warnings.append("Gmail token missing `gmail.modify` scope — archive will not work.")
    if calendar and getattr(calendar, "scope_mismatch", False):
        scope_warnings.append("Calendar token missing required scopes (`calendar.readonly` and/or `calendar.events`).")
    # Check PB-007 billing scopes (non-fatal — just disables billing intake)
    from artemis.billing import check_billing_scopes, print_scope_migration_instructions
    billing_ok, billing_missing = check_billing_scopes()
    if not billing_ok:
        scope_warnings.append(
            f"PB-007 billing scopes missing ({', '.join(billing_missing)}) — billing intake disabled."
        )
        print_scope_migration_instructions(billing_missing)

    if scope_warnings:
        warning = (
            "\u26a0\ufe0f OAuth token has wrong scopes \u2014 re-authentication required.\n"
            + "\n".join(f"- {w}" for w in scope_warnings)
            + "\nRun: `python setup_oauth.py`"
        )
        try:
            mm.post_message(config.CHANNEL_OPS, warning)
        except Exception:
            logger.exception("Failed to post scope warning")


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

    # Init databases (commitments + inbox_threads + contacts + life_ops)
    from artemis.inbox import get_db as init_inbox_db
    get_db()
    init_inbox_db()
    init_crm_db()
    init_life_ops_db()

    # Load health plan context
    load_health_plan()

    # Init Mattermost with retry loop
    _mm = MattermostClient()
    if not _connect_mattermost_with_retry(_mm):
        logger.error(
            "Failed to connect to Mattermost after %d attempts — giving up",
            config.STARTUP_RETRY_COUNT,
        )
        sys.exit(1)

    # Init Gmail (pass mm for auth failure alerts)
    _gmail = GmailClient()
    try:
        _gmail.authenticate(mm_client=_mm)
    except Exception:
        logger.warning("Gmail authentication failed — email features disabled")

    # Init Calendar (pass mm for auth failure alerts)
    _calendar = CalendarClient()
    try:
        _calendar.authenticate(mm_client=_mm)
    except Exception:
        logger.warning("Calendar authentication failed — calendar features disabled")

    # Load calendar cache on boot
    if _calendar and _calendar.service:
        from artemis import calendar_cache
        calendar_cache.refresh(_calendar)
        logger.info(calendar_cache.status())

    # Register @mention handler
    _mm.on_mention(_handle_mention)
    _mm.start_websocket()

    # Start scheduler
    _sched = ArtemisScheduler(_mm, _gmail, _calendar)
    _sched.start()

    # Run catch-up processing for any gap since last run
    try:
        _sched.run_catchup()
    except Exception:
        logger.exception("Startup catch-up failed — continuing normally")

    # Post startup message
    _post_startup_message(_mm, _gmail, _calendar, _sched)

    # Start Flask for uptime webhook + health check
    shutdown = Event()

    def signal_handler(sig, frame):
        logger.info("Shutting down...")
        # Record last run time for catch-up on next startup
        from artemis.quiet_hours import set_system_value
        set_system_value("last_run_at", datetime.utcnow().isoformat())
        _post_shutdown_message(_mm)
        _sched.stop()
        shutdown.set()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    logger.info("Artemis is running. Press Ctrl+C to stop.")
    app.run(host="0.0.0.0", port=5001, use_reloader=False)


if __name__ == "__main__":
    main()
