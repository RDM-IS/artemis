"""Google Calendar client — meeting detection."""

import json
import logging
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from artemis import config
from knowledge.secrets import get_gmail_credentials, get_calendar_token, put_secret

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/calendar.events",
]


class CalendarClient:
    def __init__(self):
        self.service = None
        self.scope_mismatch: bool = False

    def authenticate(self, mm_client=None):
        """Authenticate with Google Calendar API.

        Loads OAuth token from Secrets Manager (rdmis/dev/calendar-token).
        On refresh, writes the updated token back to Secrets Manager.

        Args:
            mm_client: Optional MattermostClient to post auth failure alerts.
        """
        creds = None

        # Load token from Secrets Manager
        try:
            token_data = get_calendar_token()
            creds = Credentials.from_authorized_user_info(token_data, SCOPES)
        except Exception:
            logger.debug("No Calendar token in Secrets Manager — will attempt interactive flow")

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                except Exception as exc:
                    logger.error("Calendar token refresh failed: %s", exc)
                    if mm_client:
                        try:
                            mm_client.post_message(
                                config.CHANNEL_OPS,
                                "\U0001f510 Calendar authentication expired — manual re-authentication "
                                "required. Run: `python setup_oauth.py`",
                            )
                        except Exception:
                            logger.debug("Failed to post auth alert to Mattermost")
                    # Continue with degraded mode — no Calendar
                    self.service = None
                    return
            else:
                # Interactive flow — local dev only (won't work on Lambda/EC2)
                client_config = get_gmail_credentials()
                flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
                creds = flow.run_local_server(port=0)

        # Persist refreshed token to Secrets Manager
        try:
            put_secret("rdmis/dev/calendar-token", json.loads(creds.to_json()))
        except Exception:
            logger.warning("Failed to persist Calendar token to Secrets Manager")

        # Validate scopes — warn but don't crash
        self.scope_mismatch = False
        granted = set(creds.scopes or []) if creds else set()
        required = {
            "https://www.googleapis.com/auth/calendar.readonly",
            "https://www.googleapis.com/auth/calendar.events",
        }
        if granted and not required.issubset(granted):
            missing = required - granted
            logger.warning(
                "Calendar token missing scopes: %s — re-authenticate.",
                ", ".join(missing),
            )
            self.scope_mismatch = True

        self._creds = creds
        self.service = build("calendar", "v3", credentials=creds)
        logger.info("Calendar authenticated")

    def _refresh_if_needed(self) -> bool:
        """Refresh credentials if expired and re-save token. Returns True if valid."""
        creds = getattr(self, "_creds", None)
        if not creds:
            return bool(self.service)
        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                put_secret("rdmis/dev/calendar-token", json.loads(creds.to_json()))
                logger.debug("Calendar token refreshed and saved to Secrets Manager")
            except Exception:
                logger.exception("Calendar token refresh failed mid-session")
                return False
        return True

    def get_today_events(self) -> list[dict]:
        """Get all events for today."""
        if not self.service:
            logger.error("Calendar not authenticated")
            return []

        local_tz = ZoneInfo(config.TIMEZONE)
        now = datetime.now(local_tz)
        start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end_of_day = start_of_day + timedelta(days=1)

        try:
            result = (
                self.service.events()
                .list(
                    calendarId="primary",
                    timeMin=start_of_day.isoformat(),
                    timeMax=end_of_day.isoformat(),
                    singleEvents=True,
                    orderBy="startTime",
                )
                .execute()
            )
        except Exception:
            logger.exception("Failed to fetch calendar events")
            return []

        events = []
        for event in result.get("items", []):
            attendees = event.get("attendees", [])
            start = event.get("start", {})
            start_time = start.get("dateTime", start.get("date", ""))

            events.append({
                "id": event["id"],
                "summary": event.get("summary", "(no title)"),
                "start": start_time,
                "attendees": [
                    {
                        "email": a["email"],
                        "name": a.get("displayName", ""),
                        "self": a.get("self", False),
                        "response": a.get("responseStatus", ""),
                    }
                    for a in attendees
                ],
                "description": event.get("description", ""),
                "location": event.get("location", ""),
            })

        return events

    def get_events_in_range(self, start_date, end_date) -> list[dict]:
        """Get all events between start_date and end_date (inclusive).

        Accepts date or datetime objects. Returns same dict shape as get_today_events()
        plus an 'end' field for duration-aware processing.
        """
        from datetime import date as date_type

        if not self.service:
            logger.error("Calendar not authenticated")
            return []

        local_tz = ZoneInfo(config.TIMEZONE)

        # Normalize to datetime at start/end of day
        if isinstance(start_date, date_type) and not isinstance(start_date, datetime):
            start_dt = datetime(start_date.year, start_date.month, start_date.day, tzinfo=local_tz)
        else:
            start_dt = start_date if start_date.tzinfo else start_date.replace(tzinfo=local_tz)

        if isinstance(end_date, date_type) and not isinstance(end_date, datetime):
            end_dt = datetime(end_date.year, end_date.month, end_date.day, 23, 59, 59, tzinfo=local_tz)
        else:
            end_dt = end_date if end_date.tzinfo else end_date.replace(tzinfo=local_tz)

        try:
            result = (
                self.service.events()
                .list(
                    calendarId="primary",
                    timeMin=start_dt.isoformat(),
                    timeMax=end_dt.isoformat(),
                    singleEvents=True,
                    orderBy="startTime",
                )
                .execute()
            )
        except Exception:
            logger.exception("Failed to fetch events for range %s to %s", start_date, end_date)
            return []

        events = []
        for event in result.get("items", []):
            attendees = event.get("attendees", [])
            start = event.get("start", {})
            end = event.get("end", {})
            events.append({
                "id": event["id"],
                "summary": event.get("summary", "(no title)"),
                "start": start.get("dateTime", start.get("date", "")),
                "end": end.get("dateTime", end.get("date", "")),
                "attendees": [
                    {
                        "email": a.get("email", ""),
                        "name": a.get("displayName", ""),
                        "self": a.get("self", False),
                        "response": a.get("responseStatus", ""),
                    }
                    for a in attendees
                ],
                "description": event.get("description", ""),
                "location": event.get("location", ""),
            })

        return events

    def get_upcoming_with_externals(self, within_minutes: int | None = None) -> list[dict]:
        """Get upcoming events that have external attendees.

        If within_minutes is set, only return events starting within that window.
        """
        events = self.get_today_events()
        now = datetime.now(timezone.utc)
        result = []

        for event in events:
            # Filter to events with non-self attendees (external)
            external = [a for a in event["attendees"] if not a["self"]]
            if not external:
                continue

            if within_minutes is not None:
                try:
                    event_start = datetime.fromisoformat(event["start"])
                    if event_start.tzinfo is None:
                        continue
                    diff = (event_start - now).total_seconds() / 60
                    if diff < 0 or diff > within_minutes:
                        continue
                except (ValueError, TypeError):
                    continue

            event["external_attendees"] = external
            result.append(event)

        return result

    def get_events_around(self, target_datetime: datetime, window_hours: int = 2) -> list[dict]:
        """Get events within ±window_hours of target_datetime for conflict detection."""
        if not self.service:
            return []

        local_tz = ZoneInfo(config.TIMEZONE)
        if target_datetime.tzinfo is None:
            target_datetime = target_datetime.replace(tzinfo=local_tz)

        time_min = target_datetime - timedelta(hours=window_hours)
        time_max = target_datetime + timedelta(hours=window_hours)

        try:
            result = (
                self.service.events()
                .list(
                    calendarId="primary",
                    timeMin=time_min.isoformat(),
                    timeMax=time_max.isoformat(),
                    singleEvents=True,
                    orderBy="startTime",
                )
                .execute()
            )
        except Exception:
            logger.exception("Failed to fetch events for conflict check")
            return []

        events = []
        for event in result.get("items", []):
            attendees = event.get("attendees", [])
            start = event.get("start", {})
            start_time = start.get("dateTime", start.get("date", ""))
            events.append({
                "id": event["id"],
                "summary": event.get("summary", "(no title)"),
                "start": start_time,
                "attendees": [
                    {
                        "email": a.get("email", ""),
                        "name": a.get("displayName", ""),
                        "self": a.get("self", False),
                    }
                    for a in attendees
                ],
            })

        return events

    def delete_event(self, event_id: str) -> bool:
        """Delete a calendar event by ID. Returns True on success."""
        if not self.service:
            logger.error("Calendar not authenticated — cannot delete event")
            return False
        try:
            self.service.events().delete(calendarId="primary", eventId=event_id).execute()
            logger.info("Deleted calendar event %s", event_id)
            return True
        except Exception:
            logger.exception("Failed to delete calendar event %s", event_id)
            return False

    def get_event(self, event_id: str) -> dict | None:
        """Get a single event by ID. Returns dict or None."""
        if not self.service:
            return None
        try:
            event = self.service.events().get(calendarId="primary", eventId=event_id).execute()
            start = event.get("start", {})
            return {
                "id": event["id"],
                "summary": event.get("summary", "(no title)"),
                "start": start.get("dateTime", start.get("date", "")),
                "attendees": [
                    {
                        "email": a.get("email", ""),
                        "name": a.get("displayName", ""),
                        "self": a.get("self", False),
                    }
                    for a in event.get("attendees", [])
                ],
            }
        except Exception:
            logger.exception("Failed to get event %s", event_id)
            return None

    def find_event_by_name(self, name: str, days_ahead: int = 7) -> dict | None:
        """Search today + days_ahead days for an event matching name (case-insensitive)."""
        from datetime import date, timedelta
        start = date.today()
        end = start + timedelta(days=days_ahead)
        events = self.get_events_in_range(start, end)
        name_lower = name.lower()
        for e in events:
            if name_lower in e["summary"].lower():
                return e
        return None

    def create_event(
        self,
        summary: str,
        start_datetime: datetime,
        end_datetime: datetime,
        description: str | None = None,
        attendees: list[str] | None = None,
        reminder_minutes: int = 15,
        _user_approved_external: bool = False,
    ) -> str | None:
        """Create a calendar event.

        Returns the event ID on success, or None on failure.

        HARD GUARDRAIL: If attendees contains any external email (not @rdm.is
        or @gmail.com), creation is BLOCKED unless _user_approved_external=True.
        This cannot be disabled by any config, env var, or mode.
        """
        if not self.service:
            logger.error("Calendar not authenticated — cannot create event")
            return None

        # ── HARD GUARDRAIL: External attendee check ──
        from artemis.guardrails import check_external_attendees
        check = check_external_attendees(
            summary, attendees, user_approved=_user_approved_external
        )
        if not check["allowed"]:
            logger.error("GUARDRAIL BLOCKED: %s", check["reason"])
            return None

        local_tz = ZoneInfo(config.TIMEZONE)

        # Ensure datetimes are timezone-aware
        if start_datetime.tzinfo is None:
            start_datetime = start_datetime.replace(tzinfo=local_tz)
        if end_datetime.tzinfo is None:
            end_datetime = end_datetime.replace(tzinfo=local_tz)

        body: dict = {
            "summary": summary,
            "start": {
                "dateTime": start_datetime.isoformat(),
                "timeZone": config.TIMEZONE,
            },
            "end": {
                "dateTime": end_datetime.isoformat(),
                "timeZone": config.TIMEZONE,
            },
            "reminders": {
                "useDefault": False,
                "overrides": [{"method": "popup", "minutes": reminder_minutes}],
            },
        }

        if description:
            body["description"] = description

        if attendees:
            body["attendees"] = [{"email": email} for email in attendees]

        try:
            event = (
                self.service.events()
                .insert(calendarId="primary", body=body)
                .execute()
            )
            event_id = event.get("id", "")
            logger.info("Created calendar event %s: %s", event_id, summary)
            return event_id
        except Exception:
            logger.exception("Failed to create calendar event: %s", summary)
            return None

    def format_events_for_brief(self, events: list[dict]) -> str:
        """Format events for inclusion in a morning brief."""
        if not events:
            return "No meetings scheduled today."

        lines = []
        for e in events:
            attendee_names = [
                a["name"] or a["email"]
                for a in e.get("external_attendees", e["attendees"])
                if not a.get("self")
            ]
            attendee_str = ", ".join(attendee_names) if attendee_names else "(solo)"
            lines.append(f"- **{e['summary']}** at {e['start']} — {attendee_str}")
        return "\n".join(lines)

    def find_free_blocks(
        self,
        duration_minutes: int,
        days_ahead: int = 5,
        max_results: int = 3,
        business_hours_only: bool = True,
        date_constraint: date | None = None,
        buffer_minutes: int = 0,
    ) -> list[dict]:
        """Find free blocks in the calendar for scheduling.

        Returns up to max_results blocks as:
        [{"start": datetime, "end": datetime, "date_label": "Tue Mar 31",
          "time_label": "10:00 AM CT"}]

        Business hours: 9 AM - 5 PM CT. 15 min buffer around events.
        Skips weekends.

        date_constraint: if set, only return blocks on that specific date.
        buffer_minutes: extra clear time required before and after the meeting
            slot (e.g. travel buffer). The calendar must be free for
            buffer_minutes + duration_minutes + buffer_minutes, but only the
            middle duration_minutes is returned as the offered slot.
        """
        if not self.service:
            logger.error("Calendar not authenticated")
            return []

        local_tz = ZoneInfo(config.TIMEZONE)
        today = date.today()
        tomorrow = today + timedelta(days=1)

        # Fetch events for the search window
        end_date = today + timedelta(days=days_ahead + 1)
        events = self.get_events_in_range(tomorrow, end_date)

        # Parse event times into (start, end) datetime pairs
        busy = []
        buffer = timedelta(minutes=15)
        for e in events:
            try:
                e_start = datetime.fromisoformat(e["start"])
                e_end = datetime.fromisoformat(e["end"])
                if e_start.tzinfo is None:
                    e_start = e_start.replace(tzinfo=local_tz)
                if e_end.tzinfo is None:
                    e_end = e_end.replace(tzinfo=local_tz)
                busy.append((e_start - buffer, e_end + buffer))
            except (ValueError, KeyError):
                continue

        busy.sort(key=lambda x: x[0])
        meeting_dur = timedelta(minutes=duration_minutes)
        travel_buf = timedelta(minutes=buffer_minutes)
        total_needed = meeting_dur + travel_buf * 2
        blocks = []

        for day_offset in range(1, days_ahead + 1):
            check_date = today + timedelta(days=day_offset)

            # If a specific date was requested, skip all other dates
            if date_constraint and check_date != date_constraint:
                continue

            # Skip weekends
            if check_date.weekday() >= 5:
                continue

            if business_hours_only:
                day_start = datetime(
                    check_date.year, check_date.month, check_date.day,
                    9, 0, tzinfo=local_tz,
                )
                day_end = datetime(
                    check_date.year, check_date.month, check_date.day,
                    17, 0, tzinfo=local_tz,
                )
            else:
                day_start = datetime(
                    check_date.year, check_date.month, check_date.day,
                    8, 0, tzinfo=local_tz,
                )
                day_end = datetime(
                    check_date.year, check_date.month, check_date.day,
                    20, 0, tzinfo=local_tz,
                )

            # Walk through the day in 30-min increments
            cursor = day_start
            while cursor + total_needed <= day_end:
                window_end = cursor + total_needed
                # Check overlap with any busy period
                conflict = False
                for b_start, b_end in busy:
                    if cursor < b_end and window_end > b_start:
                        conflict = True
                        # Jump cursor past this busy block
                        cursor = b_end
                        break
                if conflict:
                    continue

                # Offer only the middle meeting slot (after travel buffer)
                slot_start = cursor + travel_buf
                slot_end = slot_start + meeting_dur
                blocks.append({
                    "start": slot_start,
                    "end": slot_end,
                    "date_label": slot_start.strftime("%a %b %-d"),
                    "time_label": slot_start.strftime("%-I:%M %p CT"),
                })
                if len(blocks) >= max_results:
                    return blocks

                # Advance by 30 min to find next slot
                cursor += timedelta(minutes=30)

        return blocks
