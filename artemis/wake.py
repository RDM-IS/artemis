"""The 04:30 wake post (WAKE-1 §F).

Exactly five things, and nothing else:

  1. today's workout (session, duration, location, equipment, first lift, warmup)
  2. the morning check-in prompt (PB-009 survey)
  3. held health notices (e.g. an overnight ramp slide)
  4. pre-departure (checklist, first event time, weather, `depart:` commitments)
  5. a one-line header when a timezone override is active

Explicitly NOT here: email, inbox, triage, other meetings, general commitments,
action items, vault digest, ops alerts. Those wait for 06:30 (job_open).
"""

import logging

from artemis import config
from artemis.quiet_hours import get_timezone_override, local_now, local_today

logger = logging.getLogger(__name__)

_LIGHT_SESSIONS = ("rest_mobility", "walk")


def _override_header() -> str | None:
    row = get_timezone_override()
    if not row:
        return None
    label = (row.get("city_name") or row.get("timezone") or "").title()
    try:
        from zoneinfo import ZoneInfo
        from datetime import timedelta
        through = (row["expires_at"].astimezone(ZoneInfo(row["timezone"])).date()
                   - timedelta(days=1))
        return f"\U0001f30d {label} time · through {through.strftime('%b %d')}"
    except Exception:
        return f"\U0001f30d {label} time"


def _mmss(sec: int) -> str:
    m, s = divmod(int(sec), 60)
    return f"{m} min" if not s else f"{m}:{s:02d}"


def flow_lines(blocks: dict, name: str = "Recovery Flow") -> list[str]:
    """YOGA-1 — plan-exact Recovery Flow: every step with side and hold, the
    round-2 doubling, and the total. Read only from the plan row."""
    from artemis.health_office import flow_step_holds, flow_total_sec

    total = flow_total_sec(blocks)
    where = f" ({blocks['location']})" if blocks.get("location") else ""
    out = [f"\U0001f9d8 Today: **{name}**{where} — {_mmss(total)} total, hands-free."]
    for p in blocks.get("pre") or []:
        out.append(f"· {p['name']} — {_mmss(p['duration_sec'])}: {p.get('cue') or ''}".rstrip(": "))
    r1 = flow_step_holds(blocks, 1)
    steps = []
    for st, hold in zip(blocks.get("flow") or [], r1):
        side = f" {st['side']}" if st.get("side") else ""
        steps.append(f"{st['name']}{side} {hold}s")
    rounds = int(blocks.get("rounds") or 1)
    out.append(f"· Round 1 ({_mmss(sum(r1))}): " + " · ".join(steps))
    for r in range(2, rounds + 1):
        hr = flow_step_holds(blocks, r)
        doubled = [st["name"] for st, a, b in zip(blocks["flow"], r1, hr) if b != a]
        extra = (f"; {doubled[0].lower()} → {doubled[-1].lower()} held 2×" if doubled else "")
        out.append(f"· Round {r} ({_mmss(sum(hr))}): same order{extra}")
    close = blocks.get("close")
    if close:
        out.append(f"· Close: {close['name'].lower()} — {_mmss(close['duration_sec'])}")
    return out


def _workout_section(plan: dict | None) -> list[str]:
    """Today's session. Light days (rest/walk) get one line."""
    from artemis.health import (
        _session_pretty_name,
        resolve_equipment_and_location,
    )

    if not plan:
        return ["\U0001f3cb️ No workout on the calendar today."]

    blocks = plan.get("blocks") or {}
    if isinstance(blocks, str):
        import json
        try:
            blocks = json.loads(blocks)
        except (ValueError, TypeError):
            blocks = {}
    session_type = plan.get("session_type", "")
    name = blocks.get("display_name") or _session_pretty_name(session_type)
    duration = plan.get("est_duration_min")
    duration_str = f" — {duration} min" if duration else ""

    if blocks.get("type") == "recovery_flow":
        return flow_lines(blocks, name)

    if session_type in _LIGHT_SESSIONS:
        note = blocks.get("notes") or blocks.get("setup_notes") or ""
        if isinstance(note, list):
            note = "; ".join(str(n) for n in note)
        tail = f" · {note}" if note else ""
        return [f"\U0001f3cb️ Today: **{name}**{duration_str}.{tail}"]

    weather = None
    if session_type == "walk":  # pragma: no cover - walk is a light session
        from artemis.weather import get_current_conditions
        weather = get_current_conditions()
    resolved = resolve_equipment_and_location(
        session_type, weather=weather, blocks=blocks,
    )

    from artemis.health_checkin import render_plan_lines

    lines = [f"\U0001f3cb️ Today: **{name}**{duration_str}.", f"Where: {resolved['location']}"]
    if resolved.get("equipment"):
        lines.append(f"Uses: {', '.join(resolved['equipment'])}")
    if blocks.get("warmup"):
        lines.append(f"Warmup: {blocks['warmup']}")
    # Plan-exact: every exercise, set, rep, RPE and load comes from the row.
    lines.extend(render_plan_lines({**plan, "blocks": blocks}))
    if blocks.get("cooldown"):
        lines.append(f"Cooldown: {blocks['cooldown']}")
    if resolved.get("notes"):
        lines.append(f"_{resolved['notes']}_")
    lines.append("gym.rdm.is is up — full plan there.")
    return lines


def prompt_type_for(plan: dict | None) -> str:
    """Survey variant from the PLAN, never the day of week (WAKE-1 §F.2).
    A Recovery Flow (YOGA-1) is not a workout-later day either."""
    if not plan:
        return "logging_only"
    blocks = plan.get("blocks")
    flow = isinstance(blocks, dict) and blocks.get("type") == "recovery_flow"
    if flow or plan.get("session_type") in _LIGHT_SESSIONS + ("recovery_flow",):
        return "logging_only"
    return "workout_am"


def _checkin_section(plan: dict | None) -> list[str]:
    from artemis.health import build_morning_survey_prompt

    if not plan:
        return []
    return ["", build_morning_survey_prompt(plan, prompt_type_for(plan))]


def _first_event_line(calendar) -> str | None:
    if not calendar or not getattr(calendar, "service", None):
        return None
    try:
        events = calendar.get_today_events()
    except Exception:
        logger.debug("wake: calendar unavailable", exc_info=True)
        return None
    if not events:
        return None
    first = events[0]
    start = first.get("start", "")
    if "T" in start:
        try:
            from datetime import datetime
            display = datetime.fromisoformat(start).strftime("%I:%M %p").lstrip("0")
        except ValueError:
            display = start
    else:
        display = "all day"
    return f"First event: {display}"


def _weather_line() -> str | None:
    """Today's high/low + precipitation for the ACTIVE location (override city
    when one is set, else home). None when weather is unavailable."""
    from artemis import weather as weather_mod

    f = weather_mod.get_today_forecast()
    if not f.get("available"):
        return None
    bits = []
    if f.get("high_f") is not None and f.get("low_f") is not None:
        bits.append(f"{f['high_f']}°/{f['low_f']}°F")
    if f.get("summary"):
        bits.append(str(f["summary"]))
    if f.get("precip_chance") is not None:
        precip = f"{f['precip_chance']}% precip"
        if f.get("precip_in"):
            precip += f" ({f['precip_in']:.2f} in)"
        bits.append(precip)
    return "Weather: " + " · ".join(bits) if bits else None


def _depart_commitments() -> list[str]:
    """acos.commitments due today whose title starts with `depart:`."""
    from knowledge.db import execute_query
    from artemis.quiet_hours import get_active_timezone

    try:
        rows = execute_query(
            "SELECT title FROM acos.commitments "
            "WHERE status = 'active' "
            "  AND due_date = (now() AT TIME ZONE %s)::date "
            "  AND title ILIKE 'depart:%%' "
            "ORDER BY id",
            (get_active_timezone(),),
        )
    except Exception:
        logger.debug("wake: depart commitments query failed", exc_info=True)
        return []
    out = []
    for r in rows:
        title = (r.get("title") or "").split(":", 1)[-1].strip()
        if title:
            out.append(f"· {title}")
    return out


OFFICE_LOCATION = "office gym"   # health_office.LOCATION


def _is_office_day(plan: dict | None) -> bool:
    """A training day at the office gym (plan blocks.location). Rest days never
    are, even when the row carries office blocks."""
    if not plan or plan.get("session_type") == "rest_mobility":
        return False
    blocks = plan.get("blocks") or {}
    if isinstance(blocks, str):
        import json
        try:
            blocks = json.loads(blocks)
        except ValueError:
            return False
    return blocks.get("location") == OFFICE_LOCATION


def _departure_section(calendar, plan: dict | None = None) -> list[str]:
    """Office days get the DEPARTURE_CHECKLIST; home, outside and rest days
    don't. The first event, weather and `depart:` commitments show on any day.
    Nothing to say -> no block at all."""
    lines = []
    if _is_office_day(plan) and config.DEPARTURE_CHECKLIST:
        lines.append("· " + ", ".join(config.DEPARTURE_CHECKLIST))
    first_event = _first_event_line(calendar)
    if first_event:
        lines.append(f"· {first_event}")
    weather = _weather_line()
    if weather:
        lines.append(f"· {weather}")
    lines.extend(_depart_commitments())
    return ["", "**Before you leave**", *lines] if lines else []


def build_wake_message(calendar=None, held_health: list[str] | None = None) -> str:
    """Compose the 04:30 post. `held_health` are notices held overnight."""
    from artemis.health import get_today_plan

    try:
        plan = get_today_plan()
    except Exception:
        logger.exception("wake: failed to read today's plan")
        plan = None

    now = local_now().strftime("%a %b %d")
    lines = [f"\U0001f305 **{now}** — {local_today().isoformat()}"]
    header = _override_header()
    if header:
        lines.append(header)
    lines.append("")
    lines.extend(_workout_section(plan))
    lines.extend(_checkin_section(plan))
    for notice in held_health or []:
        lines.extend(["", notice])
    lines.extend(_departure_section(calendar, plan))
    return "\n".join(lines)
