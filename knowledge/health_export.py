"""HEALTH-EXPORT-1 — hand settled nutrition days to Apple Health, exactly once.

THE PATH:  nutrition.entry -> this module (served by the Lambda) -> an iOS
Shortcut on Ryan's phone -> Apple Health -> the VA "Share My Health Data" app ->
his nutritionist. Artemis cannot write to Apple Health itself; the Shortcut is
the only bridge, and it is WRITE-ONLY: a logged sample cannot be deleted or
amended from a Shortcut. Every rule below follows from that one fact.

  * A day is exported only once it can no longer change — after the 48 h
    correction window (040) has closed. Sending today's pre-fill would put the
    plan into a clinical record before he has had the chance to say what he ate.
  * A day goes WHOLE or not at all. A day with one slot missing looks, in
    Health, exactly like a day he ate less.
  * A nutrient that is NULL on any entry withholds the day. Summing a NULL as
    zero would understate intake to a clinician, silently.
  * Nothing is written twice. A batch is CLAIMED before the Shortcut writes and
    ACKED after; while a claim is open, no new batch is handed out. An unacked
    batch is surfaced as `stuck` for Ryan to resolve — the server cannot see
    Health, so it never guesses whether the write landed.

It lives in `knowledge/` because the Lambda package ships `knowledge/` and not
`artemis/` (PACKAGE-IDENTITY). It imports nothing from `artemis`. Like
`prep_store`, every function that touches the database takes a
`fetch(sql, params) -> list[dict]` callable; `build_plan` and the config parser
are pure and take no database at all.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Optional
from zoneinfo import ZoneInfo

#: Mirrors artemis.nutrition.SLOTS / CORRECTION_WINDOW_HOURS. `artemis/` is not
#: in the Lambda package, so these cannot be imported; tests/test_health_export.py
#: asserts the two copies agree (SAME-FACT-MANY-PLACES).
SLOTS = ("breakfast", "lunch", "dinner", "snacks")
CORRECTION_WINDOW_HOURS = 48

#: The five nutrients the VA app reads AND nutrition.entry records. Sugar is
#: neither (no data source, and the app does not request it).
NUTRIENTS = ("kcal", "protein_g", "carb_g", "fat_g", "fiber_g")

CONFIG_KEY = "health_export"
STATE_CLAIMED = "claimed"
STATE_SENT = "sent"
DAY_CORRECTED = "corrected"          # nutrition.day.status — a day Ryan touched

#: Days per batch. A day is at most 4 slots x 5 samples = 20 Health writes, so 7
#: days is 140 — a first-run backfill stays a Shortcut run of seconds, and a
#: stuck batch stays small enough to check by eye in the Health app.
DEFAULT_MAX_DAYS = 7
MAX_MAX_DAYS = 14


class ExportError(Exception):
    """A refusal the caller should report, not swallow."""


# ── configuration ───────────────────────────────────────────────────────────

@dataclass
class Config:
    enabled: bool = False
    start_date: Optional[date] = None
    include_unconfirmed: bool = False
    slot_times: dict = field(default_factory=dict)
    #: why the config cannot be used. Non-empty => nothing is exported.
    problems: list = field(default_factory=list)

    @property
    def usable(self) -> bool:
        return self.enabled and not self.problems


def parse_config(raw: Any) -> Config:
    """`nutrition.prep_config['health_export']` -> a validated Config.

    FAIL-CLOSED: anything this cannot read is a problem, and a config with
    problems exports nothing. In particular there is NO default start date and
    NO default slot time — when the export begins and when he eats are Ryan's
    facts, and a default would put an invented time into a clinical record.

    `enabled` must be the JSON boolean true. The strings "true"/"yes"/"on" do
    not enable it: an automation that cannot confirm it was switched on does
    not run (the MEAL-NUDGE rule).
    """
    cfg = Config()
    if raw is None:
        return cfg                       # no row: off, and not a problem
    if not isinstance(raw, dict):
        cfg.problems.append("config is not a JSON object")
        return cfg
    cfg.enabled = raw.get("enabled") is True
    if not cfg.enabled:
        return cfg

    iu = raw.get("include_unconfirmed", False)
    if isinstance(iu, bool):
        cfg.include_unconfirmed = iu
    else:
        cfg.problems.append("include_unconfirmed must be true or false")

    sd = raw.get("start_date")
    try:
        cfg.start_date = date.fromisoformat(sd)
    except (TypeError, ValueError):
        cfg.problems.append("start_date is required, as YYYY-MM-DD")

    st = raw.get("slot_times")
    if not isinstance(st, dict):
        cfg.problems.append("slot_times is required: one HH:MM per slot")
    else:
        for slot in SLOTS:
            try:
                cfg.slot_times[slot] = _parse_hhmm(st.get(slot))
            except (TypeError, ValueError):
                cfg.problems.append(f"slot_times.{slot} is required, as HH:MM")
        extra = sorted(set(st) - set(SLOTS))
        if extra:
            # A typo'd slot name would otherwise be a silently ignored setting.
            cfg.problems.append("slot_times has unknown slots: " + ", ".join(extra))
    return cfg


def _parse_hhmm(value: Any) -> time:
    if not isinstance(value, str):
        raise TypeError("not a string")
    hh, mm = value.strip().split(":")
    if not (len(hh) == 2 and len(mm) == 2):
        raise ValueError("not HH:MM")
    return time(int(hh), int(mm))


def load_config(fetch) -> Config:
    rows = fetch("SELECT value FROM nutrition.prep_config WHERE key = %s",
                 (CONFIG_KEY,))
    return parse_config(rows[0]["value"] if rows else None)


# ── when a day is settled ───────────────────────────────────────────────────

def last_closed_day(today: date) -> date:
    """The most recent day whose correction window has closed.

    artemis.nutrition.correction_deadline(d) is local midnight starting d+1,
    plus 48 h of WALL-CLOCK time — i.e. local midnight starting d+3. So on local
    date `today`, day d is closed exactly when d + 3 <= today. `today` must be
    resolved in the ACTIVE timezone by the caller; nothing here reads a clock.
    """
    return today - timedelta(days=1 + CORRECTION_WINDOW_HOURS // 24)


# ── the plan: what would be exported, and what is held back and why ─────────

@dataclass
class Item:
    day: date
    slot: str
    at: datetime
    kcal: int
    protein_g: float
    carb_g: float
    fat_g: float
    fiber_g: float
    n_entries: int
    day_status: str

    def as_json(self) -> dict:
        return {"day": self.day.isoformat(), "slot": self.slot,
                "at": self.at.isoformat(timespec="seconds"),
                "kcal": self.kcal, "protein_g": self.protein_g,
                "carb_g": self.carb_g, "fat_g": self.fat_g,
                "fiber_g": self.fiber_g}


@dataclass
class Plan:
    items: list = field(default_factory=list)
    #: [{"day": iso, "reason": code, "detail": text}] — days that are settled
    #: and have intake recorded but are NOT being sent.
    withheld: list = field(default_factory=list)
    #: settled, eligible days left for a later batch (max_days reached)
    remaining_days: int = 0

    @property
    def days(self) -> list:
        return sorted({i.day for i in self.items})


def _g(total: Decimal) -> float:
    """Grams to one decimal — what the recipe macros carry (53.5 g). Half UP:
    round() on a Decimal is half-even, which sends 104.25 to 104.2."""
    return float(total.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def _kcal(total: Decimal) -> int:
    return int(total.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def build_plan(cfg: Config, *, today: date, tz: str, entries: list,
               exported_days: set, max_days: int = DEFAULT_MAX_DAYS) -> Plan:
    """Pure. Decide what to hand to Health from rows already read.

    `entries`: one dict per nutrition.entry row in the window, carrying
    day_date, day_status, slot and the five nutrients.
    `exported_days`: every day_date that already has ANY row in
    nutrition.health_export, claimed or sent. Such a day is never offered again,
    in whole or in part — re-sending one slot of a sent day is a duplicate.

    Oldest day first, so a backfill fills Health in order and `remaining_days`
    is always the newer tail.
    """
    plan = Plan()
    if not cfg.usable:
        return plan
    zone = ZoneInfo(tz)
    last = last_closed_day(today)

    by_day: dict = {}
    for e in entries:
        d = e["day_date"]
        if d < cfg.start_date or d > last or d in exported_days:
            continue
        by_day.setdefault(d, []).append(e)

    taken = 0
    for d in sorted(by_day):
        rows = by_day[d]
        status = rows[0]["day_status"]

        if status != DAY_CORRECTED and not cfg.include_unconfirmed:
            plan.withheld.append({
                "day": d.isoformat(), "reason": "unconfirmed",
                "detail": f"day status is {status}: the plan, never confirmed "
                          "or corrected"})
            continue

        missing = sorted({n for r in rows for n in NUTRIENTS if r.get(n) is None})
        if missing:
            plan.withheld.append({
                "day": d.isoformat(), "reason": "missing_macros",
                "detail": "at least one entry has no value for: "
                          + ", ".join(missing)})
            continue

        unknown = sorted({r["slot"] for r in rows} - set(SLOTS))
        if unknown:
            plan.withheld.append({
                "day": d.isoformat(), "reason": "unknown_slot",
                "detail": "no configured time for: " + ", ".join(unknown)})
            continue

        if taken >= max_days:
            plan.remaining_days += 1
            continue
        taken += 1

        for slot in SLOTS:                       # stable order within the day
            srows = [r for r in rows if r["slot"] == slot]
            if not srows:
                continue
            sums = {n: sum((Decimal(str(r[n])) for r in srows), Decimal(0))
                    for n in NUTRIENTS}
            if not any(sums.values()):
                # Nothing to say about a slot that totals zero on every
                # nutrient; five 0-valued samples are noise, not data.
                continue
            plan.items.append(Item(
                day=d, slot=slot,
                at=datetime.combine(d, cfg.slot_times[slot], tzinfo=zone),
                kcal=_kcal(sums["kcal"]),
                protein_g=_g(sums["protein_g"]), carb_g=_g(sums["carb_g"]),
                fat_g=_g(sums["fat_g"]), fiber_g=_g(sums["fiber_g"]),
                n_entries=len(srows), day_status=status))
    return plan


# ── reads ───────────────────────────────────────────────────────────────────

_ENTRY_SQL = """
    SELECT e.day_date, d.status AS day_status, e.slot,
           e.kcal, e.protein_g, e.carb_g, e.fat_g, e.fiber_g
      FROM nutrition.entry e
      JOIN nutrition.day d ON d.day_date = e.day_date
     WHERE e.day_date >= %s AND e.day_date <= %s
     ORDER BY e.day_date, e.id
"""


def read_entries(fetch, start: date, end: date) -> list:
    if end < start:
        return []
    return fetch(_ENTRY_SQL, (start, end))


def exported_days(fetch, start: date, end: date) -> set:
    rows = fetch("SELECT DISTINCT day_date FROM nutrition.health_export "
                 " WHERE day_date >= %s AND day_date <= %s", (start, end))
    return {r["day_date"] for r in rows}


def open_batch(fetch) -> Optional[dict]:
    """The claimed-but-unconfirmed batch, if there is one.

    There is at most one by construction (`claim` refuses while one is open),
    but this reads whatever is there: more than one would mean something wrote
    around `claim`, and hiding the extra rows would be the wrong way to find out.
    """
    rows = fetch("SELECT batch_id, day_date, slot, claimed_at "
                 "  FROM nutrition.health_export WHERE state = %s "
                 " ORDER BY day_date, slot", (STATE_CLAIMED,))
    if not rows:
        return None
    ids = sorted({str(r["batch_id"]) for r in rows})
    return {"batch_id": ids[0], "batch_ids": ids,
            "days": sorted({r["day_date"].isoformat() for r in rows}),
            "items": len(rows),
            "claimed_at": min(r["claimed_at"] for r in rows)}


def plan_for(fetch, cfg: Config, *, today: date, tz: str,
             max_days: int = DEFAULT_MAX_DAYS) -> Plan:
    if not cfg.usable:
        return Plan()
    end = last_closed_day(today)
    return build_plan(cfg, today=today, tz=tz,
                      entries=read_entries(fetch, cfg.start_date, end),
                      exported_days=exported_days(fetch, cfg.start_date, end),
                      max_days=max_days)


# ── writes ──────────────────────────────────────────────────────────────────

def claim(fetch, plan: Plan) -> Optional[str]:
    """Record the plan's items as CLAIMED and return the batch id.

    Refuses while another batch is open. The UNIQUE (day_date, slot) constraint
    is the real guard against two runs racing: the loser's INSERT raises, its
    transaction rolls back, and it claims nothing. The caller commits.
    """
    if not plan.items:
        return None
    if open_batch(fetch) is not None:
        raise ExportError("a previous batch is still unconfirmed")
    batch_id = str(uuid.uuid4())
    for it in plan.items:
        fetch("""
            INSERT INTO nutrition.health_export
                (day_date, slot, kcal, protein_g, carb_g, fat_g, fiber_g,
                 n_entries, day_status, sample_at, batch_id, state)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (it.day, it.slot, it.kcal, it.protein_g, it.carb_g, it.fat_g,
              it.fiber_g, it.n_entries, it.day_status, it.at, batch_id,
              STATE_CLAIMED))
    return batch_id


def ack(fetch, batch_id: str) -> dict:
    """The Shortcut confirms it wrote the batch. Idempotent: acking a batch
    that is already sent reports it as such rather than failing, because a
    Shortcut that lost the reply to its first ack will send a second."""
    done = fetch("UPDATE nutrition.health_export "
                 "   SET state = %s, sent_at = now() "
                 " WHERE batch_id = %s AND state = %s RETURNING id",
                 (STATE_SENT, batch_id, STATE_CLAIMED))
    if done:
        return {"acked": len(done), "already": False}
    prior = fetch("SELECT count(*) AS n FROM nutrition.health_export "
                  " WHERE batch_id = %s AND state = %s", (batch_id, STATE_SENT))
    n = int(prior[0]["n"]) if prior else 0
    if n == 0:
        raise ExportError("no such batch")
    return {"acked": n, "already": True}


def release(fetch, batch_id: str) -> int:
    """The batch did NOT reach Health: forget the claim so its days are offered
    again. Only a CLAIMED batch can be released — a sent one is in Health, and
    releasing it would export those days a second time."""
    gone = fetch("DELETE FROM nutrition.health_export "
                 " WHERE batch_id = %s AND state = %s RETURNING id",
                 (batch_id, STATE_CLAIMED))
    if gone:
        return len(gone)
    sent = fetch("SELECT count(*) AS n FROM nutrition.health_export "
                 " WHERE batch_id = %s AND state = %s", (batch_id, STATE_SENT))
    if sent and int(sent[0]["n"]):
        raise ExportError("that batch was already confirmed as written; "
                          "it cannot be released")
    raise ExportError("no such batch")


# ── drift: has a day changed since it was sent? ─────────────────────────────

_DRIFT_SQL = """
    SELECT x.day_date, x.slot, x.n_entries AS sent_entries,
           x.kcal AS sent_kcal, x.protein_g AS sent_protein_g,
           x.carb_g AS sent_carb_g, x.fat_g AS sent_fat_g,
           x.fiber_g AS sent_fiber_g,
           count(e.id) AS now_entries,
           sum(e.kcal) AS now_kcal, sum(e.protein_g) AS now_protein_g,
           sum(e.carb_g) AS now_carb_g, sum(e.fat_g) AS now_fat_g,
           sum(e.fiber_g) AS now_fiber_g
      FROM nutrition.health_export x
      LEFT JOIN nutrition.entry e
             ON e.day_date = x.day_date AND e.slot = x.slot
     WHERE x.state = %s
     GROUP BY x.id
     ORDER BY x.day_date, x.slot
"""

_SLOTS_AFTER_SQL = """
    SELECT e.day_date, e.slot, count(*) AS n
      FROM nutrition.entry e
     WHERE e.day_date IN (SELECT day_date FROM nutrition.health_export
                           WHERE state = %s)
       AND NOT EXISTS (SELECT 1 FROM nutrition.health_export x
                        WHERE x.day_date = e.day_date AND x.slot = e.slot)
     GROUP BY e.day_date, e.slot
    -- A slot that totals zero on every nutrient is skipped at export time, so
    -- its absence from the ledger is not drift. IS DISTINCT FROM, so a NULL
    -- macro counts as "not zero": an entry with no value is a change too.
    HAVING count(*) FILTER (WHERE e.kcal IS DISTINCT FROM 0
                               OR e.protein_g IS DISTINCT FROM 0
                               OR e.carb_g IS DISTINCT FROM 0
                               OR e.fat_g IS DISTINCT FROM 0
                               OR e.fiber_g IS DISTINCT FROM 0) > 0
     ORDER BY e.day_date, e.slot
"""


def drift(fetch) -> list:
    """Sent slots whose entries no longer add up to what was sent.

    A settled day is not supposed to change, so this should always be empty.
    It exists because "should" is not a guard: a backfill script or a hand
    UPDATE can change a locked day, and Health would then hold a number RDS no
    longer agrees with — with nothing anywhere saying so. Reported, never
    auto-corrected: the Shortcut cannot amend a sample.
    """
    out = []
    for r in fetch(_DRIFT_SQL, (STATE_SENT,)):
        diffs = {}
        if int(r["now_entries"]) != int(r["sent_entries"]):
            diffs["n_entries"] = [int(r["sent_entries"]), int(r["now_entries"])]
        for n in NUTRIENTS:
            now = r[f"now_{n}"]
            sent = Decimal(str(r[f"sent_{n}"]))
            now_v = None if now is None else Decimal(str(now))
            if now_v is None or abs(now_v - sent) > Decimal("0.5"):
                diffs[n] = [float(sent), None if now_v is None else float(now_v)]
        if diffs:
            out.append({"day": r["day_date"].isoformat(), "slot": r["slot"],
                        "sent_vs_now": diffs})
    for r in fetch(_SLOTS_AFTER_SQL, (STATE_SENT,)):
        # Entries in a slot the ledger has no row for: added after the day
        # was sent (the HAVING above excludes the legitimate zero-total slot).
        out.append({"day": r["day_date"].isoformat(), "slot": r["slot"],
                    "sent_vs_now": {"n_entries": [0, int(r["n"])]}})
    return out


# ── status ──────────────────────────────────────────────────────────────────

def status(fetch, *, today: date, tz: str) -> dict:
    """One read of everything the export knows, for a human.

    Summary and detail come from the SAME plan (ONE-READ-ONE-STATUS): the
    `ready_days` count is `len(plan.days)`, not a second query that could
    disagree with the list below it.
    """
    cfg = load_config(fetch)
    out: dict = {
        "enabled": cfg.enabled,
        "problems": cfg.problems,
        "start_date": cfg.start_date.isoformat() if cfg.start_date else None,
        "include_unconfirmed": cfg.include_unconfirmed,
        "slot_times": {s: t.strftime("%H:%M") for s, t in cfg.slot_times.items()},
        "timezone": tz,
        "last_closed_day": last_closed_day(today).isoformat(),
    }
    sent = fetch("SELECT count(*) AS items, count(DISTINCT day_date) AS days, "
                 "       min(day_date) AS first_day, max(day_date) AS last_day, "
                 "       max(sent_at) AS last_sent "
                 "  FROM nutrition.health_export WHERE state = %s", (STATE_SENT,))
    s = sent[0] if sent else {}
    out["sent"] = {
        "items": int(s.get("items") or 0), "days": int(s.get("days") or 0),
        "first_day": s["first_day"].isoformat() if s.get("first_day") else None,
        "last_day": s["last_day"].isoformat() if s.get("last_day") else None,
        "last_sent": s["last_sent"].isoformat() if s.get("last_sent") else None,
    }
    ob = open_batch(fetch)
    if ob:
        ob = {**ob, "claimed_at": ob["claimed_at"].isoformat()}
    out["open_batch"] = ob
    plan = plan_for(fetch, cfg, today=today, tz=tz, max_days=MAX_MAX_DAYS)
    out["ready_days"] = [d.isoformat() for d in plan.days]
    out["remaining_days"] = plan.remaining_days
    out["withheld"] = plan.withheld
    out["drift"] = drift(fetch)
    return out
