"""Dietitian report (VA MOVE!) — DIET-1's "Nutrition and Activity Record".

Spec: docs/ARTEMIS_STATE.md, DIET-1 → "Dietitian report (VA MOVE!)" (decided
2026-09-21) and HEALTH-PRIORITY #5.

Three layers, so everything but the SQL and the PDF engine is pure and tested:

    collect(start, end)  -> dict of rows            (SELECTs only)
    build(data)          -> Report                  (pure: every number)
    render_html(report)  -> str                     (pure: HTML + inline SVG)
    render_pdf(html)     -> bytes                   (WeasyPrint, lazy import)

The rules that govern it:

* **Recorded data only.** Nothing is estimated, interpolated or defaulted.
  A day with no data is shown as "no data", never as zero.
* **Clinical tone.** Numbers and neutral labels only; `BANNED_WORDS` is
  enforced by a test over the rendered text.
* **Completeness is reported, not required.** Any window renders; the page-1
  line "days with intake recorded: X of N" and the reasons for the rest say
  how complete it is (DIETITIAN-DATA: the 14-day window can't fill under the
  work-day-only pre-fill, and the report must still be honest about that).
* **Detail levels.** `summary` is page 1; `full` adds page 2 (activity,
  per-day table, recurring meals).
"""

from __future__ import annotations

import html
import os
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from statistics import mean

TITLE = "Nutrition and Activity Record"

#: The language rule: the rendered text must contain none of these as words.
BANNED_WORDS = (
    "under", "over", "short", "good", "poor", "on track", "deficit", "surplus",
    "should", "bad", "great", "excellent", "failed", "missed", "behind", "ahead",
    "too", "better", "worse", "improve", "improved", "goal",
)

STATUS_LABEL = {
    "assumed": "planned, no correction received",
    "locked_unconfirmed": "planned, no correction received",
    "confirmed": "confirmed",
    "corrected": "corrected",
}

UNRECORDED_REASON = {
    "not_work_day": "non-work day (not pre-filled)",
    "no_plan": "no plan",
    "unavailable": "plan unavailable",
    None: "no record",
}

#: Day types as the dietitian reads them (cycle.day_type values).
DAY_TYPE_LABEL = {"msp_work": "work", "msp_home": "off", "wi": "off", "travel": "travel"}

BASES = ("label", "USDA", "Open Food Facts", "estimate", "placeholder", "unclassified")

MACROS = (("kcal", "Energy", "kcal"), ("protein_g", "Protein", "g"),
          ("carb_g", "Carbohydrate", "g"), ("fat_g", "Fat", "g"),
          ("fiber_g", "Fiber", "g"))

KG_TO_LB = 2.20462


# ============================================================================
# Collect (SELECTs only)
# ============================================================================

def collect(start: date, end: date) -> dict:
    """Every row the report needs, for [start, end] inclusive."""
    from knowledge.db import execute_query
    from artemis import cycle, health_eval
    from artemis.quiet_hours import local_today

    def q(sql, params):
        return [dict(r) for r in execute_query(sql, params)]

    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    return {
        "start": start, "end": end,
        "day_types": {d: cycle.day_type(d) for d in days},
        "nutrition_days": q(
            "SELECT day_date, status, day_type, prefill_outcome, prefill_note "
            "FROM nutrition.day WHERE day_date BETWEEN %s AND %s", (start, end)),
        "entries": q(
            "SELECT e.day_date, e.slot, e.description, e.quantity, e.kcal, e.protein_g, "
            "e.carb_g, e.fat_g, e.fiber_g, e.source, e.confidence, e.status, "
            "f.is_placeholder, f.source_detail, f.portion "
            "FROM nutrition.entry e LEFT JOIN nutrition.food f ON f.id = e.food_id "
            "WHERE e.day_date BETWEEN %s AND %s ORDER BY e.day_date, e.id", (start, end)),
        "target": (q(
            "SELECT kcal, protein_g, carb_g, fat_g, fiber_g, set_by, provisional, "
            "effective_from FROM nutrition.target WHERE effective_from <= %s "
            "AND (effective_to IS NULL OR effective_to >= %s) "
            "ORDER BY effective_from DESC LIMIT 1", (end, end)) or [None])[0],
        "weights_watch": q(
            "SELECT local_date, value, unit FROM health.watch_sample "
            "WHERE metric = 'weight' AND local_date BETWEEN %s AND %s "
            "ORDER BY measured_at", (start, end)),
        "weights_checkin": q(
            "SELECT state_date AS local_date, weight_lbs AS value FROM health.daily_state "
            "WHERE weight_lbs IS NOT NULL AND state_date BETWEEN %s AND %s", (start, end)),
        "hourly": q(
            "SELECT metric, local_date, SUM(value) AS value FROM health.watch_hourly "
            "WHERE local_date BETWEEN %s AND %s GROUP BY metric, local_date", (start, end)),
        "active_energy": q(
            "SELECT local_date, SUM(value) AS value FROM health.watch_sample "
            "WHERE metric = 'active_energy' AND local_date BETWEEN %s AND %s "
            "GROUP BY local_date", (start, end)),
        "sessions": health_eval.load(start, end).get("counts") or {},
        "default_day": q(
            "SELECT e.description, e.slot, e.kcal, e.protein_g, e.source, "
            "f.is_placeholder, f.source_detail, f.portion "
            "FROM nutrition.entry e LEFT JOIN nutrition.food f ON f.id = e.food_id "
            "WHERE e.day_date = (SELECT max(d.day_date) FROM nutrition.day d "
            "  WHERE d.day_date <= %s AND d.prefill_outcome = 'planned' "
            "  AND d.day_type = 'msp_work') "
            "AND e.status = 'assumed' AND e.source = 'notion' ORDER BY e.id", (end,)),
        "generated": local_today(),
    }


# ============================================================================
# Build (pure)
# ============================================================================

@dataclass
class DayRow:
    day: date
    day_type: str | None
    recorded: bool
    status: str                 # a STATUS_LABEL value, or "not recorded — <reason>"
    totals: dict                # macro -> float (only when recorded)
    est_share: float | None     # % of kcal from estimate/placeholder
    steps: float | None
    active_min: float | None
    active_kcal: float | None
    weight: tuple | None        # (lb, "watch" | "check-in")


@dataclass
class Report:
    start: date
    end: date
    generated: date
    name: str
    target_line: str
    target: dict | None
    days: list[DayRow]
    n_recorded: int
    unrecorded_by_reason: dict
    averages: dict              # macro -> {mean, min, max}
    target_diff: dict           # macro -> signed difference (kcal, protein only)
    status_counts: dict
    basis_kcal: dict
    basis_protein: dict
    weight: dict
    sessions: dict
    activity: dict
    default_day: list[dict]
    repeats: list[tuple]        # (description, count)
    notes: list[str] = field(default_factory=list)


def _f(v) -> float:
    return float(v) if v is not None else 0.0


def macro_basis(entry: dict) -> str:
    """The basis a row's macros rest on. Derived at report time from the
    entry's source and its food's `source_detail` until `macro_basis` exists
    as a column (DIET-1 decision); a row that can't be classified is shown as
    `unclassified`, never guessed."""
    src = (entry.get("source") or "").lower()
    if src == "usda":
        return "USDA"
    if src == "off":
        return "Open Food Facts"
    if src == "estimate":
        return "estimate"
    if src == "manual":
        return "label"
    if entry.get("is_placeholder"):
        return "placeholder"
    detail = (entry.get("source_detail") or "").lower()
    if "placeholder" in detail:
        return "placeholder"
    if "estimate" in detail:
        return "estimate"
    if "label" in detail:
        return "label"
    if "usda" in detail:
        return "USDA"
    if "open food facts" in detail or "openfoodfacts" in detail:
        return "Open Food Facts"
    return "unclassified"


def _shares(entries: list[dict], key: str) -> dict:
    tot = sum(_f(e.get(key)) for e in entries)
    out = {b: 0.0 for b in BASES}
    if tot <= 0:
        return {}
    for e in entries:
        out[macro_basis(e)] += _f(e.get(key)) / tot * 100.0
    return {b: v for b, v in out.items() if v > 0 or b in BASES[:5]}


def _weights(data: dict) -> dict[date, tuple]:
    """One weigh-in per day: the watch sample (last of the day), else the
    check-in value. Each carries its source."""
    out: dict[date, tuple] = {}
    for r in data.get("weights_checkin") or []:
        out[r["local_date"]] = (round(_f(r["value"]), 1), "check-in")
    for r in data.get("weights_watch") or []:
        v = _f(r["value"])
        if (r.get("unit") or "").lower() in ("kg", "kgs"):
            v *= KG_TO_LB
        out[r["local_date"]] = (round(v, 1), "watch")
    return out


def _weight_summary(w: dict[date, tuple], end: date) -> dict:
    if not w:
        return {"n": 0}
    ds = sorted(w)
    first, last = ds[0], ds[-1]
    window = [d for d in ds if end - timedelta(days=6) <= d <= end]
    out = {"n": len(ds), "first": (first, w[first][0]), "last": (last, w[last][0]),
           "change": round(w[last][0] - w[first][0], 1)}
    if len(window) >= 3:
        out["avg7"] = round(mean(w[d][0] for d in window), 1)
        out["avg7_n"] = len(window)
    else:
        out["last7"] = [(d, w[d][0]) for d in window]
    return out


def _target_line(t: dict | None) -> str:
    if not t:
        return "Reference target: none recorded"
    who = {"ryan": "patient", "joy": "dietitian"}.get((t.get("set_by") or "").lower(),
                                                        t.get("set_by") or "unknown")
    eff = t.get("effective_from")
    eff_s = eff.isoformat() if hasattr(eff, "isoformat") else str(eff or "")
    parts = [f"Reference target: {_f(t['kcal']):,.0f} kcal / {_f(t['protein_g']):,.0f} g protein"]
    if t.get("provisional"):
        parts.append("provisional")
    parts.append(f"set by {who} {eff_s}".strip())
    if t.get("provisional") and who != "dietitian":
        parts.append("pending dietitian review")
    return ", ".join(parts)


def build(data: dict, *, name: str | None = None) -> Report:
    start, end = data["start"], data["end"]
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    ndays = {r["day_date"]: r for r in data.get("nutrition_days") or []}
    by_day: dict[date, list] = {}
    for e in data.get("entries") or []:
        by_day.setdefault(e["day_date"], []).append(e)
    hourly: dict[tuple, float] = {(r["metric"], r["local_date"]): _f(r["value"])
                                  for r in data.get("hourly") or []}
    energy = {r["local_date"]: _f(r["value"]) for r in data.get("active_energy") or []}
    w = _weights(data)

    rows: list[DayRow] = []
    unrecorded: dict[str, int] = {}
    status_counts = {"planned, no correction received": 0, "confirmed": 0, "corrected": 0}
    for d in days:
        es = by_day.get(d, [])
        nd = ndays.get(d)
        recorded = bool(es)
        if recorded:
            status = STATUS_LABEL.get((nd or {}).get("status") or "corrected", "corrected")
            status_counts[status] = status_counts.get(status, 0) + 1
            totals = {k: sum(_f(e.get(k)) for e in es) for k, _, _ in MACROS}
            kc = totals["kcal"]
            est = sum(_f(e.get("kcal")) for e in es
                      if macro_basis(e) in ("estimate", "placeholder"))
            est_share = (est / kc * 100.0) if kc else None
        else:
            reason = UNRECORDED_REASON.get((nd or {}).get("prefill_outcome"), "no record")
            unrecorded[reason] = unrecorded.get(reason, 0) + 1
            status, totals, est_share = f"not recorded — {reason}", {}, None
        rows.append(DayRow(
            day=d, day_type=data.get("day_types", {}).get(d) or (nd or {}).get("day_type"),
            recorded=recorded, status=status, totals=totals, est_share=est_share,
            steps=hourly.get(("step_count", d)), active_min=hourly.get(("apple_exercise_time", d)),
            active_kcal=energy.get(d), weight=w.get(d)))

    rec = [r for r in rows if r.recorded]
    averages = {}
    for k, _, _ in MACROS:
        vals = [r.totals[k] for r in rec]
        if vals:
            averages[k] = {"mean": mean(vals), "min": min(vals), "max": max(vals)}
    t = data.get("target")
    diff = {}
    if t and rec:
        for k in ("kcal", "protein_g"):
            if t.get(k) is not None and k in averages:
                diff[k] = averages[k]["mean"] - _f(t[k])

    all_entries = [e for r in rec for e in by_day[r.day]]
    steps = [r.steps for r in rows if r.steps is not None]
    amin = [r.active_min for r in rows if r.active_min is not None]
    akcal = [r.active_kcal for r in rows if r.active_kcal is not None]
    activity = {
        "steps_avg": mean(steps) if steps else None, "steps_days": len(steps),
        "active_min_total": sum(amin) if amin else None, "active_min_days": len(amin),
        "active_kcal_avg": mean(akcal) if akcal else None, "active_kcal_days": len(akcal),
    }
    repeats: dict[str, int] = {}
    for e in data.get("entries") or []:
        if e.get("status") == "corrected":
            repeats[e["description"]] = repeats.get(e["description"], 0) + 1

    return Report(
        start=start, end=end, generated=data["generated"],
        name=name if name is not None else os.environ.get("REPORT_PATIENT_NAME", ""),
        target_line=_target_line(t), target=t, days=rows, n_recorded=len(rec),
        unrecorded_by_reason=unrecorded, averages=averages, target_diff=diff,
        status_counts=status_counts,
        basis_kcal=_shares(all_entries, "kcal"), basis_protein=_shares(all_entries, "protein_g"),
        weight=_weight_summary(w, end), sessions=data.get("sessions") or {},
        activity=activity, default_day=list(data.get("default_day") or []),
        repeats=sorted(((k, v) for k, v in repeats.items() if v >= 2), key=lambda x: (-x[1], x[0])),
    )


# ============================================================================
# Render (pure)
# ============================================================================

def _e(s) -> str:
    return html.escape(str(s))


def _n(v, dp: int = 0) -> str:
    if v is None:
        return "—"
    return f"{v:,.{dp}f}"


def _signed(v: float, dp: int = 0) -> str:
    s = f"{abs(v):,.{dp}f}"
    return f"+{s}" if v > 0 else (f"−{s}" if v < 0 else "0")


def _d(d: date) -> str:
    return f"{d:%a %-m/%-d}"


def bar_chart_svg(values: list[tuple[date, float | None]], *, title: str,
                  width: float = 255.0, height: float = 140.0) -> str:
    """Neutral-grey bars, y from 0, value labelled on each bar, and a day with
    no data drawn as an empty slot labelled "no data" (never a zero bar)."""
    n = max(len(values), 1)
    vertical = n > 14
    fs = 5.0 if vertical else 6.5
    # Headroom above the tallest bar for its label: rotated labels need more.
    pad_l, pad_b, pad_t = 4.0, 16.0, (34.0 if vertical else 22.0)
    plot_h = height - pad_b - pad_t
    slot = (width - pad_l) / n
    bw = max(slot * 0.7, 1.0)
    vmax = max([v for _, v in values if v is not None] + [0.0]) or 1.0
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}pt" height="{height}pt" '
             f'viewBox="0 0 {width} {height}" role="img" aria-label="{_e(title)}">',
             f'<text x="0" y="9" font-size="7.5" fill="#222">{_e(title)}</text>',
             f'<line x1="{pad_l}" y1="{pad_t + plot_h}" x2="{width}" y2="{pad_t + plot_h}" '
             f'stroke="#999" stroke-width="0.5"/>']
    for i, (d, v) in enumerate(values):
        x = pad_l + i * slot + (slot - bw) / 2
        cx = x + bw / 2
        base = pad_t + plot_h
        if v is None:
            parts.append(f'<rect x="{x:.2f}" y="{pad_t:.2f}" width="{bw:.2f}" height="{plot_h:.2f}" '
                         f'fill="none" stroke="#bbb" stroke-width="0.4" stroke-dasharray="1.5,1.5"/>')
            label, ly = "no data", base - 3
        else:
            h = plot_h * (v / vmax)
            parts.append(f'<rect x="{x:.2f}" y="{base - h:.2f}" width="{bw:.2f}" height="{h:.2f}" '
                         f'fill="#8a8a8a"/>')
            label, ly = f"{v:,.0f}", base - h - 2
        if vertical:
            parts.append(f'<text x="{cx:.2f}" y="{min(ly, base - 3):.2f}" font-size="{fs}" fill="#222" '
                         f'transform="rotate(-90 {cx:.2f} {min(ly, base - 3):.2f})">{_e(label)}</text>')
        else:
            parts.append(f'<text x="{cx:.2f}" y="{ly:.2f}" font-size="{fs}" fill="#222" '
                         f'text-anchor="middle">{_e(label)}</text>')
        parts.append(f'<text x="{cx:.2f}" y="{height - 4:.2f}" font-size="{fs}" fill="#444" '
                     f'text-anchor="middle">{d.day}</text>')
    parts.append("</svg>")
    return "".join(parts)


_CSS = """
@page { size: Letter; margin: 0.55in 0.6in; }
body { font-family: 'DejaVu Sans', sans-serif; font-size: 8.5pt; color: #111; }
h1 { font-size: 14pt; margin: 0 0 2pt; }
h2 { font-size: 10pt; margin: 10pt 0 3pt; border-bottom: 0.5pt solid #999; }
.meta { color: #333; margin: 0 0 1pt; }
table { border-collapse: collapse; width: 100%; }
th, td { text-align: left; padding: 1.5pt 4pt; border-bottom: 0.3pt solid #ccc; }
th { font-weight: bold; background: #f0f0f0; }
td.n, th.n { text-align: right; }
.small td, .small th { font-size: 7pt; padding: 1pt 3pt; }
.charts { display: flex; gap: 12pt; margin-top: 6pt; }
.note { color: #444; font-size: 7.5pt; margin-top: 2pt; }
.pb { page-break-before: always; }
footer, .foot { color: #444; font-size: 7pt; margin-top: 8pt; }
"""


def _page1(r: Report) -> str:
    n = (r.end - r.start).days + 1
    out = [f"<h1>{TITLE}</h1>",
           f'<p class="meta">{_e(r.name) if r.name else "Name: —"} · '
           f'{r.start:%b %-d, %Y} – {r.end:%b %-d, %Y} · generated {r.generated:%b %-d, %Y}</p>',
           f'<p class="meta">{_e(r.target_line)}</p>']

    out.append("<h2>1. Period and completeness</h2>")
    out.append(f"<p>Days in period: {n}. Days with intake recorded: {r.n_recorded} of {n}.</p>")
    if r.unrecorded_by_reason:
        out.append("<table><tr><th>Not recorded — reason</th><th class='n'>Days</th></tr>")
        for reason, c in sorted(r.unrecorded_by_reason.items()):
            out.append(f"<tr><td>{_e(reason)}</td><td class='n'>{c}</td></tr>")
        out.append("</table>")

    out.append(f"<h2>2. Daily average intake (recorded days only, n = {r.n_recorded})</h2>")
    if not r.averages:
        out.append("<p>No intake recorded in this period.</p>")
    else:
        out.append("<table><tr><th>Nutrient</th><th class='n'>Average</th><th class='n'>Range</th>"
                   "<th class='n'>Reference</th><th class='n'>Difference</th></tr>")
        for k, label, unit in MACROS:
            a = r.averages.get(k)
            if not a:
                continue
            ref = _n(_f(r.target.get(k))) + f" {unit}" if (r.target and k in r.target_diff) else "—"
            dif = f"{_signed(r.target_diff[k])} {unit}" if k in r.target_diff else "—"
            out.append(f"<tr><td>{label}</td><td class='n'>{_n(a['mean'])} {unit}</td>"
                       f"<td class='n'>{_n(a['min'])}–{_n(a['max'])} {unit}</td>"
                       f"<td class='n'>{ref}</td><td class='n'>{dif}</td></tr>")
        out.append("</table>")

    out.append("<h2>3. How days were recorded</h2><table>"
               "<tr><th>Recording status</th><th class='n'>Days</th></tr>")
    for label in ("planned, no correction received", "confirmed", "corrected"):
        out.append(f"<tr><td>{label}</td><td class='n'>{r.status_counts.get(label, 0)}</td></tr>")
    out.append("</table>")

    out.append("<h2>4. Share of intake by macro basis</h2>")
    if not r.basis_kcal:
        out.append("<p>No intake recorded in this period.</p>")
    else:
        out.append("<table><tr><th>Basis</th><th class='n'>% of energy</th>"
                   "<th class='n'>% of protein</th></tr>")
        for b in BASES:
            if b in r.basis_kcal or b in r.basis_protein:
                out.append(f"<tr><td>{b}</td><td class='n'>{_n(r.basis_kcal.get(b, 0.0), 1)}</td>"
                           f"<td class='n'>{_n(r.basis_protein.get(b, 0.0), 1)}</td></tr>")
        out.append("</table>")

    out.append("<h2>5. Weight</h2>")
    w = r.weight
    if not w.get("n"):
        out.append("<p>No weigh-ins recorded in this period.</p>")
    else:
        (fd, fv), (ld, lv) = w["first"], w["last"]
        out.append("<table>"
                   f"<tr><td>First weigh-in</td><td class='n'>{_d(fd)}</td><td class='n'>{fv:.1f} lb</td></tr>"
                   f"<tr><td>Last weigh-in</td><td class='n'>{_d(ld)}</td><td class='n'>{lv:.1f} lb</td></tr>"
                   f"<tr><td>Change</td><td></td><td class='n'>{_signed(w['change'], 1)} lb</td></tr>"
                   f"<tr><td>Weigh-ins</td><td></td><td class='n'>{w['n']}</td></tr>")
        if "avg7" in w:
            out.append(f"<tr><td>7-day average (n = {w['avg7_n']})</td><td></td>"
                       f"<td class='n'>{w['avg7']:.1f} lb</td></tr>")
        out.append("</table>")
        if "last7" in w:
            vals = ", ".join(f"{_d(d)} {v:.1f} lb" for d, v in w["last7"]) or "none"
            out.append(f"<p class='note'>Fewer than 3 weigh-ins in the last 7 days; "
                       f"values listed instead of averaged: {vals}.</p>")
    return "".join(out)


def _page2(r: Report) -> str:
    s, a = r.sessions, r.activity
    out = ['<div class="pb"></div><h2>Activity</h2><table>',
           f"<tr><td>Prescribed sessions completed</td><td class='n'>"
           f"{s.get('done', '—')} of {s.get('due', '—')} due ({s.get('planned', '—')} planned in period)"
           f"</td></tr>",
           f"<tr><td>Exercise minutes (total)</td><td class='n'>{_n(a['active_min_total'])} "
           f"({a['active_min_days']} days with data)</td></tr>",
           f"<tr><td>Daily average steps</td><td class='n'>{_n(a['steps_avg'])} "
           f"({a['steps_days']} days with data)</td></tr>",
           f"<tr><td>Daily average watch active energy</td><td class='n'>{_n(a['active_kcal_avg'])} kcal "
           f"({a['active_kcal_days']} days with data)</td></tr></table>"]
    out.append('<div class="charts">'
               + bar_chart_svg([(d.day, d.steps) for d in r.days], title="Daily steps")
               + bar_chart_svg([(d.day, d.active_min) for d in r.days],
                               title="Active minutes per day")
               + "</div>")
    out.append('<p class="note">Intensity breakdown not yet available.</p>')

    out.append("<h2>Per day</h2><table class='small'><tr><th>Date</th><th>Day type</th>"
               "<th>Recording status</th><th class='n'>kcal</th><th class='n'>Protein</th>"
               "<th class='n'>Carb</th><th class='n'>Fat</th><th class='n'>Fiber</th>"
               "<th class='n'>Est./placeholder</th><th class='n'>Weight</th></tr>")
    for d in r.days:
        t = d.totals
        wt = f"{d.weight[0]:.1f} ({d.weight[1]})" if d.weight else "—"
        cells = ([_n(t['kcal']), _n(t['protein_g']), _n(t['carb_g']), _n(t['fat_g']),
                  _n(t['fiber_g']), (_n(d.est_share) + "%") if d.est_share is not None else "—"]
                 if d.recorded else ["—"] * 6)
        out.append(f"<tr><td>{_d(d.day)}</td><td>{_e(DAY_TYPE_LABEL.get(d.day_type, d.day_type or '—'))}</td>"
                   f"<td>{_e(d.status)}</td>"
                   + "".join(f"<td class='n'>{c}</td>" for c in cells)
                   + f"<td class='n'>{_e(wt)}</td></tr>")
    out.append("</table>")

    out.append("<h2>Recurring meals</h2>")
    if r.default_day:
        out.append("<table class='small'><tr><th>Default work day</th><th>Slot</th><th>Portion</th>"
                   "<th class='n'>kcal</th><th class='n'>Protein</th><th>Basis</th></tr>")
        for m in r.default_day:
            out.append(f"<tr><td>{_e(m['description'])}</td><td>{_e(m.get('slot') or '')}</td>"
                       f"<td>{_e(m.get('portion') or '1 portion')}</td><td class='n'>{_n(_f(m.get('kcal')))}</td>"
                       f"<td class='n'>{_n(_f(m.get('protein_g')))} g</td><td>{macro_basis(m)}</td></tr>")
        out.append("</table>")
    else:
        out.append("<p>No default work day recorded.</p>")
    if r.repeats:
        out.append("<p class='note'>Foods logged 2 or more times as changes: "
                   + ", ".join(f"{_e(k)} ({v})" for k, v in r.repeats) + ".</p>")
    return "".join(out)


def _footer() -> str:
    return ('<div class="foot">Recording status: <b>planned, no correction received</b> — the day '
            'was pre-filled from the meal plan and no change was reported; <b>confirmed</b> — '
            'reported as eaten as planned; <b>corrected</b> — changed or logged by the patient. '
            'Basis: <b>label</b> — nutrition label or recipe computed from labels; <b>USDA</b> — '
            'USDA FoodData Central; <b>Open Food Facts</b> — product database; <b>estimate</b> — '
            'estimated value; <b>placeholder</b> — provisional value pending a label; '
            '<b>unclassified</b> — source not recorded. Active minutes: Apple Watch exercise '
            'minutes. Figures are recorded data only.</div>')


def render_html(r: Report, detail: str = "full") -> str:
    if detail not in ("summary", "full"):
        raise ValueError("detail is 'summary' or 'full'")
    body = _page1(r) + (_page2(r) if detail == "full" else "") + _footer()
    return (f"<!doctype html><html><head><meta charset='utf-8'><title>{TITLE}</title>"
            f"<style>{_CSS}</style></head><body>{body}</body></html>")


def visible_text(html_doc: str) -> str:
    """The words a reader sees — tags, CSS and SVG attributes removed."""
    t = re.sub(r"<style.*?</style>", " ", html_doc, flags=re.S)
    t = re.sub(r"<[^>]+>", " ", t)
    return html.unescape(re.sub(r"\s+", " ", t))


def banned_words_in(text: str) -> list[str]:
    low = text.lower()
    return [w for w in BANNED_WORDS if re.search(rf"\b{re.escape(w)}\b", low)]


def _document(html_doc: str):
    from weasyprint import HTML        # heavy import; the box renders in its own process
    return HTML(string=html_doc).render()


def render_pdf(html_doc: str) -> bytes:
    return _document(html_doc).write_pdf()


def page_count(html_doc: str) -> int:
    """Pages the PDF will have — the spec's two-page limit is checked on this."""
    return len(_document(html_doc).pages)


# ============================================================================
# Periods
# ============================================================================

def period(kind: str, anchor: date) -> tuple[date, date]:
    """day | week (the 7 days ending on `anchor`) | month (calendar month of
    `anchor`) | fortnight (the 14 days ending on `anchor`)."""
    if kind == "day":
        return anchor, anchor
    if kind == "week":
        return anchor - timedelta(days=6), anchor
    if kind == "fortnight":
        return anchor - timedelta(days=13), anchor
    if kind == "month":
        first = anchor.replace(day=1)
        nxt = (first + timedelta(days=32)).replace(day=1)
        return first, nxt - timedelta(days=1)
    raise ValueError(f"unknown period {kind!r}")
