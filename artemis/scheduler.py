"""Cron jobs for all scheduled tasks."""

import json
import logging
import multiprocessing
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
from artemis.utils import next_business_day

logger = logging.getLogger(__name__)

_GMAIL_POLL_TIMEOUT = 120  # seconds — kill subprocess if it hangs

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


def _gmail_poll_worker(result_queue: multiprocessing.Queue, max_results: int = 20):
    """Run Gmail polling in an isolated subprocess to guard against segfaults.

    Writes ("ok", messages_list) or ("error", error_string) to result_queue.
    """
    try:
        from artemis.gmail import GmailClient as _GmailClient

        g = _GmailClient()
        g.authenticate()
        messages = g.get_recent_messages(max_results=max_results)
        result_queue.put(("ok", messages))
    except Exception as exc:
        result_queue.put(("error", str(exc)))


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

        # Load playbooks at startup
        load_playbooks()

        self.scheduler.start()
        logger.info("Scheduler started")

    def stop(self):
        self.scheduler.shutdown()

    def _poll_gmail_isolated(self, max_results: int = 20) -> list[dict]:
        """Poll Gmail in a subprocess so a segfault can't crash the main process."""
        q: multiprocessing.Queue = multiprocessing.Queue()
        proc = multiprocessing.Process(
            target=_gmail_poll_worker, args=(q, max_results), daemon=True
        )
        proc.start()
        proc.join(timeout=_GMAIL_POLL_TIMEOUT)

        if proc.is_alive():
            logger.error("Gmail poll subprocess timed out — killing it")
            proc.kill()
            proc.join(timeout=5)
            return []

        if proc.exitcode != 0:
            logger.error("Gmail poll subprocess exited with code %s (possible segfault)", proc.exitcode)
            return []

        if q.empty():
            logger.error("Gmail poll subprocess produced no result")
            return []

        status, payload = q.get_nowait()
        if status == "error":
            logger.error("Gmail poll subprocess error: %s", payload)
            return []

        return payload

    def job_inbox_triage(self):
        """Poll Gmail, classify new messages, archive, and execute playbooks."""
        try:
            messages = self._poll_gmail_isolated(max_results=20)
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
                        f"**Priority email** from {msg['from']}\n"
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
                email_text = self.gmail.format_for_claude(non_priority)
                triaged = triage_emails(email_text, playbook_text=get_playbook_text())

                # Zip triage results back with original messages for thread tracking
                full_body_fetches = 0
                _MAX_FULL_FETCHES = 5

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
                            f"**High urgency email**: {item.get('one_line_summary', 'New email')}",
                        )
                    else:
                        self._pending_triage.append(item)

                    # Fetch full body for playbook matches or known CRM contacts
                    # (limited to _MAX_FULL_FETCHES per cycle to control API costs)
                    if orig and full_body_fetches < _MAX_FULL_FETCHES:
                        needs_full = bool(playbook_match)
                        if not needs_full:
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
                        self._execute_playbook(playbook_match, orig, item)

                    # Archive every processed email
                    if orig:
                        self.gmail.archive_message(orig["id"])
                        logger.info("Archived [%s] from %s", orig.get("subject", ""), orig.get("from_email", ""))

        except Exception as exc:
            self._record_gmail_failure(str(exc))
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

    def job_focus_reminder(self):
        """Post daily focus reminder for the configured focus client."""
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
        """PB-001: Demo Access Notification — create lead + follow-up commitment."""
        sender_email = msg.get("from_email", "")
        sender_name = msg.get("from", "").split("<")[0].strip().strip('"') or sender_email
        # Use sender's domain as company fallback
        company = sender_email.split("@")[1] if "@" in sender_email else "Prospect"

        upsert_contact(
            name=sender_name,
            email=sender_email,
            company=company,
            source="artemis-demo",
            status="lead",
        )

        nbd = next_business_day()
        add_commitment(
            title=f"Follow up with {sender_name} re: demo access",
            due_date=nbd.isoformat(),
            effort_days=1,
            client=company,
        )

        self.mm.post_message(
            config.CHANNEL_OPS,
            f"\U0001f3af New demo lead: {sender_name} ({company}) \u2014 "
            f"follow-up scheduled for {nbd.isoformat()}",
        )

    def _run_pb002_meeting_followup(self, msg: dict, triage_item: dict):
        """PB-002: Meeting Follow-up — create commitments for action items."""
        sender_email = msg.get("from_email", "")
        sender_name = msg.get("from", "").split("<")[0].strip().strip('"') or sender_email
        company = sender_email.split("@")[1] if "@" in sender_email else ""
        summary = triage_item.get("one_line_summary", msg.get("subject", ""))

        # Default due date: 5 days from now
        due = (date.today() + timedelta(days=5)).isoformat()

        add_commitment(
            title=f"Follow up: {summary[:80]}",
            due_date=due,
            effort_days=2,
            client=company,
        )
        add_commitment(
            title=f"Send deliverables to {sender_name}",
            due_date=due,
            effort_days=1,
            client=company,
        )

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

    def job_commitment_reminders(self):
        """PB-005: Commitment Deadline Reminder Chain."""
        try:
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
