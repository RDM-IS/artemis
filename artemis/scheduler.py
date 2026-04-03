"""Cron jobs for all scheduled tasks."""

import json
import logging
import re
import time
from datetime import date, datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler

from artemis import config
from artemis.briefs import generate_meeting_brief, generate_morning_brief, triage_emails
from artemis.calendar import CalendarClient
from artemis.commitments import (
    add_commitment,
    get_due_soon,
    get_start_alerts,
    get_commitments_for_client,
    list_commitments,
)
from artemis.crm import get_contact, upsert_contact
from artemis.gmail import GmailClient
from artemis.mattermost import MattermostClient
from artemis.inbox import (
    can_nudge,
    format_morning_inbox_section,
    format_thread_card,
    get_snoozed_due,
    get_stale_needs_action,
    get_stale_waiting,
    mark_needs_action,
    record_nudge,
    set_mattermost_post_id,
    upsert_thread,
    NEEDS_ACTION,
    NOISE,
)
from artemis.monitors import (
    check_all_ssl,
    check_domain_expiry,
    format_domain_alerts,
    format_ssl_alerts,
)
from artemis.prompts import UNTRUSTED_PREFIX
from artemis.billing import (
    check_billing_scopes,
    ensure_billing_label,
    get_billing_messages,
    process_billing_message,
)
from artemis.demo_intake import (
    get_demo_messages,
    process_demo_message,
)
from artemis.crm_client import CRMClient
from artemis.scheduling import detect_scheduling_request, draft_scheduling_response
from artemis.utils import next_business_day

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Playbook helpers
# ---------------------------------------------------------------------------

_playbook_text: str = ""


def load_playbooks() -> str:
    """Read PLAYBOOKS.md from disk and cache it.  Returns the raw text."""
    global _playbook_text
    try:
        path = config.PLAYBOOKS_PATH
        if path.exists():
            _playbook_text = path.read_text(encoding="utf-8")
            logger.info("Loaded playbooks from %s (%d bytes)", path, len(_playbook_text))
        else:
            logger.warning("Playbooks file not found at %s", path)
            _playbook_text = ""
    except Exception:
        logger.exception("Failed to load playbooks")
        _playbook_text = ""
    return _playbook_text


def get_playbook_text() -> str:
    """Return cached playbook text (load if empty)."""
    if not _playbook_text:
        return load_playbooks()
    return _playbook_text


class ArtemisScheduler:
    def __init__(
        self,
        mm: MattermostClient,
        gmail: GmailClient,
        calendar: CalendarClient,
    ):
        self.mm = mm
        self.gmail = gmail
        self.calendar = calendar
        self.crm = CRMClient()
        self.scheduler = BackgroundScheduler()
        self._pending_triage: list[dict] = []
        self._seen_message_ids: set[str] = set()
        self._pending_availability: dict[str, dict] = {}
        # Error recovery counters
        self._gmail_fail_count: int = 0
        self._calendar_fail_count: int = 0

    def start(self):
        # Inbox triage — every 5 minutes
        self.scheduler.add_job(self.job_inbox_triage, "interval", minutes=5, id="inbox_triage")

        # Triage batch post — every 30 minutes
        self.scheduler.add_job(self.job_post_triage_batch, "interval", minutes=30, id="triage_batch")

        # Pre-meeting brief check — every 10 minutes
        self.scheduler.add_job(self.job_pre_meeting_briefs, "interval", minutes=10, id="pre_meeting")

        # Morning brief
        hour, minute = config.MORNING_BRIEF_TIME.split(":")
        self.scheduler.add_job(
            self.job_morning_brief, "cron", hour=int(hour), minute=int(minute), id="morning_brief"
        )

        # SSL check — daily at 8am
        self.scheduler.add_job(self.job_ssl_check, "cron", hour=8, minute=0, id="ssl_check")

        # Domain expiry check — daily at 8am
        self.scheduler.add_job(self.job_domain_check, "cron", hour=8, minute=5, id="domain_check")

        # Inbox zero audit — every 60 minutes
        self.scheduler.add_job(self.job_inbox_zero_audit, "interval", minutes=60, id="inbox_zero_audit")

        # Inbox zero morning section — 5 minutes before morning brief
        brief_min = int(minute) - 5
        brief_hour = int(hour)
        if brief_min < 0:
            brief_min += 60
            brief_hour -= 1
        self.scheduler.add_job(
            self.job_inbox_zero_morning, "cron", hour=brief_hour, minute=brief_min, id="inbox_zero_morning"
        )

        # Titanium focus reminder — weekdays at 9am
        if config.FOCUS_CLIENT:
            self.scheduler.add_job(
                self.job_focus_reminder, "cron", hour=9, minute=0, day_of_week="mon-fri",
                id="focus_reminder",
            )

        # Weekly update check — Mondays at 8am
        self.scheduler.add_job(
            self.job_update_check, "cron", hour=8, minute=0, day_of_week="mon",
            id="update_check",
        )

        # PB-005: Commitment deadline reminders — weekdays at 8:15am
        self.scheduler.add_job(
            self.job_commitment_reminders, "cron", hour=8, minute=15, day_of_week="mon-fri",
            id="commitment_reminders",
        )

        # PB-007: Billing intake — every 15 minutes (check for billing-labeled emails)
        scopes_ok, missing = check_billing_scopes()
        if scopes_ok:
            self.scheduler.add_job(
                self.job_billing_intake, "interval", minutes=15, id="billing_intake",
            )
            logger.info("PB-007 billing intake enabled")
        else:
            logger.warning("PB-007 billing intake disabled — missing scopes: %s", missing)

        # PB-001 v2: Demo intake — every 5 minutes (scan for Lucint demo emails)
        self.scheduler.add_job(
            self.job_demo_intake, "interval", minutes=5, id="demo_intake",
        )
        logger.info("PB-001 demo intake enabled")

        # Quiet hours entry/exit announcements
        qh_start_h, qh_start_m = config.QUIET_HOURS_START.split(":")
        qh_end_h, qh_end_m = config.QUIET_HOURS_END.split(":")
        self.scheduler.add_job(
            self.job_quiet_hours_start, "cron",
            hour=int(qh_start_h), minute=int(qh_start_m), id="quiet_hours_start",
        )
        self.scheduler.add_job(
            self.job_quiet_hours_end, "cron",
            hour=int(qh_end_h), minute=int(qh_end_m), id="quiet_hours_end",
        )

        # Timezone override expiry check — daily at noon
        self.scheduler.add_job(
            self.job_check_timezone_expiry, "cron", hour=12, minute=0,
            id="timezone_expiry_check",
        )

        # Working session inactivity check — every 1 minute
        self.scheduler.add_job(
            self.job_override_expiry_check, "interval", minutes=1,
            id="override_expiry_check",
        )

        # Action item reminders — every 30 minutes
        self.scheduler.add_job(
            self.job_action_item_reminders, "interval", minutes=30,
            id="action_item_reminders",
        )

        # Follow-up radar — weekdays at 8:00 AM (same TZ as other morning jobs)
        self.scheduler.add_job(
            self.job_follow_up_radar, "cron", hour=8, minute=0,
            day_of_week="mon-fri", id="follow_up_radar",
        )

        # Load playbooks at startup
        load_playbooks()

        self.scheduler.start()
        logger.info("Scheduler started")

    def stop(self):
        self.scheduler.shutdown()

    def _is_quiet(self) -> bool:
        """Check if quiet hours are active. Used as a guard at the top of scheduled jobs."""
        try:
            from artemis.quiet_hours import is_quiet_hours
            return is_quiet_hours()
        except Exception:
            return False

    def _poll_gmail(self, max_results: int = 20) -> list[dict]:
        """Poll Gmail inline using the already-authenticated GmailClient."""
        if not self.gmail or not self.gmail.service:
            logger.warning("Gmail not authenticated — skipping poll")
            return []
        try:
            self.gmail._refresh_if_needed()
            return self.gmail.get_recent_messages(max_results=max_results)
        except Exception:
            logger.exception("Gmail poll failed")
            return []

    def job_inbox_triage(self):
        """Poll Gmail, classify new messages, archive, and execute playbooks."""
        if self._is_quiet():
            return
        try:
            messages = self._poll_gmail(max_results=20)
            if messages:
                self._record_gmail_success()
            new_messages = [
                m for m in messages if m["id"] not in self._seen_message_ids
            ]
            if not new_messages:
                return

            for m in new_messages:
                self._seen_message_ids.add(m["id"])

            # Immediate post for priority contacts
            priority_msgs = [
                m for m in new_messages if self.gmail.is_priority_sender(m["from_email"])
            ]
            non_priority = [
                m for m in new_messages if not self.gmail.is_priority_sender(m["from_email"])
            ]

            for msg in priority_msgs:
                # Track in inbox zero — safety: only archive if upsert succeeds
                try:
                    upsert_thread(
                        msg["thread_id"], msg["subject"], msg["from_email"],
                        state=NEEDS_ACTION,
                    )
                    # Fetch full body for priority contacts
                    body = self.gmail.get_full_message(msg["id"])
                    if body:
                        msg["full_body"] = body
                    post = self.mm.post_message(
                        config.CHANNEL_OPS,
                        f"\U0001f4ec **Priority email** from {msg['from']}\n"
                        f"Subject: {msg['subject']}\n"
                        f"> {msg['snippet'][:200]}\n\n"
                        f"Reply: `done {msg['thread_id'][:12]}` · `wait {msg['thread_id'][:12]}` · "
                        f"`snooze {msg['thread_id'][:12]} 3d` · `noise {msg['thread_id'][:12]}`",
                    )
                    if post.get("id"):
                        set_mattermost_post_id(msg["thread_id"], post["id"])
                    # Archive after successful tracking
                    self.gmail.archive_message(msg["id"])
                    logger.info("Archived [%s] from %s", msg["subject"], msg["from_email"])
                except Exception:
                    logger.exception(
                        "Failed to track priority email — NOT archiving %s", msg["id"]
                    )
                    self.mm.post_message(
                        config.CHANNEL_OPS,
                        f"\u26a0\ufe0f Failed to track priority email from {msg['from']} — left in inbox",
                    )

            if non_priority:
                # Pre-fetch full bodies (capped) so triage sees real content
                full_body_fetches = 0
                _MAX_FULL_FETCHES = 5
                for msg in non_priority:
                    if full_body_fetches >= _MAX_FULL_FETCHES:
                        break
                    body = self.gmail.get_full_message(msg["id"])
                    if body:
                        msg["full_body"] = body
                        full_body_fetches += 1

                email_text = self.gmail.format_for_claude(non_priority)
                triaged = triage_emails(email_text, playbook_text=get_playbook_text())

                for i, item in enumerate(triaged):
                    urgency = item.get("urgency", "low")
                    sender_type = item.get("sender_type", "")
                    playbook_match = item.get("playbook_match")
                    orig = non_priority[i] if i < len(non_priority) else None

                    if urgency in ("high", "medium") and orig:
                        upsert_thread(
                            orig["thread_id"], orig["subject"], orig["from_email"],
                            state=NEEDS_ACTION,
                        )
                    elif sender_type == "noise" and orig:
                        upsert_thread(
                            orig["thread_id"], orig["subject"], orig["from_email"],
                            state=NOISE,
                        )
                    elif orig:
                        upsert_thread(
                            orig["thread_id"], orig["subject"], orig["from_email"],
                            state=NEEDS_ACTION,
                        )

                    if urgency == "high":
                        self.mm.post_message(
                            config.CHANNEL_OPS,
                            f"\U0001f4ec **High urgency email**: {item.get('one_line_summary', 'New email')}",
                        )
                    else:
                        self._pending_triage.append(item)

                    # Fetch full body for playbook matches or known CRM contacts
                    # Playbook matches ALWAYS get full body (not subject to cap)
                    if orig and playbook_match and "full_body" not in orig:
                        body = self.gmail.get_full_message(orig["id"])
                        if body:
                            orig["full_body"] = body
                            logger.info(
                                "Fetched full body for playbook %s [%s] (%d chars)",
                                playbook_match, orig.get("subject", ""), len(body),
                            )
                        else:
                            logger.warning(
                                "Full body fetch FAILED for playbook %s [%s] — playbook will use snippet (%d chars)",
                                playbook_match, orig.get("subject", ""), len(orig.get("snippet", "")),
                            )
                    elif orig and full_body_fetches < _MAX_FULL_FETCHES:
                        needs_full = bool(get_contact(orig.get("from_email", "")))
                        if needs_full:
                            body = self.gmail.get_full_message(orig["id"])
                            if body:
                                orig["full_body"] = body
                                full_body_fetches += 1
                                logger.info(
                                    "Fetched full body for [%s] (%d chars)",
                                    orig.get("subject", ""), len(body),
                                )

                    # Execute playbook if matched
                    if playbook_match and orig:
                        body_source = "full_body" if "full_body" in orig else "snippet"
                        body_len = len(orig.get("full_body", orig.get("snippet", "")))
                        logger.info(
                            "Executing %s for [%s] with %s (%d chars)",
                            playbook_match, orig.get("subject", ""), body_source, body_len,
                        )
                        self._execute_playbook(playbook_match, orig, item)

                    # Scheduling request detection (Learning mode — approval required)
                    if orig:
                        email_text = orig.get("full_body") or orig.get("snippet", "")
                        if email_text:
                            self._check_scheduling_request(orig, email_text)

                    # Archive every processed email
                    if orig:
                        self.gmail.archive_message(orig["id"])
                        logger.info("Archived [%s] from %s", orig.get("subject", ""), orig.get("from_email", ""))

            # Record successful triage timestamp for catch-up on restart
            try:
                from artemis.quiet_hours import set_system_value
                set_system_value("last_run_at", datetime.utcnow().isoformat())
            except Exception:
                pass

        except Exception as exc:
            self._record_gmail_failure(str(exc))
            logger.exception("Inbox triage failed")

    def _check_scheduling_request(self, msg: dict, email_text: str):
        """Detect scheduling requests and post approval to Mattermost (Learning mode)."""
        try:
            result = detect_scheduling_request(email_text, msg.get("from_email", ""))
            if not result:
                return

            duration = result["suggested_duration_minutes"]
            date_constraint = result.get("date_constraint")
            buffer_minutes = result.get("buffer_minutes", 0)
            sender_email = result["sender"]
            sender_name = msg.get("from", sender_email).split("<")[0].strip().strip('"')

            # Find free blocks
            free_blocks = self.calendar.find_free_blocks(
                duration_minutes=duration,
                days_ahead=5,
                max_results=3,
                date_constraint=date_constraint,
                buffer_minutes=buffer_minutes,
            )
            if not free_blocks:
                logger.info("Scheduling request from %s but no free blocks found", sender_email)
                return

            # Draft response
            draft = draft_scheduling_response(
                sender_name=sender_name,
                sender_email=sender_email,
                duration_minutes=duration,
                free_blocks=free_blocks,
                original_subject=msg.get("subject", "Meeting"),
            )

            # Serialize free_blocks for JSON storage (datetimes aren't serializable)
            blocks_for_storage = [
                {"date_label": b["date_label"], "time_label": b["time_label"],
                 "start": b["start"].isoformat(), "end": b["end"].isoformat()}
                for b in free_blocks
            ]

            # Persist action item
            from knowledge.db import execute_write
            is_priority = self.gmail.is_priority_sender(sender_email)
            action_item = execute_write(
                """INSERT INTO acos.action_items
                   (item_type, status, priority, title, description, metadata, due_at)
                   VALUES (%s, %s, %s, %s, %s, %s, now() + interval '24 hours')
                   RETURNING id""",
                (
                    "scheduling_request",
                    "pending",
                    "high" if is_priority else "normal",
                    f"Schedule {duration}min with {sender_name}",
                    result.get("raw_request", "")[:500],
                    json.dumps({
                        **draft,
                        "free_blocks": blocks_for_storage,
                        "duration_minutes": duration,
                        "thread_id": msg.get("thread_id", ""),
                        "confidence": result["confidence"],
                    }),
                ),
            )
            item_id = str(action_item["id"])[:8] if action_item else "?"

            # Post approval request to Mattermost
            slots_preview = " | ".join(
                f"{b['date_label']} {b['time_label']}" for b in free_blocks
            )
            self.mm.post_message(
                config.CHANNEL_OPS,
                f"\U0001f4c5 **Scheduling request** from {sender_name} ({sender_email})\n"
                f"Duration: {duration} min | Confidence: {result['confidence']:.0%}\n"
                f"Request: \"{result['raw_request'][:150]}\"\n\n"
                f"**Proposed slots:** {slots_preview}\n\n"
                f"**Draft reply:**\n> {draft['body'][:500]}\n\n"
                f"\u2705 `approve sched {item_id}` · "
                f"\u274c `skip sched {item_id}` · "
                f"\U0001f4a4 `snooze sched {item_id}`",
            )
            logger.info(
                "Scheduling request detected from %s (%dmin, confidence=%.2f, action_item=%s)",
                sender_email, duration, result["confidence"], item_id,
            )
        except Exception:
            logger.debug("Scheduling detection error for %s", msg.get("from_email", ""), exc_info=True)

    def job_post_triage_batch(self):
        """Post batched triage summary."""
        if self._is_quiet():
            return
        if not self._pending_triage:
            return

        try:
            lines = ["\U0001f4ec **Inbox triage summary:**\n"]
            for item in self._pending_triage:
                urgency = item.get("urgency", "?")
                sender_type = item.get("sender_type", "?")
                summary = item.get("one_line_summary", "")
                action = " (action needed)" if item.get("needs_action") else ""
                lines.append(f"- [{urgency}/{sender_type}] {summary}{action}")

            self.mm.post_message(config.CHANNEL_OPS, "\n".join(lines))
            self._pending_triage.clear()
        except Exception:
            logger.exception("Triage batch post failed")

    def job_pre_meeting_briefs(self):
        """Generate briefs for upcoming meetings with external attendees."""
        if self._is_quiet():
            return
        try:
            # Refresh calendar cache on every cycle
            from artemis import calendar_cache
            calendar_cache.refresh(self.calendar)

            # Read from cache instead of live API
            events = calendar_cache.get_upcoming_with_externals(
                within_minutes=config.BRIEF_LEAD_TIME_MINUTES
            )
            self._record_calendar_success()
            for event in events:
                external = event.get("external_attendees", [])
                attendee_emails = [a["email"] for a in external]
                attendee_names = [a["name"] or a["email"] for a in external]

                # Gather email threads with each attendee
                email_parts = []
                for email in attendee_emails:
                    threads = self.gmail.get_threads_with_address(email, max_threads=5)
                    for t in threads:
                        msgs = t.get("messages", [])
                        for m in msgs:
                            email_parts.append(
                                f"From: {m['from']}\nSubject: {m['subject']}\n"
                                f"Date: {m['date']}\nPreview: {m['snippet']}"
                            )
                email_context = UNTRUSTED_PREFIX + "\n---\n".join(email_parts) if email_parts else "No recent email threads found."

                # Gather commitments
                commitment_lines = []
                for a in external:
                    name = a["name"] or a["email"].split("@")[0]
                    company = a["email"].split("@")[1] if "@" in a["email"] else ""
                    for search in [name, company]:
                        if search:
                            for c in get_commitments_for_client(search):
                                commitment_lines.append(
                                    f"- {c['title']} (due {c['due_date']}, status: {c['status']})"
                                )
                commitment_context = "\n".join(commitment_lines) if commitment_lines else "No open commitments."

                brief = generate_meeting_brief(
                    event["summary"],
                    event["start"],
                    attendee_names,
                    email_context,
                    commitment_context,
                )

                if brief:
                    header = f"### Brief: {event['summary']} — {event['start']}\n**Attendees**: {', '.join(attendee_names)}\n\n"
                    self.mm.post_message(config.CHANNEL_BRIEFS, header + brief)

        except Exception as exc:
            self._record_calendar_failure(str(exc))
            logger.exception("Pre-meeting brief generation failed")

    def job_morning_brief(self):
        """Generate and post the daily morning brief."""
        try:
            # Refresh calendar cache before building the brief
            from artemis import calendar_cache
            if self.calendar and self.calendar.service:
                calendar_cache.refresh(self.calendar)

            # Today + next 7 days
            start = date.today() + timedelta(days=1)
            end = date.today() + timedelta(days=7)
            upcoming_events = calendar_cache.get_events_in_range(start, end)

            # Filter to external events for brief
            events_with_external = [
                e for e in upcoming_events
                if any(not a.get("self") for a in e.get("attendees", []))
            ]
            for e in events_with_external:
                e["external_attendees"] = [a for a in e["attendees"] if not a.get("self")]

            meetings_text = self.calendar.format_events_for_brief(events_with_external)
            if not meetings_text:
                # Include all events (solo) in the brief window
                lines = []
                for e in upcoming_events:
                    lines.append(f"- {e['summary']} at {e['start'][:16]} (solo)")
                meetings_text = "\n".join(lines) if lines else "No meetings in the next 7 days."

            # Commitments due soon — try CRM API first, fall back to SQLite
            commitment_lines = []
            if self.crm.is_available():
                try:
                    open_commitments = self.crm.get_commitments(status="open")
                    today = date.today()
                    for c in open_commitments:
                        due_str = c.get("due_date", "")
                        if not due_str:
                            continue
                        try:
                            due = date.fromisoformat(due_str[:10])
                        except ValueError:
                            continue
                        days_left = (due - today).days
                        desc = c.get("description", c.get("title", ""))
                        client = c.get("client", c.get("contact_name", "n/a"))
                        if days_left <= 3:
                            commitment_lines.append(f"- **{desc}** due {due_str[:10]} (client: {client})")
                except Exception:
                    logger.warning("CRM API failed for morning brief commitments — falling back to SQLite")
                    commitment_lines = []

            if not commitment_lines:
                due_soon = get_due_soon(days=3)
                start_alerts = get_start_alerts()
                for c in due_soon:
                    commitment_lines.append(f"- **{c['title']}** due {c['due_date']} (client: {c['client'] or 'n/a'})")
                for c in start_alerts:
                    if c["id"] not in {d["id"] for d in due_soon}:
                        commitment_lines.append(
                            f"- **{c['title']}** due {c['due_date']} — needs {c['effort_days']}d effort, start now!"
                        )
            commitments_text = "\n".join(commitment_lines) if commitment_lines else "No commitments due soon."

            # Top inbox items
            messages = self.gmail.get_recent_messages(max_results=10)
            email_text = self.gmail.format_for_claude(messages[:5]) if messages else "No recent emails."

            # Monitor alerts
            ssl_results = check_all_ssl()
            domain_results = check_domain_expiry()
            monitor_lines = []
            ssl_alert = format_ssl_alerts(ssl_results)
            domain_alert = format_domain_alerts(domain_results)
            if ssl_alert:
                monitor_lines.append(ssl_alert)
            if domain_alert:
                monitor_lines.append(domain_alert)
            monitor_text = "\n".join(monitor_lines) if monitor_lines else "All monitors green."

            # Inbox zero section
            inbox_section = format_morning_inbox_section()

            brief = generate_morning_brief(
                meetings_text, commitments_text, email_text, monitor_text
            )

            if brief:
                full_brief = f"\u2600\ufe0f **Good morning! Here's your brief:**\n\n{brief}\n\n\U0001f4ec **Inbox Zero:**\n{inbox_section}"
                self.mm.post_message(config.CHANNEL_OPS, full_brief)

        except Exception:
            logger.exception("Morning brief generation failed")

    def job_ssl_check(self):
        """Check SSL certs and alert if expiring."""
        if self._is_quiet():
            return
        try:
            results = check_all_ssl()
            alert = format_ssl_alerts(results)
            if alert:
                self.mm.post_message(config.CHANNEL_OPS, f"\u26a0\ufe0f **SSL Certificate Alerts:**\n{alert}")
        except Exception:
            logger.exception("SSL check failed")

    def job_domain_check(self):
        """Check domain expiry and alert."""
        if self._is_quiet():
            return
        try:
            results = check_domain_expiry()
            alert = format_domain_alerts(results)
            if alert:
                self.mm.post_message(config.CHANNEL_OPS, f"\u26a0\ufe0f **Domain Expiry Alerts:**\n{alert}")
        except Exception:
            logger.exception("Domain check failed")

    def job_inbox_zero_audit(self):
        """Audit inbox threads — nudge stale items, resurface snoozed, detect replies."""
        if self._is_quiet():
            return
        try:
            # 1. NEEDS_ACTION older than 24h → nudge
            stale_na = get_stale_needs_action(hours=24)
            for t in stale_na:
                if can_nudge(t["id"], min_hours=12):
                    self.mm.post_message(
                        config.CHANNEL_OPS,
                        f"\U0001f4ec **Nudge:** This thread still needs action:\n"
                        f"**{t['subject']}** from {t['sender']}\n\n"
                        f"Reply: `done {t['id'][:12]}` · `wait {t['id'][:12]}` · "
                        f"`snooze {t['id'][:12]} 3d` · `noise {t['id'][:12]}`",
                    )
                    record_nudge(t["id"])

            # 2. WAITING threads — check for replies, then nudge if stale
            stale_w = get_stale_waiting(days=3)
            for t in stale_w:
                # Check if they replied
                if t.get("waiting_since") and self.gmail.check_for_reply(
                    t["id"], t["waiting_since"]
                ):
                    mark_needs_action(t["id"])
                    self.mm.post_message(
                        config.CHANNEL_OPS,
                        f"\U0001f4ec **Reply received** on: **{t['subject']}** \u2014 moved back to NEEDS_ACTION\n\n"
                        f"Reply: `done {t['id'][:12]}` · `wait {t['id'][:12]}` · "
                        f"`snooze {t['id'][:12]} 3d`",
                    )
                elif can_nudge(t["id"], min_hours=72):
                    who = t.get("waiting_on") or "them"
                    snippet = self.gmail.get_my_last_message_snippet(t["id"])
                    context = f' re: "{snippet}"' if snippet else ""
                    self.mm.post_message(
                        config.CHANNEL_OPS,
                        f"\U0001f4ec **Still waiting on {who}{context}** \u2014 no reply in 3+ days\n"
                        f"Thread: **{t['subject']}**\n\n"
                        f"Reply: `done {t['id'][:12]}` · `snooze {t['id'][:12]} 3d`",
                    )
                    record_nudge(t["id"])

            # 3. SNOOZED threads past their date → resurface
            snoozed_due = get_snoozed_due()
            for t in snoozed_due:
                mark_needs_action(t["id"])
                self.mm.post_message(
                    config.CHANNEL_OPS,
                    f"\U0001f4ec **Resurfaced (snooze ended):**\n"
                    f"**{t['subject']}** from {t['sender']}\n\n"
                    f"Reply: `done {t['id'][:12]}` · `wait {t['id'][:12]}` · "
                    f"`snooze {t['id'][:12]} 3d` · `noise {t['id'][:12]}`",
                )

        except Exception:
            logger.exception("Inbox zero audit failed")

    def job_inbox_zero_morning(self):
        """Pre-compute inbox zero stats before morning brief (stats are pulled inline)."""
        if self._is_quiet():
            return
        # This is a no-op hook — the actual data is pulled by format_morning_inbox_section()
        # during job_morning_brief. This job exists as a named anchor in case
        # we want to do pre-brief inbox processing later.
        logger.debug("Inbox zero morning pre-check complete")

    def job_focus_reminder(self):
        """Post daily focus reminder for the configured focus client."""
        if self._is_quiet():
            return
        try:
            keywords = config.FOCUS_KEYWORDS or [config.FOCUS_CLIENT]
            commitments = []
            for kw in keywords:
                for c in get_commitments_for_client(kw):
                    if c["id"] not in {x["id"] for x in commitments}:
                        commitments.append(c)

            if commitments:
                commitment_text = "\n".join(
                    f"- **{c['title']}** (due {c['due_date']})" for c in commitments
                )
            else:
                commitment_text = "No specific commitments on file — check in with the team."

            self.mm.post_message(
                config.CHANNEL_OPS,
                f"\U0001f3af Titanium focus check: {commitment_text}\n\n"
                f"Everything else is secondary.",
            )
        except Exception:
            logger.exception("Focus reminder failed")

    def job_update_check(self):
        """Check GitHub for new commits and post if an update is available."""
        if self._is_quiet():
            return
        try:
            from artemis.version import get_commit_hash, get_latest_github_version

            local_hash = get_commit_hash()
            latest_hash, latest_date = get_latest_github_version()

            if not latest_hash or not local_hash:
                return  # can't check — skip silently

            if latest_hash.startswith(local_hash):
                return  # up to date — stay silent

            self.mm.post_message(
                config.CHANNEL_OPS,
                f"\U0001f504 Artemis update available \u2014 latest commit: {latest_hash} ({latest_date}).\n"
                f"Run `git pull && pip install -r requirements.txt && restart` to update.",
            )
        except Exception:
            # GitHub unreachable — skip silently per spec
            logger.debug("Update check failed — skipping")

    def _execute_playbook(self, playbook_id: str, msg: dict, triage_item: dict):
        """Execute a matched playbook's actions for a triaged email."""
        try:
            logger.info("Executing playbook %s for [%s]", playbook_id, msg.get("subject", ""))

            if playbook_id == "PB-001":
                self._run_pb001_demo_lead(msg, triage_item)
            elif playbook_id == "PB-002":
                self._run_pb002_meeting_followup(msg, triage_item)
            elif playbook_id == "PB-003":
                self._run_pb003_survey(msg, triage_item)
            elif playbook_id == "PB-004":
                self._run_pb004_meeting_request(msg, triage_item)
            elif playbook_id == "PB-006":
                self._run_pb006_availability(msg, triage_item)
            elif playbook_id == "PB-007":
                # PB-007 runs on its own schedule via label scanning, not triage
                logger.debug("PB-007 matched in triage — handled by billing_intake job")
            else:
                logger.warning("Unknown playbook ID: %s", playbook_id)
                return

            logger.info("Playbook %s completed for [%s]", playbook_id, msg.get("subject", ""))

        except Exception:
            logger.exception("Playbook %s failed for [%s]", playbook_id, msg.get("subject", ""))
            self.mm.post_message(
                config.CHANNEL_OPS,
                f"\u26a0\ufe0f Playbook {playbook_id} failed on [{msg.get('subject', '?')}]: "
                f"check logs for details",
            )

    def _run_pb001_demo_lead(self, msg: dict, triage_item: dict):
        """PB-001: Demo Access Notification (legacy triage path).

        Delegates to demo_intake.process_demo_message for full CRM Write Guard
        processing.  The primary path is now job_demo_intake (interval scan).
        """
        message_id = msg.get("id")
        if not message_id:
            logger.warning("PB-001 triage: no message ID — skipping")
            return

        result = process_demo_message(
            self.gmail, message_id, mm_client=self.mm,
        )
        if not result.get("success"):
            logger.warning(
                "PB-001 triage: processing failed — %s", result.get("error", "unknown")
            )

    def _run_pb002_meeting_followup(self, msg: dict, triage_item: dict):
        """PB-002: Meeting Follow-up — create commitments for action items."""
        sender_email = msg.get("from_email", "")
        sender_name = msg.get("from", "").split("<")[0].strip().strip('"') or sender_email
        company = sender_email.split("@")[1] if "@" in sender_email else ""
        summary = triage_item.get("one_line_summary", msg.get("subject", ""))

        # Default due date: 5 days from now
        due = (date.today() + timedelta(days=5)).isoformat()

        followup_title = f"Follow up: {summary[:80]}"
        deliver_title = f"Send deliverables to {sender_name}"

        if self.crm.is_available():
            try:
                contact = self.crm.find_contact_by_email(sender_email)
                contact_id = contact.get("id") if contact else None
                self.crm.create_commitment({
                    "description": followup_title,
                    "due_date": due,
                    "contact_id": contact_id,
                    "status": "open",
                })
                self.crm.create_commitment({
                    "description": deliver_title,
                    "due_date": due,
                    "contact_id": contact_id,
                    "status": "open",
                })
                logger.info("PB-002: Created CRM commitments for %s", sender_name)
            except Exception:
                logger.warning("CRM commitment creation failed — falling back to SQLite")
                add_commitment(title=followup_title, due_date=due, effort_days=2, client=company)
                add_commitment(title=deliver_title, due_date=due, effort_days=1, client=company)
        else:
            add_commitment(title=followup_title, due_date=due, effort_days=2, client=company)
            add_commitment(title=deliver_title, due_date=due, effort_days=1, client=company)

        self.mm.post_message(
            config.CHANNEL_COMMITMENTS,
            f"\U0001f4cb Meeting follow-up from {sender_name}:\n"
            f"- Follow up: {summary[:80]} (due {due})\n"
            f"- Send deliverables to {sender_name} (due {due})",
        )
        self.mm.post_message(
            config.CHANNEL_OPS,
            f"\U0001f4cb {sender_name} follow-up processed \u2014 "
            f"2 commitments created, due {due}",
        )

    def _run_pb003_survey(self, msg: dict, triage_item: dict):
        """PB-003: Survey/Feedback Request — mark NEEDS_ACTION, batch for brief."""
        due = (date.today() + timedelta(days=2)).isoformat()
        upsert_thread(
            msg["thread_id"], msg["subject"], msg.get("from_email", ""),
            state=NEEDS_ACTION, due_date=due, notes="Quick task \u2014 estimated 2-5 minutes",
        )
        # Only post to ops if sender is priority contact
        if self.gmail.is_priority_sender(msg.get("from_email", "")):
            self.mm.post_message(
                config.CHANNEL_OPS,
                f"\U0001f4dd Survey/feedback request from {msg.get('from', 'unknown')} \u2014 due {due}",
            )
        # Otherwise batched into morning brief automatically

    def _run_pb004_meeting_request(self, msg: dict, triage_item: dict):
        """PB-004: Meeting Request / Calendar Invite — post to ops."""
        upsert_thread(
            msg["thread_id"], msg["subject"], msg.get("from_email", ""),
            state=NEEDS_ACTION,
        )
        self.mm.post_message(
            config.CHANNEL_OPS,
            f"\U0001f4c5 Meeting request from {msg.get('from', 'unknown')} \u2014 needs response\n"
            f"Subject: {msg.get('subject', '')}",
        )

    def _run_pb006_availability(self, msg: dict, triage_item: dict):
        """PB-006: Availability Request — analyze calendar and post slots to ops."""
        from artemis.availability import (
            format_slots_mattermost,
            get_availability,
            parse_timeframe,
        )
        from artemis.briefs import _call_claude
        from artemis.prompts import AVAILABILITY_EXTRACT_SYSTEM, AVAILABILITY_EXTRACT_USER

        sender_email = msg.get("from_email", "")
        sender_name = msg.get("from", "").split("<")[0].strip().strip('"') or sender_email
        subject = msg.get("subject", "")
        body = msg.get("full_body", msg.get("snippet", ""))

        # Extract timeframe from email using Claude
        today_str = date.today().isoformat()
        system = AVAILABILITY_EXTRACT_SYSTEM.replace("{today}", today_str)
        user_prompt = AVAILABILITY_EXTRACT_USER.format(email_text=UNTRUSTED_PREFIX + body[:3000])

        try:
            result = _call_claude(system, user_prompt)
            import json as _json
            cleaned = result.strip()
            cleaned = re.sub(r'^```json\s*', '', cleaned)
            cleaned = re.sub(r'^```\s*', '', cleaned)
            cleaned = re.sub(r'\s*```$', '', cleaned)
            extracted = _json.loads(cleaned.strip())
            start_date = extracted.get("start_date")
            end_date = extracted.get("end_date")
            duration = extracted.get("duration_minutes") or config.DEFAULT_SLOT_DURATION

            if start_date:
                start_date = date.fromisoformat(start_date)
            if end_date:
                end_date = date.fromisoformat(end_date)
        except Exception:
            logger.warning("Failed to extract timeframe via Claude — using defaults")
            start_date = None
            end_date = None
            duration = config.DEFAULT_SLOT_DURATION

        # Fallback to default timeframe
        if not start_date or not end_date:
            start_date, end_date = parse_timeframe(body)

        # Find availability — PB-006 is always MEETING mode (external request)
        slots = get_availability(
            self.calendar,
            start_date,
            end_date,
            slot_duration=int(duration),
            mode="meeting",
        )

        # Format and post to ops
        original_quote = msg.get("snippet", "")[:200]
        formatted = format_slots_mattermost(
            slots,
            sender_name=sender_name,
            sender_email=sender_email,
            subject=subject,
            original_quote=original_quote,
            booking_link=config.BOOKING_LINK,
        )

        post_result = self.mm.post_message(config.CHANNEL_OPS, formatted)

        # Track in inbox
        upsert_thread(
            msg["thread_id"], subject, sender_email,
            state=NEEDS_ACTION,
        )

        # Store pending availability for reply flow in main.py
        # Keyed by the ops channel so the user can reply in that channel
        try:
            from artemis.main import _pending_availability
            # Use CHANNEL_OPS as key since that's where the user will reply
            ops_channel = config.CHANNEL_OPS
            _pending_availability[ops_channel] = {
                "sender_name": sender_name,
                "sender_email": sender_email,
                "subject": subject,
                "thread_id": msg["thread_id"],
                "message_id": msg.get("id", ""),
                "slots": slots,
                "snippet": original_quote,
                "created_at": time.time(),
                "phase": "slot_selection",
            }
        except ImportError:
            logger.warning("Could not import _pending_availability from main")

        logger.info(
            "PB-006: Posted %d availability slots for %s (%s)",
            len(slots), sender_name, subject,
        )

    def job_commitment_reminders(self):
        """PB-005: Commitment Deadline Reminder Chain."""
        if self._is_quiet():
            return
        try:
            # Try CRM API first, fall back to SQLite
            active = None
            if self.crm.is_available():
                try:
                    crm_commitments = self.crm.get_commitments(status="open")
                    # Normalize CRM fields to match SQLite shape
                    active = []
                    for c in crm_commitments:
                        active.append({
                            "id": c.get("id", ""),
                            "title": c.get("description", c.get("title", "")),
                            "due_date": (c.get("due_date", "") or "")[:10],
                            "effort_days": c.get("effort_days", 1),
                            "client": c.get("client", c.get("contact_name", "")),
                            "status": c.get("status", "active"),
                        })
                except Exception:
                    logger.warning("CRM API failed for commitment reminders — falling back to SQLite")
                    active = None

            if active is None:
                active = list_commitments(status="active")
            today = date.today()

            for c in active:
                try:
                    due = date.fromisoformat(c["due_date"])
                except (ValueError, TypeError):
                    continue

                days_left = (due - today).days
                effort = c.get("effort_days", 1)

                if days_left == 0:
                    self.mm.post_message(
                        config.CHANNEL_OPS,
                        f"\U0001f6a8 **TODAY**: {c['title']} is due today! (client: {c.get('client', 'n/a')})",
                    )
                elif days_left == 1:
                    self.mm.post_message(
                        config.CHANNEL_COMMITMENTS,
                        f"\U0001f534 **Due tomorrow**: {c['title']} (client: {c.get('client', 'n/a')})",
                    )
                elif days_left == effort:
                    self.mm.post_message(
                        config.CHANNEL_COMMITMENTS,
                        f"\u26a0\ufe0f **Start today**: {c['title']} \u2014 needs {effort}d effort, "
                        f"due {c['due_date']} (client: {c.get('client', 'n/a')})",
                    )
                elif days_left == 5:
                    self.mm.post_message(
                        config.CHANNEL_COMMITMENTS,
                        f"\U0001f4c5 **5 days out**: {c['title']} due {c['due_date']} "
                        f"(client: {c.get('client', 'n/a')})",
                    )

        except Exception:
            logger.exception("Commitment reminder chain failed")

    def job_demo_intake(self):
        """PB-001 v2: Scan for Lucint demo notification emails and process them."""
        if self._is_quiet():
            return

        try:
            message_ids = get_demo_messages(self.gmail)
            if not message_ids:
                return

            logger.info("PB-001: Found %d unprocessed demo email(s)", len(message_ids))
            for msg_id in message_ids:
                try:
                    result = process_demo_message(
                        self.gmail, msg_id, mm_client=self.mm
                    )
                    if result.get("success"):
                        logger.info(
                            "PB-001: Processed demo lead — %s (%s)",
                            result.get("name", "?"), result.get("company", "?"),
                        )
                    else:
                        logger.error(
                            "PB-001: Failed to process %s — %s",
                            msg_id, result.get("error", "unknown"),
                        )
                except Exception:
                    logger.exception("PB-001: Error processing demo message %s", msg_id)
                    try:
                        self.mm.post_message(
                            config.CHANNEL_OPS,
                            f"\u26a0\ufe0f PB-001 demo intake failed on message "
                            f"{msg_id[:12]}\u2026 — check logs. Lead NOT processed.",
                        )
                    except Exception:
                        pass
        except Exception:
            logger.exception("PB-001 demo intake job failed")

    _billing_label_checked: bool = False

    def job_billing_intake(self):
        """PB-007: Scan for billing-labeled emails and process them."""
        if self._is_quiet():
            return

        # One-time label creation check per process lifetime
        if not self._billing_label_checked:
            label_id = ensure_billing_label(self.gmail)
            if label_id:
                # Check if we just created it (no messages would exist yet)
                # by comparing to cached state — only announce once
                if not getattr(self, "_billing_label_announced", False):
                    try:
                        results = self.gmail.service.users().messages().list(
                            userId="me", labelIds=[label_id], maxResults=1
                        ).execute()
                        if not results.get("messages"):
                            self.mm.post_message(
                                config.CHANNEL_OPS,
                                "\U0001f4c1 Created Gmail label **artemis/billing** — "
                                "tag expense emails with this label for automatic intake",
                            )
                    except Exception:
                        pass
                    self._billing_label_announced = True
                self._billing_label_checked = True
            else:
                logger.warning("PB-007: Could not ensure artemis/billing label")

        try:
            message_ids = get_billing_messages(self.gmail)
            if not message_ids:
                return

            logger.info("PB-007: Found %d unprocessed billing email(s)", len(message_ids))
            for msg_id in message_ids:
                try:
                    result = process_billing_message(
                        self.gmail, msg_id, mm_client=self.mm
                    )
                    if result.get("success"):
                        logger.info(
                            "PB-007: Processed billing email — %s (%s)",
                            result.get("subject", "?"), result.get("amount", "no amount"),
                        )
                    else:
                        logger.error(
                            "PB-007: Failed to process %s — %s",
                            msg_id, result.get("error", "unknown"),
                        )
                except Exception:
                    logger.exception("PB-007: Error processing billing message %s", msg_id)
                    # Post failure alert — never silently drop an expense
                    try:
                        self.mm.post_message(
                            config.CHANNEL_OPS,
                            f"\u26a0\ufe0f PB-007 billing intake failed on message {msg_id[:12]}… "
                            f"— check logs. Email NOT processed.",
                        )
                    except Exception:
                        pass
        except Exception:
            logger.exception("PB-007 billing intake job failed")

    def job_quiet_hours_start(self):
        """Enter quiet hours and announce."""
        try:
            from artemis.quiet_hours import enter_quiet, get_quiet_state

            # Don't override a manual goodnight that's already active
            state = get_quiet_state()
            if state.get("manual_override") and state.get("is_quiet"):
                return  # Already quiet via manual goodnight

            announcement = enter_quiet(manual=False)
            self.mm.post_message(config.CHANNEL_OPS, announcement)
        except Exception:
            logger.exception("Quiet hours start failed")

    def job_quiet_hours_end(self):
        """Exit quiet hours and post overnight summary."""
        try:
            from artemis.quiet_hours import exit_quiet, get_quiet_state

            # Don't auto-wake if user has a custom wake time set
            state = get_quiet_state()
            wake = state.get("wake_time")
            if wake:
                # Check if we've reached the custom wake time
                from artemis.quiet_hours import get_active_timezone
                tz_name = get_active_timezone()
                try:
                    tz = ZoneInfo(tz_name)
                except (KeyError, ValueError):
                    tz = ZoneInfo(config.HOME_TIMEZONE)
                now_local = datetime.now(tz).time()
                from datetime import time as _time
                parts = wake.split(":")
                wake_time = _time(int(parts[0]), int(parts[1]))
                if now_local < wake_time:
                    return  # Not yet time to wake

            exit_quiet()
            summary = self._build_overnight_summary()
            self.mm.post_message(config.CHANNEL_OPS, summary)
        except Exception:
            logger.exception("Quiet hours end failed")

    def _build_overnight_summary(self) -> str:
        """Build the overnight summary message for quiet hours exit or good morning."""
        from artemis.inbox import get_stale_needs_action

        lines = ["\u2600\ufe0f Good morning! Here's what came in overnight:"]

        # Overnight emails
        email_count = 0
        try:
            messages = self._poll_gmail(max_results=50)
            new_messages = [m for m in messages if m["id"] not in self._seen_message_ids]
            email_count = len(new_messages)
        except Exception:
            logger.debug("Failed to count overnight emails")

        inbox_items = get_stale_needs_action(hours=0)
        inbox_count = len(inbox_items)
        lines.append(f"\U0001f4ec {email_count} new emails \u2014 {inbox_count} need action")

        # Today's meetings
        try:
            events = self.calendar.get_today_events()
            if events:
                meeting_parts = []
                for e in events:
                    start_str = e.get("start", "")
                    if "T" in start_str:
                        try:
                            t = datetime.fromisoformat(start_str)
                            display = t.strftime("%I:%M %p").lstrip("0")
                        except ValueError:
                            display = start_str
                    else:
                        display = "all day"
                    meeting_parts.append(f"{e['summary']} ({display})")
                lines.append(f"\U0001f4c5 Today: {', '.join(meeting_parts)}")
            else:
                lines.append("\U0001f4c5 No meetings today")
        except Exception:
            logger.debug("Failed to fetch today's meetings for summary")

        # Commitments due today
        try:
            due = get_due_soon(days=0)
            if due:
                for c in due:
                    lines.append(f"\u2705 Due today: {c['title']} ({c.get('client', 'n/a')})")
        except Exception:
            logger.debug("Failed to fetch commitments for summary")

        # Urgent items (high-urgency inbox items)
        if inbox_count > 5:
            lines.append(f"\u26a0\ufe0f {inbox_count} items need attention \u2014 consider triaging now")

        return "\n".join(lines)

    def job_check_timezone_expiry(self):
        """Check if timezone override has expired and announce if so."""
        try:
            from artemis.quiet_hours import check_expired_overrides

            announcement = check_expired_overrides()
            if announcement:
                self.mm.post_message(config.CHANNEL_OPS, announcement)
        except Exception:
            logger.exception("Timezone expiry check failed")

    def job_override_expiry_check(self):
        """Check if working session override has expired due to inactivity."""
        try:
            from artemis.quiet_hours import check_override_expiry

            announcement = check_override_expiry()
            if announcement:
                self.mm.post_message(config.CHANNEL_OPS, announcement)
        except Exception:
            logger.debug("Override expiry check failed", exc_info=True)

    def job_action_item_reminders(self):
        """Remind about pending action items and auto-expire stale ones."""
        if self._is_quiet():
            return
        try:
            from knowledge.db import execute_query, execute_write

            # Find pending items needing a reminder
            pending = execute_query("""
                SELECT id, item_type, title, created_at, reminder_count, priority
                FROM acos.action_items
                WHERE status = 'pending'
                  AND (snoozed_until IS NULL OR snoozed_until < now())
                  AND (last_reminded_at IS NULL
                       OR last_reminded_at < now() - interval '2 hours')
                ORDER BY priority DESC, created_at ASC
            """)

            for item in pending:
                item_id = str(item["id"])[:8]
                age = datetime.utcnow() - item["created_at"].replace(tzinfo=None)

                # Auto-expire items older than 7 days
                if age.days >= 7:
                    execute_write(
                        """UPDATE acos.action_items
                           SET status = 'expired', resolved_at = now(),
                               resolved_by = 'auto-expire', updated_at = now()
                           WHERE id = %s""",
                        (item["id"],),
                    )
                    self.mm.post_message(
                        config.CHANNEL_OPS,
                        f"\u23f0 **Expired:** {item['title']} (no action after 7 days)",
                    )
                    continue

                # Post reminder
                age_str = f"{age.days}d {age.seconds // 3600}h" if age.days else f"{age.seconds // 3600}h"
                priority_tag = " \U0001f534" if item["priority"] == "high" else ""
                self.mm.post_message(
                    config.CHANNEL_OPS,
                    f"\u23f0 **Pending action{priority_tag}:** {item['title']}\n"
                    f"Waiting since: {age_str} ago (reminded {item['reminder_count']}x)\n"
                    f"\u2705 `approve sched {item_id}` · "
                    f"\u274c `skip sched {item_id}` · "
                    f"\U0001f4a4 `snooze sched {item_id}`",
                )
                execute_write(
                    """UPDATE acos.action_items
                       SET reminder_count = reminder_count + 1,
                           last_reminded_at = now(), updated_at = now()
                       WHERE id = %s""",
                    (item["id"],),
                )

        except Exception:
            logger.debug("Action item reminders failed", exc_info=True)

    def run_catchup(self):
        """Run catch-up processing after startup to handle missed emails during downtime."""
        from artemis.quiet_hours import get_system_value, set_system_value

        last_run = get_system_value("last_run_at")
        now = datetime.utcnow()

        if not last_run:
            # First run ever — process last 24h
            gap_hours = 24
            logger.info("First run — catching up on last 24 hours of email")
        else:
            try:
                last_dt = datetime.fromisoformat(last_run)
                gap_hours = (now - last_dt).total_seconds() / 3600
            except (ValueError, TypeError):
                gap_hours = 24

        if gap_hours < 0.2:  # Less than ~12 minutes — skip
            logger.info("Catch-up: last run %.1f hours ago — nothing to catch up", gap_hours)
            return

        logger.info("Catch-up: last run %.1f hours ago — processing gap", gap_hours)

        # Fetch and process emails from the gap
        emails_processed = 0
        playbooks_fired = 0
        try:
            messages = self._poll_gmail(max_results=50)
            if messages:
                self._record_gmail_success()
            new_messages = [m for m in messages if m["id"] not in self._seen_message_ids]

            if new_messages:
                from artemis.briefs import triage_emails
                from artemis.prompts import UNTRUSTED_PREFIX

                for m in new_messages:
                    self._seen_message_ids.add(m["id"])

                # Pre-fetch full bodies so triage sees real content
                for msg in new_messages[:5]:
                    body = self.gmail.get_full_message(msg["id"])
                    if body:
                        msg["full_body"] = body

                # Track in inbox + triage
                email_text = self.gmail.format_for_claude(new_messages)
                triaged = triage_emails(email_text, playbook_text=get_playbook_text())

                for i, item in enumerate(triaged):
                    orig = new_messages[i] if i < len(new_messages) else None
                    if orig:
                        from artemis.inbox import upsert_thread, NEEDS_ACTION, NOISE
                        sender_type = item.get("sender_type", "")
                        if sender_type == "noise":
                            upsert_thread(orig["thread_id"], orig["subject"], orig.get("from_email", ""), state=NOISE)
                        else:
                            upsert_thread(orig["thread_id"], orig["subject"], orig.get("from_email", ""), state=NEEDS_ACTION)
                        emails_processed += 1

                        # Execute playbooks
                        playbook_match = item.get("playbook_match")
                        if playbook_match:
                            body = self.gmail.get_full_message(orig["id"])
                            if body:
                                orig["full_body"] = body
                            self._execute_playbook(playbook_match, orig, item)
                            playbooks_fired += 1

                        # Archive
                        self.gmail.archive_message(orig["id"])
        except Exception:
            logger.exception("Catch-up email processing failed")

        # Check missed commitment alerts
        commitment_checks = 0
        try:
            active = list_commitments(status="active")
            today = date.today()
            for c in active:
                try:
                    due = date.fromisoformat(c["due_date"])
                except (ValueError, TypeError):
                    continue
                days_left = (due - today).days
                if days_left <= 0:
                    commitment_checks += 1
        except Exception:
            logger.debug("Catch-up commitment check failed")

        # Update last_run_at
        set_system_value("last_run_at", now.isoformat())

        # Post catch-up summary
        gap_str = f"{gap_hours:.0f} hours" if gap_hours >= 1 else f"{gap_hours * 60:.0f} minutes"
        if emails_processed or playbooks_fired:
            self.mm.post_message(
                config.CHANNEL_OPS,
                f"\U0001f504 Catch-up complete \u2014 processed {emails_processed} emails and "
                f"{commitment_checks} commitment checks since last run ({gap_str} ago). "
                f"{playbooks_fired} playbooks fired.",
            )
        else:
            self.mm.post_message(
                config.CHANNEL_OPS,
                f"\u2705 All caught up \u2014 nothing missed since last run {gap_str} ago.",
            )

    def job_follow_up_radar(self):
        """Daily follow-up radar: upcoming actions, stale deals, open commitments."""
        from knowledge.db import execute_query

        today = date.today()
        window_start = today - timedelta(days=1)
        window_end = today + timedelta(days=2)
        stale_threshold = today - timedelta(days=7)

        lines = [f"\U0001f514 **Follow-up Radar \u2014 {today.strftime('%A %B %d')}**\n"]
        has_items = False

        # 1. Next actions from data_vault_satellites
        try:
            actions = execute_query(
                """SELECT id, entity_id, content FROM acos.data_vault_satellites
                   WHERE satellite_type = 'next_action'
                     AND created_at > NOW() - interval '30 days'
                   ORDER BY created_at DESC
                   LIMIT 20"""
            )
            due_actions = []
            for a in actions:
                try:
                    import json as _json
                    data = _json.loads(a["content"]) if isinstance(a["content"], str) else a["content"]
                    action_date = data.get("date")
                    notified = data.get("notified")
                    if notified:
                        continue
                    if action_date:
                        from datetime import datetime as _dt
                        try:
                            d = _dt.strptime(action_date, "%Y-%m-%d").date()
                        except ValueError:
                            continue
                        if window_start <= d <= window_end:
                            label = "TODAY" if d == today else (
                                "TOMORROW" if d == today + timedelta(days=1) else
                                "OVERDUE" if d < today else d.strftime("%m/%d")
                            )
                            action_text = data.get("action", "?")
                            account = data.get("account", "")
                            due_actions.append(f"  \u00b7 [{label}] {action_text}" + (f" ({account})" if account else ""))
                            # Mark as notified
                            data["notified"] = "true"
                            from knowledge.db import execute_write as _db_write
                            _db_write(
                                "UPDATE acos.data_vault_satellites SET content = %s WHERE id = %s",
                                (_json.dumps(data), str(a["id"])),
                            )
                except Exception:
                    continue

            if due_actions:
                has_items = True
                lines.append("**DUE TODAY / TOMORROW:**")
                lines.extend(due_actions)
                lines.append("")
        except Exception:
            logger.debug("Follow-up radar: next_action query failed", exc_info=True)

        # 2. Open commitments due soon
        try:
            commitments = execute_query(
                """SELECT c.description, c.due_date, ct.name AS contact_name
                   FROM public.commitments c
                   LEFT JOIN public.contacts ct ON c.contact_id = ct.id
                   WHERE c.status = 'open'
                     AND c.due_date >= %s AND c.due_date <= %s
                   ORDER BY c.due_date ASC
                   LIMIT 10""",
                (window_start, window_end),
            )
            if commitments:
                has_items = True
                lines.append("**OPEN COMMITMENTS:**")
                for cm in commitments:
                    due = cm["due_date"].strftime("%m/%d") if cm.get("due_date") else "?"
                    who = cm.get("contact_name", "")
                    who_str = f" ({who})" if who else ""
                    lines.append(f"  \u00b7 {cm['description'][:120]}{who_str} \u2014 due {due}")
                lines.append("")
        except Exception:
            logger.debug("Follow-up radar: commitments query failed", exc_info=True)

        # 3. Stale deals
        try:
            stale = execute_query(
                """SELECT d.name, d.stage, d.updated_at, o.name AS org_name
                   FROM public.deals d
                   JOIN public.organizations o ON d.org_id = o.id
                   WHERE d.updated_at < %s
                     AND LOWER(d.stage) NOT IN ('closed', 'lost', 'msa', 'signed')
                   ORDER BY d.updated_at ASC
                   LIMIT 10""",
                (stale_threshold,),
            )
            if stale:
                has_items = True
                lines.append("**STALE DEALS (no activity 7+ days):**")
                for d in stale:
                    last = d["updated_at"].strftime("%b %d") if d.get("updated_at") else "?"
                    lines.append(f"  \u00b7 {d.get('org_name', d['name'])} \u2014 last updated {last}")
                lines.append("")
        except Exception:
            logger.debug("Follow-up radar: stale deals query failed", exc_info=True)

        if has_items:
            try:
                self.mm.post_message(config.CHANNEL_OPS, "\n".join(lines))
            except Exception:
                logger.exception("Failed to post follow-up radar")
        else:
            logger.info("Follow-up radar: nothing due today")

    def _record_gmail_success(self):
        """Reset Gmail failure counter on success."""
        self._gmail_fail_count = 0

    def _record_gmail_failure(self, error: str):
        """Increment Gmail failure counter and alert if threshold reached."""
        self._gmail_fail_count += 1
        logger.error("Gmail failure #%d: %s", self._gmail_fail_count, error)
        if self._gmail_fail_count == 3:
            try:
                self.mm.post_message(
                    config.CHANNEL_OPS,
                    f"\u26a0\ufe0f Gmail polling has failed 3 times \u2014 check credentials. "
                    f"Last error: {error[:300]}",
                )
            except Exception:
                logger.exception("Failed to post Gmail failure alert")

    def _record_calendar_success(self):
        """Reset Calendar failure counter on success."""
        self._calendar_fail_count = 0

    def _record_calendar_failure(self, error: str):
        """Increment Calendar failure counter and alert if threshold reached."""
        self._calendar_fail_count += 1
        logger.error("Calendar failure #%d: %s", self._calendar_fail_count, error)
        if self._calendar_fail_count == 3:
            try:
                self.mm.post_message(
                    config.CHANNEL_OPS,
                    f"\u26a0\ufe0f Calendar API has failed 3 times \u2014 check credentials. "
                    f"Last error: {error[:300]}",
                )
            except Exception:
                logger.exception("Failed to post Calendar failure alert")
