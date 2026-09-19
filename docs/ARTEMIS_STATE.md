# Artemis / ACOS — Architecture & Build State

> **What this is.** The *authored intent* layer: north star, architecture, roadmap, backlog, and the disciplines that govern the build. It holds **no volatile facts** — IPs, instance IDs, ports, module lists, schema dumps live in `context/CONTEXT.generated.md`, generated from the box. A concrete runtime fact appearing here is a bug; move it to the snapshot. Operating rules live in `CLAUDE.md` at repo root.
>
> **Baseline:** updated at the completion of the SQLite→RDS retirement (7/7 phases). Status markers: ✅ built & verified · 🚧 in flight · ⬜ designed, not built.

---

## 0. North star

A single-operator **AI Chief of Staff** that runs operations autonomously *within guardrails*, proposing before acting. The dividing line is fixed:

> **Artemis does statistics. Ryan does semantics.**
> Artemis counts, connects, surfaces, detects, drafts, executes within rules. Ryan means, names, blesses, adjudicates. Artemis never authors meaning or standing rules.

Trajectory toward "Jarvis" is three additive capabilities on the working system: **unified state** (✅ done — the migration), **memory + learning** (⬜ the cognition layer), **bounded autonomy** (⬜ gated standing automations). Each shippable, reversible, no rewrite.

---

## 1. Current architecture

### Tier 1 — Transactional (system of record) ✅
**RDS PostgreSQL**, three schemas — and as of the migration, the **single source of truth** for all operational state:
- `public` — CRM: persons/companies/relationships/engagements/touch_events, deals, interactions, commitments (CRM-scoped), invoices, financials. *Externally owned by the Lambda CRM API.*
- `acos` — operational: action_items, audit_log, calendar_audit, guardrail_violations, **inbox_threads** (018), **system_state/quiet_state/timezone_overrides** (019), **commitments** (020, personal tracker), processed_billing, schema_migrations.
- `health` — plan, session_log, daily_state, adjustments, training_rules, phase_config.

SQLite (`artemis.db`) is **deleted from the box**; no module imports `sqlite3`; CI guards prevent reintroduction.

### Tier 2 — The agent ✅
`acos.service` on EC2 (`python3.11 -m artemis.main`): APScheduler process running cron jobs + routing `@artemis` mentions to domain handlers. RDS via `knowledge/db.py` (now params-hardened); secrets via `knowledge/secrets.py` (Secrets Manager only, defensive shape-parsing). LLM via Anthropic API.

### Tier 3 — Surfaces ✅
- **Mattermost** (`#artemis-ryan`) — primary, plus scheduled proactive posts.
- **gym-display** (`gym.rdm.is`) — owns structured per-set strength capture.
- **Lambda CRM API** (`api/`) — RDS-backed, for external/frontend consumers. The bot reads RDS directly via `knowledge.db` *except* the three CRM commands (`crm status`/`contacts`/`leads`), which go through this API (key from Secrets Manager).

### Meta — context system ✅
`scripts/context_snapshot.sh` → `context/CONTEXT.generated.md`, committed. Precedence: live repo/DB → snapshot → authored docs → memory. Access via `ssh rdmis` (SSH-over-SSM, instance-ID based — no stale IP). `CLAUDE.md` wires this into every Claude Code session.

---

## 2. Second-brain / lakehouse design ⬜

Three layers; **medallion discipline in Postgres**, not an enterprise lakehouse. S3+DuckDB only when a workload demands it. No Iceberg/Glue/Athena at solo scale.

- **Layer A — Transactional:** RDS (Tier 1). Real.
- **Layer B — Analytical/memory:** bronze→silver→gold as Postgres schemas. Bronze = raw immutable capture; silver = deterministic-rule-cleaned, machine-trusted; gold = confidence-weighted observations that inform but don't act.
- **Layer C — Knowledge (Obsidian):** human-authored semantic surface. **Hard split:** `generated/` (machine-written, freely regenerated) vs `authored/` (Ryan's thinking, never overwritten). Postgres is rebuildable cache; vault file is canon. `pgvector` for embeddings. Deterministic transforms may write back to a note; inferential/model-judgment stays a Postgres projection with confidence+provenance, never silently editing canon.

### Artemis's cognition medallion ⬜ (the "memory + learning")
A medallion over Artemis's **own decisions**:
- **Bronze = `acos.cognition_log`** — every consequential decision, immutable, append-only: decision, `assumptions` (jsonb — highest-value column), confidence, outcome, correction, `manual_gap` flag. `manual_gap=true` entries self-generate the playbook roadmap.
- **Silver = hard rules** — deterministic, Ryan+Claude author, Artemis executes, machine-trusted.
- **Gold = soft rules** — observed tendencies, confidence-weighted, context-conditioned; bias defaults, **never auto-act**. Evolution-ready schema: `(rule, context, confidence, last_reinforced_at, evidence_count, supersedes?)`. Accretion + decay + supersession (Ryan adjudicates growth vs drift). Future `/drift` diffs authored gold vs observed gold.
- **Actionable gold** = self-proposed-but-human-approved playbooks; activation always gated.

> **Note (HEALTH-1 relevance):** a premature, half-built "learning"/`add_note` stub is currently live in routing and confabulating ("I've learned that…"). The cognition layer above is the *correct* version, built deliberately and gated. The stub must come out of live routing — see backlog.

---

## 3. The @artemis ↔ Ryan loop ⬜

Capture (dumb, immutable) → Surface (synthesis surfacer detects high-surprise cross-domain co-occurrence) → Adjudicate (Ryan assigns meaning) → Propose (gated playbook draft) → Build loop (headless Claude Code → branch → build → preview → critique → PR, never auto-merged). Integrity depends on the statistics/semantics wall and activation-gating.

---

## 4. What's built ✅

- **9 playbooks** (PB-001…009) + CRM Write Guard (PB-008).
- **Inbox-zero** triage with state machine (now `acos.inbox_threads`); full-body fetch fix.
- **CRM** on RDS; Lambda API; three Mattermost CRM commands wired (Secrets-Manager key).
- **Health subsystem**: `plan_detail` (verified-live "today's workout"), cardio capture, nutrition + grocery→Postgres, plan reseed + validator.
- **Calendar guards**: dupe-detection + audit (Brad guards), anti-confabulation guard.
- **Context-snapshot system** + `ssh rdmis` over SSM + clean branch hygiene (auto-delete on merge).
- **SQLite→RDS migration — COMPLETE (7/7):** guardrails, life_ops workouts (+ rest-day shim), crm (deleted), inbox (018), quiet_hours (019), commitments (020). `artemis.db` deleted; `execute_query` params-hardened (killed the `%`/quote landmine); no-SQLite CI regression guards in every migrated module. CT-anchoring applied inline where each phase touched date logic.
- **Day phases + timezone-following schedule (WAKE-1) ✅** — quiet/wake/open phases on the active timezone, durable held posts by tier, one cron registry re-registered by `apply_timezone`, `set timezone to <place> [through <date>]`, and the 04:30 wake post. Detail in the day-phase section below.
- **PB-011 Vault / Second Brain Ingest (v1) ✅** — schema `vault` (migration 028); sync (03:30 local cron + on-demand `vault sync` / `digest`); one extraction pass per new note → gated proposals adjudicated with the E3 `approve`/`reject` range syntax; morning-brief digest + journal-diff coverage + undictated-meeting nudge. The vault file is canon; Postgres is a rebuildable projection.

---

### Day phases + timezone-following schedule ✅ (WAKE-1)

The day has three phases, evaluated on the **local wall clock in the active
timezone** (`artemis/quiet_hours.py`):

| phase | Mon–Fri | Sat/Sun | what may post |
|---|---|---|---|
| `quiet` | 17:00 → 04:30 | 22:30 → 07:30 | nothing proactive; posts are **held**, not dropped |
| `wake` | 04:30 → 06:30 | 07:30 → 08:30 | health tier only — the wake post + pre-departure |
| `open` | 06:30 → 17:00 | 08:30 → 22:30 | everything |

Each boundary belongs to the local date it falls on: Friday night goes quiet at 17:00 and Saturday wakes at 07:30; Sunday night goes quiet at 22:30 and Monday wakes at 04:30. Weekend times are `WEEKEND_WAKE_TIME` / `WEEKEND_OPEN_TIME` / `WEEKEND_QUIET_HOURS_START`, read through `quiet_hours.wake_time_on` / `open_time_on` / `quiet_start_on`. Time-bound crons have `*_weekend` twins (`sat,sun`) that share the weekday job's once-per-day guard; the Sunday health review runs at the weekend open (08:30). The SSL and domain checks are weekday-only.

- **Posting tiers.** Every *scheduled* post goes through `posting.post_or_hold(mm, channel, text, tier)`. `health` posts in wake+open, `business` in open only. A held post is appended to a durable JSON list in `acos.system_state` (`held_posts:health` / `held_posts:business`), so a restart between hold and flush keeps it. `job_wake` folds held health notices into the wake post; `job_open` flushes business FIFO. Replies to Ryan's own messages are never held.
- **One cron registry.** `ArtemisScheduler.cron_specs()` is the single source of truth for every cron job (id, method, local h:mm, optional day-of-week, tier). `apply_timezone(tz)` is the **only** registration path — a test asserts no `add_job(..., "cron", ...)` exists outside it. `job_dump()` prints every job's next fire in local + CT and is the verify surface.
- **The schedule follows Ryan.** `@artemis set timezone to <place> [through <date>]` writes `acos.timezone_overrides` (id=1 singleton, TIMESTAMPTZ `expires_at`); `job_tz_sync` (60s, and called directly by the command so there is no lag) re-registers every cron against the new zone and sweeps an expired override. `through 9/23` means all of 9/23 **local-away**; 9/24 runs Central.
- **Duplicate guard.** A zone switch can replay a wall-clock time on the same local date (a Paris override expiring at 17:00 CT would re-arm the 17:00 jobs). `_once_per_local_day(job_id)` keys on `(job_id, local date)` in `system_state`.
- **The wake post** (`artemis/wake.py`) is strictly scoped: today's session, the morning survey, held health notices, pre-departure. No email, inbox, triage, action items, vault digest, or ops alerts — those wait for 06:30.

> **EOD-1 note:** an end-of-day wrap must fire at `QUIET_HOURS_START − 60 min` = **16:00 local**, not later. Anything scheduled at or after 17:00 lands inside the quiet window and will be held until the next morning.

---

## 5. In flight 🚧

Nothing mid-migration. Next build is **HEALTH-1** (below) — top of backlog.

**HEALTH-2 — office gym rebuild (`feat/health-office-gym`).** Ryan trains at the office gym (all Precor); the rower and outdoor bike are retired from the plan. Location is now plan data (`blocks.location` / `blocks.equipment`, static map is fallback); bike branch, `trainer set` override, and cardio weather removed (weather stays for `walk`); modality swap retargeted to office machines; office program seeded via `reseed_health_plan_v2.py --office`. The feat/health-ramp nightly job and `--ramp` reseed are retired (window 7/25-9/11 passed undeployed; it would slide/re-propose over the office plan). **Live since 2026-09-16** (#85, #88): phase 1 anchored Wed 9/16, Wed–Tue week windows, 9/16 → 11/03, pattern Wed A · Thu rest · Fri B · Sat rest · Sun walk · Mon C · Tue Z2, all at the office gym. Inventory correction (2026-09-18): there is no 45° back extension / roman chair — back extensions run on the seated Precor Abdominal / Back Extension machine ("Seated back extension", class `machine`); Strength B rows from 9/18 reseeded. The inventory is unverified in person (OFFICE-WALK). `session_log` 123 and 129 (9/18, "45° back extension", 12 reps, no load) predate the correction and are left as logged; they were done on the seated machine. Day phases per WAKE-1 (wake post 04:30, business 06:30, quiet 17:00).

**HARDEN-1 — post-launch hardening (2026-09-16).**
| Item | Status |
|---|---|
| 1a host guard + no key in the bundle | ✅ merged (gym-display #4), verified: `gym-display.pages.dev/api` → 403, `gym.rdm.is/api` → 302 |
| 1b Access JWT on `/api` | PR gym-display #3 — needs `ACCESS_AUD_PRODUCTION` / `ACCESS_AUD_PREVIEW` set before merge |
| 2 tampering audit | done — no suspect `session_log` rows; see backlog for the 7/19 read flood |
| 3 log redaction / 5 free OWM endpoints | PR #89, deployed on the box pre-merge, weather live |
| 3 key rotations | **pending (Ryan)** — OpenWeatherMap key (24 journal lines) and the health API key (was in the public bundle) |
| 4 9/16 workout check | clean — 12 sets + summary, no duplicates; no `setting=` notes captured |
| 6 session-expired banner | in gym-display #3 |
| 8 ramp dry run | engine incompatible with the office program — keep retired (see RAMP-RETIRE) |

**FRIDAY-1 — check-in-driven morning (2026-09-17).** The fixed 04:45 calibration post is retired. The 04:30 wake post is plan-exact and opens the check-in; the reply is parsed deterministically and adjusts today's `health.plan` row by fixed rules (`blocks.original` + `blocks.adjustment`, `original` to undo), with a 05:15 nudge if nothing arrives. Short morning replies are answered from the DB, and the LLM fallback gets no business data outside the OPEN phase. A plan-claim guard rejects invented exercises, loads, or "last session" figures. Flag: `CHECKIN_ADJUST`. See PB-009.

**PAIN-1 — pain ladder + pattern surfacing (`feat/pain-ladder`, gym-display `feat/pain-chip`; target live before 04:30 Mon 2026-09-21).** Pain gets its own ladder ahead of the soreness rules: pain 4–5 or rising pain (1→2→3 over three consecutive check-in days, each naming the region; a rise from 0 only adds a "rising:" note) → day off; pain 3 → the region's exercises become a mobility block (whole day Mobility / Yoga when the region is primary on ≥ 50% of the session); pain 2 → 80% of the last logged load, rounded down to a reachable load; pain 0–1 → noted. gym-display adds a Pain chip (`pain=<region>:<n>` in set notes). A nightly 21:55 recompute finds exercise × region patterns (≥ 3 hits, ≥ 60%, 8 weeks); the Sunday 08:30 health review (weekend open) posts new/changed ones, the completing check-in mentions it once, and thread replies are stored verbatim as reflections (`dismiss` / `resolved`). Migration 032 (`health.pain_pattern`, `health.reflection`). See PB-009.

**YOGA-1 — Recovery Flow (2026-09-16).** Thu (office) and Sat (home) become `recovery_flow` days: a hands-free two-round mobility flow in gym-display (auto-advance, switch-sides cue, voice, automatic complete/partial logging). Artemis: builder + side validator, day-off-only check-in rules, nudge on, no nag / inferred miss, excluded from the ramp, plan-exact wake post. Migration 033 widens the `session_type` CHECK. Thu 9/17 went live as a `rest_mobility` row carrying flow blocks (the running box predates the new type); 9/19 → 11/03 reseed to `recovery_flow` with the PAIN-1 deploy. See PB-009.

**ENERGY-0 — `daily_state.energy` accepts 0 (2026-09-16).** Migration 031 (#93) relaxed 013's 1–5 CHECK to 0–5 so an `energy 0` check-in can't fail the insert. Applied and verified on RDS 2026-09-16 (rolled-back insert of 0 accepted, 6 rejected).

---

## 6. Backlog (prioritized)

**HEALTH-1 (high) — morning check-in misroute + confabulation loop.** A health check-in ("sleep 7 energy 5 …") is not logged. `detect_health_intent` correctly returns `log_morning_state` and `handle_morning_intent` works (verified: writes `health.daily_state`, returns "Logged: …"). But the LLM classifier returns `general_reply` (0.95), a "Correction re-route" sends it to a confabulating `add_note` path that "learns" fake rules (including rules about its own errors) and captures the wrong message as the note. **Root causes:** (a) deterministic health intent not short-circuiting the LLM classifier; (b) a premature `add_note`/"learning" stub live in routing with no backing store. **Fix:** make positive `detect_health_intent` unoverridable → `handle_morning_intent`; disable/gate the `add_note` stub out of live routing. Verify by re-sending the check-in via Mattermost.

**CONFIRM-ARB — confirm-handler arbitration / bare-`yes` disambiguation (medium; own future branch off `main` after feat/health-ramp merges).** Multiple flows consume a bare `yes`/`no`/`confirm`/`cancel` in `#artemis-ryan`, each gating on its *own* pending-state, and those states are **independent** — several can be live at once. A bare control word is then resolved by fixed `deterministic_chain` order (first-match-wins), which is deterministic but **not** intent-aware: a `yes` meant for flow A can silently execute flow B.

- **Pending stores (independent; nothing clears the others):**
  | flow | store | expiry |
  |---|---|---|
  | calendar create / delete / dupe-override, `rule add`, dossier/org set | in-mem `_pending_confirms[channel]` (single value → these are mutually exclusive *with each other*) | 600s (calendar) / none (rule, dossier) |
  | debrief capture | `acos.system_state` `debrief_pending:{ch}` | 600s |
  | modality swap | `acos.system_state` `modality_swap_pending:{ch}` | 600s |
  | nutrition target / grocery staples | `acos.system_state` keys | 900s / 1800s |
  | **ramp repeat/restart** | `health.ramp_state.pending_payload` | **none** |
  | inbox disposition batch | `_inbox_listing_state[ch]` | none |
- **Chain trace (bare `yes`, ramp + `rule add` both live):** duplicate_override→no · calendar/delete_confirm→no (wrong type) · debrief/swap→no pending · **ramp_confirm→WOULD match (index before rule_command)** · rule_command never reached. The confirm backstop only re-runs the in-memory calendar handlers, so it doesn't help.
- **Worst case:** the ramp pending **never expires**, so it can coexist for days with a just-created `rule add`/dossier pending; the user types `yes` to activate the rule and (pre-fix) the ramp repeat/restart fires instead.
- **Interim (shipped on feat/health-ramp):** `ramp_confirm` matches **only** the qualified `yes ramp`/`no ramp` and never a bare control word — it removes ramp from the bare-`yes` contention entirely. The *general* race (debrief↔swap↔nutrition↔rule↔disposition ordering) remains.
- **Fix (this item):** a shared `_count_open_pendings(channel_id)` helper over all stores; when **>1** pending is open and a bare control word arrives, reply with a disambiguation prompt (`reply `yes ramp` or `yes rule``) and consume the word safely instead of first-match-wins; teach each confirm handler to also accept its qualified form. Keep first-match-wins when exactly one pending is open.

**RAMP-RETIRE — delete the dormant feat/health-ramp engine (medium — the confirm route is live).** HARDEN-1's dry run showed the engine cannot be re-pointed as-is: Sun–Sat windows, Sunday-night evaluation, rest days counted and marked missed, a 5/5 threshold the 4-session office week can never meet, and a restart proposal that re-seeds the home-gym program — accepting it with `yes ramp` deletes the office plan. Adherence evaluation is EVAL-1 (report-only, below), not a re-pointed engine. Original note: HEALTH-2 unregistered the nightly job and made `--ramp` refuse, but `artemis/health_ramp.py`, `_handle_ramp_confirm` (+ its chain entry and routing-gate tests), `tests/test_health_ramp.py`, and the `health.ramp_state` table (migration 030) remain. Nothing writes `ramp_state.pending_payload` any more, so `yes ramp` is inert. Remove them (drop-table migration, migrate-first) or re-point the engine at the office program. With ramp gone, CONFIRM-ARB's worst case (never-expiring ramp pending) no longer applies.

**WATCH-1 — Apple Watch ingest via Health Auto Export (medium; before REPORT-1 so reports can include watch data).**
- **Endpoint:** `POST /api/health/ingest` on the Lambda (`api/app/routers/health.py`), accepting the Health Auto Export JSON payload (`data.metrics[]` + `data.workouts[]`). It has **its own key** in Secrets Manager via `knowledge/secrets.py`, checked on its own dependency. Never the display key, and the display key must not work here.
- **Idempotent** by (metric, timestamp); workouts by (workout, start). A re-sent or overlapping export is a no-op, and the response reports inserted/duplicate counts.
- **Payload limits:** a large backfill can exceed the Lambda payload limit and the stage throttle (5 req/s, burst 10), so the export is configured in batches.
- **Store:** one table in `health`, with a row per sample: metric, timestamp (UTC), numeric value, unit, and the raw JSON kept. It covers:
  - sleep hours and phases (deep/REM/core/awake)
  - resting heart rate, HRV, weight
  - workout records: type, start, duration, average and max heart rate, calories
- **Migration: propose first.** The DDL and the unique key go to Ryan for review before anything is written. Deploy migrate-first.
- **Morning check-in pre-fill:** sleep, resting heart rate and weight come from last night's data, where "last night" is anchored to the active timezone (`quiet_hours.local_now()`), never UTC. Ryan only fills in energy and soreness. A value he types always wins over the watch. The reply says which values came from the watch, and when there's no watch data it says so plainly instead of guessing. Pre-fill stays deterministic and must not change intent routing.
- **Workout match:** a watch workout attaches to the logged session by time overlap with that day's `session_log`. It fills `session_log.hr_avg` / `hr_peak`; calories come from the watch table. With no overlap there's no match, and it's never attached by date alone.

**EVAL-1 — read-only weekly evaluator (medium; before REPORT-1).**
- **Scope:** read-only. It makes **no plan changes and no recommendations**, and holds no confirm routes or pending state. It's the small report-only evaluator RAMP-RETIRE calls for, and it doesn't reuse `health_ramp.py`.
- **Week and timezone:** weeks are the office Wed–Tue windows counted from `health_office.WEEK1_START`. Dates come from the active timezone.
- **Inputs:** the week's `health.plan` rows and `health.session_log` rows (`logged_via <> 'inferred'`).
- **Output**, as one structured result:
  - sessions done vs planned (training days only; rest days are neither done nor missed)
  - per-exercise load change vs the prior week (top logged weight per exercise; an exercise with no load either week is reported as such, not as 0)
  - average RPE (`session_log.rpe_actual`) vs the cap (`plan.target_rpe`)
  - missed sessions
  - adjustments applied (`blocks.adjustment`: date, rules fired, reason)
- **Consumers:**
  - the weekly report (REPORT-1): the full Wed–Tue week, Tuesday evening.
  - a Sunday post: Sunday falls mid-week, so it covers the week to date (Wed–Sat) and is **labelled partial**.
- **Tests:** a full week, a partial week, a week with a missed session, a week with adjustments, an empty week, and an exercise renamed between weeks (no false "new exercise" load change).

**EXPORT-1 — `scripts/export_report.py` (small; ahead of REPORT-1; built).**
- **Usage:** `--daily YYYY-MM-DD`, `--weekly <week start>`, `--monthly YYYY-MM` on the box. It writes `/tmp/artemis-report-<type>-<period>.md` and a plain `.html`; open the HTML in a browser and print to PDF.
- **Reads only:** `health.plan`, `health.session_log` (inferred rows excluded), `health.daily_state`, `health.pain_pattern`, and `acos.system_state.health_program`. No WeasyPrint, no S3, no new dependencies.
- **Contents** follow the REPORT-1 spec. Watch data, nutrition and the weekly evaluation print "not yet tracked", as does machine settings until any are logged.
- **Pre-program rows:** plan rows before the program anchor (the old phase-3 home plan) are listed as not counted, never as missed.
- **Renamed exercise:** 45° back extension logs are grouped under Seated back extension for comparisons, with a footnote.
- REPORT-1 replaces the output step, and the builders carry over.

**REPORT-1 — PDF training reports (medium; after STATUS-1, WATCH-1 and EVAL-1).**
- **Prerequisites.** These are numbered steps, each verified on the box or in AWS before any report work starts:
  1. **WeasyPrint + pango on the box.** Install pango (and its cairo/harfbuzz dependencies) with `dnf`, and `weasyprint` for `/usr/bin/python3.11`. Add both to the deploy path. **Verify:** a test HTML page renders to a PDF on the box with fonts and CSS applied.
  2. **S3 bucket and permissions.** Create a private bucket (no public access, encrypted). Give the EC2 role put/get on the reports prefix and the Lambda role get plus signing. **Verify:** the box writes an object, the Lambda signs a link to it, the link downloads, and the same object is not reachable without a signature.
  3. **Mattermost upload call.** Add file upload to `artemis/mattermost.py` (`POST /files`, then a post carrying `file_ids`). **Verify:** a test PDF posts to `#artemis-ryan` as an attachment. **If upload proves awkward, post a signed S3 link instead. Don't block REPORT-1 on it.**
- **Rendering:** PDFs generated on the box with WeasyPrint from HTML/CSS templates, styled to match gym-display. No new services (prerequisite 1).
- **Daily** (after a session, 1 page):
  - the session and every set: weight × reps and effort
  - machine settings, total time
  - the check-in and any adjustment
  - watch data when available: average and max heart rate, calories
  - Machine settings are shown only when logged; today they're never captured (HARDEN-1 follow-up).
- **Weekly** (Tuesday evening, closing the Wed–Tue week; 2 pages):
  - adherence and all sessions
  - weight progress per exercise, with the change from last week
  - check-in trends, body weight
  - pain and soreness summary, open patterns (`health_patterns`)
  - the EVAL-1 result for the week, as data. Without EVAL-1 the section is omitted, never filled in.
- **Monthly** (1 page, less detail, no per-set detail):
  - sessions done vs planned
  - main lifts start vs end (proposed: each session's first lift — leg press, DB goblet squat, DB Romanian deadlift)
  - body weight trend, phase and week progress
  - a highlights list
- **Storage:** S3 (prerequisite 2), keyed by type and date (e.g. `reports/{daily|weekly|monthly}/{period}.pdf`), served by signed link.
- **Idempotent per period:** re-running replaces the object and never duplicates it. It posts once per period unless forced.
- **Delivery:**
  - Posted to Mattermost as an attachment when generated (prerequisite 3; a signed S3 link is the fallback).
  - Downloadable from the Status page: a "Reports" section showing the last 12, backed by a Lambda endpoint that lists and signs links on request. Signed links are generated per request, never stored.
- **Timezone:** all period boundaries use the active timezone (`local_today()`; SQL with `get_active_timezone()` passed as a parameter).
- **Tests:**
  - a golden-file layout for each type
  - an empty period
  - a partial week
  - a period with check-in adjustments

**DIET-1 — nutrition logging, rollup and dietitian reports (medium; after WATCH-1 for energy out, and extends REPORT-1).**
- **Existing code this replaces (coverage-check first):** a nutrition subsystem is already live from migration 016.
  - Tables `health.nutrition_target`, `health.meal`, `health.nutrition_log`: all empty in RDS as of 2026-09-18.
  - A `nutrition_budget` function.
  - Intents `set_nutrition_target` / `log_nutrition` / `nutrition_status` in `artemis/health.py`.
  - The `health.meal` → `acos.grocery_list` staples write in `artemis/life_ops.py`.
  - DIET-1 moves nutrition to a `nutrition` schema. Per "one system of record" the `health.*` tables are retired, not kept alongside. Map every capability above (the staples write included) to its new home before dropping anything, and stop on any capability with no home.
  - The `nutrition_budget` default hard-codes `'America/Chicago'`. That violates the timezone discipline and is fixed in the move.
- **Schema (propose the migration first):**
  - `nutrition.food`: saved foods with macros, source and source ID.
  - `nutrition.entry`: date, meal, food, quantity, macros, source, source ID, confidence.
  - `nutrition.target`: calories, protein, carbs, fat, effective date, set by the dietitian.
  - Deploy migrate-first.
- **Recording is default-to-plan; logging is by exception.**
  - **00:15 local pre-fill:** plan days are pre-filled from the Notion meal plan. Meals stay `assumed`. **There is no prompt and no nudge**, and no scheduled nutrition post of any kind in quiet hours. If Notion is unreachable, nothing is pre-filled and the day says so; no plan is invented.
  - **Morning check-in line:** the check-in reply appends one line: "Yesterday logged as planned — reply `fix` to correct." It's omitted when the previous day had corrections or had no plan. It's part of the reply, not a separate post, and it must not change check-in intent routing (HEALTH-1).
  - **`fix`** opens a correction for the **previous** day.
    - It routes deterministically, and only when that day is still open. It's added to the bare-control-word inventory in CONFIRM-ARB.
    - Corrections are accepted any time within **48 hours of the day's end** (local midnight in the active timezone). After that the day locks and a correction is refused with a plain reply.
  - **Status is separate from confidence.**
    - Entry status: `assumed` | `corrected`.
    - Day status: `assumed` | `corrected` | `locked_unconfirmed`, one row per day. A still-`assumed` day becomes `locked_unconfirmed` at the 48-hour lock; a `corrected` day stays `corrected`.
    - Reports show all three day statuses. Both go in the schema proposal.
- **Deviations, one line each:** "lunch: chipotle chicken bowl", "skipped breakfast", "+2 beers". Each is parsed into entries against the source order: saved foods → USDA FoodData Central (API key in Secrets Manager) → Open Food Facts. Every entry stores its source and ID.
- **Confidence tiers:**
  - `exact`: plan, barcode or saved food.
  - `matched`: database lookup.
  - `estimated`: a photo, or text that is still vague after the one question.
  - Macros are never invented silently. An unmatched food first gets exactly one question ("how big was the portion?" or "which brand?"). Only if the answer is still vague is it stored as `estimated`.
  - `estimated` is always visibly marked in every report.
  - The existing `estimate_nutrition()` (a Claude estimator writing `estimated=TRUE`) is replaced by this tiered path, not kept alongside.
- **Photo:** a photo posted to Mattermost with no text returns a best guess and a portion question, stored `confidence=estimated`. Being a model's guess, it's labelled as a guess in the reply and never presented as a lookup.
- **Voice:**
  - iOS dictation into Mattermost needs no build.
  - Also provide an Apple Shortcut, "Log meal", that takes dictation and posts to a new `POST /api/nutrition/log` on the Lambda. It has **its own key** (Secrets Manager, stored only in the Shortcut) and goes through the same parser. That lets it work from the Home Screen and the watch.
  - Deliver the Shortcut as documented build steps: Shortcuts files are signed and can't be generated from the repo.
- **Barcode:** `gym.rdm.is/scan` in gym-display. The camera scans the barcode → Open Food Facts → confirm the portion → log.
  - It must work on iPhone Safari. iOS Safari has no `BarcodeDetector`, so it needs a JS decoder (e.g. ZXing), `getUserMedia` over HTTPS, and a manual-entry fallback.
  - It logs through gym-display's same-origin `/api` proxy behind Cloudflare Access. **It must not use the Shortcut key; no key in the bundle** (the HARDEN-1 lesson).
- **Notion meal plan:** read the meal plan database, and **report its exact property names to Ryan before building**. "as planned" logs that meal's macros. Notion is the meal-plan source; `health.meal` retires with the other tables and no copy is kept in RDS, which would be a second store.
- **Logging:** extend the existing nutrition handler (deterministic intent routing is unchanged).
  - One-line entries in Mattermost.
  - A same-day `undo last`.
- **Shortcuts:**
  - Any food logged twice becomes a one-word shortcut. Reserved words (`yes`, `no`, `undo`, `fix`, command verbs) are never shortcuts.
  - "usual breakfast" (and the same for other meals) resolves to that meal's most frequent entry over the last 14 days; a tie goes to the most recent. With no history, Artemis says so rather than picking one.
- **Daily rollup (21:50 local, silent, no post):**
  - totals in
  - energy out = baseline + watch active energy
  - net
  - the 7-day weight average
  - **Baseline is not defined yet.** Ryan decides whether it's the watch's basal energy or a fixed number he or the dietitian provides. Artemis never estimates it.
  - With no watch data, net is reported as baseline-only and flagged, not left blank or estimated.
- **Reports (extend REPORT-1):**
  - **Daily:** meals, totals, comparison to target.
  - **Weekly:** daily averages, protein per day, net per day, weight trend, workout summary.
  - **Monthly for the dietitian:** one page, clinical tone, data only, with no advice or judgment language. It covers:
    - daily average intake and macros, protein per day
    - weight start/end/trend
    - sessions completed, activity minutes
    - logging completeness (days logged out of days, split into `assumed` / `corrected` / `locked_unconfirmed` days)
    - the percentage of intake from each confidence tier (`exact` / `matched` / `estimated`)
  - **Email:** the monthly PDF can be emailed as an attachment, but by the Brad Spaits rule Artemis only prepares a Gmail **draft**. Ryan sends it.
- **Provisional seed:** 2,100 calories and 175 g protein, marked "provisional — pending dietitian", with an effective date. Ryan chose these values; it's a human-run seed, not an Artemis decision. The dietitian's target later closes it cleanly by effective date.
- **Targets:** `@artemis set nutrition target calories 2200 protein 180 …` writes `nutrition.target` with an effective date. It keeps the existing propose-then-confirm flow. Artemis never sets or changes a target itself and never recommends a deficit. The existing target parser is a Claude call; replace it with a deterministic parse (targets are numbers Ryan typed).
- **Tests:**
  - parsing with and without a plan match
  - an unmatched food asks rather than guesses
  - `undo last`
  - rollup math
  - PDF golden files
  - missing-watch-data fallback
  - a guard that no code path writes macros without a source ID, except `estimated` entries, which carry their tier and origin (photo / vague text)
  - a day with a correction (status `corrected`)
  - a `locked_unconfirmed` day in the report, alongside `assumed` and `corrected` days
  - the barcode path
  - a photo marked estimated
  - "usual breakfast" resolution
  - the tier percentages
  - the morning line appears only when relevant (a plan day with no corrections), and is absent after a corrected day or a day with no plan
  - `fix` corrects the previous day, not today
  - a correction at 47 h after the day's end works; at 49 h it is refused and the day is `locked_unconfirmed`
  - no scheduled nutrition post exists in quiet hours (registry check)

**OFFICE-WALK — confirm the office-gym inventory in person (medium; blocks the `TODO(office)` values).** The PB-009 inventory came from Ryan's description and has never been checked on the floor — the 45° back extension was listed and doesn't exist. Walk the gym once and confirm every entry against PB-009 / `health_office.py` / gym-display `src/lib/equipment.ts`: each Precor machine exists (and which are combo stations), the Precor pin-stack step per machine (default 10 lb; record overrides in `STEP_OVERRIDES` and `health_regions.STACK_STEP`), the S3.23 functional trainer stack step, the Icarian Smith effective bar weight (`SMITH_BAR_LBS`, currently 0), the Olympic bar weight, the hex dumbbell range (5–45 assumed), the plate set (one each 45/35/25/10/5 per side assumed), benches, captain's chair/dip tower, cardio pieces, Stretch Trainer, balls and mats. Fix anything wrong in code and docs, then reseed the affected rows.

**PB9-CRON — morning prompt times vs office arrival (low).** The PB-009 morning prompt schedule predates the office gym; retime once Ryan's office arrival time is confirmed (TODO in PLAYBOOKS.md).

**CRM-2 / COMMIT-1 — two-store seams (medium).** Two contact stores: `public.contacts`/`organizations` (CRM API) vs `public.persons`/`companies` (Write Guard, PB-008). Two commitment stores: `acos.commitments` (personal tracker) vs `public.commitments` (CRM contact/deal-scoped). Both intentional/legitimate, but the boundaries need documenting before the cognition layer reasons over them. `crm status` reads only the `contacts`/`public.commitments` side.

**CRM-1 — `leads` has no real meaning (low).** No lead-status concept in RDS (`leads` returns same as `contacts`). Lead-status probably belongs on `deals`/pipeline, not contacts. Future schema decision, not a bug.

**INBOX-1 — triage doesn't populate inbox-zero (medium).** Triage summarizes live but never calls `upsert_thread`, so the snooze/waiting/done lifecycle has no data. Either wire triage to persist threads, or retire the lifecycle if superseded.

**CAL-1 — double calendar-audit write (low).** Three create/delete sites now write `acos.calendar_audit` twice (`log_calendar_action` + `_audit_calendar_write`). Dedupe; decide canonical writer.

**SCHEMA-DRIFT — deploy must run migrations (process).** Merging a migration doesn't apply it (016/017 were unapplied for weeks). Add `run_migrations.py` to the deploy path or a scheduled drift check (it's idempotent-safe).

**VAULT-UNAPPROVE — gated `unapprove <n>` reversal (low).** A gated reversal of an approved vault proposal using its `target_ref` (`commitment:N` / `dossier_entry:N` / `org_note:N`): flip the proposal back to pending and undo/retire the written row. Human-gated (propose-then-confirm); do not build the auto-path. Deferred from OPS-1.

**OPS-RUNBOOK — runbook registry expansion (low).** `artemis/opsdiag.py` seeds vault-pat-auth / vault-secret-missing / vault-clone-network / google-oauth-refresh / rds-unreachable. Add a TLS/cert-expiry class (feed the existing SSL monitor's findings through `classify`), and more classes as new failure shapes surface in the audit log (`action='failure'`, `metadata.failure_class`).

**EXTRACT-DEDUPE-MONITOR — extraction dedupe monitoring (low).** The OPS-1 prompt tuning added cross-type dedupe + commitment-direction + decision-ownership discipline. Watch the proposal stream for residual duplicates (same fact under two types) and mis-directed commitments; if the prompt guidance proves insufficient, add a deterministic post-extraction dedupe pass over `vault.extraction_proposal` before proposals surface.

**Papercuts.** SIGTERM-ignored shutdown (90s SIGKILL every restart — likely websocket/scheduler not closing on signal); Mattermost websocket flap (~60s reconnect loop); SSO re-auth friction (longer session or self-healing ProxyCommand); Mac-vs-EC2 prompt confusion (distinct prompt / dedicated tab).

---

**PAIN-1 follow-ups (open).**
- **Rising pain is explicit-only** — every day of the chain must name the region with a number; a day that doesn't mention it breaks the chain. If Ryan tends to omit a region on low-pain days, real rises will go unflagged.
- **Load rounding is a Python port** of gym-display `equipment.ts` (`health_regions.lighter_load`); the office `TODO(office)` values (stack step, Smith bar) live in both places until they're measured.
- **Check-in mention thread** — the reflection link for a check-in mention is one-shot for the same local day (`acos.system_state`); Sunday posts link permanently via `pain_pattern.post_ids`.
- **Pattern reflections aren't read by anything yet** — stored for Ryan; no summarization or LLM use.
- **`test_health_office`** has 5 assertion failures identical on `main` (date-window checks); not from PAIN-1.

**HARDEN-1 follow-ups (open).**
- **Office equipment unconfirmed** — Precor pin-stack step (default 10 lb), Icarian Smith bar weight, and the hex dumbbell range are guesses; `TODO(office)` in gym-display `src/lib/equipment.ts`. Tracked as **OFFICE-WALK** (backlog).
- **Pre-existing test failures — fixed 2026-09-19 (TEST-STUBS).** `test_confirm_dispatch` now runs on an in-memory DB (it had been writing test rows into production `acos.calendar_audit` / `acos.guardrail_violations` whenever run on the box); `test_commitments_rds` matches the PB-010/011 `add_commitment` and `#id` formats, and its live tier applies the 024/028 columns; `test_opsdiag` moved to `tests/` (run from `artemis/`, `artemis/calendar.py` shadowed the stdlib).
- **Lambda deploy drift** — `rdmis-crm-api` is deployed by hand (`api/deploy.sh`); there is no CI deploy and no check that the live function matches `main`.
- **API Gateway has no access logs or detailed metrics** — per-route counts don't exist. On 2026-07-19 (**10:00–15:00 CDT**, 15:00–20:00 UTC — corrected from an earlier 05:00–10:00 misreading of local-labelled CloudWatch buckets) the API served **154,023 authenticated, successful reads** (~10/s, 0 4xx, no writes); cause unattributed, most likely a client refetch loop during that day's OPS-2 dashboard work (10:35–14:45 CDT, ending with a 14:45 fix for silent reload loops). A second flood ran 2026-05-04 23:00 CDT → 05-05 ~15:00 (~394k requests). Since 2026-09-16 the stage is throttled to 5 req/s, burst 10. Enable HTTP API access logging.
- **Machine settings never captured** — the 9/16 session logged 6 machine sets with no `setting=` note; Artemis's own parser also discards seat/pin.
- **WAKE-2** — the next day-phase slice (not started).
- **TV retirement decision** — the gym-display TV layout is gone; decide whether the TV is retired for good or gets a read-only glance view.

## 7. Operating disciplines (non-negotiable)

Propose-then-confirm · the Brad Spaits rule (no autonomous external comms; activation gates) · trust-the-data-not-the-report · verify-on-the-live-box · statistics-vs-semantics wall · generated-vs-authored split · CT-anchored "today" · one system of record (RDS) · no-tokens-on-disk (Secrets Manager) · solo-scale (no enterprise patterns) · `feat/*`→PR→`main`, migrate-first deploy. Full detail in `CLAUDE.md`.

---

## 8. Why the order

1. **Unified state (✅ done)** — no trustworthy analytical/cognition layer over a split-brain. Done first, correctly.
2. **Knowledge layer (vault) (✅ v1 — PB-011)** — the semantic surface Ryan authors; the synthesis surfacer needs it populated. Precedes the cognition medallion (the June decision, now reality): the cognition layer reasons *over* adjudicated knowledge, so the knowledge substrate is laid first.
3. **Cognition layer** — learning/self-proposal needs one coherent decision log. (HEALTH-1's confabulating stub is what happens when this is half-built and ungated — build it deliberately.)
4. **Bounded autonomy** — safe only once state is trustworthy, knowledge adjudicated, decisions logged, automations gated.

The migration was load-bearing foundation, now laid. The system has one source of truth; the next layers can be built on solid ground.
