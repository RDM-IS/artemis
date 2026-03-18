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

    def authenticate(self):
        creds = None
        token_path = config.CALENDAR_TOKEN_PATH
        creds_path = config.CALENDAR_CREDENTIALS_PATH

        if token_path.exists():
            creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), SCOPES)
                creds = flow.run_local_server(port=0)
            token_path.write_text(creds.to_json())

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

        self.service = build("calendar", "v3", credentials=creds)
        logger.info("Calendar authenticated")

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

    def create_event(
        self,
        summary: str,
        start_datetime: datetime,
        end_datetime: datetime,
        description: str | None = None,
        attendees: list[str] | None = None,
        reminder_minutes: int = 15,
    ) -> str | None:
        """Create a calendar event.

        Returns the event ID on success, or None on failure.
        """
        if not self.service:
            logger.error("Calendar not authenticated — cannot create event")
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
