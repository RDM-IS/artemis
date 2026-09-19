#!/usr/bin/env python3.11
"""Export a training report as Markdown + plain HTML (EXPORT-1).

The stop-gap ahead of REPORT-1: same contents, no WeasyPrint, no S3, no new
dependencies. READ-ONLY — it only SELECTs from health.plan, health.session_log,
health.daily_state and health.pain_pattern.

    /usr/bin/python3.11 scripts/export_report.py --daily 2026-09-18
    /usr/bin/python3.11 scripts/export_report.py --weekly 2026-09-16
    /usr/bin/python3.11 scripts/export_report.py --monthly 2026-09

Writes /tmp/artemis-report-<type>-<period>.md and .html; open the HTML in a
browser and print to PDF. Parts with no data source yet (watch metrics,
nutrition, the weekly evaluation) print "not yet tracked" — never omitted,
never estimated.

"Today" is the ACTIVE timezone's local date (quiet_hours.local_today()).
Rows logged_via='inferred' (the nightly no-debrief backstop) never count as a
completed session.
"""

import sys

if sys.version_info < (3, 11):
    sys.exit("export_report.py requires Python 3.11+ "
             "(run: /usr/bin/python3.11 scripts/export_report.py ...).")

import argparse
import html
import json
import os
from calendar import monthrange
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

NOT_TRACKED = "not yet tracked"
OUT_DIR = Path("/tmp")

from artemis.health_eval import EXERCISE_ALIASES  # noqa: E402  (one alias list)

FIRST_LIFT = {"strength_a": "Leg press", "strength_b": "DB goblet squat",
              "strength_c": "DB Romanian deadlift"}
REST_TYPES = {"rest_mobility"}
PROGRAM_START_FALLBACK = date(2026, 9, 16)   # health_office.WEEK1_START


# ============================================================================
# Data
# ============================================================================

@dataclass
class Data:
    start: date
    end: date
    today: date
    plans: list[dict] = field(default_factory=list)       # health.plan rows
    logs: list[dict] = field(default_factory=list)        # health.session_log (real)
    checkins: dict = field(default_factory=dict)          # date -> daily_state row
    prior_logs: list[dict] = field(default_factory=list)  # the 7 days before start
    patterns: list[dict] = field(default_factory=list)    # open pain patterns
    program: dict | None = None                           # acos.system_state health_program

    @property
    def anchor(self) -> date:
        """First day of the current program. Plan rows before it belong to an
        older program (e.g. the phase-3 home plan) and are never counted."""
        a = (self.program or {}).get("anchor")
        return date.fromisoformat(a) if a else PROGRAM_START_FALLBACK

    def program_week(self, d: date) -> int | None:
        if d < self.anchor:
            return None
        wk = (d - self.anchor).days // 7 + 1
        total = (self.program or {}).get("weeks_total")
        return min(wk, total) if total else wk


def _blocks(v) -> dict:
    if isinstance(v, str):
        return json.loads(v or "{}")
    return v or {}


def load(start: date, end: date) -> Data:
    from knowledge.db import execute_query
    from artemis.quiet_hours import local_today

    d = Data(start, end, local_today())
    d.plans = [dict(r, blocks=_blocks(r["blocks"])) for r in execute_query(
        "SELECT plan_id, plan_date, phase, week_num, session_type, blocks, target_rpe, "
        "est_duration_min FROM health.plan WHERE plan_date BETWEEN %s AND %s ORDER BY plan_date",
        (start, end))]
    log_sql = (
        "SELECT sl.log_id, p.plan_date, sl.plan_id, sl.log_type, sl.exercise, sl.set_num, "
        "sl.round_num, sl.reps_done, sl.weight_lbs, sl.rpe_actual, sl.duration_sec, "
        "sl.notes, sl.is_skipped, sl.logged_at "
        "FROM health.session_log sl JOIN health.plan p ON p.plan_id = sl.plan_id "
        "WHERE p.plan_date BETWEEN %s AND %s AND sl.logged_via <> 'inferred' "
        "ORDER BY p.plan_date, sl.logged_at, sl.log_id")
    d.logs = [dict(r) for r in execute_query(log_sql, (start, end))]
    d.prior_logs = [dict(r) for r in execute_query(
        log_sql, (start - timedelta(days=7), start - timedelta(days=1)))]
    d.checkins = {r["state_date"]: dict(r) for r in execute_query(
        "SELECT state_date, weight_lbs, sleep_hrs, energy, soreness, resting_hr, free_text "
        "FROM health.daily_state WHERE state_date BETWEEN %s AND %s ORDER BY state_date",
        (start, end))}
    d.patterns = [dict(r) for r in execute_query(
        "SELECT exercise, region, hits, exposures, last_seen FROM health.pain_pattern "
        "WHERE status = 'open' AND qualifies ORDER BY hits DESC, exercise", ())]
    rows = execute_query("SELECT value FROM acos.system_state WHERE key = %s",
                         ("health_program",))
    if rows and rows[0]["value"]:
        d.program = json.loads(rows[0]["value"])
    return d


# ============================================================================
# Derivations (pure — tested without a DB)
# ============================================================================

def canon(name: str | None) -> str | None:
    return EXERCISE_ALIASES.get(name, name) if name else name


def _num(v):
    if v is None:
        return None
    f = float(v)
    return int(f) if f == int(f) else round(f, 1)


def fmt(v, unit: str = "") -> str:
    n = _num(v)
    return "—" if n is None else f"{n}{unit}"


def logs_for(data: Data, plan_id: int) -> list[dict]:
    return [l for l in data.logs if l["plan_id"] == plan_id]


def sets_for(data: Data, plan_id: int) -> list[dict]:
    return [l for l in logs_for(data, plan_id) if l["log_type"] == "strength_set"]


def is_done(data: Data, plan: dict) -> bool:
    return any(not l["is_skipped"] for l in logs_for(data, plan["plan_id"]))


def day_status(data: Data, plan: dict) -> str:
    if plan["plan_date"] < data.anchor:
        return "pre-program"
    if plan["session_type"] in REST_TYPES:
        return "rest"
    if is_done(data, plan):
        return "done"
    if plan["plan_date"] > data.today:
        return "upcoming"
    if plan["plan_date"] == data.today:
        return "not logged yet"
    return "missed"


def label(plan: dict) -> str:
    b = plan["blocks"]
    name = b.get("display_name") or plan["session_type"]
    loc = b.get("location")
    return f"{name} ({loc})" if loc else name


def adherence(data: Data) -> tuple[int, int, int]:
    """(done, due so far, still upcoming) over training days. A day is due
    once it has passed, or today once it's logged."""
    done = due = upcoming = 0
    for p in data.plans:
        st = day_status(data, p)
        if st in ("rest", "pre-program"):
            continue
        if st == "done":
            done += 1
            due += 1
        elif st == "missed":
            due += 1
        else:
            upcoming += 1
    return done, due, upcoming


def logged_span_min(logs: list[dict]) -> int | None:
    ts = [l["logged_at"] for l in logs if l.get("logged_at")]
    if len(ts) < 2:
        return None
    return round((max(ts) - min(ts)).total_seconds() / 60)


def session_rpe(logs: list[dict]):
    s = [l["rpe_actual"] for l in logs if l["log_type"] == "session_summary" and l["rpe_actual"] is not None]
    return s[-1] if s else None


def avg_set_rpe(sets: list[dict]):
    v = [float(l["rpe_actual"]) for l in sets if l["rpe_actual"] is not None]
    return round(sum(v) / len(v), 1) if v else None


def top_weights(logs: list[dict]) -> dict[str, float]:
    """Heaviest logged weight per (canonical) exercise; bodyweight sets skipped."""
    out: dict[str, float] = {}
    for l in logs:
        if l["log_type"] != "strength_set" or l["is_skipped"] or l["weight_lbs"] is None:
            continue
        name = canon(l["exercise"])
        out[name] = max(out.get(name, 0.0), float(l["weight_lbs"]))
    return out


def exercises_logged(logs: list[dict]) -> list[str]:
    seen = []
    for l in logs:
        n = canon(l["exercise"])
        if l["log_type"] == "strength_set" and n and n not in seen:
            seen.append(n)
    return seen


def no_load_label(name: str | None) -> str:
    """Why a set has no weight: bodyweight by equipment class, else not logged."""
    from artemis.health_regions import equipment_class
    return "bodyweight" if equipment_class(name or "") == "bodyweight" else "no load logged"


def settings_in(notes: str | None) -> str | None:
    for part in (notes or "").split(";"):
        part = part.strip()
        if part.lower().startswith("setting="):
            return part.split("=", 1)[1].strip()
    return None


_SIDE_MAPS = ("pain", "sides", "pain_sides")


def _sided(region: str, sides: dict) -> str:
    side = (sides or {}).get(region)
    return f"{side} {region}" if side in ("left", "right") else region


def soreness_text(sore) -> str:
    sore = _blocks(sore)
    if not sore:
        return "—"
    parts = []
    pain = sore.get("pain") or {}
    for k, v in sore.items():
        if k in _SIDE_MAPS:
            continue
        parts.append(f"{_sided(k, sore.get('sides'))} {v}")
    for k, v in pain.items():
        parts.append(f"pain {_sided(k, sore.get('pain_sides'))} {v}")
    return ", ".join(parts) or "—"


def pain_summary(data: Data) -> list[str]:
    """Per region: days reported and the peak, from check-ins (soreness + pain)."""
    agg: dict[tuple[str, str], list[tuple[date, int]]] = {}
    for d, row in sorted(data.checkins.items()):
        sore = _blocks(row.get("soreness"))
        for k, v in sore.items():
            if k == "pain":
                for region, n in (v or {}).items():
                    if isinstance(n, int):
                        key = _sided(region, sore.get("pain_sides"))
                        agg.setdefault(("pain", key), []).append((d, n))
            elif k not in _SIDE_MAPS and isinstance(v, int) and v > 0:
                agg.setdefault(("soreness", _sided(k, sore.get("sides"))), []).append((d, v))
    lines = []
    for (kind, region), vals in sorted(agg.items()):
        peak = max(n for _, n in vals)
        days = ", ".join(f"{d:%a %-m/%-d} {n}" for d, n in vals)
        lines.append(f"{kind} — {region}: peak {peak}/5 ({days})")
    return lines


def adjustments(data: Data) -> list[str]:
    out = []
    for p in data.plans:
        adj = p["blocks"].get("adjustment")
        if isinstance(adj, dict):
            rules = ", ".join(adj.get("rules_fired") or [])
            out.append(f"{p['plan_date']:%a %-m/%-d}: {adj.get('reason') or 'adjusted'}"
                       + (f" (rules: {rules})" if rules else ""))
    return out


# ============================================================================
# Document model -> Markdown / HTML
# ============================================================================

# Blocks: ("h1"|"h2"|"h3", text) · ("p", text) · ("ul", [text]) · ("table", header, rows)

def md(blocks) -> str:
    out = []
    for b in blocks:
        kind = b[0]
        if kind in ("h1", "h2", "h3"):
            out.append("#" * int(kind[1]) + " " + b[1])
        elif kind == "p":
            out.append(b[1])
        elif kind == "ul":
            out.append("\n".join(f"- {x}" for x in b[1]) if b[1] else "- none")
        elif kind == "table":
            header, rows = b[1], b[2]
            esc = lambda c: str(c).replace("|", "\\|")  # noqa: E731
            out.append("| " + " | ".join(header) + " |")
            out.append("|" + "---|" * len(header))
            out.extend("| " + " | ".join(esc(c) for c in r) + " |" for r in rows)
        out.append("")
    return "\n".join(out).rstrip() + "\n"


_CSS = """
body{font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;
     color:#111;background:#fff;max-width:780px;margin:24px auto;padding:0 16px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:22px 0 6px;border-bottom:1px solid #ccc}
h3{font-size:15px;margin:14px 0 4px}p{margin:4px 0}ul{margin:4px 0 4px 20px;padding:0}
table{border-collapse:collapse;width:100%;margin:6px 0;font-size:13px}
th,td{border:1px solid #ccc;padding:3px 6px;text-align:left;vertical-align:top}
th{background:#f3f3f3}.nt{color:#777;font-style:italic}
@media print{body{margin:0;max-width:none}h2{break-after:avoid}table{break-inside:avoid}}
"""


def _inline(text: str) -> str:
    t = html.escape(str(text))
    # **bold** only — the one inline style the builders use.
    parts = t.split("**")
    t = "".join(f"<strong>{p}</strong>" if i % 2 else p for i, p in enumerate(parts))
    return t.replace(NOT_TRACKED, f'<span class="nt">{NOT_TRACKED}</span>')


def to_html(blocks, title: str) -> str:
    out = ["<!doctype html>", '<html lang="en"><head><meta charset="utf-8">',
           '<meta name="viewport" content="width=device-width,initial-scale=1">',
           f"<title>{html.escape(title)}</title><style>{_CSS}</style></head><body>"]
    for b in blocks:
        kind = b[0]
        if kind in ("h1", "h2", "h3"):
            out.append(f"<{kind}>{_inline(b[1])}</{kind}>")
        elif kind == "p":
            out.append(f"<p>{_inline(b[1])}</p>")
        elif kind == "ul":
            items = b[1] or ["none"]
            out.append("<ul>" + "".join(f"<li>{_inline(x)}</li>" for x in items) + "</ul>")
        elif kind == "table":
            out.append("<table><thead><tr>" + "".join(f"<th>{_inline(h)}</th>" for h in b[1])
                       + "</tr></thead><tbody>")
            out.extend("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in r) + "</tr>" for r in b[2])
            out.append("</tbody></table>")
    out.append("</body></html>")
    return "\n".join(out) + "\n"


# ============================================================================
# Builders
# ============================================================================

def _header(data: Data, title: str, generated: datetime | None) -> list:
    gen = (generated or datetime.now()).strftime("%a %b %-d %Y %H:%M")
    span = (f"{data.start:%a %b %-d}" if data.start == data.end
            else f"{data.start:%a %b %-d} – {data.end:%a %b %-d %Y}")
    blocks = [("h1", title), ("p", f"{span} · data through {data.today:%a %b %-d} · generated {gen}")]
    if data.end > data.today:
        blocks.append(("p", "**Partial period** — days after today are shown as upcoming."))
    return blocks


def _pre_program_note(data: Data) -> str:
    n = sum(1 for p in data.plans if p["plan_date"] < data.anchor)
    return (f" · {n} day(s) before the program start ({data.anchor:%-m/%-d}) not counted"
            if n else "")


def _set_table(sets: list[dict]) -> tuple:
    rows = []
    for l in sets:
        name = l["exercise"] or "—"
        if canon(name) != name:
            name = f"{name} *"
        rows.append([name, fmt(l["set_num"]), fmt(l["weight_lbs"], " lb") if l["weight_lbs"] is not None
                     else no_load_label(canon(l["exercise"])), fmt(l["reps_done"]), fmt(l["rpe_actual"]),
                     "skipped" if l["is_skipped"] else (l["notes"] or "")])
    return ("table", ["Exercise", "Set", "Weight", "Reps", "Effort (RPE)", "Notes"], rows)


def _alias_note(logs) -> list:
    old = sorted({l["exercise"] for l in logs if l["exercise"] and canon(l["exercise"]) != l["exercise"]})
    return [("p", f"\\* {n}: logged before the equipment correction; counted as "
                  f"{canon(n)} for comparisons.") for n in old]


def _checkin_line(row: dict | None) -> str:
    if not row:
        return "no check-in"
    return (f"sleep {fmt(row['sleep_hrs'], ' h')} · energy {fmt(row['energy'])}/5 · "
            f"soreness {soreness_text(row['soreness'])} · weight {fmt(row['weight_lbs'], ' lb')} · "
            f"resting HR {fmt(row['resting_hr'])}")


def build_daily(data: Data, generated: datetime | None = None) -> list:
    blocks = _header(data, f"Daily training report — {data.start:%a %b %-d, %Y}", generated)
    plan = data.plans[0] if data.plans else None
    if plan is None:
        return blocks + [("p", "No plan row for this date.")]
    logs = logs_for(data, plan["plan_id"])
    sets = [l for l in logs if l["log_type"] == "strength_set"]
    b = plan["blocks"]
    blocks += [("h2", "Session"),
               ("ul", [f"**{label(plan)}** — {plan['session_type']}, phase {plan['phase']} week {plan['week_num']}",
                       f"Status: {day_status(data, plan)}",
                       f"Planned: {len(b.get('exercises') or [])} exercises × {b.get('rounds') or '—'} rounds"
                       f" · RPE cap {fmt(plan['target_rpe'])} · est. {fmt(plan['est_duration_min'], ' min')}",
                       f"Session RPE: {fmt(session_rpe(logs))} · average set RPE: {fmt(avg_set_rpe(sets))}",
                       "Total time (first → last logged entry): "
                       + (f"{logged_span_min(logs)} min" if logged_span_min(logs) is not None else "—")])]
    blocks += [("h2", "Sets")]
    blocks += [_set_table(sets)] if sets else [("p", "No sets logged.")]
    blocks += _alias_note(sets)
    settings = [f"{canon(l['exercise'])}: {settings_in(l['notes'])}" for l in sets if settings_in(l["notes"])]
    blocks += [("h2", "Machine settings"), ("ul", settings) if settings else ("p", NOT_TRACKED)]
    blocks += [("h2", "Check-in"), ("p", _checkin_line(data.checkins.get(data.start)))]
    adj = adjustments(data)
    blocks += [("h2", "Adjustment"), ("ul", adj) if adj else ("p", "none — ran as written")]
    blocks += [("h2", "Watch data (average / max heart rate, calories)"), ("p", NOT_TRACKED)]
    return blocks


def build_weekly(data: Data, generated: datetime | None = None) -> list:
    blocks = _header(data, f"Weekly training report — week of {data.start:%b %-d, %Y}", generated)
    wk = sorted({w for p in data.plans if (w := data.program_week(p["plan_date"]))})
    if wk:
        blocks.append(("p", f"Program week {', '.join(map(str, wk))}"
                            + (f" of {data.program['weeks_total']}" if data.program else "")))
    done, due, upcoming = adherence(data)
    blocks += [("h2", "Adherence"),
               ("p", f"**{done} of {due}** training sessions due so far completed"
                     + (f" · {upcoming} still upcoming" if upcoming else "")
                     + f" · {sum(1 for p in data.plans if day_status(data, p) == 'rest')} rest day(s)"
                     + _pre_program_note(data))]

    rows = []
    for p in data.plans:
        logs = logs_for(data, p["plan_id"])
        sets = [l for l in logs if l["log_type"] == "strength_set"]
        span = logged_span_min(logs)
        rows.append([f"{p['plan_date']:%a %-m/%-d}", label(p), day_status(data, p),
                     str(len(sets)) if sets else "—", fmt(session_rpe(logs)), fmt(p["target_rpe"]),
                     f"{span} min" if span is not None else "—"])
    blocks += [("h2", "Sessions"),
               ("table", ["Date", "Session", "Status", "Sets", "Session RPE", "RPE cap", "Logged span"], rows)]

    now_w, prev_w = top_weights(data.logs), top_weights(data.prior_logs)
    prow = []
    for name in exercises_logged(data.logs):
        cur, prev = now_w.get(name), prev_w.get(name)
        if cur is None:
            change = no_load_label(name)
        elif prev is None:
            change = "first week"
        else:
            delta = cur - prev
            change = f"{delta:+g} lb" if delta else "same"
        prow.append([name, fmt(cur, " lb") if cur is not None else "—",
                     fmt(prev, " lb") if prev is not None else "—", change])
    blocks += [("h2", "Weight progress per exercise (top set vs last week)")]
    blocks += [("table", ["Exercise", "This week", "Last week", "Change"], prow)] if prow \
        else [("p", "No sets logged.")]
    blocks += _alias_note(data.logs)

    crow = []
    d = data.start
    while d <= min(data.end, data.today):
        r = data.checkins.get(d)
        crow.append([f"{d:%a %-m/%-d}"] + (["no check-in", "", "", "", ""] if not r else
                    [fmt(r["sleep_hrs"], " h"), fmt(r["energy"]), soreness_text(r["soreness"]),
                     fmt(r["weight_lbs"], " lb"), fmt(r["resting_hr"])]))
        d += timedelta(days=1)
    blocks += [("h2", "Check-in trends"),
               ("table", ["Date", "Sleep", "Energy (0–5)", "Soreness / pain", "Weight", "Resting HR"], crow)]

    weights = [(d, float(r["weight_lbs"])) for d, r in sorted(data.checkins.items()) if r["weight_lbs"] is not None]
    if weights:
        (d0, w0), (d1, w1) = weights[0], weights[-1]
        wline = (f"{w0:g} lb ({d0:%a}) → {w1:g} lb ({d1:%a}), {w1 - w0:+g} lb over "
                 f"{len(weights)} weigh-in(s)")
    else:
        wline = "no weigh-ins"
    blocks += [("h2", "Body weight"), ("p", wline)]
    blocks += [("h2", "Pain and soreness"), ("ul", pain_summary(data) or ["none reported"])]
    blocks += [("h2", "Open pain patterns"),
               ("ul", [f"{p['region']} after {p['exercise']}: {p['hits']} of {p['exposures']} sessions"
                       for p in data.patterns] or ["none"])]
    blocks += [("h2", "Adjustments applied"), ("ul", adjustments(data) or ["none"])]
    from artemis import health_eval
    ev = health_eval.evaluate(data.plans, data.logs, data.prior_logs, start=data.start,
                              end=data.end, today=data.today, anchor=data.anchor)
    blocks += [("h2", "Weekly evaluation (EVAL-1)"), ("ul", health_eval.render_lines(ev))]
    blocks += [("h2", "Watch data"), ("p", NOT_TRACKED)]
    blocks += [("h2", "Nutrition"), ("p", NOT_TRACKED)]
    return blocks


def build_monthly(data: Data, generated: datetime | None = None) -> list:
    blocks = _header(data, f"Monthly training report — {data.start:%B %Y}", generated)
    done, due, upcoming = adherence(data)
    planned = done + (due - done) + upcoming
    blocks += [("h2", "Sessions"),
               ("p", f"**{done}** done · {due - done} missed · {upcoming} still upcoming · "
                     f"{planned} planned in the month" + _pre_program_note(data))]

    lifts = []
    for st, lift in FIRST_LIFT.items():
        pts = [(l["plan_date"], float(l["weight_lbs"])) for l in data.logs
               if canon(l["exercise"]) == lift and l["weight_lbs"] is not None and not l["is_skipped"]]
        if not pts:
            lifts.append([lift, "—", "—", "not logged"])
            continue
        first_day, last_day = pts[0][0], pts[-1][0]
        start_w = max(w for d, w in pts if d == first_day)
        end_w = max(w for d, w in pts if d == last_day)
        lifts.append([lift, f"{start_w:g} lb ({first_day:%-m/%-d})", f"{end_w:g} lb ({last_day:%-m/%-d})",
                      f"{end_w - start_w:+g} lb" if last_day != first_day else "one session"])
    blocks += [("h2", "Main lifts (top set, start vs end)"),
               ("table", ["Lift", "Start", "End", "Change"], lifts)]

    weights = [(d, float(r["weight_lbs"])) for d, r in sorted(data.checkins.items()) if r["weight_lbs"] is not None]
    if weights:
        ws = [w for _, w in weights]
        wline = (f"{weights[0][1]:g} lb ({weights[0][0]:%-m/%-d}) → {weights[-1][1]:g} lb "
                 f"({weights[-1][0]:%-m/%-d}) · {weights[-1][1] - weights[0][1]:+g} lb · "
                 f"low {min(ws):g} · high {max(ws):g} · {len(ws)} weigh-ins")
    else:
        wline = "no weigh-ins"
    blocks += [("h2", "Body weight"), ("p", wline)]

    prog = data.program or {}
    wk = data.program_week(min(data.today, data.end))
    blocks += [("h2", "Phase and week"),
               ("p", (f"{prog.get('name', 'Program')} phase {prog.get('phase', '—')} · "
                      f"week {wk or '—'} of {prog.get('weeks_total', '—')} "
                      f"(started {data.anchor:%-m/%-d}, deload week {prog.get('deload_week', '—')}, "
                      f"ends {prog.get('end', '—')})"))]

    hl = [f"{done} session(s) completed"]
    by_day: dict[str, dict[date, float]] = {}   # exercise -> day -> top set
    for l in data.logs:
        if l["log_type"] == "strength_set" and l["weight_lbs"] is not None and not l["is_skipped"]:
            days = by_day.setdefault(canon(l["exercise"]), {})
            days[l["plan_date"]] = max(days.get(l["plan_date"], 0.0), float(l["weight_lbs"]))
    gains = sorted(((days[max(days)] - days[min(days)], n) for n, days in by_day.items()
                    if len(days) > 1 and days[max(days)] > days[min(days)]), reverse=True)
    hl += [f"{n}: +{g:g} lb top set" for g, n in gains[:3]]
    n_adj = len(adjustments(data))
    hl.append(f"{n_adj} check-in adjustment(s)" if n_adj else "no check-in adjustments")
    if data.patterns:
        hl.append(f"{len(data.patterns)} open pain pattern(s)")
    blocks += [("h2", "Highlights"), ("ul", hl)]
    blocks += [("h2", "Watch data"), ("p", NOT_TRACKED), ("h2", "Nutrition"), ("p", NOT_TRACKED)]
    return blocks


# ============================================================================
# CLI
# ============================================================================

def _load_dotenv() -> None:
    env_path = _REPO_ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def period(args) -> tuple[str, str, date, date]:
    if args.daily:
        d = date.fromisoformat(args.daily)
        return "daily", d.isoformat(), d, d
    if args.weekly:
        d = date.fromisoformat(args.weekly)
        return "weekly", d.isoformat(), d, d + timedelta(days=6)
    y, m = map(int, args.monthly.split("-"))
    return "monthly", f"{y:04d}-{m:02d}", date(y, m, 1), date(y, m, monthrange(y, m)[1])


def main() -> None:
    ap = argparse.ArgumentParser(description="Export a training report (Markdown + HTML).")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--daily", metavar="YYYY-MM-DD")
    g.add_argument("--weekly", metavar="WEEK_START", help="first day of the week (program weeks start Wed)")
    g.add_argument("--monthly", metavar="YYYY-MM")
    ap.add_argument("--out-dir", default=str(OUT_DIR))
    args = ap.parse_args()

    kind, key, start, end = period(args)
    if kind == "weekly" and start.weekday() != 2:
        print(f"note: {start} is a {start:%A}; program weeks run Wed–Tue.", file=sys.stderr)
    _load_dotenv()
    data = load(start, end)
    from artemis.quiet_hours import local_now
    build = {"daily": build_daily, "weekly": build_weekly, "monthly": build_monthly}[kind]
    blocks = build(data, local_now())
    title = f"Artemis {kind} report {key}"
    out = Path(args.out_dir)
    md_path, html_path = out / f"artemis-report-{kind}-{key}.md", out / f"artemis-report-{kind}-{key}.html"
    md_path.write_text(md(blocks))
    html_path.write_text(to_html(blocks, title))
    print(md_path)
    print(html_path)


if __name__ == "__main__":
    main()
