"""Google Calendar client — meeting detection."""

import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from artemis import config

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

        Args:
            mm_client: Optional MattermostClient to post auth failure alerts.
        """
        creds = None
        token_path = config.CALENDAR_TOKEN_PATH
        creds_path = config.CALENDAR_CREDENTIALS_PATH

        if token_path.exists():
            creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

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
                flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), SCOPES)
                creds = flow.run_local_server(port=0)

        # Persist refreshed token
        try:
            token_path.write_text(creds.to_json())
        except Exception:
            logger.warning("Failed to persist Calendar token to %s", token_path)

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
                config.CALENDAR_TOKEN_PATH.write_text(creds.to_json())
                logger.debug("Calendar token refreshed and saved")
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
