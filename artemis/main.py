"""Entry point — starts all schedulers and webhook listener."""

import json
import logging
import re
import signal
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import Event, Thread
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
    close_commitment_by_id,
    format_close_result,
    format_commitments_list,
    list_commitments,
    get_commitments_for_client,
    log_calendar_action,
    parse_close_title,
)
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
    handle_grocery_command,
    handle_health_command,
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
from artemis.version import (
    VERSION,
    format_version_status,
    get_commit_hash,
    get_commit_subject,
    get_latest_github_version,
    get_version,
)

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

# Control words for pending-action confirm/cancel. These are matched
# case-insensitively and trimmed. CRITICAL: when a pending action is open for a
# channel, a control-word reply MUST be consumed by a confirm handler and must
# never reach the LLM intent classifier — the classifier mislabels them
# general_reply and confabulates "sending now…" while create_event is never
# called. See _handle_calendar_confirm / the _handle_mention backstop.
_CONFIRM_WORDS = frozenset({"confirm", "approved", "approve", "yes", "send", "send it", "go"})
_CANCEL_WORDS = frozenset({"cancel", "no", "deny", "discard"})
_CONTROL_WORDS = _CONFIRM_WORDS | _CANCEL_WORDS

# Duplicate-block override. DELIBERATELY DISTINCT from the confirm words: a
# confirm/approved/yes must NEVER bypass a duplicate block (Brad Spaits guard #2).
# The only phrase that creates past a block is exactly this one.
_DUP_OVERRIDE_PHRASE = "override duplicate"

# Pending availability reply flow keyed by channel_id
# Stores slots and email context for send/confirm/edit flow
_pending_availability: dict[str, dict] = {}

# E2: per-channel inbox-listing state (the numbered-listing cursor + the
# number→message_id mapping E3 will act on). Keyed by channel_id. In-memory only
# — resets on restart, which is fine for v1 (a fresh `inbox` rebuilds it).
# Each value:
#   {"offset": int,          # rows already shown — where `more` resumes
#    "total": int,           # working-set size at the time of the listing
#    "mapping": {int: str}}  # displayed number → Gmail message_id
# THE E3 HANDOFF: `archive 3` (E3) resolves 3 → message_id from "mapping" here.
# Numbers are per-listing and GLOBAL across pages (1..total): a fresh listing
# (`inbox`/`triage`/…) re-syncs, resets the cursor, and CLEARS the mapping
# (re-numbering from 1); `more` extends the same mapping with the next page's
# numbers without re-syncing (so 1..total stay stable for the whole listing).
_inbox_listing_state: dict[str, dict] = {}
_INBOX_PAGE_SIZE = 20

# PB-010 dossier review state: per-channel {display_number → pending-item dict}
# from dossier.render_review(). `approve`/`edit`/`drop` resolve numbers against this
# (the same number→row indirection E2/E3 use for the inbox). In-memory; a fresh
# `dossier review` rebuilds it. approve/edit/drop only fire when this is populated
# for the channel — otherwise a bare `drop 4` falls through to the LLM.
_dossier_review_state: dict[str, dict] = {}

# PB-011 vault digest state: per-channel {display_number → extraction_proposal row}
# from vault.render_digest(). `approve`/`reject` resolve numbers against this (same
# indirection as the dossier review + E3 inbox). In-memory; a fresh `digest` /
# `proposals` rebuilds it. approve/reject only fire when this is populated for the
# channel — otherwise `approve 1` with no vault digest falls through (to the dossier
# review or the LLM).
_vault_digest_state: dict[str, dict] = {}

# Phrases that open a fresh numbered inbox listing (E2). `more` pages an existing
# listing. These are repointed away from the old inbox_threads summary in
# _handle_inbox_command to the index-backed _handle_inbox_listing.
_INBOX_LISTING_PHRASES = frozenset({
    "inbox", "triage", "triage inbox",
    "what's in my inbox", "whats in my inbox", "what is in my inbox",
})


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

    # Training plan slice — so general_reply (trainer voice) can answer workout
    # questions from REAL data and never claim the plan/database doesn't exist.
    try:
        from artemis.health import build_context_slice
        health_slice = build_context_slice()
        if health_slice:
            parts.append("\n" + health_slice)
    except Exception:
        logger.exception("Failed to add training slice to mention context")

    return UNTRUSTED_PREFIX + "\n".join(parts) if parts else "No context available."


def _inbox_display_sender(row: dict) -> str:
    """Readable sender for a listing line. The index stores the raw From header
    ('Name <addr>') in `sender`; prefer the display name, fall back to the bare
    address, then to whatever's there."""
    from email.utils import parseaddr
    raw = (row.get("sender") or "").strip()
    name, addr = parseaddr(raw)
    return name or addr or raw or "(unknown sender)"


def _render_inbox_listing(channel_id: str, fresh: bool) -> str:
    """Render one page of the index-backed numbered inbox listing (E2).

    fresh=True  → sync from Gmail, reset the per-channel cursor + mapping, show
                  page 1. fresh=False (`more`) → page the already-synced working
                  set from the stored cursor WITHOUT re-syncing (keeps numbering
                  1..total stable across the listing).

    Read-only: syncs the mirror and queries it; never mutates Gmail. Stores the
    displayed number→message_id mapping in _inbox_listing_state for E3.
    """
    from artemis import email_index

    if fresh:
        # Sync-on-command: refresh the mirror from Gmail before listing.
        email_index.sync_from_gmail(_gmail)
        total = email_index.count_working_set()
        state = {"offset": 0, "total": total, "mapping": {}}
        _inbox_listing_state[channel_id] = state
    else:
        state = _inbox_listing_state.get(channel_id)
        if not state:
            return "No active inbox listing — say `inbox` to start one."
        total = state["total"]

    offset = state["offset"]
    if total == 0:
        return "\U0001f4ec **Inbox** — 0 emails. Inbox zero \U0001f389"
    if offset >= total:
        return f"That's all {total} — say `inbox` to refresh the list."

    rows = email_index.query_working_set(limit=_INBOX_PAGE_SIZE, offset=offset)
    if not rows:
        return f"That's all {total} — say `inbox` to refresh the list."

    start_num = offset + 1
    end_num = offset + len(rows)
    lines = [
        f"\U0001f4ec **Inbox** — {total} email{'s' if total != 1 else ''} "
        f"(showing {start_num}–{end_num})",
    ]
    for i, r in enumerate(rows):
        num = start_num + i
        sender = _inbox_display_sender(r)
        subj = (r.get("subject") or "(no subject)").strip() or "(no subject)"
        if len(subj) > 70:
            subj = subj[:69] + "…"
        lines.append(f"{num}. {sender} — {subj}")
        # THE E3 HANDOFF: record number → Gmail message_id for this listing.
        state["mapping"][num] = r["message_id"]

    # Advance the cursor so a following `more` resumes after this page.
    state["offset"] = end_num
    if end_num < total:
        lines.append(f"\nSay `more` for {end_num + 1}–{total}.")
    return "\n".join(lines)


def _handle_inbox_listing(post: dict, question: str) -> bool:
    """E2: numbered, paginated inbox listing from the email_index mirror.

    Handles the listing phrases (inbox / triage / triage inbox / what's in my
    inbox) and `more` for pagination. Repoints those phrases AWAY from the old
    inbox_threads summary in _handle_inbox_command. Strictly read-only — no Gmail
    mutations, no dispositions (E3 acts on the mapping this leaves behind).

    Returns True if it handled the message, else False (so other inbox
    subcommands — waiting/snoozed/done/snooze/wait/noise — still reach the old
    handler).
    """
    q = question.lower().strip()
    fresh = q in _INBOX_LISTING_PHRASES
    is_more = q == "more"
    if not (fresh or is_more):
        return False

    channel_id = post.get("channel_id", "")
    # Only intercept the generic word `more` when THIS channel has a live
    # listing; otherwise let it fall through to the LLM untouched.
    if is_more and channel_id not in _inbox_listing_state:
        return False

    root_id = post.get("root_id") or post["id"]
    try:
        reply = _render_inbox_listing(channel_id, fresh=fresh)
    except Exception:
        logger.exception("inbox listing failed")
        reply = "⚠️ Inbox listing failed — check logs."
    if _mm:
        _mm.post_to_channel_id(channel_id, reply, root_id=root_id)
    return True


# ── E3: dispositions (act → verify → log), resolved from the E2 mapping ──────
# Gmail is the source of truth: we resolve a number → message_id, act against
# Gmail, RE-READ the labels to verify the change actually happened, log the real
# result via log_audit (with corpus features), and report ONLY what we verified.
# No outbound (reply/forward) — that's E4. Read EMAIL_MODEL.md "Actions".

_DISPOSITION_PAST = {
    "archive": "Archived", "file": "Filed", "delete": "Deleted", "spam": "Marked spam on",
}
# Category for `file ## as <cat>` is constrained to label-safe chars to prevent
# label-path injection; archive/file land under the @artemis/* namespace.
_ARCHIVE_LABEL = "@artemis/archive"
# Upper bound on operands in one disposition command — caps a runaway range
# (`archive 1-99999`) so it refuses once instead of flooding per-number.
_DISPOSITION_MAX_BATCH = 100


def _parse_numbers(s: str) -> list[int]:
    """Parse a comma/space list of numbers and/or inclusive ranges, deduped in
    first-seen order.

    Supports singles ("2"), inclusive ranges ("1-5" → 1,2,3,4,5), and mixes
    ("1-3, 7, 9-11" → 1,2,3,7,9,10,11). Separators are commas, whitespace, "&",
    and the word "and" (so "1 & 2" and "1 and 2" both parse). A reversed range
    ("5-1") and any non-numeric token are ignored. Resolve-or-refuse downstream
    still validates every expanded number against the listing mapping, so
    out-of-range numbers just refuse per-number.
    """
    out: list[int] = []
    seen: set[int] = set()

    def _add(n: int) -> None:
        if n not in seen:
            seen.add(n)
            out.append(n)

    for tok in re.split(r"[,\s&]+", s.strip()):
        if not tok or tok.lower() == "and":
            continue
        tok = tok.lstrip("#")  # accept #15, #1-#3 style references
        rng = re.match(r"^(\d+)-#?(\d+)$", tok)
        if rng:
            a, b = int(rng.group(1)), int(rng.group(2))
            if a <= b:
                for n in range(a, b + 1):
                    _add(n)
            # a > b (reversed) → malformed, ignore the token
        elif tok.isdigit():
            _add(int(tok))
        # else: non-numeric token ignored
    return out


def _parse_disposition_command(question: str) -> tuple[str, list[int], str | None] | None:
    """Parse a disposition command. Returns (verb, numbers, category|None) or None.

    Forms: `archive 2` · `delete 1, 5, 9` · `spam 20` · `file 3 as billing`, plus
    inclusive ranges/mixes (`archive 1-5` · `archive 1-3, 7, 9-11`), the `&`
    separator (`archive 1 & 2`), and `as`/`under`/`in`/`to` for filing
    (`file 3 under billing`). Operands are numbers/ranges only (so `delete event …`
    never matches here). Category is constrained to [\\w/-] — no spaces, no
    label-path injection.
    """
    q = question.strip()
    # `file <nums> as <category>` — numbers before category.
    m = re.match(r"^file\s+([\d,\s&#-]+?)\s+(?:as|under|in|to)\s+(.+?)\s*$", q, re.IGNORECASE)
    if m:
        nums = _parse_numbers(m.group(1))
        cat = _slugify_category(m.group(2))  # multi-word → slug ("founder loans"→"founder-loans")
        return ("file", nums, cat) if (nums and cat) else None
    # `file as <category> <nums>` — category before numbers (the natural inverse).
    m = re.match(r"^file\s+(?:as|under|in|to)\s+(.+?)\s+([\d,\s&#-]+)$", q, re.IGNORECASE)
    if m:
        cat = _slugify_category(m.group(1))
        nums = _parse_numbers(m.group(2))
        return ("file", nums, cat) if (nums and cat) else None
    m = re.match(r"^(archive|delete|spam)\s+([\d,\s&#-]+)$", q, re.IGNORECASE)
    if m:
        nums = _parse_numbers(m.group(2))
        return (m.group(1).lower(), nums, None) if nums else None
    return None


def _slugify_category(text: str) -> str:
    """Turn a free-text category ("founder loans") into a label-safe slug
    ("founder-loans"): lowercase, punctuation dropped, spaces→hyphens, capped."""
    s = re.sub(r"[^\w\s/-]", "", text.strip().lower())
    s = re.sub(r"[\s_]+", "-", s)
    return s.strip("-/")[:40]


def _is_pure_number_ref(s: str) -> bool:
    """True iff s is ONLY listing-number tokens/separators (digits, N-M ranges,
    commas, '&', the word 'and', whitespace) — e.g. '1 & 2', '1-3, 7'. Guards the
    declarative form so ordinary prose ('the attendees are 3') can't be read as a
    file command."""
    tokens = [t for t in re.split(r"[,\s&]+", s.strip()) if t]
    if not tokens:
        return False
    for t in tokens:
        if t.lower() == "and":
            continue
        if not re.match(r"^\d+(-\d+)?$", t):
            return False
    return True


def _parse_listing_reference(question: str) -> tuple[list[int], str] | None:
    """Listing-gated declarative form: '<numbers> are|is <category>'
    (e.g. '1 & 2 are founder loans'). Returns (numbers, category_slug) or None.

    Only a PURE numeric prefix qualifies, so this never fires on ordinary prose.
    This is NOT an explicit disposition verb — the caller proposes the matching
    `file` command rather than auto-acting, and only when a listing is active and
    at least one number resolves.
    """
    m = re.match(r"^(.+?)\s+(?:are|is)\s+(.+)$", question.strip(), re.IGNORECASE)
    if not m:
        return None
    prefix, tail = m.group(1), m.group(2)
    if not _is_pure_number_ref(prefix):
        return None
    nums = _parse_numbers(prefix)
    cat = _slugify_category(tail)
    # A real category is a short label; a long clause ("…coming to the meeting
    # next week…") is prose, not a filing target — don't treat it as a reference.
    if not (nums and cat) or cat.count("-") > 2:
        return None
    return (nums, cat)


def _verify_disposition(verb: str, post_labels: list[str] | None, expected_label_id: str | None) -> bool:
    """Re-read truth: does Gmail's post-action label state match the intent?
    post_labels is None when the verify read failed → treat as UNVERIFIED."""
    if post_labels is None:
        return False
    labels = set(post_labels)
    if verb in ("archive", "file"):
        return "INBOX" not in labels and bool(expected_label_id) and expected_label_id in labels
    if verb == "delete":
        return "TRASH" in labels and "INBOX" not in labels
    if verb == "spam":
        return "SPAM" in labels and "INBOX" not in labels
    return False


def _execute_disposition(
    verb: str, num: int | None, message_id: str, category: str | None,
    *, source: str = "user_directed", gmail_client=None,
    metadata_extra: dict | None = None,
) -> dict:
    """Act on Gmail → verify → log via log_audit → drop the index row if verified.

    The SINGLE audited/labeled filing primitive. Out-of-inbox ALWAYS carries an
    @artemis/* label AND an audit row — there is no bare strip. Commands and the
    autonomous filing gate both flow through here; `source` distinguishes them
    (user_directed vs automation_*), `metadata_extra` carries context like the
    triage state. `gmail_client` lets a caller (e.g. the scheduler) pass its own
    client; defaults to the module global.

    Returns {num, ok(bool=verified), display, detail}. Never reports success
    unless the post-action Gmail re-read confirmed the change.
    """
    from artemis import email_index
    from knowledge.db import log_audit

    g = gmail_client or _gmail
    row = email_index.get_by_message_id(message_id) or {}
    sender = row.get("sender", "") or ""
    sender_domain = row.get("sender_domain", "") or ""
    subject = row.get("subject", "") or ""
    thread_id = row.get("thread_id", "") or ""
    display = _inbox_display_sender(row) if row else (f"#{num}" if num else message_id[:12])

    if not g or not getattr(g, "service", None):
        return {"num": num, "ok": False, "display": display, "detail": "Gmail not connected"}

    # Labels at decision time (for the corpus + delta computation).
    prior_labels = g.get_message_labels(message_id)

    # Execute against Gmail.
    expected_label_id = None
    api_ok = False
    if verb in ("archive", "file"):
        label_name = _ARCHIVE_LABEL if verb == "archive" else f"@artemis/{category}"
        expected_label_id = g.ensure_gmail_label(label_name)
        if not expected_label_id:
            return {"num": num, "ok": False, "display": display,
                    "detail": f"could not ensure label {label_name}"}
        api_ok = g.modify_labels(
            message_id, add_label_ids=[expected_label_id],
            remove_label_ids=["INBOX", "UNREAD"],
        )
    elif verb == "delete":
        api_ok = g.trash_message(message_id)
    elif verb == "spam":
        api_ok = g.modify_labels(
            message_id, add_label_ids=["SPAM"], remove_label_ids=["INBOX"],
        )

    # Verify against Gmail (re-read), regardless of api_ok — truth, not report.
    post_labels = g.get_message_labels(message_id)
    verified = _verify_disposition(verb, post_labels, expected_label_id)

    prior_set = set(prior_labels or [])
    post_set = set(post_labels or [])
    applied = sorted(post_set - prior_set)
    removed = sorted(prior_set - post_set)

    # Log the REAL result (verified or not) with corpus features.
    try:
        meta = {"category": category} if verb == "file" else {}
        if metadata_extra:
            meta = {**(meta or {}), **metadata_extra}
        log_audit(
            agent="inbox", action=verb,
            outcome="verified" if verified else "unverified",
            message_id=message_id, thread_id=thread_id, source=source,
            action_class="disposition", sender=sender, sender_domain=sender_domain,
            subject=subject, prior_labels=prior_labels or [], applied_labels=applied,
            removed_labels=removed, verified=verified, metadata=(meta or None),
        )
    except Exception:
        logger.exception("log_audit failed for disposition %s on %s", verb, message_id)

    # Drop the mirror row only on a VERIFIED terminal disposition.
    if verified:
        try:
            email_index.drop_from_index(message_id)
        except Exception:
            logger.exception("drop_from_index failed for %s", message_id)
        return {"num": num, "ok": True, "display": display,
                "detail": category if verb == "file" else ""}

    detail = "could not confirm in Gmail" if api_ok else "Gmail action failed"
    return {"num": num, "ok": False, "display": display, "detail": detail}


def file_message_for_rule(message_id: str, rule: dict, gmail_client=None) -> dict:
    """Execute a chat-authored playbook rule's action on one message, through the
    SAME audited/labeled primitive as everything else. source='automation_rule',
    with rule id/name in the audit metadata so every rule-driven action is
    traceable to the rule that caused it (automation is inspectable, not trusted).
    """
    action = rule.get("action")
    category = rule.get("action_label") if action == "file" else None
    return _execute_disposition(
        action, None, message_id, category,
        source="automation_rule", gmail_client=gmail_client,
        metadata_extra={"rule_id": rule.get("id"), "rule_name": rule.get("name")},
    )


def file_message_for_automation(message_id: str, triage_state: str, gmail_client=None) -> dict:
    """Audited, labeled archive for the autonomous filing gate.

    Same primitive as a command disposition — strips INBOX, applies
    @artemis/archive, re-reads to verify, writes an audit row, drops the mirror.
    Replaces the legacy bare `gmail.archive_message()` so automation can NEVER
    again remove mail from the inbox without a label + audit row (the location
    invariant). `triage_state` is recorded in the audit metadata so the WHY
    (NOISE/DONE) is preserved without proliferating labels.
    """
    return _execute_disposition(
        "archive", None, message_id, None,
        source="automation_triage", gmail_client=gmail_client,
        metadata_extra={"triage_state": triage_state},
    )


_DISPOSITION_VERBS = {"archive", "delete", "spam", "file"}
_FILE_PREPS = {"as", "under", "in", "to"}
# Reversible (label-only, mailbox-local) vs consequential dispositions. A
# COMPOUND batch with a consequential verb is proposed-then-confirmed; a
# reversible-only batch auto-executes. Single commands are unchanged.
_CONSEQUENTIAL_VERBS = {"delete", "spam"}


def _strip_parentheticals(s: str) -> str:
    """Drop `(inline comments)` so `14 delete (was a test)` parses as `14 delete`."""
    return re.sub(r"\([^)]*\)", " ", s)


def _looks_dispositional(question: str) -> bool:
    """True iff the line carries a disposition verb AND a listing-number token.
    Gates the in-context refusal: a disposition-SHAPED line that fails to parse
    during an active listing must refuse-in-context, never fall through to the
    keyword/financial classifier (the bug that mis-fired the financial report on
    `file ... as founder loans`)."""
    q = _strip_parentheticals(question).lower()
    toks = [t for t in re.split(r"[\s,&]+", q.strip()) if t]
    has_verb = any(t in _DISPOSITION_VERBS for t in toks)
    has_num = any(re.match(r"^\d+(-\d+)?$", t) for t in toks)
    return has_verb and has_num


def _parse_compound_dispositions(
    question: str,
) -> list[tuple[str, list[int], str | None]] | None:
    """Parse a BATCH of disposition groups on one line. Numbers may sit on either
    side of the verb; `file as <category>` may be multi-word (slugified);
    parentheticals are stripped. Mixable per group:
        nums-first : `1-4 archive` · `5-7, 13 file as founder loans`
        verb-first : `archive 1-4`  · `file 5-7 as founder-loans`
    A `file` category runs until the next number-or-verb token, so the next
    group's numbers terminate it. Groups returned in order; a group whose numbers
    don't resolve (or a `file` with no category) is dropped. Returns None if the
    line isn't disposition-shaped at all.
    """
    q = _strip_parentheticals(question).strip()
    if not q:
        return None
    toks = q.split()
    if not any(t.lower() in _DISPOSITION_VERBS for t in toks):
        return None

    def is_num(t: str) -> bool:
        return bool(re.match(r"^#?\d+(-#?\d+)?$", t.strip(",&")))

    groups: list[tuple[str, list[int], str | None]] = []
    i, n = 0, len(toks)
    while i < n:
        tok = toks[i]
        low = tok.lower()
        nums_src: list[str] = []
        verb: str | None = None

        if is_num(tok):
            # nums-first: leading numbers then a required verb.
            while i < n and is_num(toks[i]):
                nums_src.append(toks[i]); i += 1
            if i < n and toks[i].lower() in _DISPOSITION_VERBS:
                verb = toks[i].lower(); i += 1
            else:
                continue  # numbers with no following verb — stray, keep scanning
        elif low in _DISPOSITION_VERBS:
            # verb-first: verb then numbers (file may carry them before `as`).
            # Lookahead: a number whose NEXT token is a verb leads the following
            # (nums-first) group, so don't steal it — `archive 1-2 5-7 file as x`
            # means archive 1-2, then file 5-7, not archive 1-2,5-7.
            verb = low; i += 1
            while i < n and is_num(toks[i]):
                if nums_src and i + 1 < n and toks[i + 1].lower() in _DISPOSITION_VERBS:
                    break  # this number leads the next group, not ours
                nums_src.append(toks[i]); i += 1
        else:
            i += 1
            continue

        category: str | None = None
        if verb == "file":
            if i < n and toks[i].lower() in _FILE_PREPS:
                i += 1
            cat_words: list[str] = []
            while (
                i < n
                and not is_num(toks[i])
                and toks[i].lower() not in _DISPOSITION_VERBS
            ):
                cat_words.append(toks[i]); i += 1
            category = _slugify_category(" ".join(cat_words)) or None
            # `file as <category> <nums>` — numbers AFTER the category bind to
            # this file group (the natural inverse of `file <nums> as <cat>`).
            while i < n and is_num(toks[i]):
                if nums_src and i + 1 < n and toks[i + 1].lower() in _DISPOSITION_VERBS:
                    break
                nums_src.append(toks[i]); i += 1

        nums = _parse_numbers(" ".join(nums_src))
        if verb and nums and not (verb == "file" and not category):
            groups.append((verb, nums, category))

    return groups or None


def _format_disposition_plan(groups: list[tuple[str, list[int], str | None]]) -> str:
    """One-line-per-group readback of a parsed batch, for confirm-then-act."""
    def rng(nums: list[int]) -> str:
        return ", ".join(str(x) for x in nums)
    out = []
    for verb, nums, cat in groups:
        out.append(f"file {rng(nums)} → `@artemis/{cat}`" if verb == "file"
                   else f"{verb} {rng(nums)}")
    return "\n".join(f"  • {p}" for p in out)


def _parse_disposition_batch(
    question: str,
) -> tuple[list[tuple[str, list[int], str | None]], list[str]]:
    """P6 — parse a (possibly multi-line) disposition batch line-by-line so that
    NO line is silently dropped.

    Returns (groups, unrecognized): every non-empty input line either contributes
    one or more parsed disposition groups or is echoed back verbatim under
    `unrecognized`. A line that itself carries several groups
    (`archive 1-4 file 5 as x`) is parsed whole. This is what makes the batch
    accountable — the observed bug filed 6 of 8 lines and said nothing about the
    2 that were missing the `file` verb (`14 founder loan`)."""
    groups: list[tuple[str, list[int], str | None]] = []
    unrecognized: list[str] = []
    for raw in question.splitlines():
        line = raw.strip()
        if not line:
            continue
        parsed_line = _parse_compound_dispositions(line)
        if not parsed_line:
            single = _parse_disposition_command(line)
            parsed_line = [single] if single else None
        if parsed_line:
            groups.extend(parsed_line)
        else:
            unrecognized.append(line)
    return groups, unrecognized


def _format_unrecognized(lines: list[str]) -> str:
    """Echo the literal lines the batch parser couldn't understand (P6)."""
    body = "\n".join(f"  • `{ln}`" for ln in lines)
    return f"⚠️ Didn't understand:\n{body}"


def _execute_disposition_group(
    channel_id: str, mapping: dict, verb: str, numbers: list[int],
    category: str | None,
) -> list[str]:
    """Act+verify+log each number in one group; return per-item report lines.
    Mutates `mapping` (retires verified numbers). Shared by single & compound."""
    lines: list[str] = []
    for n in numbers:
        message_id = mapping.get(n)
        if not message_id:
            # Resolve-or-refuse: never guess, never act on an unresolved number.
            lines.append(f"\U0001f6ab #{n} — not in the current listing (say `inbox` to refresh)")
            continue
        try:
            res = _execute_disposition(verb, n, message_id, category)
        except Exception:
            logger.exception("disposition %s failed on #%s", verb, n)
            lines.append(f"⚠️ #{n} — disposition errored, check logs")
            continue
        if res["ok"]:
            mapping.pop(n, None)  # the item left the working set — retire its number
            extra = f" as {res['detail']}" if verb == "file" and res.get("detail") else ""
            lines.append(f"✅ {_DISPOSITION_PAST[verb]} #{n} ({res['display']}){extra}")
        else:
            lines.append(f"⚠️ #{n} ({res['display']}) — {res['detail']}")
    return lines


def _handle_rule_command(post: dict, question: str) -> bool:
    """Chat-authored playbook rules (feature #1): `rules`, `rule add <spec>`,
    `rule off <id>`. Authoring is propose-then-confirm — the spec is parsed
    deterministically, echoed for confirmation, and only create_rule() writes the
    row. The LLM never authors a rule; every message here is rendered from the
    parsed struct or the written row. Returns True if handled.
    """
    from artemis import playbook_rules

    channel_id = post.get("channel_id", "")
    root_id = post.get("root_id") or post["id"]
    q = question.strip()
    ql = q.lower()

    def _say(text: str) -> None:
        if _mm:
            _mm.post_to_channel_id(channel_id, text, root_id=root_id)

    # 1) Pending rule-activation confirm (type-gated; coexists with other confirms).
    pending = _pending_confirms.get(channel_id)
    if pending and pending.get("type") == "playbook_rule":
        if ql in _CONFIRM_WORDS:
            del _pending_confirms[channel_id]
            spec = pending["spec"]
            try:
                row = playbook_rules.create_rule(
                    name=pending["name"], action=spec["action"],
                    action_label=spec["action_label"],
                    match_sender=spec["match_sender"],
                    match_subject=spec["match_subject"],
                    match_body=spec["match_body"],
                )
            except Exception as exc:
                logger.exception("create_rule failed")
                _say(f"\u26a0\ufe0f Couldn't save the rule: {exc}")
                return True
            # Confirmation rendered FROM THE WRITTEN ROW — not fabricated.
            _say(
                f"\u2705 Rule #{row['id']} active — {playbook_rules.describe_rule(row)}\n"
                f"It runs on new inbox mail from now on. `rules` to review, "
                f"`rule off {row['id']}` to disable."
            )
            return True
        if ql in _CANCEL_WORDS:
            del _pending_confirms[channel_id]
            _say("Cancelled — no rule created.")
            return True
        # else: fall through and re-parse as a fresh command

    # 2) List active rules.
    if ql in ("rules", "list rules", "show rules"):
        rows = playbook_rules.list_rules(active_only=True)
        if not rows:
            _say('No active rules. Add one, e.g. `rule add archive '
                 'from:cloudflare-workers-and-pages body:"Deploy successful"`.')
            return True
        lines = ["\U0001f4cb Active rules:"]
        for r in rows:
            fired = f"fired {r['times_fired']}\u00d7" if r.get("times_fired") else "never fired"
            lines.append(f"  #{r['id']} — {playbook_rules.describe_rule(r)}  ({fired})")
        lines.append("\nDisable one with `rule off <id>`.")
        _say("\n".join(lines))
        return True

    # 3) Deactivate a rule.
    m = re.match(r"^rule\s+(?:off|disable|remove|delete)\s+#?(\d+)\s*$", ql)
    if m:
        rid = int(m.group(1))
        _say(f"\u2705 Rule #{rid} deactivated." if playbook_rules.deactivate_rule(rid)
             else f"No rule #{rid} found.")
        return True

    # 4) Add a rule → propose-then-confirm.
    m = re.match(r"^rule\s+add\s+(.+)$", q, re.IGNORECASE | re.DOTALL)
    if m:
        try:
            spec = playbook_rules.parse_rule_spec(m.group(1))
        except playbook_rules.RuleSpecError as exc:
            _say(f"\u26a0\ufe0f {exc}\n\nFormat: `rule add <archive|spam|file as LABEL> "
                 '[from:…] [subject:"…"] [body:"…"]`')
            return True
        anchor = spec.get("match_sender") or spec.get("match_subject") or spec.get("match_body")
        name = f"{spec['action']}:{anchor}"[:80]
        _pending_confirms[channel_id] = {
            "type": "playbook_rule", "spec": spec, "name": name, "timestamp": time.time(),
        }
        _say(
            f"\U0001f4cb Proposed rule — **{playbook_rules.describe_rule(spec)}**\n"
            f"This becomes standing automation on all new inbox mail. "
            f"Reply `yes` to activate, `no` to cancel."
        )
        return True

    return False


def _collect_post_attachments(post: dict) -> list[dict]:
    """Fetch text attachments referenced by a Mattermost post → list of
    {filename, ext, content(bytes)} for dossier capture. The text-only policy
    (reject binaries) is applied downstream in dossier._extract_attachment_text."""
    file_ids = post.get("file_ids") or []
    if not file_ids:
        meta = post.get("metadata") or {}
        file_ids = [f.get("id") for f in (meta.get("files") or []) if f.get("id")]
    out = []
    for fid in file_ids:
        if not _mm:
            break
        try:
            info = _mm.get_file_metadata(fid)
            content = _mm.get_file_content(fid)
            out.append({"filename": info.get("name"), "ext": info.get("extension"),
                        "content": content})
        except Exception:
            logger.exception("failed to fetch dossier attachment %s", fid)
    return out


def _parse_brief_args(q: str) -> tuple[str, str | None]:
    """Extract (person, topic) from the brief phrasings: `brief jeremy about x`,
    `prepare a meeting package for jeremy`, `i'm meeting with jeremy about x[,
    prepare a meeting package]`."""
    s = q.strip()
    # Strip a LEADING trigger first (so `prepare a meeting package for jeremy`
    # keeps `jeremy`), then a TRAILING `…, prepare a meeting package` clause (the
    # `i'm meeting with jeremy about x, prepare a meeting package` form).
    s = re.sub(r"^(?:brief|i'?m\s+meeting\s+with|meeting\s+with)\s+", "", s, flags=re.IGNORECASE).strip()
    s = re.sub(r"^prepare\s+(?:a\s+)?meeting\s+package(?:\s+for)?\s+", "", s, flags=re.IGNORECASE).strip()
    s = re.sub(r"[,;]?\s*prepare\s+(?:a\s+)?meeting\s+package\b.*$", "", s, flags=re.IGNORECASE).strip()
    topic = None
    tm = re.search(r"\s+about\s+(.+)$", s, re.IGNORECASE)
    if tm:
        topic = tm.group(1).strip().rstrip(".")
        s = s[:tm.start()].strip()
    return s.strip(" ,."), topic


def _handle_dossier_subcommand(post: dict, q: str, say, channel_id: str) -> bool:
    """`dossier review | show | new | set` dispatch. Returns True (always handles a
    `dossier …` message — falls to a help line for anything unrecognized)."""
    from artemis import dossier
    ql = q.lower()

    if re.match(r"^dossier\s+review\b", ql):
        reply, mapping = dossier.render_review()
        if mapping:
            _dossier_review_state[channel_id] = mapping
        else:
            _dossier_review_state.pop(channel_id, None)
        say(reply)
        return True

    m = re.match(r"^dossier\s+show\s+(.+)$", q, re.IGNORECASE)
    if m:
        arg = m.group(1).strip()
        include_drafts = bool(re.search(r"--drafts\b", arg, re.IGNORECASE))
        arg = re.sub(r"--drafts\b", "", arg, flags=re.IGNORECASE).strip()
        say(dossier.show(arg, include_drafts=include_drafts))
        return True

    m = re.match(r"^dossier\s+new\s+(.+)$", q, re.IGNORECASE)
    if m:
        say(dossier.dossier_new(m.group(1).strip()))
        return True

    if re.match(r"^dossier\s+set\b", ql):
        parsed = dossier.parse_set_command(q)
        if parsed.get("error"):
            say(parsed["error"])
            return True
        # Propose-then-confirm — Ryan-authored §1/§2 prose AND org-assignment facts.
        _pending_confirms[channel_id] = {
            "type": "dossier_set", "dossier_id": parsed["dossier_id"],
            "full_name": parsed["full_name"], "payload": parsed["payload"],
            "timestamp": time.time(),
        }
        say(f"\U0001f4cb Set for {parsed['full_name']}?\n"
            f"> {parsed['preview']}\n\nReply `yes` to save, `no` to cancel.")
        return True

    say("Dossier commands: `dossier review` · `dossier show <name> [--drafts]` · "
        "`dossier new <name>` · `dossier set <name> position:|needs:|title:|reports_to:|org: …`")
    return True


# P1/P3: the on-demand morning brief. These phrases route to the deterministic
# composer (below) BEFORE the LLM mention path or the `log_morning_state`
# classifier can free-compose a brief from a naive now() + stale calendar cache
# (the Jul-6 root cause). Kept distinct from dossier's `brief <name>` meeting
# package — none of these carry a person.
_MORNING_BRIEF_PHRASES = frozenset({
    "morning brief", "morning briefing", "daily brief", "my brief",
    "brief me", "today's brief", "todays brief", "the brief",
    "what's my day", "whats my day", "what's my day look like",
    "whats my day look like",
})


def _handle_help_command(post: dict, question: str) -> bool:
    """`help` / `commands` — render the command vocabulary from help_registry
    (generated, never a hand-maintained string). `help <word>` filters. Returns
    True if handled."""
    from artemis import help_registry

    q = question.strip().rstrip("?.! ")
    # `help` / `commands`, optionally + a single filter word (`help email`). A
    # longer phrase ("help me draft an email") is NOT a help command — fall
    # through to the LLM.
    m = re.match(r"^(?:help|commands)(?:\s+([\w-]+))?$", q, re.IGNORECASE)
    if not m:
        return False

    channel_id = post.get("channel_id", "")
    root_id = post.get("root_id") or post["id"]
    reply = help_registry.render_help(m.group(1))
    if _mm:
        _mm.post_to_channel_id(channel_id, reply, root_id=root_id)
    return True


def _handle_morning_brief_command(post: dict, question: str) -> bool:
    """P1/P3 — on-demand `morning brief`: a fresh, deterministic composition at
    invocation (CT-anchored today, live inbox count, absolutes beside relatives),
    NOT a replay of the 04:00 brief and never the LLM. Returns True if handled."""
    q = question.lower().strip().rstrip("?.! ")
    if q not in _MORNING_BRIEF_PHRASES:
        return False

    channel_id = post.get("channel_id", "")
    root_id = post.get("root_id") or post["id"]
    if _sched is None:
        reply = "⚠️ Brief unavailable — scheduler not ready yet."
    else:
        try:
            # include_monitors=False: the SSL/domain block is a 04:00 ops concern;
            # on-demand stays fast and focused on the day.
            reply = _sched.compose_morning_brief(include_monitors=False)
        except Exception:
            logger.exception("on-demand morning brief failed")
            reply = "⚠️ Couldn't build the brief — check logs."
    if _mm:
        _mm.post_to_channel_id(channel_id, reply, root_id=root_id)
    return True


def _handle_version_command(post: dict, question: str) -> bool:
    """OPS-1 deterministic `version` — deploy-accurate version truth, never an LLM
    guess. Replies with VERSION (`1.4.0+<sha7>`), the running commit's subject line,
    and the service start time. Runs in the deterministic chain, ahead of LLM
    routing. `update check` (GitHub compare) stays on the direct-command path below.
    Returns True if handled, else False.
    """
    q = question.lower().strip().rstrip("?")
    if q not in ("version", "what version", "what version are you", "which version"):
        return False
    channel_id = post.get("channel_id", "")
    root_id = post.get("root_id") or post["id"]
    subject = get_commit_subject()
    started = (
        datetime.fromtimestamp(_start_time).strftime("%Y-%m-%d %H:%M:%S")
        if _start_time else "unknown"
    )
    lines = [
        f"\U0001f9ed **Artemis {VERSION}**",
        f"- Commit: {subject}" if subject else "- Commit: unknown",
        f"- Started: {started}",
    ]
    if _mm:
        _mm.post_to_channel_id(channel_id, "\n".join(lines), root_id=root_id)
    return True


def _handle_vault_command(post: dict, question: str) -> bool:
    """PB-011 deterministic vault router — evaluated BEFORE the LLM classifier and
    ahead of the dossier router in the chain. Handles `vault sync|status`, `digest`
    (also `today's digest` / `vault digest`), `proposals[ expired]`, and the
    `approve`/`reject` adjudication of a live digest. Every reply renders from written
    rows (no-fabrication gate). Returns True if handled, else False (falls through).

    `approve`/`reject` are claimed ONLY when a vault digest is live for this channel
    AND the message carries listing numbers (or `approve all`) — so a bare `approve`
    with no digest, or a non-numeric `approve sched …`, falls through untouched to
    the dossier review / scheduling handlers.
    """
    from artemis import vault

    channel_id = post.get("channel_id", "")
    root_id = post.get("root_id") or post["id"]

    def say(text: str) -> None:
        if _mm:
            _mm.post_to_channel_id(channel_id, text, root_id=root_id)

    q = question.strip()
    ql = q.lower()

    # ── Adjudication (gated on a live digest + parseable numbers) ──
    state = _vault_digest_state.get(channel_id)
    if state:
        if re.match(r"^approve\b", ql):
            rest = q[len("approve"):].strip()
            nums = sorted(state) if rest.lower() in ("all", "everything") else _parse_numbers(rest)
            if nums:
                say(vault.adjudicate("approve", nums, state))
                if not state:
                    _vault_digest_state.pop(channel_id, None)
                return True
            # non-numeric approve (e.g. `approve sched …`) — not ours, fall through.
        if re.match(r"^reject\b", ql):
            nums = _parse_numbers(q[len("reject"):])
            if nums:
                say(vault.adjudicate("reject", nums, state))
                if not state:
                    _vault_digest_state.pop(channel_id, None)
                return True

    # ── Commands ──
    if re.match(r"^vault\s+sync\b", ql):
        say(vault.cmd_sync())
        return True
    if re.match(r"^vault\s+status\b", ql):
        say(vault.cmd_status())
        return True
    if re.match(r"^proposals\s+expired\b", ql):
        reply, _ = vault.cmd_proposals(expired=True)
        say(reply)
        return True
    if re.match(r"^proposals\b", ql):
        reply, mapping = vault.cmd_proposals(expired=False)
        if mapping:
            _vault_digest_state[channel_id] = mapping
        else:
            _vault_digest_state.pop(channel_id, None)
        say(reply)
        return True
    if re.match(r"^(?:vault\s+digest|today'?s\s+digest|digest)\b", ql):
        reply, mapping = vault.cmd_digest()
        if mapping:
            _vault_digest_state[channel_id] = mapping
        else:
            _vault_digest_state.pop(channel_id, None)
        say(reply)
        return True

    return False


def _handle_dossier_command(post: dict, question: str) -> bool:
    """PB-010 deterministic dossier router — evaluated BEFORE the LLM classifier
    (it runs ahead of _handle_intent_routed in the mention chain) and unoverridable
    on a positive match. Confirmations render from written rows (no-fabrication
    gate). Returns True if handled, else False (falls through).
    """
    from artemis import dossier
    from artemis.intent import detect_dossier_intent

    channel_id = post.get("channel_id", "")
    root_id = post.get("root_id") or post["id"]

    def say(text: str) -> None:
        if _mm:
            _mm.post_to_channel_id(channel_id, text, root_id=root_id)

    q = question.strip()
    ql = q.lower()

    # Pending `dossier set` confirm (propose-then-confirm). Checked regardless of
    # tag because the `yes`/`no` reply carries no dossier trigger word. Other
    # confirm handlers already ran and ignore a non-matching pending type.
    pending = _pending_confirms.get(channel_id)
    if pending and pending.get("type") == "dossier_set":
        if ql in _CONFIRM_WORDS:
            del _pending_confirms[channel_id]
            # apply_set renders the confirmation from the written rows.
            say(dossier.apply_set(pending["dossier_id"], pending["payload"]))
            return True
        if ql in _CANCEL_WORDS:
            del _pending_confirms[channel_id]
            say("Cancelled — nothing changed.")
            return True
        # else: fall through and re-parse as a fresh command

    # Pending `org set` confirm (PB-010d org-profile authoring).
    if pending and pending.get("type") == "org_set":
        if ql in _CONFIRM_WORDS:
            del _pending_confirms[channel_id]
            say(dossier.apply_org_set(pending["org"], pending["column"], pending["value"]))
            return True
        if ql in _CANCEL_WORDS:
            del _pending_confirms[channel_id]
            say("Cancelled — nothing changed.")
            return True

    tag = detect_dossier_intent(question)
    if not tag:
        return False

    # ── review-context commands: only when a review is pending for this channel ──
    if tag in ("approve", "drop", "edit"):
        mapping = _dossier_review_state.get(channel_id)
        if not mapping:
            return False  # no pending review → let it fall through to the LLM
        if tag == "approve":
            rest = q[len("approve"):].strip()
            if rest.lower() in ("all", "everything"):
                say(dossier.approve_all(mapping))
            else:
                nums = _parse_numbers(rest)
                say(dossier.approve_items(nums, mapping) if nums
                    else "Usage: `approve all` · `approve 1-4` · `approve 1 & 3`")
        elif tag == "drop":
            nums = _parse_numbers(q[len("drop"):])
            say("\n".join(dossier.drop_item(n, mapping) for n in nums) if nums
                else "Usage: `drop <n>`")
        else:  # edit
            m = re.match(r"^edit\s+(\d+)\s*:\s*(.+)$", q, re.IGNORECASE | re.DOTALL)
            say(dossier.edit_item(int(m.group(1)), m.group(2).strip(), mapping) if m
                else "Usage: `edit <n>: <new text>`")
        if not mapping:  # review emptied out
            _dossier_review_state.pop(channel_id, None)
        return True

    if tag == "capture":
        say(dossier.capture_meeting(question, _collect_post_attachments(post)))
        return True

    if tag == "brief":
        person, topic = _parse_brief_args(q)
        say(dossier.brief(person, topic) if person
            else "Who are you meeting? `brief <name> [about <topic>]`")
        return True

    if tag == "remind":
        say(dossier.direct_commitment(question) or "Try `remind me to <task> <when>`.")
        return True

    if tag == "todos":
        # B4: pick the window from the phrasing (CT-anchored inside dossier.todos).
        if re.search(r"\bnext\s+week\b", ql):
            window = "next_week"
        elif re.search(r"\btomorrow\b", ql):
            window = "tomorrow"
        elif re.search(r"\btoday\b", ql):
            window = "today"
        else:
            window = "week"  # "this week" and the bare form
        say(dossier.todos(window))
        return True

    if tag == "org":
        if re.match(r"^org\s+set\b", ql):
            parsed = dossier.parse_org_set(q)
            if parsed.get("error"):
                say(parsed["error"])
                return True
            _pending_confirms[channel_id] = {
                "type": "org_set", "org": parsed["org"], "column": parsed["column"],
                "label": parsed["label"], "value": parsed["value"], "timestamp": time.time(),
            }
            say(f"\U0001f4cb Set for {parsed['org']}?\n> {parsed['preview']}\n\n"
                f"Reply `yes` to save, `no` to cancel.")
            return True
        m = re.match(r"^org\s+notes\s+(.+)$", q, re.IGNORECASE)
        if m:
            say(dossier.org_notes_render(m.group(1).strip()))
            return True
        if re.match(r"^org\s+notes\b", ql):
            say("Usage: `org notes <orgname>`")
            return True
        say(dossier.org_query(_org_query_arg(q)))
        return True

    if tag == "dossier":
        return _handle_dossier_subcommand(post, q, say, channel_id)

    return False


def _org_query_arg(q: str) -> str:
    """Extract the subject of an org query from `org <arg>` or a natural form
    (`where does X fit`, `who does X report to`, `who reports to X`)."""
    m = re.match(r"^org\b\s*(.*)$", q, re.IGNORECASE)
    if m:
        return m.group(1).strip().rstrip("?").strip()
    for pat in (r"where\s+does\s+(.+?)\s+fit\b",
                r"who\s+does\s+(.+?)\s+reports?\s+to\b",
                r"who\s+reports?\s+to\s+(.+?)\s*$"):
        m = re.search(pat, q, re.IGNORECASE)
        if m:
            return m.group(1).strip().rstrip("?").strip()
    return ""


def _handle_disposition_command(post: dict, question: str) -> bool:
    """E3: archive/delete/file/spam <numbers>, single or COMPOUND batch.
    Resolve-or-refuse on numbers, act+verify+log per item, report verified-only.
    A disposition-SHAPED line during an active listing is handled here or refused
    in-context — it never falls through to the financial/LLM classifier.
    Read EMAIL_MODEL.md. Returns True if handled (or refused), else False.
    """
    channel_id = post.get("channel_id", "")
    root_id = post.get("root_id") or post["id"]
    state = _inbox_listing_state.get(channel_id)
    mapping = state.get("mapping") if state else None

    # ── Pending compound confirm (consequential batch awaiting yes/no) ──
    # Defer to the canonical _pending_confirms system if one is open for this
    # channel — never steal a calendar/CRM control word. Reuses _CONFIRM_WORDS/
    # _CANCEL_WORDS so the confirm vocabulary can't drift between subsystems.
    pending = state.get("pending_dispositions") if state else None
    if pending and channel_id not in _pending_confirms:
        ans = _strip_parentheticals(question).strip().lower()
        if ans in _CONFIRM_WORDS:
            state.pop("pending_dispositions", None)
            lines: list[str] = []
            for verb, numbers, category in pending:
                lines.extend(_execute_disposition_group(channel_id, mapping or {}, verb, numbers, category))
            if _mm:
                _mm.post_to_channel_id(channel_id, "\n".join(lines), root_id=root_id)
            return True
        if ans in _CANCEL_WORDS:
            state.pop("pending_dispositions", None)
            if _mm:
                _mm.post_to_channel_id(channel_id, "Cancelled — nothing was changed.", root_id=root_id)
            return True
        # Anything else: fall through to re-parse (treat as a new command).

    parsed = _parse_disposition_command(question)

    # ── COMPOUND batch (multiple groups, or a form the single parser missed) ──
    if not parsed:
        # P6: parse line-by-line so every line is accounted for — parsed into the
        # plan or echoed under "Didn't understand:". Zero silent drops.
        compound, unrecognized = _parse_disposition_batch(question)
        if compound:
            if not mapping:
                if _mm:
                    _mm.post_to_channel_id(
                        channel_id,
                        "No active inbox listing — say `inbox` first, then e.g. `archive 2`.",
                        root_id=root_id,
                    )
                return True
            total = sum(len(g[1]) for g in compound)
            if total > _DISPOSITION_MAX_BATCH:
                if _mm:
                    _mm.post_to_channel_id(
                        channel_id,
                        f"That's {total} items — too many at once. Use smaller ranges.",
                        root_id=root_id,
                    )
                return True
            destructive = any(v in _CONSEQUENTIAL_VERBS for v, _, _ in compound)
            plan = _format_disposition_plan(compound)
            if destructive:
                # propose-then-confirm: the parse itself is the new risky surface,
                # and the batch deletes/spams — read it back (WITH any unparsed
                # lines echoed) and wait for `yes`.
                if state is not None:
                    state["pending_dispositions"] = compound
                echo = f"\n\n{_format_unrecognized(unrecognized)}" if unrecognized else ""
                reply = (
                    f"\U0001f4cb Parsed {len(compound)} groups ({total} items):\n{plan}{echo}\n\n"
                    f"This batch includes delete/spam. Reply `yes` to run it, `no` to cancel."
                )
                if _mm:
                    _mm.post_to_channel_id(channel_id, reply, root_id=root_id)
                return True
            # reversible-only (archive/file) → execute now, with a parse readback.
            lines = [f"\U0001f4cb Parsed {len(compound)} groups:", plan]
            if unrecognized:
                lines += ["", _format_unrecognized(unrecognized)]
            lines.append("")
            for verb, numbers, category in compound:
                lines.extend(_execute_disposition_group(channel_id, mapping, verb, numbers, category))
            if _mm:
                _mm.post_to_channel_id(channel_id, "\n".join(lines), root_id=root_id)
            return True

        # No group parsed, but disposition-shaped lines that ALL failed → reply
        # with the usage summary (never a silent drop, never the LLM). Gated on an
        # active listing + a line that actually looks like an attempt.
        if unrecognized and mapping and any(_looks_dispositional(u) for u in unrecognized):
            if _mm:
                _mm.post_to_channel_id(
                    channel_id,
                    f"{_format_unrecognized(unrecognized)}\n\n"
                    "Grammar:\n"
                    "  • `archive 1-4` · `delete 2, 5` · `spam 9`\n"
                    "  • `file 5-7, 13 as founder loans` (multi-word ok)\n"
                    "  • batch: one action per line, or "
                    "`archive 1-4  file 5-7 as founder loans  delete 14`",
                    root_id=root_id,
                )
            return True

    # EMAIL-ROUTING (context-aware routing): when a listing is ACTIVE in this
    # channel, a declarative reference to listing numbers ("1 & 2 are founder
    # loans") must resolve in listing context — NOT fall through to the keyword/
    # financial classifiers (the HEALTH-1 context-blind-routing class, which
    # mis-fired the financial report on "founder loans"). It carries no explicit
    # disposition verb, so we PROPOSE the matching `file` command rather than
    # auto-mutating on a loosely-phrased declarative (propose-then-confirm). Only
    # fires when a listing is active and at least one number actually resolves.
    if not parsed:
        if mapping:
            ref = _parse_listing_reference(question)
            if ref and any(n in mapping for n in ref[0]):
                nums, cat = ref
                nums_str = ", ".join(str(n) for n in nums)
                plural = "s" if len(nums) > 1 else ""
                reply = (
                    f"\U0001f4cc That refers to inbox item{plural} {nums_str}. "
                    f"To file under `@artemis/{cat}`: `file {nums_str} as {cat}` "
                    f"(or `archive`/`delete`/`spam` {nums_str})."
                )
                if _mm:
                    _mm.post_to_channel_id(channel_id, reply, root_id=root_id)
                return True
        # ARCHITECTURAL GUARD: a disposition-SHAPED line (verb + number) that
        # reached here failed every parse. With a listing active, refuse IN
        # CONTEXT — it must never fall through to the keyword/financial/LLM
        # classifier (which mis-routed `file ... as founder loans` to the
        # financial report). Reversibility: this only refuses, never acts.
        if mapping and _looks_dispositional(question):
            reply = (
                "\u26a0\ufe0f Couldn't parse that as inbox actions. Grammar:\n"
                "  • `archive 1-4` · `delete 2, 5` · `spam 9`\n"
                "  • `file 5-7, 13 as founder loans` (multi-word ok)\n"
                "  • batch: `archive 1-4  file 5-7 as founder loans  delete 14`\n"
                "Numbers may go before or after the verb."
            )
            if _mm:
                _mm.post_to_channel_id(channel_id, reply, root_id=root_id)
            return True
        return False

    verb, numbers, category = parsed

    # Guard a runaway range (e.g. `archive 1-99999`) from flooding the channel
    # with per-number refusals. Real listings are at most a few hundred.
    if len(numbers) > _DISPOSITION_MAX_BATCH:
        reply = (
            f"That's {len(numbers)} items — too many at once. "
            f"Use a range within the listing (say `inbox` to see the count)."
        )
        if _mm:
            _mm.post_to_channel_id(channel_id, reply, root_id=root_id)
        return True

    if not mapping:
        reply = "No active inbox listing — say `inbox` first, then e.g. `archive 2`."
        if _mm:
            _mm.post_to_channel_id(channel_id, reply, root_id=root_id)
        return True

    lines = _execute_disposition_group(channel_id, mapping, verb, numbers, category)
    if _mm:
        _mm.post_to_channel_id(channel_id, "\n".join(lines), root_id=root_id)
    return True


# A Gmail thread id is a long hex token; the short id shown in nudges is 12 hex
# chars. A single hex token of 8+ chars is the thread-id SHAPE; anything with a
# space, or a non-hex character, is words (a commitment title).
_THREAD_ID_SHAPE = re.compile(r"^#?[0-9a-f]{8,}$", re.IGNORECASE)

_DONE_USAGE = (
    "`done` needs a target — I route on its shape:\n"
    "  • `done <thread-id>` → mark an inbox email thread done "
    "(a hex id like `18c9f0a2b3d4`)\n"
    "  • `done <title>` → close a commitment by title (same as `close`)\n"
    "  • `close #<id>` → close a commitment by its number"
)


def _classify_done_arg(arg: str) -> str:
    """P8 — route `done <arg>` deterministically on argument SHAPE.

    Returns "thread" (hex-id shape → email-thread lifecycle), "commitment"
    (words → close_commitment), or "empty" (no argument → show both usages).
    Pure/side-effect-free so the routing is unit-testable without Mattermost."""
    a = arg.strip()
    if not a:
        return "empty"
    if _THREAD_ID_SHAPE.match(a):
        return "thread"
    return "commitment"


def _handle_done_command(post: dict, question: str) -> bool:
    """P8 — deterministic `done` router.

    Before this, the email-thread lifecycle consumed any `done <token>` and read
    token 2 as a thread id, so `done follow up with jennifer` answered "Thread not
    found: follow" and the commitment closer was unreachable via `done`. Now the
    argument's shape decides: a hex-id → the thread lifecycle (resolve → mark_done);
    words → close_commitment (identical to `close`); nothing (or a hex id that
    matches no thread) → one reply listing both interpretations. Same precedence
    discipline as PR #77 — deterministic shape routing, never an LLM guess.

    Placed AHEAD of _handle_inbox_command (which still owns wait/snooze/noise/…)
    and BEHIND _handle_health_conversation, so an active workout session's bare
    `done` is still claimed by the session loop first."""
    q = question.strip()
    m = re.match(r"^done\b\s*(.*)$", q, re.IGNORECASE | re.DOTALL)
    if not m:
        return False

    channel_id = post.get("channel_id", "")
    root_id = post.get("root_id") or post["id"]
    arg = m.group(1).strip()
    kind = _classify_done_arg(arg)

    if kind == "empty":
        reply = _DONE_USAGE
    elif kind == "thread":
        tid = resolve_thread_id(arg.lstrip("#"))
        if tid:
            mark_done(tid)
            reply = "✅ Marked thread DONE."
        else:
            # Hex-shaped but no thread matched — ambiguous/no-match: show both paths.
            reply = f"No inbox thread matches `{arg}`.\n\n{_DONE_USAGE}"
    else:  # commitment — same fuzzy-title close as `close`
        reply = format_close_result(close_commitment(arg))

    if _mm:
        _mm.post_to_channel_id(channel_id, reply, root_id=root_id)
    return True


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

            # ── Rule 2: Duplicate detection (Brad guard #2) ──
            # Confident duplicate (time overlap + title/attendee match) → BLOCK
            # by default; only `override duplicate` proceeds. A bare overlap with
            # an unrelated event is a soft note, not a block.
            verdict = _detect_calendar_duplicate(data)
            if verdict.get("duplicate"):
                block_msg = _register_duplicate_block(
                    channel_id, data, user_approved_external=False, match=verdict["match"],
                )
                replacement = "\n> " + block_msg.replace("\n", "\n> ") + "\n"
                response = response[:match.start()] + replacement + response[match.end():]
                continue

            # ── No blockers — create directly (audited) ──
            reply = _create_calendar_from_data(
                channel_id, data, user_approved_external=False, dup_override=False,
            )
            replacement = "\n> " + reply.replace("\n", "\n> ") + "\n"
            if verdict.get("soft_note"):
                replacement += f"> _Note: {verdict['soft_note']}_\n"

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


def _audit_calendar_write(
    action: str,
    event_id: str,
    *,
    title: str = "",
    start_ts: str = None,
    attendees: list[str] | None = None,
    has_external: bool = False,
    approved_by: str | None = None,
    dup_override: bool = False,
) -> None:
    """Record a calendar write to acos.calendar_audit (queryable). Never raises —
    an audit failure (e.g. RDS unavailable) must not fail the calendar action."""
    try:
        from knowledge.db import log_calendar_audit
        log_calendar_audit(
            action=action, event_id=event_id, title=title, start_ts=start_ts,
            attendees=attendees or [], has_external=has_external,
            approved_by=approved_by, dup_override=dup_override, actor="artemis",
        )
    except Exception:
        logger.exception("calendar_audit write failed (%s %s)", action, event_id)


def _detect_calendar_duplicate(event_data: dict) -> dict:
    """Fetch nearby events and classify the proposed event as a duplicate or not.

    Returns the guardrails.check_duplicate_event verdict; on any error or with no
    calendar, returns a non-duplicate verdict (fail-open for detection, never
    blocking a legitimate create on infra failure)."""
    from artemis.guardrails import check_duplicate_event
    if not _calendar or not getattr(_calendar, "service", None):
        return {"duplicate": False, "match": None, "soft_note": None}
    try:
        local_tz = ZoneInfo(config.TIMEZONE)
        start_dt = datetime.strptime(
            f"{event_data['date']} {event_data['start_time']}", "%Y-%m-%d %H:%M"
        ).replace(tzinfo=local_tz)
    except (KeyError, ValueError):
        return {"duplicate": False, "match": None, "soft_note": None}
    try:
        existing = _calendar.get_events_around(start_dt, window_hours=2)
        return check_duplicate_event(
            event_data.get("summary", ""), start_dt,
            event_data.get("attendees") or [], existing,
        )
    except Exception:
        # Detection must never block a legitimate create on an infra/parse error.
        logger.exception("Duplicate detection failed — proceeding without block")
        return {"duplicate": False, "match": None, "soft_note": None}


def _format_dup_block(match: dict) -> str:
    """Format the duplicate-block message. `override duplicate` is the ONLY way past."""
    title = match.get("summary", "(event)")
    eid = match.get("id", "?")
    when = match.get("start", "") or ""
    date_part, time_part = when, ""
    if "T" in when:
        date_part, _, rest = when.partition("T")
        time_part = rest[:5]
    return (
        f":warning: Blocked — this looks like a duplicate of '{title}' already on your "
        f"calendar {date_part} {time_part} (id {eid}). I did NOT create a new event.\n"
        f"To create it anyway, reply exactly: `override duplicate`"
    )


def _register_duplicate_block(
    channel_id: str, event_data: dict, *, user_approved_external: bool, match: dict
) -> str:
    """Store a duplicate_override pending and return the block message.

    user_approved_external is carried so a later override preserves (but does NOT
    grant) external-attendee approval — the hard guardrail still applies at create."""
    _pending_confirms[channel_id] = {
        "type": "duplicate_override",
        "data": event_data,
        "user_approved_external": user_approved_external,
        "match": match,
        "timestamp": time.time(),
    }
    return _format_dup_block(match)


def _create_calendar_from_data(
    channel_id: str, data: dict, *, user_approved_external: bool, dup_override: bool
) -> str:
    """Create the event, audit the write, and return the reply message.

    Nothing is narrated as "sent" before create_event returns a real id. The
    external-attendee hard guardrail lives inside create_event and is NOT
    bypassed here — a dup_override does not grant attendee approval."""
    from artemis.guardrails import get_external_attendees
    summary = data["summary"]
    date_str = data["date"]
    start_time_str = data["start_time"]
    end_time_str = data["end_time"]
    description = data.get("description")
    attendees = data.get("attendees") or []
    local_tz = ZoneInfo(config.TIMEZONE)
    start_dt = datetime.strptime(f"{date_str} {start_time_str}", "%Y-%m-%d %H:%M").replace(tzinfo=local_tz)
    end_dt = datetime.strptime(f"{date_str} {end_time_str}", "%Y-%m-%d %H:%M").replace(tzinfo=local_tz)
    attendee_str = ", ".join(attendees) if attendees else ""

    try:
        event_id = _calendar.create_event(
            summary=summary,
            start_datetime=start_dt,
            end_datetime=end_dt,
            description=description,
            attendees=attendees if attendees else None,
            _user_approved_external=user_approved_external,
            add_conference=bool(attendees),  # Google Meet for real invites
        )
    except Exception:
        logger.exception("create_event raised for '%s'", summary)
        event_id = None

    if not event_id:
        return (
            f":red_circle: Couldn't create the event **{summary}** — nothing was sent. "
            f"Check logs and try again."
        )

    has_external = bool(get_external_attendees(attendees))
    approved_by = "ryan" if (user_approved_external or dup_override) else None
    _audit_calendar_write(
        "create", event_id, title=summary, start_ts=start_dt.isoformat(),
        attendees=attendees, has_external=has_external,
        approved_by=approved_by, dup_override=dup_override,
    )
    log_calendar_action(
        action="create", event_id=event_id, summary=summary, attendees=attendee_str,
        user_approved=user_approved_external,
        notes="dup_override create" if dup_override else "confirmed create",
    )

    meet_link = ""
    try:
        meet_link = _calendar.get_meet_link(event_id) or ""
    except Exception:
        logger.exception("Failed to fetch Meet link for %s", event_id)

    reply = (
        f":white_check_mark: Event created: **{summary}** on "
        f"{date_str} {start_time_str}–{end_time_str} (ID: `{event_id}`)"
    )
    if attendee_str:
        reply += f"\nInvite sent to {attendee_str}."
    if meet_link:
        reply += f"\nGoogle Meet: {meet_link}"
    return reply


def _handle_duplicate_override(post: dict, question: str) -> bool:
    """Consume a pending duplicate_override. Only `override duplicate` creates;
    confirm words explicitly do NOT bypass the block. Returns True if handled."""
    channel_id = post.get("channel_id", "")
    root_id = post.get("root_id") or post["id"]
    if channel_id not in _pending_confirms:
        return False
    pending = _pending_confirms[channel_id]
    if pending.get("type") != "duplicate_override":
        return False
    if time.time() - pending["timestamp"] > 600:
        del _pending_confirms[channel_id]
        return False

    q_lower = question.lower().strip()

    if q_lower == _DUP_OVERRIDE_PHRASE:
        data = pending["data"]
        approved_ext = bool(pending.get("user_approved_external", False))
        del _pending_confirms[channel_id]
        reply = _create_calendar_from_data(
            channel_id, data, user_approved_external=approved_ext, dup_override=True,
        )
        if _mm:
            _mm.post_to_channel_id(channel_id, reply, root_id=root_id)
        return True

    if q_lower in _CANCEL_WORDS:
        del _pending_confirms[channel_id]
        if _mm:
            _mm.post_to_channel_id(channel_id, "Discarded — no event created.", root_id=root_id)
        return True

    if q_lower in _CONFIRM_WORDS:
        # A confirm word must NOT bypass a duplicate block — consume it (so it
        # never reaches the classifier) and restate the override instruction.
        if _mm:
            _mm.post_to_channel_id(
                channel_id,
                ":warning: That's a duplicate — `confirm`/`yes` won't override it. "
                "Reply exactly `override duplicate` to create it anyway, or `cancel` to discard.",
                root_id=root_id,
            )
        return True

    return False


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

    if q_lower in _CANCEL_WORDS:
        decision = "cancel"
    elif q_lower in _CONFIRM_WORDS:
        decision = "confirm"
    else:
        return False

    # Only handle calendar create types here; other types handled by their own handlers
    if pending.get("type") not in (None, "calendar_create", "calendar_create_external", "calendar_create_conflict"):
        return False

    local_tz = ZoneInfo(config.TIMEZONE)
    data = pending["data"]

    if decision == "cancel":
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

    # confirm — actually create the event. NOTHING is narrated as "sent" before
    # create_event returns a real id; an exception or None is reported as failure.
    if decision == "confirm":
        # Brad guard #2: duplicate detection runs BEFORE create and INDEPENDENTLY
        # of the external-approval gate — the user may have already accepted this
        # event and would not remember it, so approval alone can't prevent a dup.
        verdict = _detect_calendar_duplicate(data)
        if verdict.get("duplicate"):
            # Replace the confirm pending with a duplicate_override pending,
            # carrying the external approval already given in this confirm.
            msg = _register_duplicate_block(
                channel_id, data, user_approved_external=True, match=verdict["match"],
            )
            if _mm:
                _mm.post_to_channel_id(channel_id, msg, root_id=root_id)
            return True

        del _pending_confirms[channel_id]
        reply = _create_calendar_from_data(
            channel_id, data, user_approved_external=True, dup_override=False,
        )
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

    if q_lower in _CANCEL_WORDS:
        decision = "cancel"
    elif q_lower in _CONFIRM_WORDS:
        decision = "confirm"
    else:
        return False

    data = pending["data"]
    del _pending_confirms[channel_id]

    if decision == "cancel":
        if _mm:
            _mm.post_to_channel_id(channel_id, "Deletion cancelled.", root_id=root_id)
        return True

    if decision == "confirm":
        success = _calendar.delete_event(data["event_id"])
        if success:
            log_calendar_action(
                action="delete",
                event_id=data["event_id"],
                summary=data["summary"],
                user_approved=True,
                notes="Deleted by user via @mention",
            )
            _audit_calendar_write(
                "delete", data["event_id"], title=data.get("summary", ""),
                start_ts=data.get("start"), approved_by="ryan",
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
            if q_lower in _CONFIRM_WORDS or q_lower == "execute":
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
                        _audit_calendar_write(
                            "delete", ev["event_id"], title=ev.get("summary", ""),
                            approved_by="ryan",
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
            if q_lower in _CANCEL_WORDS:
                del _pending_confirms[channel_id]
                if _mm:
                    _mm.post_to_channel_id(channel_id, "Bulk convert cancelled.", root_id=root_id)
                return True
            # Not a control word — fall through so other handlers can try
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
    """Try ad-hoc rest day, then grocery and health commands in order.

    Workout logging was removed — the RDS health path (_handle_health_conversation,
    which runs earlier in dispatch) owns sessions, set-logging, debrief/capture,
    and history Q&A. Ad-hoc rest day stays HERE (not in the early health handler)
    so calendar/inbox claim a "day off"/"rest day" event first — the same low
    precedence the legacy workout handler had.
    """
    q = question.lower()
    # Ad-hoc rest day → RDS (replaces life_ops.log_rest_day; marks health.plan).
    from artemis.health import handle_rest_day
    rest = handle_rest_day(question)
    if rest:
        return rest
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
        from artemis.crm_writer import write_contacts, write_sales_plan_context

        contacts = []
        sales_plan = None
        if has_attachment and _mm and file_ids:
            # Download file from Mattermost
            try:
                resp = _mm._api("GET", f"/files/{file_ids[0]}")
                file_bytes = resp.content
                mime = attachment_mime or "application/octet-stream"
                contacts, sales_plan = parse_document(file_bytes, mime, user_context=question)
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

        if not contacts and not sales_plan:
            return "\u26a0\ufe0f I couldn't extract any contacts. Try attaching a document or mentioning names."

        # Sales plan path vs regular contacts
        if sales_plan:
            result = write_sales_plan_context(sales_plan, contacts)
            return result.summary
        else:
            result = write_contacts(contacts, ryan_context=question)
            return f"\U0001f4c7 **Contacts imported**\n{result.summary}"

    # ── query_crm ──
    if intent.primary_action == "query_crm":
        from artemis.crm_query import query_account, query_contact

        if not intent.entities:
            return "\U0001f50d Who or what would you like me to look up?"

        parts = []
        for entity_name in intent.entities:
            # Check if it's an organization or person
            from knowledge.db import execute_one as db_one
            is_org = db_one(
                "SELECT 1 FROM public.organizations WHERE LOWER(name) LIKE '%%' || LOWER(%s) || '%%' LIMIT 1",
                (entity_name,),
            )
            is_person = db_one(
                "SELECT 1 FROM public.contacts WHERE LOWER(name) LIKE '%%' || LOWER(%s) || '%%' LIMIT 1",
                (entity_name,),
            )

            if is_person:
                parts.append(query_contact([entity_name]))
            if is_org:
                parts.append(query_account(entity_name))
            if not is_person and not is_org:
                # Try both — might be an acos entity
                contact_result = query_contact([entity_name])
                if "not in the CRM" not in contact_result:
                    parts.append(contact_result)
                else:
                    account_result = query_account(entity_name)
                    if "No organization found" not in account_result:
                        parts.append(account_result)
                    else:
                        parts.append(contact_result)

        return "\n\n---\n\n".join(parts) if parts else f"\U0001f50d No results for: {', '.join(intent.entities)}"

    # ── log_interaction ──
    if intent.primary_action == "log_interaction":
        from artemis.interaction_logger import log_interaction as do_log_interaction
        try:
            return do_log_interaction(question, intent.entities)
        except Exception:
            logger.exception("Interaction logging failed")
            return "\u26a0\ufe0f Failed to log interaction \u2014 check DB connection."

    # ── log_morning_state (training) ──
    if intent.primary_action == "log_morning_state":
        from artemis.health import handle_morning_intent
        try:
            return handle_morning_intent(question, message_id=post.get("id"))
        except Exception:
            logger.exception("Morning check-in handler failed")
            return "\u26a0\ufe0f Couldn\u2019t save morning check-in \u2014 check DB."

    # ── log_workout_debrief (training) ──
    if intent.primary_action == "log_workout_debrief":
        from artemis.health import build_and_store_proposal, handle_fix_intent
        # First check if this is a "fix <exercise> rpe <N>" edit
        fix_result = handle_fix_intent(question)
        if fix_result is not None:
            return fix_result
        try:
            # Unified capture: propose-then-confirm, never an immediate write.
            # Belt-and-suspenders for debriefs the regex discriminator missed;
            # most pastes are caught earlier by _handle_capture_propose.
            return build_and_store_proposal(question, post.get("channel_id", ""))
        except Exception:
            logger.exception("Workout debrief propose failed")
            return "\u26a0\ufe0f Couldn\u2019t parse that debrief \u2014 try again."

    # ── trainer_override (training, T4) ──
    if intent.primary_action == "trainer_override":
        from artemis.health import handle_trainer_override
        try:
            return handle_trainer_override(question, message_id=post.get("id"))
        except Exception:
            logger.exception("Trainer override handler failed")
            return "\u26a0\ufe0f Couldn\u2019t save trainer override \u2014 check DB."

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

    # ── financial_summary ──
    if intent.primary_action == "financial_summary":
        from artemis.billing import get_financial_summary
        try:
            return get_financial_summary()
        except Exception:
            logger.exception("Financial summary failed")
            return "\u26a0\ufe0f Financial summary unavailable — check DB connection."

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


_WAKE_WORD_RE = re.compile(
    r"^\s*(?:hey\s+)?@?artemis\b[\s,:.\-]*|^\s*(?:hey\s+)?at\s+artemis\b[\s,:.\-]*",
    re.IGNORECASE,
)


def _strip_wake_word(message: str) -> str:
    """Strip a leading wake word before parsing.

    The always-listen channel (PB-009) means dictation often prefixes a message
    with '@artemis' / 'at artemis' / 'artemis' even though no mention is needed.
    Remove a single leading occurrence, then drop any remaining inline @artemis
    tokens (preserving prior behavior).
    """
    stripped = _WAKE_WORD_RE.sub("", message or "", count=1)
    return stripped.replace("@artemis", "").strip()


def _handle_health_conversation(post: dict, question: str) -> bool:
    """PB-009 — conversational workout session + training history Q&A.

    Runs BEFORE _try_life_ops (and before the inbox handler, so an active
    session's bare 'done' isn't shadowed by `done <thread_id>`). RDS-backed
    plan/session reads and writes always win over the legacy life_ops SQLite
    path. Returns True if a reply was posted.
    """
    channel_id = post.get("channel_id", "")
    root_id = post.get("root_id") or post["id"]
    # Never claim a message that a deterministic dossier/org verb owns (met with /
    # dossier / brief / org / remind / review-context). Belt-and-suspenders behind
    # the dispatch order: even if the chain is reordered, health won't swallow a
    # capture whose notes happen to contain weekday/plan words.
    from artemis.intent import detect_dossier_intent
    if detect_dossier_intent(question):
        return False
    try:
        from artemis.health import (
            INTENT_PLAN_DETAIL,
            INTENT_PLAN_LOOKUP,
            detect_health_intent,
            get_plan_detail,
            get_plan_lookup,
            handle_plan_query,
            handle_workout_session,
        )
        # plan_detail (single-day depth) and plan_lookup (multi-day breadth) are
        # RDS read-intents that win outright and NEVER fall through to
        # general_reply: each always returns a string (real plan or the exact
        # "No plan seeded for <date>." guard), which we post here.
        intent = detect_health_intent(question)
        if intent == INTENT_PLAN_DETAIL:
            reply = get_plan_detail(question)
        elif intent == INTENT_PLAN_LOOKUP:
            reply = get_plan_lookup(question)
        else:
            # Read-intent (history Q&A) first, then the session loop.
            reply = handle_plan_query(question)
            if reply is None:
                reply = handle_workout_session(question)
    except Exception:
        logger.exception("Health conversation handler failed")
        return False

    if reply:
        if _mm:
            _mm.post_to_channel_id(channel_id, reply, root_id=root_id)
        return True
    return False


def _handle_capture_propose(post: dict, question: str) -> bool:
    """Unified workout-capture entry (cardio + strength debrief).

    Highest-priority deterministic discriminator: a workout-metrics paste is
    parsed and echoed back as a both-units proposal (NOTHING is written), with
    the parsed rows stored in the durable system_state KV. The actual write
    happens only on a later `confirm` (see _handle_debrief_confirm). Returns True
    if this was a capture paste and a proposal was posted.

    Runs AFTER _handle_health_conversation so the live-session set-logging loop
    and plan reads are untouched; a cardio paste is something that loop never
    claims, so it falls through to here instead of to the LLM general_reply.
    """
    from artemis.health import build_and_store_proposal, is_capture_paste

    if not is_capture_paste(question):
        return False

    channel_id = post.get("channel_id", "")
    root_id = post.get("root_id") or post["id"]
    try:
        reply = build_and_store_proposal(question, channel_id)
    except Exception:
        logger.exception("Capture propose failed")
        return False
    if reply and _mm:
        _mm.post_to_channel_id(channel_id, reply, root_id=root_id)
    return True


def _handle_debrief_confirm(post: dict, question: str) -> bool:
    """Confirm/cancel leg for a pending workout capture (durable KV).

    Only acts when a pending capture exists for this channel AND the reply is a
    confirm/cancel word. On confirm, all rows insert in one transaction and the
    real log_ids + count are posted back. Returns True if handled.
    """
    from artemis.health import cancel_capture, commit_capture, load_capture_pending

    channel_id = post.get("channel_id", "")
    if load_capture_pending(channel_id) is None:
        return False

    q = question.lower().strip()
    if q in _CONFIRM_WORDS:
        reply = commit_capture(channel_id)
    elif q in _CANCEL_WORDS:
        reply = cancel_capture(channel_id)
    else:
        return False

    root_id = post.get("root_id") or post["id"]
    if reply and _mm:
        _mm.post_to_channel_id(channel_id, reply, root_id=root_id)
    return True


def _handle_nutrition_confirm(post: dict, question: str) -> bool:
    """Confirm/cancel leg for the two confirmed nutrition writes:
    a pending nutrition-target change, or a pending grocery-staples batch.

    Each guards on its own durable pending payload, so a bare `confirm`/`cancel`
    only acts when exactly that flow is awaiting one. Returns True if handled.
    """
    from artemis.health import (
        cancel_nutrition_target,
        commit_nutrition_target,
        load_nutrition_target_pending,
    )
    from artemis.life_ops import (
        cancel_grocery_staples,
        commit_grocery_staples,
        load_staples_pending,
    )

    channel_id = post.get("channel_id", "")
    q = question.lower().strip()
    if q not in _CONTROL_WORDS:
        return False

    reply = None
    if load_nutrition_target_pending(channel_id) is not None:
        reply = (commit_nutrition_target(channel_id) if q in _CONFIRM_WORDS
                 else cancel_nutrition_target(channel_id))
    elif load_staples_pending(channel_id) is not None:
        reply = (commit_grocery_staples(channel_id) if q in _CONFIRM_WORDS
                 else cancel_grocery_staples(channel_id))
    else:
        return False

    root_id = post.get("root_id") or post["id"]
    if reply and _mm:
        _mm.post_to_channel_id(channel_id, reply, root_id=root_id)
    return True


_BUILD_GROCERY_RE = re.compile(
    r"\bbuild\s+(?:my\s+|the\s+)?(?:grocery|shopping)\s+list\b"
    r"|\bgenerate\s+(?:my\s+|the\s+)?(?:grocery|shopping)\s+list\b"
    r"|\bgrocery\s+staples\b",
    re.IGNORECASE,
)


def _handle_grocery_staples(post: dict, question: str) -> bool:
    """`@artemis build grocery list` — propose the week's staples from the active
    meal plan (READS health.meal, proposes a write to acos.grocery_list). The
    upsert happens only on a later `confirm`. Returns True if this was a build
    request and a proposal was posted."""
    if not _BUILD_GROCERY_RE.search(question or ""):
        return False
    from artemis.life_ops import build_grocery_staples

    channel_id = post.get("channel_id", "")
    root_id = post.get("root_id") or post["id"]
    try:
        reply = build_grocery_staples(channel_id)
    except Exception:
        logger.exception("Grocery staple build failed")
        reply = "⚠️ Couldn't build the staples list — check DB."
    if reply and _mm:
        _mm.post_to_channel_id(channel_id, reply, root_id=root_id)
    return True


def _handle_nutrition(post: dict, question: str) -> bool:
    """PB-009 nutrition: set target (propose-then-confirm), log intake
    (append-only), or budget status. Runs before _try_life_ops so the dynamic
    budget coach wins over the static health-command stub. Returns True if a
    nutrition intent matched and a reply was posted."""
    from artemis.health import (
        INTENT_LOG_NUTRITION,
        INTENT_NUTRITION_STATUS,
        INTENT_SET_NUTRITION_TARGET,
        detect_nutrition_intent,
        log_nutrition,
        nutrition_status,
        propose_nutrition_target,
    )

    intent = detect_nutrition_intent(question)
    if intent is None:
        return False

    channel_id = post.get("channel_id", "")
    root_id = post.get("root_id") or post["id"]
    try:
        if intent == INTENT_SET_NUTRITION_TARGET:
            reply = propose_nutrition_target(question, channel_id)
        elif intent == INTENT_NUTRITION_STATUS:
            reply = nutrition_status()
        else:
            reply = log_nutrition(question)
    except Exception:
        logger.exception("Nutrition handler failed (%s)", intent)
        reply = "⚠️ Nutrition handler hit an error — check DB."
    if reply and _mm:
        _mm.post_to_channel_id(channel_id, reply, root_id=root_id)
    return True


def _handle_mention(post: dict, thread: list[dict]):
    """Handle an @artemis mention."""
    question = _strip_wake_word(post.get("message", ""))
    if not question:
        return

    # Track interaction for inactivity detection
    update_last_interaction()

    channel_id = post.get("channel_id", "")
    root_id = post.get("root_id") or post["id"]

    # A4: deterministic dispatch as a NAMED, instrumented chain. The handler that
    # claims the message logs its name at INFO ("dispatch: <name> handled …"); a
    # handler that raises is logged with a full traceback and answered with a
    # one-line reply. Silence is never a failure mode — and this is the instrument
    # for diagnosing the bare-message routing inconsistency (no speculative fix for
    # that until these logs show the real event flow). Order is load-bearing:
    #   * confirm flows first (a `yes`/`confirm` must reach its pending handler,
    #     not the LLM) — duplicate_override BEFORE calendar_confirm so a confirm
    #     can never bypass a duplicate block.
    #   * PB-009 nutrition/health BEFORE inbox (so a bare "done" isn't shadowed)
    #     and BEFORE the LLM router (RDS health path wins over legacy life_ops).
    #   * rule/dossier BEFORE the inbox + LLM router (deterministic short-circuits).
    deterministic_chain = [
        ("availability_command", _handle_availability_command),
        ("duplicate_override", _handle_duplicate_override),
        ("calendar_confirm", _handle_calendar_confirm),
        ("delete_confirm", _handle_delete_confirm),
        ("debrief_confirm", _handle_debrief_confirm),
        ("nutrition_confirm", _handle_nutrition_confirm),
        # PB-010 dossier/org: an explicit capture/authoring verb (met with /
        # dossier / brief / org / remind / review-context) outranks topical keyword
        # matching. Placed AHEAD of the nutrition/health matchers so meeting notes
        # that happen to contain weekday/plan words can't be claimed by
        # health_conversation (the HEALTH-1 principle — deterministic intent wins).
        # PB-011 vault: `vault sync|status`, `digest`, `proposals`, and the
        # approve/reject of a live digest. Ahead of dossier so a vault digest's
        # approve/reject is claimed here; a bare/non-numeric approve falls through
        # to the dossier review (its own gate) or the LLM.
        # OPS-1 version truth — deterministic, ahead of LLM routing.
        # help — generated command vocabulary; deterministic, ahead of the LLM.
        ("help_command", _handle_help_command),
        # P1/P3 on-demand morning brief — deterministic, ahead of the LLM path and
        # the `morning`-prefixed check-in classifier.
        ("morning_brief_command", _handle_morning_brief_command),
        ("version_command", _handle_version_command),
        ("vault_command", _handle_vault_command),
        ("dossier_command", _handle_dossier_command),
        ("grocery_staples", _handle_grocery_staples),
        ("nutrition", _handle_nutrition),
        ("health_conversation", _handle_health_conversation),
        ("capture_propose", _handle_capture_propose),
        ("quiet_command", _handle_quiet_command),
        ("rule_command", _handle_rule_command),
        ("inbox_listing", _handle_inbox_listing),
        ("disposition_command", _handle_disposition_command),
        # P8: `done <hex-id>` → thread lifecycle, `done <words>` → commitment close.
        # Ahead of inbox_command (which still owns wait/snooze/noise/inbox) so the
        # thread lifecycle can't swallow a commitment-close phrased as `done …`.
        ("done_command", _handle_done_command),
        ("inbox_command", _handle_inbox_command),
        ("action_item_command", _handle_action_item_command),
    ]
    for _name, _fn in deterministic_chain:
        try:
            if _fn(post, question):
                logger.info("dispatch: %s handled post %s", _name, post.get("id"))
                return
        except Exception:
            logger.exception("handler %s raised on post %s", _name, post.get("id"))
            if _mm:
                _mm.post_to_channel_id(
                    channel_id, "⚠️ error handling that — logged.", root_id=root_id
                )
            return

    # A4: no deterministic handler claimed it — mark the fall-through so a message
    # that ends up silent (or at the LLM) is never invisible in the journal.
    logger.info(
        "dispatch: no deterministic handler for post %s (msg=%r) — trying direct/LLM",
        post.get("id"), (question or "")[:60],
    )

    # Direct commands
    q_lower = question.lower().strip()

    # `version` / `what version` are claimed by _handle_version_command (deploy
    # truth). `update check` stays here — it compares the running commit to GitHub.
    if q_lower in ("update check", "check for updates"):
        reply = format_version_status()
        if _mm:
            _mm.post_to_channel_id(channel_id, reply, root_id=root_id)
        return

    # TEMP debug (Phase E1): sync the email_index mirror from Gmail, then report
    # the working-set count + first 5 rows. Verifies Artemis now sees the ENTIRE
    # unread inbox (e.g. 95), not just the last 5. Read-only — no dispositions.
    if q_lower in ("index status", "email index status"):
        from artemis import email_index
        try:
            summary = email_index.sync_from_gmail(_gmail)
            rows = email_index.query_working_set(limit=5, offset=0)
            lines = [
                f"\U0001f4c7 **Email index** — {summary['working_set']} in working set "
                f"(listed {summary['listed']}, fetched {summary['fetched']}, "
                f"upserted {summary['upserted']}, pruned {summary['pruned']})",
            ]
            for r in rows:
                subj = (r.get("subject") or "(no subject)")[:60]
                lines.append(f"- {r.get('sender', '?')} — {subj}")
            reply = "\n".join(lines)
        except Exception:
            logger.exception("index status command failed")
            reply = "⚠️ index status failed — check logs."
        if _mm:
            _mm.post_to_channel_id(channel_id, reply, root_id=root_id)
        return

    if q_lower in ("contacts", "leads"):
        # Routed to the RDS CRM API (CRMClient) — the SQLite crm.py contacts
        # store was removed. `leads` passes status='lead' to the API; an API
        # that doesn't filter returns all. Gated/handled like `crm status`.
        crm = CRMClient()
        if crm.is_available():
            try:
                status = "lead" if q_lower == "leads" else None
                reply = crm.format_contacts(crm.get_contacts(status=status))
            except Exception:
                logger.exception("CRM contacts fetch failed")
                reply = "⚠️ CRM API error — check logs."
        else:
            reply = "CRM API not configured (CRM_API_URL / CRM_API_KEY not set)."
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

    if q_lower.startswith("crm confirm "):
        pending_id = q_lower.split("crm confirm ", 1)[1].strip()
        try:
            from artemis.crm_write_guard import confirm_pending_write
            cw_result = confirm_pending_write(pending_id)
            if cw_result.get("status") == "confirmed":
                reply = (
                    f"\u2705 Confirmed CRM write: {cw_result.get('entity_type', '?')} "
                    f"created (id=`{cw_result.get('entity_id', '?')}`)"
                )
            else:
                reply = f"\u26a0\ufe0f Could not confirm: {cw_result.get('error', 'unknown error')}"
        except Exception:
            logger.exception("CRM confirm failed")
            reply = "\u26a0\ufe0f CRM confirm failed \u2014 check logs."
        if _mm:
            _mm.post_to_channel_id(channel_id, reply, root_id=root_id)
        return

    if q_lower.startswith("crm reject "):
        pending_id = q_lower.split("crm reject ", 1)[1].strip()
        try:
            from artemis.crm_write_guard import reject_pending_write
            cw_result = reject_pending_write(pending_id)
            if cw_result.get("status") == "rejected":
                reply = f"\u2705 Rejected and removed pending CRM write (`{pending_id[:8]}...`)"
            else:
                reply = f"\u26a0\ufe0f Could not reject: {cw_result.get('error', 'unknown error')}"
        except Exception:
            logger.exception("CRM reject failed")
            reply = "\u26a0\ufe0f CRM reject failed \u2014 check logs."
        if _mm:
            _mm.post_to_channel_id(channel_id, reply, root_id=root_id)
        return

    if q_lower == "crm pending":
        try:
            from artemis.crm_write_guard import list_pending_writes
            pending = list_pending_writes()
            if pending:
                lines = ["**Pending CRM writes:**"]
                for p in pending:
                    p_data = p["data"] if isinstance(p["data"], dict) else {}
                    lines.append(
                        f"- `{str(p['id'])[:8]}` {p['entity_type']}: "
                        f"{p_data.get('name', '?')} (from {p['source_pb']}, "
                        f"expires {p['expires_at'].strftime('%m/%d')})"
                    )
                reply = "\n".join(lines)
            else:
                reply = "No pending CRM writes."
        except Exception:
            logger.exception("CRM pending list failed")
            reply = "\u26a0\ufe0f Failed to list pending writes \u2014 check logs."
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
        arg = question.strip()[len("close "):].strip()
        # P5: `close #6` / `close 6` closes by id (deterministic); anything else
        # keeps the fuzzy-title path.
        id_m = re.match(r"^#?(\d+)$", arg)
        if id_m:
            result = close_commitment_by_id(int(id_m.group(1)))
            reply = format_close_result(result)
        else:
            title = parse_close_title(question)
            if title:
                result = close_commitment(title)
                reply = format_close_result(result)
            else:
                reply = 'Usage: `close #<id>` · `close "<title>"` · `close commitment "<title>"`'
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

    # ── Confirm backstop ──
    # If a pending action is still open for this channel and the user sent a
    # control word, it MUST NOT reach the intent classifier (which mislabels it
    # general_reply and confabulates a "sent" reply). The typed confirm handlers
    # above normally consume it; this is the last-resort guarantee for any
    # pending type/word they didn't match. Re-run them, then consume safely.
    if channel_id in _pending_confirms and (
        q_lower in _CONTROL_WORDS or q_lower == _DUP_OVERRIDE_PHRASE
    ):
        if _handle_duplicate_override(post, question):
            return
        if _handle_calendar_confirm(post, question):
            return
        if _handle_delete_confirm(post, question):
            return
        if _handle_convert_to_tasks(post, question):
            return
        logger.warning(
            "Open pending + control word '%s' unmatched by typed handlers — "
            "consuming to prevent a confabulated classifier reply", q_lower,
        )
        _pending_confirms.pop(channel_id, None)
        if _mm:
            _mm.post_to_channel_id(
                channel_id,
                ":warning: I couldn't match that confirmation to the pending action "
                "(it may have expired or already been handled). Please re-issue the request.",
                root_id=root_id,
            )
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

    # Anti-confabulation guard: Artemis must NEVER deny it has a workout database
    # or claim training data "came from the chat". If the LLM draft does, discard
    # it and return the real plan detail instead.
    try:
        from artemis.health import scrub_db_denial
        response = scrub_db_denial(response, question)
    except Exception:
        logger.exception("scrub_db_denial failed")

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


def _shutdown_cleanup() -> None:
    """STAB-1 A3: orchestrate a fast, bounded shutdown (no os._exit here so it's
    unit-testable). Each step is best-effort and cannot block the caller.

    Order: record last-run → time-boxed shutdown notice → scheduler.shutdown
    (wait=False, never blocks on a running job) → close websocket → close DB pool.
    """
    try:
        from artemis.quiet_hours import set_system_value
        set_system_value("last_run_at", datetime.utcnow().isoformat())
    except Exception:
        logger.debug("failed to record last_run_at", exc_info=True)
    # Time-box the shutdown notice — a hung REST post must not block exit.
    if _mm is not None:
        notice = Thread(target=_post_shutdown_message, args=(_mm,), daemon=True)
        notice.start()
        notice.join(timeout=2.0)
    try:
        if _sched:
            _sched.scheduler.shutdown(wait=False)
    except Exception:
        logger.debug("scheduler shutdown failed", exc_info=True)
    try:
        if _mm:
            _mm.close()
    except Exception:
        logger.debug("websocket close failed", exc_info=True)
    try:
        from knowledge.db import close_pool
        close_pool()
    except Exception:
        logger.debug("db pool close failed", exc_info=True)


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
    logger.info("Starting Artemis %s...", VERSION)

    # All state now lives in RDS (acos.* / public.*) — no local SQLite tables to
    # create. commitments was the last SQLite module (migration 020); artemis.db
    # has no writers anymore.

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

    # Pre-create @artemis Gmail label hierarchy
    if _gmail and _gmail.service:
        for _label in [
            "@artemis",
            "@artemis/billing",
            "@artemis/billing/paid",
            "@artemis/billing/disputed",
            "@artemis/pipeline",
            "@artemis/pipeline/demo-request",
            "@artemis/crm",
            "@artemis/crm/needs-review",
            "@artemis/needs-review",
            "@artemis/funding",
            "@artemis/funding/kiva",
            "@artemis/funding/aws-activate",
            "@artemis/funding/microsoft",
            "@artemis/funding/nsf",
            "@artemis/calendar",
            "@artemis/calendar/needs-confirm",
        ]:
            _gmail.ensure_gmail_label(_label)
        logger.info("Gmail label hierarchy initialized")

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
        # STAB-1 A3: fast, bounded shutdown (<5s) so systemd never has to SIGKILL
        # at 90s. Cleanup is best-effort; os._exit guarantees a prompt exit even
        # with Flask's blocking dev server on the main thread.
        logger.info("Shutting down (signal %s)...", sig)
        _shutdown_cleanup()
        logging.shutdown()
        import os
        os._exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    logger.info("Artemis is running. Press Ctrl+C to stop.")
    app.run(host="0.0.0.0", port=5001, use_reloader=False)


if __name__ == "__main__":
    main()
