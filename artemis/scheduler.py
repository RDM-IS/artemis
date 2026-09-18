"""Cron jobs for all scheduled tasks."""

import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from artemis import config
from artemis import morning_brief
from artemis.briefs import generate_meeting_brief, generate_morning_brief, triage_emails
from artemis.calendar import CalendarClient
from artemis.commitments import (
    add_commitment,
    get_due_soon,
    get_start_alerts,
    get_commitments_for_client,
    list_commitments,
)
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
    should_keep_in_inbox,
    state_from_triage,
    upsert_thread,
    NEEDS_ACTION,
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


def _local_today():
    """Today for Ryan — active timezone (WAKE-1). Never the box's zone."""
    from artemis.quiet_hours import local_today
    return local_today()


def _hhmm(value: str) -> tuple[int, int]:
    h, _, m = value.partition(":")
    return int(h), int(m or 0)


def _plus_minutes(hour: int, minute: int, delta: int) -> tuple[int, int]:
    total = (hour * 60 + minute + delta) % (24 * 60)
    return total // 60, total % 60


def _minus_minutes(hour: int, minute: int, delta: int) -> tuple[int, int]:
    total = (hour * 60 + minute - delta) % (24 * 60)
    return total // 60, total % 60


@dataclass(frozen=True)
class CronSpec:
    """One cron job, in LOCAL wall-clock time.

    THE registry: every cron job in Artemis is declared here and registered
    through apply_timezone(), so a timezone override reschedules all of them
    together. Interval and one-shot date jobs are unaffected.
    """
    id: str
    func_name: str
    hour: int
    minute: int
    day_of_week: str | None = None
    #  health   → posts in wake + open      business → posts in open only
    tier: str = "business"
    # Once-per-local-day guard key. A weekend twin shares its weekday job's key
    # so the two can never both fire on one date (e.g. after a custom wake).
    guard: str | None = None


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
        self.scheduler = BackgroundScheduler(timezone=ZoneInfo(config.HOME_TIMEZONE))
        self._cron_specs: list[CronSpec] = []
        self._applied_tz: str | None = None
        self._pending_triage: list[dict] = []
        self._seen_message_ids: set[str] = set()
        self._pending_availability: dict[str, dict] = {}
        # Error recovery counters
        self._gmail_fail_count: int = 0
        self._calendar_fail_count: int = 0

    # ── Cron registry (WAKE-1 §C) ─────────────────────────────────────────
    #  Local wall-clock times. apply_timezone() is the ONLY registration path;
    #  no add_job(..., "cron", ...) call may live outside it.

    def cron_specs(self) -> list[CronSpec]:
        wake_h, wake_m = _hhmm(config.WAKE_TIME)
        open_h, open_m = _hhmm(config.OPEN_TIME)
        quiet_h, quiet_m = _hhmm(config.QUIET_HOURS_START)
        brief_h, brief_m = _hhmm(config.MORNING_BRIEF_TIME)
        pre_h, pre_m = _minus_minutes(brief_h, brief_m, 5)
        # Weekends (Sat/Sun): wake, open and quiet move; the morning brief rides
        # on open (it is 06:30 = OPEN_TIME on weekdays).
        we_wake_h, we_wake_m = _hhmm(config.WEEKEND_WAKE_TIME)
        we_open_h, we_open_m = _hhmm(config.WEEKEND_OPEN_TIME)
        we_quiet_h, we_quiet_m = _hhmm(config.WEEKEND_QUIET_HOURS_START)
        we_pre_h, we_pre_m = _minus_minutes(we_open_h, we_open_m, 5)
        WD, WE = "mon-fri", "sat,sun"

        specs = [
            # 03:30 — silent vault ingest, before the wake window.
            CronSpec("vault_sync", "job_vault_sync", 3, 30),
            # 04:30 (Sat/Sun 07:30) — wake: health holds flush + the wake post.
            CronSpec("wake", "job_wake", wake_h, wake_m, WD, tier="health"),
            CronSpec("wake_weekend", "job_wake", we_wake_h, we_wake_m, WE,
                     tier="health", guard="wake"),
            # wake + 45 min — one nudge if no check-in arrived (FRIDAY-1).
            # Event-driven adjustment replaced the fixed 04:45 calibration post.
            CronSpec("checkin_nudge", "job_checkin_nudge",
                     *_plus_minutes(wake_h, wake_m, config.CHECKIN_NUDGE_OFFSET_MIN), WD,
                     tier="health"),
            CronSpec("checkin_nudge_weekend", "job_checkin_nudge",
                     *_plus_minutes(we_wake_h, we_wake_m, config.CHECKIN_NUDGE_OFFSET_MIN), WE,
                     tier="health", guard="checkin_nudge"),
            # 06:25 / 06:30 (Sat/Sun 08:25 / 08:30) — open: business holds
            # flush, then the brief.
            CronSpec("inbox_zero_morning", "job_inbox_zero_morning", pre_h, pre_m, WD),
            CronSpec("inbox_zero_morning_weekend", "job_inbox_zero_morning",
                     we_pre_h, we_pre_m, WE, guard="inbox_zero_morning"),
            CronSpec("open", "job_open", open_h, open_m, WD),
            CronSpec("open_weekend", "job_open", we_open_h, we_open_m, WE, guard="open"),
            CronSpec("morning_brief", "job_morning_brief", brief_h, brief_m, WD),
            CronSpec("morning_brief_weekend", "job_morning_brief", we_open_h, we_open_m, WE,
                     guard="morning_brief"),
            # 08:00 / 08:05 — weekdays only (open-only checks).
            CronSpec("ssl_check", "job_ssl_check", 8, 0, WD),
            CronSpec("domain_check", "job_domain_check", 8, 5, WD),
            CronSpec("follow_up_radar", "job_follow_up_radar", 8, 0, "mon-fri"),
            CronSpec("update_check", "job_update_check", 8, 0, "mon"),
            CronSpec("commitment_reminders", "job_commitment_reminders", 8, 15, "mon-fri"),
            # 16:30 — workouts are AM now, so the debrief nag moved off 21:00
            # (which would sit inside the 17:00 quiet window and never fire).
            CronSpec("health_nag", "job_health_nag", 16, 30, tier="health"),
            CronSpec("vault_coverage", "job_vault_coverage", 16, 30, "mon-fri"),
            # 17:00 (Sat/Sun 22:30) — quiet.
            CronSpec("quiet_hours_start", "job_quiet_hours_start", quiet_h, quiet_m, WD),
            CronSpec("quiet_hours_start_weekend", "job_quiet_hours_start",
                     we_quiet_h, we_quiet_m, WE, guard="quiet_hours_start"),
            # 21:50 — silent DB backstop; writes only, posts nothing.
            CronSpec("health_inferred_summary", "job_health_inferred_summary", 21, 50),
            # 21:55 — silent pain-pattern recompute (PAIN-1); writes only.
            CronSpec("pain_pattern_recompute", "job_pain_pattern_recompute", 21, 55),
            # Sunday at the weekend open (08:30) — weekly health review: new or
            # changed pain patterns only. It posts in the OPEN phase only.
            CronSpec("health_review", "job_health_review", we_open_h, we_open_m, "sun",
                     tier="health"),
        ]
        if config.FOCUS_CLIENT:
            specs.append(CronSpec("focus_reminder", "job_focus_reminder", 9, 0, "mon-fri"))
        return specs

    def _wrap_cron(self, spec: CronSpec):
        """Wrap a registry job in the once-per-LOCAL-day duplicate guard."""
        func = getattr(self, spec.func_name)

        def runner():
            if not self._once_per_local_day(spec.guard or spec.id):
                return
            func()

        runner.__name__ = f"cron_{spec.id}"
        return runner

    def _once_per_local_day(self, job_id: str) -> bool:
        """False when this job already ran on today's LOCAL date.

        A timezone switch can replay a wall-clock time on the same local date
        (e.g. a Paris override expiring at 17:00 CT would re-arm the 17:00 jobs).
        The guard is keyed on (job_id, local date at fire time) in system_state.
        """
        from artemis.quiet_hours import get_system_value, local_today, set_system_value

        key = f"cron_last_run:{job_id}"
        today = local_today().isoformat()
        if get_system_value(key) == today:
            logger.info("Cron %s already ran on local day %s — skipping (tz replay guard)",
                        job_id, today)
            return False
        set_system_value(key, today)
        return True

    def apply_timezone(self, tz_name: str) -> None:
        """(Re)register every registry job against `tz_name` and record it."""
        from artemis.quiet_hours import set_system_value

        try:
            tz = ZoneInfo(tz_name)
        except (KeyError, ValueError):
            logger.error("apply_timezone: unknown zone %r — keeping %s", tz_name, self._applied_tz)
            return

        self._cron_specs = self.cron_specs()
        for spec in self._cron_specs:
            trigger = CronTrigger(
                hour=spec.hour, minute=spec.minute,
                day_of_week=spec.day_of_week, timezone=tz,
            )
            if self.scheduler.get_job(spec.id):
                self.scheduler.reschedule_job(spec.id, trigger=trigger)
            else:
                self.scheduler.add_job(
                    self._wrap_cron(spec), trigger=trigger, id=spec.id, replace_existing=True,
                )
        self._applied_tz = tz_name
        set_system_value("scheduler_tz", tz_name)
        logger.info("Scheduler timezone applied: %s", tz_name)
        for line in self.job_dump():
            logger.info("  %s", line)

    def job_dump(self) -> list[str]:
        """Next fire time of every cron job, in local and CT — the verify surface."""
        home = ZoneInfo(config.HOME_TIMEZONE)
        out = []
        for spec in self._cron_specs or self.cron_specs():
            job = self.scheduler.get_job(spec.id)
            nxt = getattr(job, "next_run_time", None) if job else None
            if nxt is None and job is not None:
                # Before scheduler.start() APScheduler hasn't computed one yet —
                # ask the trigger directly so the startup dump is still useful.
                try:
                    tz = job.trigger.timezone
                    nxt = job.trigger.get_next_fire_time(None, datetime.now(tz))
                except Exception:  # pragma: no cover - defensive
                    nxt = None
            if nxt is None:
                out.append(f"{spec.id:<24} {spec.hour:02d}:{spec.minute:02d} local  next=—")
                continue
            out.append(
                f"{spec.id:<24} {spec.hour:02d}:{spec.minute:02d} local  "
                f"next={nxt.isoformat()}  ct={nxt.astimezone(home).isoformat()}"
            )
        return out

    def start(self):
        from artemis.quiet_hours import get_active_timezone

        # ── Interval jobs (timezone-independent) ──
        # Business intervals require the OPEN phase; each checks _is_open().
        self.scheduler.add_job(self.job_inbox_triage, "interval", minutes=5, id="inbox_triage")
        self.scheduler.add_job(self.job_post_triage_batch, "interval", minutes=30, id="triage_batch")
        self.scheduler.add_job(self.job_pre_meeting_briefs, "interval", minutes=10, id="pre_meeting")
        self.scheduler.add_job(self.job_inbox_zero_audit, "interval", minutes=60, id="inbox_zero_audit")
        self.scheduler.add_job(self.job_action_item_reminders, "interval", minutes=30,
                               id="action_item_reminders")
        self.scheduler.add_job(self.job_demo_intake, "interval", minutes=5, id="demo_intake")
        logger.info("PB-001 demo intake enabled")

        scopes_ok, missing = check_billing_scopes()
        if scopes_ok:
            self.scheduler.add_job(self.job_billing_intake, "interval", minutes=15, id="billing_intake")
            logger.info("PB-007 billing intake enabled")
        else:
            logger.warning("PB-007 billing intake disabled — missing scopes: %s", missing)

        # STAB-1 A2: websocket watchdog — every 60s.
        self.scheduler.add_job(self.job_ws_watchdog, "interval", seconds=60, id="ws_watchdog")

        # Working session inactivity check — every 1 minute.
        self.scheduler.add_job(self.job_override_expiry_check, "interval", minutes=1,
                               id="override_expiry_check")

        # WAKE-1: the schedule follows the active timezone. This is the ONLY
        # path that switches it (replaces the old noon expiry cron), and it also
        # sweeps an expired override and announces the change.
        self.scheduler.add_job(self.job_tz_sync, "interval", seconds=60, id="tz_sync")

        # A goodnight with a custom wake time ("wake me at 6") must actually
        # wake — the 04:30 cron alone returned early and nothing re-checked.
        self.scheduler.add_job(self.job_wake_watch, "interval", minutes=1, id="wake_watch")

        # ── Cron jobs: registry, in the active timezone ──
        self.apply_timezone(get_active_timezone())

        # Load playbooks at startup
        load_playbooks()

        self.scheduler.start()
        logger.info("Scheduler started (cron tz=%s)", self._applied_tz)

    def stop(self):
        self.scheduler.shutdown()

    def _is_quiet(self) -> bool:
        """True in the QUIET phase — health jobs guard on this."""
        try:
            from artemis.quiet_hours import is_quiet_hours
            return is_quiet_hours()
        except Exception:
            return False

    def _is_open(self) -> bool:
        """True in the OPEN phase — every business job guards on this."""
        try:
            from artemis.quiet_hours import is_open
            return is_open()
        except Exception:
            return False

    def _post(self, channel: str, text: str, tier: str = "business") -> bool:
        """Scheduled posts go through the phase gate: post now, or hold durably."""
        from artemis.posting import post_or_hold
        return post_or_hold(self.mm, channel, text, tier)

    # ── Phase jobs (WAKE-1) ───────────────────────────────────────────────

    def _do_wake(self) -> None:
        """Enter the wake phase, flush health holds into the wake post, and open
        today's check-in. There is no timed calibration post any more: the plan
        is adjusted when the check-in arrives (artemis.health_checkin)."""
        from artemis import wake as wake_mod
        from artemis.health_checkin import checkin_key
        from artemis.posting import take_holds
        from artemis.quiet_hours import exit_quiet, set_system_value

        exit_quiet()
        held = take_holds("health")
        text = wake_mod.build_wake_message(calendar=self.calendar, held_health=held)
        self.mm.post_message(config.CHANNEL_OPS, text)
        set_system_value(checkin_key(_local_today()), "open")

    def job_wake(self):
        """04:30 local (Sat/Sun 07:30) — wake. Health only; business waits for open."""
        try:
            from artemis.quiet_hours import get_quiet_state
            state = get_quiet_state()
            wake_at = state.get("wake_time")
            if state.get("manual_override") and wake_at:
                # A custom wake time was asked for — job_wake_watch owns it.
                logger.info("Wake: custom wake time %s set — deferring to wake_watch", wake_at)
                return
            self._do_wake()
        except Exception:
            logger.exception("Wake job failed")

    def job_wake_watch(self):
        """Every minute: honour a goodnight's custom wake time ("wake me at 6").

        Finding 8 on main: the 04:30 cron returned early when a custom wake time
        was set and nothing ever re-checked, so Artemis never woke.
        """
        try:
            from artemis.quiet_hours import _parse_time, get_quiet_state, local_now
            state = get_quiet_state()
            if not (state.get("manual_override") and state.get("is_quiet")):
                return
            wake_at = state.get("wake_time")
            if not wake_at:
                return
            if local_now().time() < _parse_time(wake_at):
                return
            if not self._once_per_local_day("wake"):
                return
            self._do_wake()
        except Exception:
            logger.debug("Wake watch failed", exc_info=True)

    def job_open(self):
        """06:30 local — business opens: flush held business posts, then the
        overnight summary that used to ride on quiet-hours-end at 04:00."""
        try:
            from artemis.posting import flush_holds
            from artemis.quiet_hours import get_quiet_state
            state = get_quiet_state()
            if state.get("is_quiet") and state.get("manual_override"):
                # Still manually quiet (goodnight with a late wake) — hold.
                logger.info("Open: manual quiet still active — not opening")
                return
            flush_holds(self.mm, "business")
            self.mm.post_message(config.CHANNEL_OPS, self._build_overnight_summary())
        except Exception:
            logger.exception("Open job failed")

    def job_tz_sync(self):
        """Every 60s: keep the cron schedule on the ACTIVE timezone.

        Also sweeps an expired override (atomic delete) and announces the
        change. The set/clear command calls this directly so there is no lag.
        """
        try:
            from artemis.quiet_hours import check_expired_overrides, get_active_timezone

            announcement = check_expired_overrides()
            active = get_active_timezone()
            if active != self._applied_tz:
                logger.info("Timezone change: %s → %s — rescheduling crons",
                            self._applied_tz, active)
                self.apply_timezone(active)
            if announcement:
                self._post(config.CHANNEL_OPS, announcement, tier="business")
        except Exception:
            logger.exception("Timezone sync failed")

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

    _state_from_triage = staticmethod(state_from_triage)

    def _apply_playbook_rules(self, messages: list[dict]) -> list[dict]:
        """Match each new message against active chat-authored rules; dispose the
        matches through the audited primitive and drop them from the pool.

        Rules are a hard layer ahead of the LLM triage: a match archives/spams/
        files with a labeled, verified, audited action tagged to the rule id.
        Body matching fetches the full body lazily, only for a candidate that
        needs it. Unmatched messages flow on to the normal triage path.
        """
        from artemis import playbook_rules
        from artemis.main import file_message_for_rule

        try:
            active = playbook_rules.list_rules(active_only=True)
        except Exception:
            logger.exception("playbook rules unavailable — skipping rule pass")
            return messages
        if not active:
            return messages

        remaining: list[dict] = []
        for m in messages:
            def _fetch_body(msg=m):
                return self.gmail.get_full_message(msg["id"]) or ""

            try:
                rule = playbook_rules.match_message(
                    m.get("from_email", ""), m.get("subject", ""), body_fetcher=_fetch_body,
                )
            except Exception:
                logger.exception("rule match failed for %s", m.get("id"))
                rule = None

            if not rule:
                remaining.append(m)
                continue

            res = file_message_for_rule(m["id"], rule, gmail_client=self.gmail)
            if res.get("ok"):
                playbook_rules.record_fired(rule["id"])
                logger.info(
                    "Rule #%s fired on [%s] → %s%s",
                    rule["id"], m.get("subject", ""), rule["action"],
                    f" @artemis/{rule.get('action_label')}" if rule["action"] == "file" else "",
                )
                # Disposed + audited + index dropped by the primitive; do not
                # re-add to the pool (it has left the inbox).
            else:
                # Unverified → leave it in the normal triage path, don't lose it.
                logger.error(
                    "Rule #%s UNVERIFIED on [%s] — %s; falling through to triage",
                    rule["id"], m.get("subject", ""), res.get("detail", "?"),
                )
                remaining.append(m)
        return remaining

    def _archive_for_state(self, message_id: str, state: str, subject: str = "") -> None:
        """State-conditional archive gate (spec §2).

        NEEDS_ACTION stays in INBOX; everything else is filed. Filing now goes
        through the SAME audited/labeled primitive as commands — INBOX is never
        stripped without an @artemis/* label and an audit row (location
        invariant). Financial documents are kept in the inbox regardless of
        state: filing them is a loan-vs-paid decision only a command can make.
        Under FILING_DRY_RUN the decision is only logged — no mutation.
        """
        from artemis.billing import is_financial_document

        if should_keep_in_inbox(state):
            logger.info("Filing gate: KEEP inbox [%s] state=%s", subject, state)
            return
        if is_financial_document(subject):
            logger.info(
                "Filing gate: KEEP inbox [%s] state=%s (financial — awaits command)",
                subject, state,
            )
            return
        if config.FILING_DRY_RUN:
            logger.info("Filing gate: DRY-RUN would ARCHIVE [%s] state=%s", subject, state)
            return

        # Audited, labeled archive — NOT a bare gmail.archive_message().
        from artemis.main import file_message_for_automation

        res = file_message_for_automation(message_id, triage_state=state, gmail_client=self.gmail)
        if res.get("ok"):
            logger.info("Filing gate: ARCHIVED [%s] state=%s (labeled+audited)", subject, state)
        else:
            logger.error(
                "Filing gate: archive UNVERIFIED [%s] state=%s — %s; mail left in place",
                subject, state, res.get("detail", "?"),
            )

    def job_inbox_triage(self):
        """Poll Gmail, classify new messages, archive, and execute playbooks."""
        if not self._is_open():
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

            # Feature #1: chat-authored playbook rules run FIRST — a user rule is a
            # hard rule and takes precedence over the LLM triage. Matched messages
            # are disposed through the audited primitive and removed from the pool.
            new_messages = self._apply_playbook_rules(new_messages)
            if not new_messages:
                return

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
                    post = self._post(
                        config.CHANNEL_OPS,
                        f"\U0001f4ec **Priority email** from {msg['from']}\n"
                        f"Subject: {msg['subject']}\n"
                        f"> {msg['snippet'][:200]}\n\n"
                        f"Reply: `done {msg['thread_id'][:12]}` · `wait {msg['thread_id'][:12]}` · "
                        f"`snooze {msg['thread_id'][:12]} 3d` · `noise {msg['thread_id'][:12]}`",
                    )
                    if post.get("id"):
                        set_mattermost_post_id(msg["thread_id"], post["id"])
                    # Priority sender is tracked NEEDS_ACTION → the gate keeps it
                    # in INBOX (a human decision is required).
                    self._archive_for_state(msg["id"], NEEDS_ACTION, msg.get("subject", ""))
                except Exception:
                    logger.exception(
                        "Failed to track priority email — NOT archiving %s", msg["id"]
                    )
                    self._post(
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
                    playbook_match = item.get("playbook_match")
                    orig = non_priority[i] if i < len(non_priority) else None

                    # Rubric-assigned state drives both tracking and the archive gate.
                    state = self._state_from_triage(item)
                    if orig:
                        upsert_thread(
                            orig["thread_id"], orig["subject"], orig["from_email"],
                            state=state,
                        )

                    if urgency == "high":
                        self._post(
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
                        # Known CRM contact? Check the RDS CRM API (was SQLite
                        # crm.get_contact). Unavailable/error → treat as unknown
                        # (snippet only), exactly as a None lookup did before.
                        needs_full = False
                        from_email = orig.get("from_email", "")
                        if from_email and self.crm.is_available():
                            try:
                                needs_full = bool(self.crm.find_contact_by_email(from_email))
                            except Exception:
                                logger.debug("CRM contact lookup failed for %s", from_email)
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

                    # State-conditional archive gate (NEEDS_ACTION stays in INBOX)
                    if orig:
                        self._archive_for_state(orig["id"], state, orig.get("subject", ""))

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
            self._post(
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
        if not self._is_open():
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

            self._post(config.CHANNEL_OPS, "\n".join(lines))
            self._pending_triage.clear()
        except Exception:
            logger.exception("Triage batch post failed")

    def job_pre_meeting_briefs(self):
        """Generate briefs for upcoming meetings with external attendees."""
        if not self._is_open():
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
                    self._post(config.CHANNEL_BRIEFS, header + brief)

        except Exception as exc:
            self._record_calendar_failure(str(exc))
            logger.exception("Pre-meeting brief generation failed")

    def compose_morning_brief(self, *, include_monitors: bool = True) -> str:
        """POLISH-1 P1/P3 - the ONE deterministic composer, shared by the 04:00
        cron (here) and the on-demand `morning brief`
        (main._handle_morning_brief_command). `today` is CT-anchored once and
        threaded through every date, so the header and every relative line share
        one anchor and can't drift. No LLM - the LLM free-composition path is
        exactly what produced the Jul-6 header."""
        today = _local_today()
        return morning_brief.compose(
            today, gmail=self.gmail, calendar=self.calendar,
            include_monitors=include_monitors,
        )

    def job_morning_brief(self):
        """Compose and post the daily morning brief (04:00 CT)."""
        try:
            brief = self.compose_morning_brief(include_monitors=True)
            if brief:
                self._post(config.CHANNEL_OPS, brief)
                # OPS-2 health-panel truth: record a DURABLE last-brief timestamp
                # (acos.system_state, same helper as last_run_at). The ops health
                # strip reads this real value — the dashboard's old "41d ago" came
                # from approximating the brief time off an unrelated action item.
                try:
                    from artemis.quiet_hours import set_system_value
                    set_system_value("last_morning_brief_at", datetime.utcnow().isoformat())
                except Exception:
                    logger.debug("last_morning_brief_at write failed", exc_info=True)
        except Exception:
            logger.exception("Morning brief generation failed")

    def job_vault_sync(self):
        """PB-011: 03:30 local vault ingest — fetch mirror, upsert notes, recompute
        links, run the throttled extraction pass. Identical code path to the
        `@artemis vault sync` command. Silent, and deliberately UNGATED: it runs
        inside quiet hours and posts nothing (only a failure runbook, which is
        held until business opens).
        """
        try:
            from artemis import vault
            summary = vault.sync_vault()
            logger.info("Vault sync (03:30 local): %s", summary)
        except Exception as exc:
            logger.exception("Vault sync job failed")
            # OPS-1: a nightly sync failure is otherwise silent-until-morning — classify
            # it and post the runbook (or the raw error, if unclassified) to ops so the
            # remediation is on hand. report_failure also writes the audit row.
            try:
                from artemis import opsdiag
                self._post(
                    config.CHANNEL_OPS,
                    "\U0001f5c4️ **Vault sync (03:30 local) failed**\n"
                    + opsdiag.report_failure(exc, {"stage": "vault sync (cron)"}, agent="vault"),
                )
            except Exception:
                logger.exception("Vault sync failure report failed")

    def job_vault_coverage(self):
        """PB-011: weekday 16:30 CT — compare today's real calendar meetings to
        today's dictated captures; post at most one nudge to #artemis-ryan."""
        if not self._is_open():
            return
        try:
            from artemis import vault
            vault.run_coverage_monitor(self.calendar, self.mm)
        except Exception:
            logger.exception("Vault coverage monitor failed")

    def job_ssl_check(self):
        """Check SSL certs and alert if expiring."""
        if not self._is_open():
            return
        try:
            results = check_all_ssl()
            alert = format_ssl_alerts(results)
            if alert:
                self._post(config.CHANNEL_OPS, f"\u26a0\ufe0f **SSL Certificate Alerts:**\n{alert}")
        except Exception:
            logger.exception("SSL check failed")

    def job_domain_check(self):
        """Check domain expiry and alert."""
        if not self._is_open():
            return
        try:
            results = check_domain_expiry()
            alert = format_domain_alerts(results)
            if alert:
                self._post(config.CHANNEL_OPS, f"\u26a0\ufe0f **Domain Expiry Alerts:**\n{alert}")
        except Exception:
            logger.exception("Domain check failed")

    def job_inbox_zero_audit(self):
        """Audit inbox threads — nudge stale items, resurface snoozed, detect replies."""
        if not self._is_open():
            return
        try:
            # 1. NEEDS_ACTION older than 24h → nudge
            stale_na = get_stale_needs_action(hours=24)
            for t in stale_na:
                if can_nudge(t["id"], min_hours=12):
                    self._post(
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
                    self._post(
                        config.CHANNEL_OPS,
                        f"\U0001f4ec **Reply received** on: **{t['subject']}** \u2014 moved back to NEEDS_ACTION\n\n"
                        f"Reply: `done {t['id'][:12]}` · `wait {t['id'][:12]}` · "
                        f"`snooze {t['id'][:12]} 3d`",
                    )
                elif can_nudge(t["id"], min_hours=72):
                    who = t.get("waiting_on") or "them"
                    snippet = self.gmail.get_my_last_message_snippet(t["id"])
                    context = f' re: "{snippet}"' if snippet else ""
                    self._post(
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
                self._post(
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
        if not self._is_open():
            return
        # This is a no-op hook — the actual data is pulled by format_morning_inbox_section()
        # during job_morning_brief. This job exists as a named anchor in case
        # we want to do pre-brief inbox processing later.
        logger.debug("Inbox zero morning pre-check complete")

    def job_focus_reminder(self):
        """Post daily focus reminder for the configured focus client."""
        if not self._is_open():
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

            self._post(
                config.CHANNEL_OPS,
                f"\U0001f3af Titanium focus check: {commitment_text}\n\n"
                f"Everything else is secondary.",
            )
        except Exception:
            logger.exception("Focus reminder failed")

    def job_ws_watchdog(self):
        """STAB-1 A2: force-close the Mattermost websocket if it's gone stale
        (>90s with no event/pong), so the reconnect loop can re-establish it."""
        try:
            self.mm.watchdog_check()
        except Exception:
            logger.debug("ws watchdog job failed", exc_info=True)

    def job_update_check(self):
        """Check GitHub for new commits and post if an update is available."""
        if not self._is_open():
            return
        try:
            from artemis.version import get_commit_hash, get_latest_github_version

            local_hash = get_commit_hash()
            latest_hash, latest_date = get_latest_github_version()

            if not latest_hash or not local_hash:
                return  # can't check — skip silently

            if latest_hash.startswith(local_hash):
                return  # up to date — stay silent

            self._post(
                config.CHANNEL_OPS,
                f"\U0001f504 Artemis update available \u2014 latest commit: {latest_hash} ({latest_date}).\n"
                f"Deploy with `bash scripts/deploy.sh` on the box (pull \u2192 migrate \u2192 "
                f"restart, atomically). Never a bare `git pull` \u2014 that leaves the "
                f"running process stale and migrations unapplied (STAB-1 A5).",
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
            self._post(
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

        self._post(
            config.CHANNEL_COMMITMENTS,
            f"\U0001f4cb Meeting follow-up from {sender_name}:\n"
            f"- Follow up: {summary[:80]} (due {due})\n"
            f"- Send deliverables to {sender_name} (due {due})",
        )
        self._post(
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
            self._post(
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
        self._post(
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

        post_result = self._post(config.CHANNEL_OPS, formatted)

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
        if not self._is_open():
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
                    self._post(
                        config.CHANNEL_OPS,
                        f"\U0001f6a8 **TODAY**: {c['title']} is due today! (client: {c.get('client', 'n/a')})",
                    )
                elif days_left == 1:
                    self._post(
                        config.CHANNEL_COMMITMENTS,
                        f"\U0001f534 **Due tomorrow**: {c['title']} (client: {c.get('client', 'n/a')})",
                    )
                elif days_left == effort:
                    self._post(
                        config.CHANNEL_COMMITMENTS,
                        f"\u26a0\ufe0f **Start today**: {c['title']} \u2014 needs {effort}d effort, "
                        f"due {c['due_date']} (client: {c.get('client', 'n/a')})",
                    )
                elif days_left == 5:
                    self._post(
                        config.CHANNEL_COMMITMENTS,
                        f"\U0001f4c5 **5 days out**: {c['title']} due {c['due_date']} "
                        f"(client: {c.get('client', 'n/a')})",
                    )

        except Exception:
            logger.exception("Commitment reminder chain failed")

    def job_demo_intake(self):
        """PB-001 v2: Scan for Lucint demo notification emails and process them."""
        if not self._is_open():
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
                        self._post(
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
        if not self._is_open():
            return

        # One-time label check per process lifetime. POLISH-1: announce ONLY on an
        # actual creation (ensure_billing_label's `created` flag), never on the old
        # "label has no messages" proxy — that re-fired the "Created …" message on
        # every overnight restart even though the label already existed.
        if not self._billing_label_checked:
            label_id, created = ensure_billing_label(self.gmail)
            if label_id:
                if created:
                    self._post(
                        config.CHANNEL_OPS,
                        "\U0001f4c1 Created Gmail label **artemis/billing** — "
                        "tag expense emails with this label for automatic intake",
                    )
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
                        self._post(
                            config.CHANNEL_OPS,
                            f"\u26a0\ufe0f PB-007 billing intake failed on message {msg_id[:12]}… "
                            f"— check logs. Email NOT processed.",
                        )
                    except Exception:
                        pass
        except Exception:
            logger.exception("PB-007 billing intake job failed")

    def job_health_nag(self):
        """Health: 16:30 local — prompt for a workout debrief if one is missing.

        Suppression is by PLAN, not day-of-week: run_nag_check() already skips
        rest_mobility / walk / skipped days and days that already have a log.
        """
        if self._is_quiet():
            return
        try:
            from artemis.health import run_nag_check
            msg = run_nag_check()
            if msg:
                self._post(config.CHANNEL_OPS, msg, tier="health")
                logger.info("Posted health debrief nag")
        except Exception:
            logger.exception("Health nag job failed")

    def job_health_inferred_summary(self):
        """Health: at 21:50 CT, insert a placeholder summary for today if no
        debrief was logged. Marked logged_via='inferred' so the autoregulator
        knows it's not real data.

        Fires every day regardless of nag suppression — data-quality backstop.
        """
        try:
            from artemis.health import insert_inferred_summary
            inserted = insert_inferred_summary()
            if inserted:
                logger.info("Inserted inferred session_summary for today")
        except Exception:
            logger.exception("Health inferred-summary job failed")

    # ── T4: Proactive prompts (PB-009) ────────────────────────────────────

    # job_health_morning_prompt is gone (WAKE-1): the survey prompt is part of
    # the 04:30 wake post, and its variant comes from plan.session_type
    # (wake.prompt_type_for) instead of a hardcoded day-of-week map.

    def job_checkin_nudge(self):
        """WAKE + 45 min (05:15) — one nudge on a training day with no check-in.

        Never repeats (the registry's once-per-local-day guard) and says only
        what the plan says; it does not adjust anything.
        """
        try:
            from artemis.health_checkin import has_checkin, load_plan, logged_set_count, nudge_text
            from knowledge.db import get_connection

            today = _local_today()
            with get_connection() as conn:
                cur = conn.cursor()
                plan = load_plan(cur, today)
                if plan is None or has_checkin(cur, today):
                    return
                if logged_set_count(cur, plan["plan_id"]) > 0:
                    return
                text = nudge_text(plan)
            if text:
                self._post(config.CHANNEL_OPS, text, tier="health")
                logger.info("Posted check-in nudge for %s", today)
        except Exception:
            logger.exception("Check-in nudge failed")

    def job_pain_pattern_recompute(self):
        """21:55 local — refresh health.pain_pattern. Silent: posts nothing."""
        try:
            from artemis.health_patterns import recompute
            from knowledge.db import get_connection

            with get_connection() as conn:
                rows = recompute(conn.cursor(), _local_today())
            logger.info("Pain patterns recomputed: %d row(s), %d candidate(s)",
                        len(rows), sum(1 for r in rows if r["qualifies"]))
        except Exception:
            logger.exception("Pain pattern recompute failed")

    def job_health_review(self):
        """Sun 08:30 local (weekend open) — one post per new or changed open pain pattern.

        Posts only in the OPEN phase and only when something is new; replies
        in each post's thread become reflections (main._handle_pattern_thread).
        """
        if not self._is_open():
            logger.info("Health review: phase not open — skipping")
            return
        try:
            from artemis.health_patterns import mark_surfaced, recompute, render, to_surface
            from knowledge.db import get_connection

            now = datetime.now(timezone.utc)
            with get_connection() as conn:
                cur = conn.cursor()
                rows = to_surface(recompute(cur, _local_today(), now))
                for row in rows:
                    resp = self.mm.post_message(config.CHANNEL_OPS, render(row))
                    mark_surfaced(cur, row, (resp or {}).get("id"), now)
            if rows:
                logger.info("Health review posted %d pattern(s)", len(rows))
        except Exception:
            logger.exception("Health review failed")

    def job_health_evening_prompt(self):
        """Wed/Sat 16:30 CT — pre-workout prompt with location + equipment.

        Idempotent per day. Skips if quiet hours active or no plan row exists.
        """
        if self._is_quiet():
            return
        try:
            from artemis.health import (
                already_prompted_today, build_evening_prompt,
                get_today_plan, mark_prompted, resolve_equipment_and_location,
            )
            from artemis.weather import get_current_conditions

            today = _local_today()
            slot = "evening"
            if already_prompted_today(slot, today):
                return

            plan = get_today_plan()
            if not plan:
                return

            session_type = plan.get("session_type", "")
            # HEALTH-2: weather only matters for an outdoor walk; cardio is at the
            # office gym and location comes from the plan row's blocks.
            weather = get_current_conditions() if session_type == "walk" else None

            resolved = resolve_equipment_and_location(
                session_type, weather=weather, blocks=plan.get("blocks"),
            )
            text = build_evening_prompt(plan, resolved)
            self._post(config.CHANNEL_OPS, text, tier="health")
            mark_prompted(slot, today)
            logger.info("Posted evening prompt for %s", today)
        except Exception:
            logger.exception("Health evening prompt failed")

    def job_quiet_hours_start(self):
        """Enter quiet hours and announce."""
        try:
            from artemis.quiet_hours import enter_quiet, get_quiet_state

            # Don't override a manual goodnight that's already active
            state = get_quiet_state()
            if state.get("manual_override") and state.get("is_quiet"):
                return  # Already quiet via manual goodnight

            announcement = enter_quiet(manual=False)
            # The boundary post itself — still open for this instant.
            self.mm.post_message(config.CHANNEL_OPS, announcement)
        except Exception:
            logger.exception("Quiet hours start failed")

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

    def job_override_expiry_check(self):
        """Check if working session override has expired due to inactivity."""
        try:
            from artemis.quiet_hours import check_override_expiry

            announcement = check_override_expiry()
            if announcement:
                self._post(config.CHANNEL_OPS, announcement)
        except Exception:
            logger.debug("Override expiry check failed", exc_info=True)

    def job_action_item_reminders(self):
        """Remind about pending action items, with backoff + caps, and auto-expire.

        POLISH-1 P7 - the observed bug reminded one item 27-28x (~every 3h for
        3.5 days, overnight included). The chain now escalates and caps instead of
        nagging on a flat 2h cadence:

          * quiet hours are never pinged (the early return below);
          * at most MAX_REMINDERS_PER_DAY reminders per CT day per item;
          * spacing backs off with each reminder: 4h -> 8h -> daily -> every 3 days
            (REMINDER_BACKOFF_HOURS, keyed by reminders already sent);
          * after MAX_REMINDERS ignored reminders it STOPS pinging and the item is
            demoted to the morning brief's "Stale items" line.

        The budget/cap/backoff decision is morning_brief.reminder_due(); the
        demotion surface is morning_brief.stale_action_items() (rendered into the
        brief). Both share the policy constants so the loop and the brief agree.

        Per-item, per-day state lives in the existing acos.action_items row -
        reminder_count / last_reminded_at plus a small counter in the metadata JSONB
        (reminders_today / reminders_today_date). No new table.
        """
        if not self._is_open():
            return
        try:
            import json as _json
            from knowledge.db import execute_query, execute_write

            today_iso = _local_today().isoformat()

            # Fetch all live pending items; the send/skip decision (backoff, daily
            # cap, demotion) is made per item below where the counter is visible.
            pending = execute_query("""
                SELECT id, item_type, title, created_at, reminder_count, priority,
                       last_reminded_at, metadata
                FROM acos.action_items
                WHERE status = 'pending'
                  AND (snoozed_until IS NULL OR snoozed_until < now())
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
                    self._post(
                        config.CHANNEL_OPS,
                        f"\u23f0 **Expired:** {item['title']} (no action after 7 days)",
                    )
                    continue

                sent_count = item["reminder_count"] or 0
                meta = item["metadata"] if isinstance(item["metadata"], dict) else \
                    _json.loads(item["metadata"] or "{}")

                # Reminders already sent on the current CT day (daily cap input).
                reminders_today = 0
                if meta.get("reminders_today_date") == today_iso:
                    reminders_today = int(meta.get("reminders_today", 0) or 0)

                # Budget / daily-cap / backoff decision (pure, tested in test_polish1).
                # Demoted items (sent_count >= MAX_REMINDERS) fall out here and are
                # surfaced instead by stale_action_items() in the morning brief.
                last = item["last_reminded_at"]
                if not morning_brief.reminder_due(
                    sent_count=sent_count, reminders_today=reminders_today,
                    last_reminded_at=last,
                    now=(datetime.now(last.tzinfo) if last is not None else None),
                ):
                    continue

                # Post reminder
                age_str = f"{age.days}d {age.seconds // 3600}h" if age.days else f"{age.seconds // 3600}h"
                priority_tag = " \U0001f534" if item["priority"] == "high" else ""
                self._post(
                    config.CHANNEL_OPS,
                    f"\u23f0 **Pending action{priority_tag}:** {item['title']}\n"
                    f"Waiting since: {age_str} ago (reminded {sent_count}x)\n"
                    f"\u2705 `approve sched {item_id}` · "
                    f"\u274c `skip sched {item_id}` · "
                    f"\U0001f4a4 `snooze sched {item_id}`",
                )
                meta_update = _json.dumps({
                    "reminders_today": reminders_today + 1,
                    "reminders_today_date": today_iso,
                })
                execute_write(
                    """UPDATE acos.action_items
                       SET reminder_count = reminder_count + 1,
                           last_reminded_at = now(), updated_at = now(),
                           metadata = COALESCE(metadata, '{}'::jsonb) || %s::jsonb
                       WHERE id = %s""",
                    (meta_update, item["id"]),
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
                        # Rubric-assigned state drives tracking and the archive gate.
                        state = self._state_from_triage(item)
                        upsert_thread(orig["thread_id"], orig["subject"], orig.get("from_email", ""), state=state)
                        emails_processed += 1

                        # Execute playbooks
                        playbook_match = item.get("playbook_match")
                        if playbook_match:
                            body = self.gmail.get_full_message(orig["id"])
                            if body:
                                orig["full_body"] = body
                            self._execute_playbook(playbook_match, orig, item)
                            playbooks_fired += 1

                        # State-conditional archive gate (NEEDS_ACTION stays in INBOX)
                        self._archive_for_state(orig["id"], state, orig.get("subject", ""))
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
            self._post(
                config.CHANNEL_OPS,
                f"\U0001f504 Catch-up complete \u2014 processed {emails_processed} emails and "
                f"{commitment_checks} commitment checks since last run ({gap_str} ago). "
                f"{playbooks_fired} playbooks fired.",
            )
        else:
            self._post(
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
                self._post(config.CHANNEL_OPS, "\n".join(lines))
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
                self._post(
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
                self._post(
                    config.CHANNEL_OPS,
                    f"\u26a0\ufe0f Calendar API has failed 3 times \u2014 check credentials. "
                    f"Last error: {error[:300]}",
                )
            except Exception:
                logger.exception("Failed to post Calendar failure alert")
