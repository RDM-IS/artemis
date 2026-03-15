"""Cron jobs for all scheduled tasks."""

import logging
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler

from artemis import config
from artemis.briefs import generate_meeting_brief, generate_morning_brief, triage_emails
from artemis.calendar import CalendarClient
from artemis.commitments import get_due_soon, get_start_alerts, get_commitments_for_client
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

logger = logging.getLogger(__name__)


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
        self.scheduler = BackgroundScheduler()
        self._pending_triage: list[dict] = []
        self._seen_message_ids: set[str] = set()

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

        self.scheduler.start()
        logger.info("Scheduler started")

    def stop(self):
        self.scheduler.shutdown()

    def job_inbox_triage(self):
        """Poll Gmail and classify new messages."""
        try:
            messages = self.gmail.get_recent_messages(max_results=20)
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
                # Track in inbox zero
                upsert_thread(
                    msg["thread_id"], msg["subject"], msg["from_email"],
                    state=NEEDS_ACTION,
                )
                post = self.mm.post_message(
                    config.CHANNEL_OPS,
                    f"**Priority email** from {msg['from']}\n"
                    f"Subject: {msg['subject']}\n"
                    f"> {msg['snippet'][:200]}\n\n"
                    f"Reply: `done {msg['thread_id'][:12]}` · `wait {msg['thread_id'][:12]}` · "
                    f"`snooze {msg['thread_id'][:12]} 3d` · `noise {msg['thread_id'][:12]}`",
                )
                if post.get("id"):
                    set_mattermost_post_id(msg["thread_id"], post["id"])

            if non_priority:
                email_text = self.gmail.format_for_claude(non_priority)
                triaged = triage_emails(email_text)

                # Zip triage results back with original messages for thread tracking
                for i, item in enumerate(triaged):
                    urgency = item.get("urgency", "low")
                    sender_type = item.get("sender_type", "")
                    # Try to match back to original message for thread_id
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
                            f"**High urgency email**: {item.get('one_line_summary', 'New email')}",
                        )
                    else:
                        self._pending_triage.append(item)

        except Exception:
            logger.exception("Inbox triage failed")

    def job_post_triage_batch(self):
        """Post batched triage summary."""
        if not self._pending_triage:
            return

        try:
            lines = ["**Inbox triage summary:**\n"]
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
        try:
            events = self.calendar.get_upcoming_with_externals(
                within_minutes=config.BRIEF_LEAD_TIME_MINUTES
            )
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

        except Exception:
            logger.exception("Pre-meeting brief generation failed")

    def job_morning_brief(self):
        """Generate and post the daily morning brief."""
        try:
            # Today's meetings
            events = self.calendar.get_upcoming_with_externals()
            meetings_text = self.calendar.format_events_for_brief(events)

            # Commitments due soon
            due_soon = get_due_soon(days=3)
            start_alerts = get_start_alerts()
            commitment_lines = []
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
                full_brief = f"**Good morning! Here's your brief:**\n\n{brief}\n\n**Inbox Zero:**\n{inbox_section}"
                self.mm.post_message(config.CHANNEL_OPS, full_brief)

        except Exception:
            logger.exception("Morning brief generation failed")

    def job_ssl_check(self):
        """Check SSL certs and alert if expiring."""
        try:
            results = check_all_ssl()
            alert = format_ssl_alerts(results)
            if alert:
                self.mm.post_message(config.CHANNEL_OPS, f"**SSL Certificate Alerts:**\n{alert}")
        except Exception:
            logger.exception("SSL check failed")

    def job_domain_check(self):
        """Check domain expiry and alert."""
        try:
            results = check_domain_expiry()
            alert = format_domain_alerts(results)
            if alert:
                self.mm.post_message(config.CHANNEL_OPS, f"**Domain Expiry Alerts:**\n{alert}")
        except Exception:
            logger.exception("Domain check failed")

    def job_inbox_zero_audit(self):
        """Audit inbox threads — nudge stale items, resurface snoozed, detect replies."""
        try:
            # 1. NEEDS_ACTION older than 24h → nudge
            stale_na = get_stale_needs_action(hours=24)
            for t in stale_na:
                if can_nudge(t["id"], min_hours=12):
                    self.mm.post_message(
                        config.CHANNEL_OPS,
                        f"**Nudge:** This thread still needs action:\n"
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
                        f"**Reply received** on: **{t['subject']}** — moved back to NEEDS_ACTION\n\n"
                        f"Reply: `done {t['id'][:12]}` · `wait {t['id'][:12]}` · "
                        f"`snooze {t['id'][:12]} 3d`",
                    )
                elif can_nudge(t["id"], min_hours=72):
                    who = t.get("waiting_on") or "them"
                    snippet = self.gmail.get_my_last_message_snippet(t["id"])
                    context = f' re: "{snippet}"' if snippet else ""
                    self.mm.post_message(
                        config.CHANNEL_OPS,
                        f"**Still waiting on {who}{context}** — no reply in 3+ days\n"
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
                    f"**Resurfaced (snooze ended):**\n"
                    f"**{t['subject']}** from {t['sender']}\n\n"
                    f"Reply: `done {t['id'][:12]}` · `wait {t['id'][:12]}` · "
                    f"`snooze {t['id'][:12]} 3d` · `noise {t['id'][:12]}`",
                )

        except Exception:
            logger.exception("Inbox zero audit failed")

    def job_inbox_zero_morning(self):
        """Pre-compute inbox zero stats before morning brief (stats are pulled inline)."""
        # This is a no-op hook — the actual data is pulled by format_morning_inbox_section()
        # during job_morning_brief. This job exists as a named anchor in case
        # we want to do pre-brief inbox processing later.
        logger.debug("Inbox zero morning pre-check complete")
