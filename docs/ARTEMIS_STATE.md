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

> **Note (HEALTH-1, closed 2026-09-19):** the premature "learning"/`add_note` stub is out of live routing (HEALTH-1 A1/A2); only the unrouted `_handle_correction` function remains (see CORRECTION-DEADCODE). The cognition layer above is still the *correct* version, built deliberately and gated.

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

Nothing mid-migration. HEALTH-1 is closed (verified 2026-09-19). Next builds, in order: **WATCH-1 → EVAL-1 → REPORT-1 → DIET-1** (backlog).

**Shipped 2026-09-17 → 09-19** (all merged and live; details in the PRs):
- **STATUS-1** — the Status page rebuild: `GET /health/overview` on the Lambda (#98, key-gated, reads `acos.system_state.health_program` with a plan-row fallback) and gym-display #10.
- **Seated back extension** (#100, gym-display #11) — no 45° back extension in the office gym; Strength B rows 9/18 → 10/30 reseeded. `session_log` 123 and 129 (9/18) predate the correction and are left as logged.
- **Recovery Flow rows** — the 13 Thu (office) / Sat (home) rows 9/19 → 10/31 reseeded to `recovery_flow`; migrations 032 + 033 applied.
- **EXPORT-1** (#101) — `scripts/export_report.py`.
- **Weekend schedule** (#102) — Sat/Sun wake 07:30, business 08:30, quiet 22:30; the Sunday health review at 08:30. First live weekend 9/19: wake 07:30, nudge 08:15, open 08:30.
- **Location-aware departure block** (#103) — the wake-post checklist on office days only.
- **YOGA-2** (gym-display #12) — original SVG pose figures in the Recovery Flow (active pose, 5 s preview, switch card, ready list).
- **TEST-STUBS** (#104) — the three pre-existing test failures fixed; `test_confirm_dispatch` no longer writes to production.

**HEALTH-2 — office gym rebuild (`feat/health-office-gym`).** Ryan trains at the office gym (all Precor); the rower and outdoor bike are retired from the plan. Location is now plan data (`blocks.location` / `blocks.equipment`, static map is fallback); bike branch, `trainer set` override, and cardio weather removed (weather stays for `walk`); modality swap retargeted to office machines; office program seeded via `reseed_health_plan_v2.py --office`. The feat/health-ramp nightly job and `--ramp` reseed are retired (window 7/25-9/11 passed undeployed; it would slide/re-propose over the office plan). **Live since 2026-09-16** (#85, #88): phase 1 anchored Wed 9/16, Wed–Tue week windows, 9/16 → 11/03, pattern Wed A · Thu rest · Fri B · Sat rest · Sun walk · Mon C · Tue Z2, all at the office gym. Inventory correction (2026-09-18): there is no 45° back extension / roman chair — back extensions run on the seated Precor Abdominal / Back Extension machine ("Seated back extension", class `machine`); Strength B rows from 9/18 reseeded. The inventory is unverified in person (OFFICE-WALK). `session_log` 123 and 129 (9/18, "45° back extension", 12 reps, no load) predate the correction and are left as logged; they were done on the seated machine. Day phases per WAKE-1 (wake post 04:30, business 06:30, quiet 17:00).

**HARDEN-1 — post-launch hardening (2026-09-16).**
| Item | Status |
|---|---|
| 1a host guard + no key in the bundle | ✅ merged (gym-display #4), verified: `gym-display.pages.dev/api` → 403, `gym.rdm.is/api` → 302 |
| 1b Access JWT on `/api` | PR gym-display #3 — needs `ACCESS_AUD_PRODUCTION` / `ACCESS_AUD_PREVIEW` set before merge |
| 2 tampering audit | done — no suspect `session_log` rows; see backlog for the 7/19 read flood |
| 3 log redaction / 5 free OWM endpoints | PR #89, deployed on the box pre-merge, weather live |
| 3 key rotations | ✅ done 2026-09-16 — OpenWeatherMap and health API keys (Secrets Manager `LastChangedDate` 9/16) |
| 4 9/16 workout check | clean — 12 sets + summary, no duplicates; no `setting=` notes captured |
| 6 session-expired banner | in gym-display #3 |
| 8 ramp dry run | engine incompatible with the office program — deleted in RAMP-RETIRE (2026-09-19) |

**FRIDAY-1 — check-in-driven morning (2026-09-17).** The fixed 04:45 calibration post is retired. The 04:30 wake post is plan-exact and opens the check-in; the reply is parsed deterministically and adjusts today's `health.plan` row by fixed rules (`blocks.original` + `blocks.adjustment`, `original` to undo), with a 05:15 nudge if nothing arrives. Short morning replies are answered from the DB, and the LLM fallback gets no business data outside the OPEN phase. A plan-claim guard rejects invented exercises, loads, or "last session" figures. Flag: `CHECKIN_ADJUST`. See PB-009.

**PAIN-1 — pain ladder + pattern surfacing (`feat/pain-ladder`, gym-display `feat/pain-chip`; target live before 04:30 Mon 2026-09-21).** Pain gets its own ladder ahead of the soreness rules: pain 4–5 or rising pain (1→2→3 over three consecutive check-in days, each naming the region; a rise from 0 only adds a "rising:" note) → day off; pain 3 → the region's exercises become a mobility block (whole day Mobility / Yoga when the region is primary on ≥ 50% of the session); pain 2 → 80% of the last logged load, rounded down to a reachable load; pain 0–1 → noted. gym-display adds a Pain chip (`pain=<region>:<n>` in set notes). A nightly 21:55 recompute finds exercise × region patterns (≥ 3 hits, ≥ 60%, 8 weeks); the Sunday 08:30 health review (weekend open) posts new/changed ones, the completing check-in mentions it once, and thread replies are stored verbatim as reflections (`dismiss` / `resolved`). Migration 032 (`health.pain_pattern`, `health.reflection`). See PB-009.

**YOGA-1 — Recovery Flow (2026-09-16).** Thu (office) and Sat (home) become `recovery_flow` days: a hands-free two-round mobility flow in gym-display (auto-advance, switch-sides cue, voice, automatic complete/partial logging). Artemis: builder + side validator, day-off-only check-in rules, nudge on, no nag / inferred miss, plan-exact wake post. Migration 033 widens the `session_type` CHECK. Thu 9/17 went live as a `rest_mobility` row carrying flow blocks (the running box predates the new type); 9/19 → 11/03 reseed to `recovery_flow` with the PAIN-1 deploy. See PB-009.

**ENERGY-0 — `daily_state.energy` accepts 0 (2026-09-16).** Migration 031 (#93) relaxed 013's 1–5 CHECK to 0–5 so an `energy 0` check-in can't fail the insert. Applied and verified on RDS 2026-09-16 (rolled-back insert of 0 accepted, 6 rejected).

---

## 6. Backlog (prioritized)

**SCHEDULE-2 — reschedule the lifting days for the cycle — DONE 2026-09-19 (#119, #121, #122).** Decided, built, reseeded and verified in RDS; the Z2 days later gained the 5 min Stretch Trainer cooldown. History follows. **Was urgent:** with the anchor confirmed, **Fri 2026-09-25 is Richfield** and **Mon 2026-09-28 is a travel day**, and both are seeded with office-equipment strength sessions (B, 7 exercises; C, 6). That's 6 and 9 days out. The plan is Wed A / Fri B / Mon C, but under CYCLE-1 Friday is a `wi` day and Monday is `travel` or Richfield, so two of three lifting days fall outside the office gym. **Tue/Wed/Thu are the only days that are office days in both weeks.** Decide the lifting days first: it shrinks LOCATION-1, because the substitution table then only has to cover what's genuinely needed (mostly the WI days' flows and cardio) instead of every office strength exercise. The conflict repeats every cycle: per 14 days, the `wi` Friday and the `travel` Monday collide with 2 of the 6 lifting sessions. **DECIDED 2026-09-19 and built (#119):** lift on the 1st, 3rd and 4th office day of each cycle week — wk 1 Mon/Wed/Thu, wk 2 Tue/Thu/Fri — as A, B, C. Z2 takes the office non-lift days; Recovery Flows take the `wi` days (mat travels); walks cover Brown Deer and the travel Monday. Program weeks moved to Sun–Sat and the program now ends Sat 10/31. The B/C back-to-back pair each week is **accepted and intentional**.
- ~~**Known gap — wake times are not yet cycle-aware.**~~ **Closed 2026-09-19 by CYCLE-1 (#123):** the wake, nudge, open, brief and quiet jobs are location-derived, so Fri 9/25 wakes at Richfield's 06:00.
- **Consequence:** with the flows on `wi` days, no flow is scheduled at the office. **Resolved 2026-09-19 (#121):** the office Z2 days carry a 5 min Stretch Trainer cooldown, so it stays in the program. The office flow variant still builds if a flow is ever put back on an office day.

**HEALTH-1 — morning check-in misroute + confabulation loop — CLOSED 2026-09-19.** Fixed by HEALTH-1 A1/A2 (`add_note` and the "I've learned…" correction re-route removed from live routing) and FRIDAY-1 (the deterministic `morning_flow` handler ahead of the LLM). Verified on a real message: the 9/19 08:12 check-in ("Sleep 9, energy 4, sore 1 right knee, …") was dispatched to `morning_flow` and stored in `health.daily_state` in ~75 ms, no classifier involved; the 08:15 nudge then correctly stayed silent. 9/18 has no check-in because none arrived (only the 05:15 nudge is logged), not a misroute. Follow-ups: CHECKIN-TEXT, CORRECTION-DEADCODE.

**CONFIRM-ARB — confirm-handler arbitration / bare-`yes` disambiguation (medium).** Multiple flows consume a bare `yes`/`no`/`confirm`/`cancel` in `#artemis-ryan`, each gating on its *own* pending-state, and those states are **independent** — several can be live at once. A bare control word is then resolved by fixed `deterministic_chain` order (first-match-wins), which is deterministic but **not** intent-aware: a `yes` meant for flow A can silently execute flow B.

- **Pending stores (independent; nothing clears the others):**
  | flow | store | expiry |
  |---|---|---|
  | calendar create / delete / dupe-override, `rule add`, dossier/org set | in-mem `_pending_confirms[channel]` (single value → these are mutually exclusive *with each other*) | 600s (calendar) / none (rule, dossier) |
  | debrief capture | `acos.system_state` `debrief_pending:{ch}` | 600s |
  | modality swap | `acos.system_state` `modality_swap_pending:{ch}` | 600s |
  | nutrition target / grocery staples | `acos.system_state` keys | 900s / 1800s |
  | inbox disposition batch | `_inbox_listing_state[ch]` | none |
- **Chain trace (bare `yes`, two pendings live):** the first handler in `deterministic_chain` whose own store is pending wins; the others are never reached. The confirm backstop only re-runs the in-memory calendar handlers, so it doesn't help.
- **Worst case (now smaller):** the never-expiring ramp pending is gone with RAMP-RETIRE (2026-09-19); the `rule add` / dossier pendings still never expire, so they can coexist for days with a shorter-lived one.
- **Interim:** none left — the qualified `yes ramp` / `no ramp` route was deleted with the ramp engine. The general race (debrief↔swap↔nutrition↔rule↔disposition ordering) remains.
- **Fix (this item):** a shared `_count_open_pendings(channel_id)` helper over all stores; when **>1** pending is open and a bare control word arrives, reply with a disambiguation prompt (e.g. `reply `yes swap` or `yes rule``) and consume the word safely instead of first-match-wins; teach each confirm handler to also accept its qualified form. Keep first-match-wins when exactly one pending is open.

**RAMP-RETIRE — RETIRED 2026-09-19 (#111).** Deleted the dormant feat/health-ramp engine (`artemis/health_ramp.py`, the `yes ramp` / `no ramp` confirm route, the `--ramp` script flags, their tests). Reason: HARDEN-1's dry run showed it couldn't be re-pointed at the office program (Sun–Sat windows, Sunday-night evaluation, rest days counted as missed, a 5/5 threshold a 4-session week can't meet, a restart that re-seeds the home-gym plan), EVAL-1 now reports on weeks, and the plan is pre-seeded through 11/03. `health.ramp_state` dropped by migration 034 after an export to the box (`~/backups/ramp_state_2026-09-19.json`); `health.plan.status` / `original_date` from migration 030 are **kept** — the Lambda `/plan` and `/overview` read `plan.status`. There was no scheduled ramp job left to remove (HEALTH-2 had unregistered it). **Progression is manual:** the weekly evaluation reports, Ryan decides (PB-009). What the ramp did that nothing does now: slides of missed sessions to makeup slots (→ MAKEUP-1), week classification and repeat/restart proposals (→ manual via EVAL-1), the week-2 revisit prompt, travel-week templates (→ TRAVEL-1).

**MAKEUP-1 — DECIDED 2026-09-20: record intent, move nothing (option C).** `@artemis skip <reason>` (and `skip <date> <reason>`) marks the plan row `is_skipped` with the reason. EVAL-1 and EXPORT-1 show it as **skipped — <reason>** instead of **missed**, and a skipped day no longer counts as due. The nag and the 21:50 inferred backstop already gate on `is_skipped`, so both respect it. **Nothing moves dates.**
- **Why moving is deferred (Ryan's reasoning, recorded so the deferral is legible later):** the options that slide a session — `@artemis makeup <date>` (B) and a swap with the target day (D) — are **deferred until a missed session is a recurring problem. One missed flow isn't evidence of one.** B also risks quietly wrecking SCHEDULE-2's lifting-day spacing: moving Strength B onto a Thursday that already holds C creates the three-in-a-row week the schedule was designed to avoid. If movement is ever built, D (swap) is the safer shape.
- **What happens today when a session is missed** (unchanged by this): 16:30 the nag asks for a debrief; 21:50 `insert_inferred_summary` writes a placeholder `session_summary` marked `logged_via='inferred'`; every consumer filters those out, so a backstop row never counts as done. "Missed" is **derived** from "a past training day with no real logs" — it is never recorded anywhere.

**PLAN-STATUS-DEBRIS — dropping `health.plan.status` and `original_date` — DONE 2026-09-20 (#132, #133, migration 039).** Migration 039 applied 17:54:12Z; both columns verified gone 2026-09-21. The 53 `status` rows were exported first to `~/backups/plan_status_2026-09-20.json` on the box (53 rows, verified). `done` now derives from `session_log` alone. The ship order was not held — the column was dropped while the deploying code still named it in a SELECT — which took `/plan` and `/overview` down for 1 min 40 s; see COLUMN-GREP. History follows. Both came from migration 030 with the ramp engine. **Nothing in the codebase writes either.** 53 rows carry `status='missed'`, all dated 7/25–9/15 — the retired ramp wrote them; zero rows in the current program have it. `original_date` is populated on **0 rows**. Meanwhile the Lambda still **reads** `status`: `_plan_day_status` treats `plan.status = 'completed'` as done.
- **Recommendation: drop both columns** and let the Lambda derive `done` from `session_log` like every other consumer already does. A column that looks authoritative, is abandoned, and is still consulted is exactly the kind of thing that later gets trusted.
- **The alternative** — adopt `status` as authoritative and have something write it — means a second source of truth for "done" alongside `session_log`, which the one-system-of-record discipline argues against.
- **The two changes MUST ship together.** Dropping `status` removes the Lambda's only non-log source for `done`; dropping the column first would silently downgrade any session marked complete that way to `missed`, and changing the Lambda first leaves a stale column still being read until the migration lands. One PR: the Lambda change, then the migration, then the Lambda deploy.
- **Cost of the Lambda change:** small — one branch in `_plan_day_status` (`api/app/routers/health.py`), plus its comment block and the `/plan` and `/overview` tests that name the status. No new query: the function already has the session_log rows it needs. The risk is not the code but the deploy ordering above, and the 53 stale `missed` rows should be checked once more before the drop in case any sit inside a period Ryan still cares about (they don't today: all are pre-program).



**RPE-SCALE — the logged RPE was 2 too high — CORRECTED 2026-09-20.** Ryan had been rating with 8 = four reps in reserve; the program means **10 = none left, 8 = two, 6 = four** (now written into PB-009, #141). Every value he logged read 2 high, and EVAL-1 reported under-cap sessions as over-cap.
- **Corrected, one transaction per run:** 26 values on 2026-09-16 and 09-18 (24 sets + 2 session summaries), then the 27 hand-logged sets on 2026-06-08 — `GREATEST(1, rpe_actual - 2)`, nothing reaching the floor, verified row by row before commit. **Re-verified 2026-09-21:** 9/16+9/18 average 5.33, 6/8 average 5.59 — each corrected exactly once.
- **Originals recoverable, not overwritten:** two `acos.audit_log` rows (`action = 'rpe_scale_correction'`, 26 and 27 rows of before-image). No `rpe_original` column, deliberately — it would be NULL on every future row and look authoritative while abandoned, the `plan.status` shape PLAN-STATUS-DEBRIS removed.
- **EVAL-1 week 1 now reads "average session RPE 5.2 vs cap 6"**, `over_cap` empty (was 7.2). Its effort line prefers the session-summary RPE over the set average, which is why it read 7.2 (mean of 7 and 7.5) rather than the set mean 7.17.
- **Left alone permanently (Ryan):** the 72 `inferred` session_summary rows, 6/6–9/14 — ramp-engine output, not his ratings.
- **No consumer cached the old values:** EVAL-1, EXPORT-1, the Status page, `health_guard` and the gym-display prefill all read `rpe_actual` live.
- *This entry was meant to land with #141 and silently didn't* — the insertion keyed on a heading absent from that branch. Recorded here on 2026-09-21; see the DOC-ASSERT rule in CLAUDE.md.

**PLANNED-SETS — the planned set count honours a per-exercise cap — LIVE 2026-09-21 (#137).** `_planned_set_count` multiplied rounds × exercises flat while its docstring claimed to mirror gym-display's `totalSetsFor`. A check-in adjustment that caps one exercise below the rounds (health_checkin writes `ex["sets"]` and persists the blocks) made a session finished exactly as asked read 11 of 12 and classify `partial`. `_exercise_sets` now mirrors gym-display (`min(ex.sets, rounds)`); the finisher stays `rounds × occurrences`, as gym-display counts it; the docstring was corrected. Finisher rounds are loggable — eight were logged for real on 2026-06-08 — so they count. A session summary still returns `done` before any of this arithmetic runs.

**SKIPPED-SLOT — a skipped set completes its slot in a session Ryan actually trained — LIVE 2026-09-21 (#139).** Ryan's rule, 2026-09-20, in two halves: a set logged `is_skipped` completes its slot, so 11 logged + 1 skipped of 12 reads `done`; but a session of **nothing but skips** stays `missed` — because a walk is a single cardio block, and without that half a skipped walk would read `done` (which is exactly what a first attempt did, caught by `test_plan_range`). `progress_for` follows the same rule so the ring and the day status agree. The skipped rows stay in `session_log` for EVAL-1.

**CYCLE-1 — pay-period day types — LIVE 2026-09-19 (#123, migration 035).** `artemis/cycle.py` is the one resolution: day type, location and the day boundaries, read by both the cron registry and the `quiet_hours` helpers. The six weekend twins are collapsed, `job_location_recompute` (22:00) re-points tomorrow's jobs, `job_tz_sync` also catches location drift within 60 s, and `health_review` / `weekly_eval` are pinned to 08:30 / 08:35. Overrides are stored but **not yet writable from chat — see CYCLE-2**. Spec as built: A 14-day pay-period cycle with an **anchor date**. Cycle position is **derived from the anchor**, never stored per day, and must survive DST and holidays.
- **Four day types, per 14 days:** `msp_work` 8, `msp_home` 2, `wi` 3, `travel` 1.

  | | Week 1 | Week 2 |
  |---|---|---|
  | Sun | MSP home | WI (Richfield) |
  | Mon | MSP work | Travel Richfield → MSP, leave 11:00 |
  | Tue–Thu | MSP work | MSP work |
  | Thu eve | drive to the farm, 5 h | — |
  | Fri | WI (farm = Richfield), day off work, → Brown Deer 16:00 | MSP work |
  | Sat | WI (Brown Deer) | MSP home |

- **The Wisconsin stretch is continuous** from Thursday evening of week 1 to Monday midday of week 2. **Thursday itself is still an office day.**
- **Each day type carries:** location, whether a departure checklist applies, and whether meals are pre-filled. **Wake time is not one of them — it follows the location** (below).
- **Wake time follows the location, not the day type** (corrected 2026-09-19). The travel Monday is a Richfield morning, so keying wake off the day type would need a special case for it; keying off the location doesn't. **The resolver reads the location first, then takes the wake time from it.**

  | location | wake |
  |---|---|
  | `office` (`msp_work`) | 04:30 |
  | `richfield` — any day type: `wi` farm day, the `travel` Monday, and Sunday evening onward | 06:00 |
  | `brown_deer` (`wi`, non-farm) | 07:30 |
  | `msp_home` | 07:30 |
- **Overrides:** a single date **or a date range** can set a day type (e.g. Thanksgiving week 2026 = all `wi`). An override **wins over the derived position**, and the cycle **resumes afterwards with no drift**, because position comes from the anchor rather than being counted forward.
- **Consumers:** the wake post, the departure block, the plan location/equipment resolver, meal pre-fill, and reports.
- **Scheduler impact — wake, nudge and quiet hours stop being fixed weekday/weekend crons** and become **derived from tomorrow's location**.
  - **Reuse WAKE-1's machinery:** the `CronSpec` registry and `ArtemisScheduler.apply_timezone()` (`artemis/scheduler.py`) already rebuild every cron against a new wall-clock basis. Location recompute is the same move with a different input.
  - **A nightly job** computes tomorrow's location from the cycle, then reschedules the wake, nudge and open jobs to that location's times.
  - **Drift check:** `job_tz_sync` already runs minutely and re-applies when the active timezone moves away from `_applied_tz`. Either extend it to compare the applied *location* as well, or mirror it as a second minutely check with the same shape.
  - **The recompute runs during quiet hours, before the next wake, and posts nothing.**
  - **A manual override (single date or range) takes effect immediately**, rescheduling the same jobs — the way the `set timezone` command calls `job_tz_sync` directly instead of waiting for the next tick.
  - **The duplicate guard must hold when a reschedule moves a job's time on the same local date** (04:30 → 06:00). `_once_per_local_day()` is keyed on `cron_last_run:{guard or id}` against the local date, so a job that already fired can't fire again after being moved. **Weekend twins today share their weekday job's guard key for exactly this reason** — collapsing to one job per function keeps that property instead of relying on paired keys.
  - **The weekend schedule (#102) becomes a special case of the location table, not a separate rule.** `msp_home` and `brown_deer` are both 07:30, which is what the weekend times already do, so the two mechanisms collapse into one. **Remove the weekday/weekend split** (`WE` day-of-week specs and the `*_weekend` twins: `wake_weekend`, `checkin_nudge_weekend`, `open_weekend`, `quiet_hours_start_weekend`, `morning_brief_weekend`, `inbox_zero_morning_weekend`) once locations drive the times.
  - **Tests:** a Friday at Richfield 06:00; the travel Monday 06:00; an office Tuesday 04:30; an override at 22:00 that changes tomorrow's location; and **no double-fire across a reschedule**.
- **Deferred decision — flag, don't act.** The plan is Wed A / Fri B / Mon C, but Friday is a WI day and Monday is travel or Richfield, so **two of three lifting days fall outside the office gym**. **Tue/Wed/Thu are the only days that are office days in both weeks.** The plan needs rescheduling once CYCLE-1 exists; **don't change it yet.**
- **Storage:** the **anchor** lives in `acos.system_state` with the locations registry (LOCATION-1). **Overrides get a small table** when CYCLE-1 is built — date ranges and an audit trail are what a table is for.
- **Anchor (confirmed 2026-09-19, live): Sunday 2026-09-20 is week 1, day 1.** Verified against the FCA start: 2026-09-20 − 2026-08-09 = 42 days = 3 × 14, and both are Sundays.
  - **Profile discrepancy — flagged, not resolved:** the profile records the FCA start as **8/8/2026, a Saturday**; Ryan states **Sunday 8/9**. Both can't be right. The anchor above doesn't depend on which it is (42 days either way only works from the Sunday), but the profile should be corrected once Ryan says which is true.

**CYCLE-2 — the chat command for day-type overrides (small; not urgent).** CYCLE-1 shipped the resolution, the storage (`acos.cycle_day_overrides`, migration 035) and the timing rule, but **nothing writes a row yet** — an override can only be inserted by hand. Thanksgiving week 2026 is the first real use, and the current program ends 10/31, so there is time.
- **`@artemis set <date|range> to <day_type> [at <location>]`** — inserts one row. `day_type` and `location` are independent, so `@artemis set 11/23-11/27 to wi` and `@artemis set tuesday to msp_work at richfield` are both valid, and either field may be omitted (the CHECK requires at least one).
- **`@artemis overrides`** — lists the live ones (`revoked_at IS NULL`) with their id, span, what they set and the reason.
- **`@artemis revoke <id>`** — **soft delete**: sets `revoked_at`, never DELETEs, so a cleared override stays readable as history.
- **The reply echoes the resolved wake / open / quiet for the affected dates**, so the effect is visible before the morning it lands. Use `cycle.describe()`.
- **Same next-day timing rule as CYCLE-1**, via `cycle.apply_override_effect` / `describe_override_effect`: an override takes effect from the next day; a boundary still ahead today applies; one already passed is skipped for today and **the confirmation says so explicitly**. Never a silent skip.
- **Routing:** deterministic, like the other health commands — it must not go through the LLM classifier (HEALTH-1). `set` and `revoke` are destructive-ish, so they follow the existing confirm pattern and are added to the bare-control-word inventory in CONFIRM-ARB.
- **After a write, re-point the schedule immediately** (`job_tz_sync` already detects location drift within 60 s; the command can call it directly, the way the `set timezone` command does, so there is no lag).
- **Dates:** parse through `quiet_hours.parse_date_token` so `tuesday`, `11/23` and `2026-11-23` all work and anchor to the ACTIVE timezone.

**LOCATION-1 — locations, substitutions and per-location weight steps (companion to CYCLE-1; created 2026-09-19).** **Don't build yet** — the open questions below come first.
- **Locations registry:** `office`, `richfield`, `brown_deer`, `msp_home`, `outside`. Each carries a display name, a timezone, a **wake time** (CYCLE-1: wake follows the location, not the day type), an equipment inventory **by class** (machine, cable, smith, barbell, dumbbell with min/max/step, bands, TRX, bodyweight, cardio machines) and constraints (Richfield: **6.5 ft ceiling → no standing overhead work**).
- **`richfield` is the farm** — one place, not two. There is no fifth WI location.
- **Day type → location**, from CYCLE-1. **Brown Deer Sunday has a time boundary:** the fitness center before 17:00, then Richfield.
- **Exercise substitution:** every plan exercise carries a **movement pattern** (squat, hinge, horizontal push/pull, vertical push/pull, carry, core, calf) and a **required equipment class**. A resolver picks the best available match at the day's location, preferring the same pattern, then the same class. **Substitutions are deterministic from a table, never invented.**
- **Seed table (office → Richfield):** leg press → DB goblet / split squat · lat pulldown → band pulldown or TRX row · seated row → TRX row · leg curl → ball hamstring curl · leg extension → DB step-up · face pull / rear delt → band face pull · pec fly → DB fly · calf press → standing DB calf raise · Pallof → band Pallof · back extension → DB RDL · captain's chair → lying leg raise.
- **Resolution order: location first, then pain.** The location resolver runs first and produces the session for that day's inventory; the pain / soreness ladder then applies **to the resolved session**.
  - `SUBSTITUTION_POOL` (`artemis/health_regions.py`) must be **filtered to the location's inventory** before pain picks a replacement.
  - **A pain removal applies to the location substitute too**, not only to the exercise as written.
  - **Test to write:** shoulder pain 4 at Richfield removes the *substituted* exercise, and its replacement also comes from Richfield's inventory.
- **The resolved session is what gym-display and the wake post show**, with a line naming the location and any substitutions.
- **Weight steps follow the location:** PowerBlocks at Richfield have their own increments, not the office's 5 lb hex steps.
  - **The per-location load config travels on the plan row**, not as a table shipped to the client.
  - gym-display's `EquipmentClass` union **grows by `bands` and `trx`**, both with a **no-numeric-load mode** like `bodyweight`.
  - **Part of the work:** `src/lib/equipment.ts` and `src/lib/weight-step.ts` move from compile-time constants (`LOAD_CONFIG`, `PLATES_PER_SIDE`, `OLYMPIC_BAR_LBS`, `SMITH_BAR_LBS`) to config delivered with the plan. Those constants are read by typed paths (`inferEquipmentClass`, the logger prefill), so it's a refactor, not a config edit.
- **Richfield inventory** (the farm): PowerBlocks to 80 lb; curl bar with 70 lb of plates; flat bench; TRX; resistance bands with a wall mount; stability ball; rower; bike on a trainer; **6.5 ft ceiling → no standing overhead work**.
- **Storage:** the **registry and the CYCLE-1 anchor live in `acos.system_state`** (same shape as `health_program`) — no migration. Day-type **overrides get their own small table**, built with CYCLE-1.
- **Unknown — ask Ryan:**
  - What equipment is at the **MSP home**.
  - Brown Deer's inventory (it's a fitness center, so probably machines and cables, but nothing is recorded).

**TRAVEL-1 — no travel handling in the office program (small; revisit by early November).** The ramp's travel-week templates are gone and the office program has none. The Paris trip is around Thanksgiving (note: Thanksgiving is Thu 2026-11-26, after this program's 11/03 end, so it lands in the next phase). A travel week needs a hand-chosen substitute: a bodyweight/hotel variant of Strength A/B/C, or Recovery Flows plus walks.

**WATCH-1 — Apple Watch ingest via Health Auto Export — INGEST AND PRE-FILL LIVE; workout matching waits on the first workout.**
- **Status 2026-09-21:** 130 ingest calls, latest 13:17Z. **The first real night arrived** (9/20→9/21) with all seven sleep metrics, and the 04:30 pre-fill wrote 9/21's sleep 5.7 h, resting HR 64 and weight 282.0, all marked `watch`. **The 64 was 9/20's reading** (the old 36 h window); #148 fixed the carry-forward and the live Lambda pre-fill corrected 9/21 to 65 at 15:21Z. **No workout has arrived yet** (`health.watch_workout` has 0 rows), so the workout match is unexercised.
- **The sleep assumption held, and the order mattered.** `sleep_total_sleep` = 5.73 h, matching core + deep + rem = 5.72 h. But **`asleep` and `inBed` both arrive as 0** for stage-based watch sleep — had the unspecified stage been read as a total, the pre-fill would have written 0.0 h. Since #148 those zeros are skipped at parse time and the total is always preferred; the two 0.0 rows stored for 9/21 were deleted 2026-09-21 (audit `38231782…`).

- **Endpoint:** `POST /api/health/ingest` on the Lambda (`api/app/routers/health.py`), accepting the Health Auto Export JSON payload (`data.metrics[]` + `data.workouts[]`). It has **its own key** in Secrets Manager via `knowledge/secrets.py`, checked on its own dependency. Never the display key, and the display key must not work here.
- **Idempotent** by (metric, timestamp); workouts by (workout, start). A re-sent or overlapping export is a no-op, and the response reports inserted/duplicate counts.
- **Payload limits:** a large backfill can exceed the Lambda payload limit and the stage throttle (5 req/s, burst 10), so the export is configured in batches.
- **Store:** one table in `health`, with a row per sample: metric, timestamp (UTC), numeric value, unit, and the raw JSON kept. It covers:
  - sleep hours and phases (deep/REM/core/awake)
  - resting heart rate, HRV, weight
  - workout records: type, start, duration, average and max heart rate, calories
- **Migration: propose first.** The DDL and the unique key go to Ryan for review before anything is written. Deploy migrate-first.
- **Morning check-in pre-fill** (`knowledge/watch_prefill.py`, shared by the box and the Lambda): sleep, resting heart rate and weight, in the active timezone. Ryan only fills in energy and soreness. A value he types is `manual` and always wins. Pre-fill stays deterministic and must not change intent routing.
  - **Nothing is carried forward (2026-09-21).** A value is today's only if it was measured today: resting HR and weight by `local_date = today`; sleep is the night whose `sleepEnd` falls in (yesterday 12:00, today 12:00], or, with no readable `sleepEnd`, the record labelled today. No lookback window. The first version used a 36 h window and pre-filled 9/20's resting HR as 9/21's; a 9/22 read would have returned 9/21's sleep, resting HR and weight.
  - **Runs at the wake job and again on every ingest for today**, filling only fields that are empty or `watch`. The wake run leaves an `acos.audit_log` row (`watch_prefill`); the ingest's own audit row carries `prefill`. Both keep `sleep_source_metric`. Sleep is kept to one decimal.
  - **Recorded, never acted on.** Watch data never triggers a plan adjustment; only a typed check-in does. A watch-only `daily_state` row is not a check-in (it had been suppressing the 05:15 nudge).
  - The wake post carries one line — `From the watch: sleep 5.7h (20:57–03:19), weight 282. Not synced: resting HR.` — and the check-in reply names which values came from the watch.
- **Why sleep and workouts were missing — settled 2026-09-20, NOT an app toggle.** Across 227k samples and 6/1–9/20, sleep and workout rows were zero. The obvious reading was a wrong export selection; **that was wrong**. The cause is upstream of the app: **Ryan charges the watch overnight**, so there was no sleep in Apple Health to export, and **he hasn't been starting workouts on the watch**, so no workout records existed either. Nothing in the endpoint, the parser or the app selection was at fault. **Don't go hunting a toggle.**
  - He wore the watch overnight for the first time on the night of **2026-09-19 → 09-20**, so **sleep should start appearing from 9/20–9/21**.
  - The first watch workout was a **Traditional Strength Training** session at the office on **Monday 2026-09-21** (an earlier "9/22" here was a wrong date — 9/22 is a Tuesday).
  - **Sleep: observed 2026-09-21** (the 9/20 → 9/21 night, pushed 03:52 CDT). One aggregated record per night; `date` is **local midnight of the wake day**; camelCase stage fields in hours (`totalSleep` = core + deep + REM, `awake` separate); `sleepStart`/`sleepEnd`/`inBedStart`/`inBedEnd` strings with offsets. `asleep` and `inBed` read **0** when absent — skipped, not stored (the two 0.0 rows from 9/21 predate the fix). A later push with a later `sleepEnd` replaces a stored partial night (all its stage rows).
  - **Workout: still an assumption.** The first watch workout (Office Strength A, 9/21) did not arrive — `workouts_in = 0` on both pushes — though `watch_heart_rate` shows workout-mode sampling 05:21–05:54 CDT covering every logged set. Check the Fitness app saved it and whether the export includes workouts before touching code.
  - **Weight was never missing** — it arrives sparsely, one row per weigh-in (first seen 7/13). Early "0 weight" was a coverage artefact of which dates had been pushed, not a gap.
- **Source decode (Ryan, 2026-09-20).** Every Health Auto Export sample carries its own `source` marker: **`RAW` = Ryan's Apple Watch, `RIP` = Ryan's iPhone, `RAW|RIP` = both.** Both forms are stored — `device_raw` as it arrived, `device` decoded (migration 038) — and an unrecognised marker stores the raw string with a NULL decode rather than a guess. **Where a metric can come from either device, prefer the WATCH for resting HR, HRV and sleep**; weight reaches either device from the scale and needs no preference (`knowledge/watch_payload.prefers_watch`).
- **Heart rate** lives in `health.watch_heart_rate` (migration 037): timestamp + three smallints + device, no raw JSON. It is what makes ZONE-1 possible. **Measured 2026-09-21:** readings are NOT per-minute as 037 assumed — every sample arrives separately (Min = Max = Avg), every ~5 s during a workout and every few minutes otherwise. Real volume 433–556 rows/day (9/19–9/21), of which ~300–390 are the dense stretch; ~145 B/row with its index → **~180k rows, ~26 MB a year**. For scale, `watch_sample` holds 230k rows / 74 MB after 110 days, ~2/3 of it `basal_energy` per minute (~250 MB a year). No change decided; Ryan to review. 1,493 pre-037 `heart_rate` rows still sit in `watch_sample`.
- **Workout match — design approved 2026-09-21; build when the first workout record arrives,** and verify it against the 9/21 evidence (05:21–05:54 CDT, ~217 kcal of active energy, ~104 average HR). A pure function in `knowledge/`, run at ingest and again from a box job (a workout can arrive before or after the sets are logged).
  - **Match:** a workout matches a non-skipped plan for its local date when at least one of that plan's `session_log` rows has `logged_at` in [start, end + 10 min] — logging trails the set (on 9/21 the first set was logged 8 min after the HR rise).
  - **Kind gate:** Traditional/Functional Strength Training → `strength_*`; Yoga/Flexibility → `recovery_flow`; walking/cycling/elliptical → `cardio_z2` (and `walk`). A mismatch is not matched; it is reported.
  - **Ambiguity:** several workouts on one plan → the one containing the most log rows wins, the others are reported. A workout with no logged sets stays unmatched and is reported — never attached by date alone.
  - **Writes only `watch_workout.plan_id`.** Nothing is written into `session_log`; session HR and time in zone are read from `watch_heart_rate` over the workout window.
  - **Confirm on the first real record:** `duration` is seconds (≈ end − start), and whether `heartRateAvg` is a number or `{Avg, Min, Max}`.

**ZONE-1 — time in zone from minute-level heart rate (small; data only; BLOCKED until there is an observed max HR).** The Z2 sessions carry a target zone, and a workout's single average HR cannot show whether the session was actually spent in it or drifted. With `health.watch_heart_rate` (037) the answer is arithmetic: intersect the minute samples with the session window and report minutes per zone.
- **Data only.** Minutes in each zone, and the share of the session. **No verdict, no "too easy / too hard", no plan change** — the same rule EVAL-1 follows.
- **Zone thresholds are NEVER computed from a formula** (Ryan, 2026-09-20). No 220-minus-age, no estimate. The basis is **Ryan's own observed max HR across logged sessions**, taken once there are a few weeks of workout HR in `health.watch_heart_rate`. **Until that exists, ZONE-1 stays unbuilt** — reporting bands off a guessed maximum would be a fabricated number wearing a data label.
- **Session window** is the logged session's span, or a matched `watch_workout` when one exists. No overlap → no zone line, never a guess.
- **Consumers:** the daily and weekly reports (EXPORT-1), and EVAL-1's recovery area.

**ENERGY-HOURLY — aggregate active/basal energy to hourly on ingest (small; backlog, not urgent; option recorded 2026-09-21, NOT approved to build).** Minute-level energy is nearly all of `health.watch_sample` and nothing uses it at that grain: no code reads `active_energy` or `basal_energy` today, and DIET-1 only needs daily energy-out. Heart rate stays at full resolution in `watch_heart_rate` — ZONE-1 needs it.
- **Measured 2026-09-21:** `watch_sample` = 229,778 rows / 71 MB. `basal_energy` 149,607 rows (~1,360/day, one a minute) + `active_energy` 77,691 (~713/day) = **227,298 rows, 98.9% of the table**, ~2,020 rows/day over the last 30 days. Everything else (resting HR, HRV, walking HR, weight, sleep) is ~2,500 rows in total.
- **Size:** at current rates, ~740k energy rows / **~230 MB a year**. Hourly would be ≤ 48 rows/day (observed ~38, as there are hours with no samples): ~17k rows / **~5.5 MB a year**, a ~42× reduction.
- **Design notes, for whenever it's built:** bucket by metric and hour, value = sum of `qty`, plus `sample_count` and first/last minute. Pushes overlap and resend minutes, so an ingest must NOT add to a stored bucket (that double-counts). Replace the bucket when the incoming payload has at least as many minutes for that hour. Keep one representative `raw` per bucket, not all 60. Existing rows could be rolled up by a one-off migration, with an export to the box first. Decide then.
- **What is lost:** minute-level active energy inside a session. On 9/21 it was the only calorie evidence for the unrecorded workout (~217 kcal over 05:21–05:51). A `watch_workout` record carries its own kcal, so that stops mattering once workouts arrive. Hourly still covers daily energy-out for DIET-1.

**SLEEP-PERF — does sleep show up in performance? (small; data only; NOT before ~4 weeks of watch sleep, so no earlier than ~2026-10-19).** Ryan, 2026-09-21: sleep < 6 was removed as a recovery trigger; energy drives recovery. This checks whether that was right.
- **Data only:** per training day, performance — load, reps, RPE vs cap — beside watch sleep, typed sleep and typed energy for that morning. Tables and simple pairings; no model, no verdict.
- **No rule changes from it** until Ryan has looked at the results.
- Needs: ≥ 4 weeks of nights from the watch (first night 9/20 → 9/21) and the corrected RPE scale (RPE-SCALE, 2026-09-20).

**EVAL-1 — read-only weekly evaluator — BUILT 2026-09-19** (`artemis/health_eval.py`; `python3.11 -m artemis.health_eval --week <day>`). Feeds the weekly report (EXPORT-1's "Weekly evaluation" section) and the Sunday 08:35 post (`job_weekly_eval`, week to date, labelled partial). Spec as written:
- **Scope:** read-only. It makes **no plan changes and no recommendations**, and holds no confirm routes or pending state. It's the small report-only evaluator RAMP-RETIRE calls for, and it doesn't reuse `health_ramp.py`.
- **Week and timezone:** weeks are **Sun–Sat** from `health_office.WEEK2_START` (SCHEDULE-2 moved them off Wed–Tue; week 1 is the 9/16–9/19 stub). Dates come from the active timezone.
- **Inputs:** the week's `health.plan` rows and `health.session_log` rows (`logged_via <> 'inferred'`).
- **Output**, as one structured result:
  - sessions done vs planned (training days only; rest days are neither done nor missed)
  - per-exercise load change vs the prior week (top logged weight per exercise; an exercise with no load either week is reported as such, not as 0)
  - average RPE (`session_log.rpe_actual`) vs the cap (`plan.target_rpe`)
  - **Recovery (WATCH-1, 2026-09-19):** average sleep hours and average resting HR for the week, from `health.daily_state`. Data only — no interpretation, no target, and "no sleep or resting HR recorded" when there is none.
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

**WORK-DAY SCOPE BUILT 2026-09-21 (`feat/diet1-workday`) — migration 040 applied and deployed pre-merge 2026-09-21.** What exists: `artemis/nutrition.py` (pre-fill, day/entry status, the 48h correction window, the deviation parser and the saved-food → USDA → Open Food Facts source order), `artemis/notion_meal_plan.py` (read-only Notion reader), the silent 00:15 `nutrition_prefill` cron job + the lock sweep, the morning check-in line, a bare-`fix` handler, and `tests/test_diet1.py` (46 tests).
- **Notion rows written 2026-09-21:** 8 `recipes` rows (the 7 confirmed + `Protein bar — peanut`, all `status = active`, bodies carry ingredients and weights) and one undated `meal planning` row, **"default day — work day"**. Totals 2,015 kcal / 188 P / 206 C / 55 F / 45 fiber. A new `source` text column was added to `recipes`; the cottage cheese bowl carries `USDA generic low-fat cottage cheese — placeholder, pending label`. `meal planning` has **no fiber rollup**, so Notion's UI cannot show the 45 g.
- **Notion access — LIVE 2026-09-21.** Token at `rdmis/dev/notion-token` (key `token`), readable by `acos-ec2-role`; the integration ("internal") is shared with `meal planning`, `recipes` and `ingredients`. Verified from the box: the default day reads back 7 recipes, 2,015 kcal / 188 P. Without the token or the shares, the 00:15 job records `prefill_outcome = 'unavailable'` and pre-fills nothing.
- **Still no USDA key** (`rdmis/dev/usda-api-key`): tier 2 is skipped, not guessed. Open Food Facts needs no key.
- **Saved foods = recipes, then ingredients.** `nutrition.food.kind` is `recipe` | `ingredient`, unique per `(kind, slug)` — "Protein bar — peanut" and "protein bar (peanut)" slugify identically. Ingredient macros are per the Notion `serving`, stored as `portion`; a deviation quantity multiplies it (`+2 banana` = 2 × 1 medium). The 00:15 job refreshes the ingredient mirror in a savepoint before the pre-fill and deactivates rows that vanished from Notion. Ingredient macros live in `ingredients`, NOT as `course = ingredient` rows in `recipes` (that plan was dropped). `ingredients` has no sodium property.
- **`health.*` nutrition tables are NOT retired here.** 016's `nutrition_target` / `meal` / `nutrition_log` are all empty in RDS (re-verified 2026-09-21) but `artemis/life_ops.py`'s grocery staple generator still READS `health.meal` + `health.nutrition_target`. Dropping them now would silently drop the staples capability, so retirement stays a separate coverage-checked change. Migration 040 is purely additive.
- **The 48h refusal is unreachable on the normal path** — `fix` targets yesterday, which is at most ~24h past its end. The refusal exists for a correction opened inside the window and continued after it closed; that path is guarded and tested.
- **`fix` collides by verb with `fix <exercise> rpe <n>`** (the workout-set correction in `artemis/health.py`). Only the BARE form is claimed for nutrition, and the handler sits after `morning_flow`. Add both to the CONFIRM-ARB bare-control-word inventory.
- **Still out of scope:** non-work days (no weekend meal set exists), the barcode page, photo estimates, the Apple Shortcut, the dietitian report.
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
- **Notion meal plan — decided 2026-09-19** (after the read-only schema report; property names as reported then):
  - **Databases:** the "— ryan's brain / in the kitchen" copy of `meal planning` is the only meal-plan database, and its `recipes` database is the foods table. Don't create a separate one; this is where "saved foods" in the source order come from. The "— second brain" duplicate is marked archived in Notion (not deleted) so nothing reads it.
  - **Four meal slots:** `breakfast`, `lunch`, `dinner`, `snacks`, each a link to `recipes`. The 3 pm snack and the 7 pm cottage-cheese bowl both go in `snacks`; there's no dessert slot.
  - **One recipe per portion as eaten**, e.g. "Overnight oats jar", "3 hard-boiled eggs". A linked recipe means one serving.
  - **A single "default day" row** in `meal planning`, not dated weeks. The rotation is the same every day, so the 00:15 pre-fill reads that row and only deviations are logged.
  - **Sauces count:** they're `condiment` recipes linked to lunch.
  - **Macros come from real labels, or USDA where there's no label**, never from the plan page's headline. The day's total is whatever the foods add up to.
  - **Target:** 2,100 kcal / 175 g protein (provisional), a goal to aim at, not a number to make the data match. The "the plan — 2100" page is a prose plan and isn't read.
  - **Confirmed meal set (2026-09-19) — the spec for the `recipes` rows. Not yet written to Notion.** Each is one portion as eaten; `course` in brackets.

    | recipe | kcal | protein | course |
    |---|---|---|---|
    | Overnight oats | 520 | 30 g | breakfast |
    | Chicken wrap | 265 | 25 g | lunch |
    | Yogurt parfait | 330 | 26 g | lunch |
    | Protein bar | 240 | 20 g | snack |
    | Protein coffee | 130 | 30 g | beverage — **work days only, on the commute** |
    | Patty + veg | 310 | 32 g | dinner |
    | Cottage cheese bowl | ~220 | ~25 g | snack, evening — **macros pending a label** |

  - **Work-day total ≈ 2,015 kcal / 188 g protein** against the provisional 2,100 / 175.
  - **Two default days, not one** (amends the single "default day" decision above): the protein coffee is a work-day item, so a non-work day is **1,885 kcal / 158 g protein** — 215 under on calories and **17 g under the protein target**. Whether the non-work day gets a replacement item is open; flagged, not decided.
  - **Which days are work days comes from CYCLE-1**: the 8 `msp_work` days per pay period. The `wi` Friday is a day off work, and the `travel` Monday and both `msp_home` days aren't work days either — so 8 of 14 days use the work-day default and 6 use the non-work one.
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

**TIME-CAP — 45 min is a target, not a limit (Ryan's decision, 2026-09-19; replaces the earlier "cut the finisher, then the lowest-value exercise" rule) — BUILT.**
- **Validator** (`health_office.duration_verdict`, used by `validate_rows` on every reseed and by `scripts/validate_health_plan.py`): `est_duration_min >= 60` is rejected; 45–59 passes with a note in the reseed diff and a logged warning (and in the reseed audit row). Nothing is ever auto-cut.
- **Weeks 3–6 stay as planned** — exercises and the Strength C finisher unchanged.
- **Calibration pending:** the estimate (10 + sets × exercises × 2.5 min) puts Strength B weeks 3–6 at 62 min and Strength C weeks 5–6 at 67. Ryan chose "keep 60, recalibrate later": those six slots are in `health_office.CALIBRATION_PENDING` and warn instead of failing; any other 60+ row fails. **Next step (~2 weeks of logs, after week 4 ≈ 10/13):** recalibrate the per-set estimate from the reports' planned-vs-logged numbers (the first two sessions ran ~2.0 and ~2.4 min/set, logged span), then empty `CALIBRATION_PENDING` so the 60 reject applies to them too.
- **Display:** the wake post, the Tomorrow/Week views and Setup show the estimate as "45 min", or "~52 min" once it's over 45 — no warnings or apologies.
- **Reports:** `export_report` shows planned vs logged span (daily line, weekly column + average, monthly average) so the estimates can be checked against reality.

**PB9-CRON — weekday morning times vs office arrival (low; Ryan's decision).** The morning is event-driven since FRIDAY-1 and the PLAYBOOKS TODO is gone. What's left: whether the weekday wake (04:30) and check-in nudge (05:15) suit Ryan's office arrival time. Both are config (`WAKE_TIME`, `CHECKIN_NUDGE_OFFSET_MIN`); weekends are already 07:30 / 08:15.

**CRM-2 / COMMIT-1 — two-store seams (medium).** Two contact stores: `public.contacts`/`organizations` (CRM API) vs `public.persons`/`companies` (Write Guard, PB-008). Two commitment stores: `acos.commitments` (personal tracker) vs `public.commitments` (CRM contact/deal-scoped). Both intentional/legitimate, but the boundaries need documenting before the cognition layer reasons over them. `crm status` reads only the `contacts`/`public.commitments` side.

**CRM-1 — `leads` has no real meaning (low).** No lead-status concept in RDS (`leads` returns same as `contacts`). Lead-status probably belongs on `deals`/pipeline, not contacts. Future schema decision, not a bug.

**INBOX-1 — triage doesn't populate inbox-zero (medium).** Triage summarizes live but never calls `upsert_thread`, so the snooze/waiting/done lifecycle has no data. Either wire triage to persist threads, or retire the lifecycle if superseded.

**CAL-1 — double calendar-audit write — FIXED 2026-09-19.** `acos.calendar_audit` has one writer, `main._audit_calendar_write` (it keeps start_ts / has_external / dup_override / approved_by). The draft and cancelled actions moved onto it; the create / delete / bulk-delete sites lost their second write; `commitments.log_calendar_action` was deleted. Tests assert one row per action.

**SCHEMA-DRIFT — deploy must run migrations — DONE 2026-09-19.** `scripts/deploy.sh` runs `run_migrations.py` before every restart (STAB-1 A5), and the Monday 08:00 update check now also compares `migrations/*.sql` on the box with `acos.schema_migrations` (`artemis/schema_drift.py`, read-only). It posts only when a migration is on disk but unapplied, or applied but missing from the repo; a merge not yet pulled is the existing "update available" post. A test keeps migration numbers unique and gap-free.

**TEST-DB-GUARD — no test may reach production — DONE 2026-09-19.** `knowledge/dbguard.py` refuses every real connection (knowledge.db pool, the Lambda API engine, the reseed / validate / seed / migration scripts) while a test runs: `ARTEMIS_TEST_NO_DB=1`, which every `test_*.py` in `tests/`, `artemis/` and `knowledge/` sets (enforced by `tests/test_db_guard.py`), or pytest. One named exemption: `tests/test_phase1_schema.py`, the intentional live-schema check behind the Lambda's `/admin/run-tests`. Audit (recording fake, empty-DB behaviour): `test_archive_gate` would write 8 `acos.audit_log` rows per run (never run on the box — 0 in prod); `test_scheduler_registry` wrote `checkin_open:2026-09-18` at 15:05 on 9/18 (same value the real wake had set); `test_health_seed`'s live tier would insert the legacy 137-row baseline plan into production (never ran — every date already has a row; now skipped under the guard); `tests/test_mattermost.py` posted "ACOS Phase 1 complete…" to the live channel on every run — 3 posts on 9/19 09:59 from the verification run; moved to `scripts/check_mattermost.py` with posting behind `--post`. Everything else writes nothing. The 64 prod rows from 9/19 were deleted (audit `test_row_cleanup`).

**CHECKIN-TEXT — check-in free text keeps the leftovers (low).** The 9/19 check-in stored `free_text = ", , sore 1 right knee,"` (9/17: `", , ,"`): separators survive, the soreness phrase is duplicated into free text, and "right" is dropped from the structured `{'knee': 1}`. Strip consumed tokens and separators; decide whether a side belongs in the soreness JSON.

**CORRECTION-DEADCODE — remove `_handle_correction` (low).** Unrouted since HEALTH-1 A2 but still in `artemis/main.py`, including an INSERT into `acos.data_vault_satellites` and the "I've learned that…" reply. Delete it with the tests that reference it.

**OPS — Engagement Ops (`ops.rdm.is`) — PARKED 2026-09-21, a separate product.** No further work in Artemis sessions; it stays as it is. State when parked (read-only check, 2026-09-21):
- **Production works.** Signed in, `ops.rdm.is` renders the portfolio; its bundle matches main built with `VITE_OPS_API_BASE=https://ops-api.rdm.is`.
- **All four OPS-2 deploy blockers are done:**
  - **Tunnel:** `ops-api.rdm.is → localhost:5001`.
  - **Access:** separate apps for `ops.rdm.is` and `ops-api.rdm.is`; OPTIONS bypass works.
  - **Env:** `CF_ACCESS_*` and `OPS_ALLOWED_ORIGIN` are set in `/etc/systemd/system/acos.service.d/override.conf`, not `.env`.
  - **Old bundled key:** it was the CRM API key; rotated 2026-07-19, and the live API rejects it.
- **Known and left as-is:**
  - "portfolio — non-JSON response" on a non-production deployment. It is the signature of a build with `VITE_OPS_API_BASE` empty: same-origin `/api/portfolio` falls through to the Pages SPA `index.html` (200 `text/html`). Probably a Pages Preview deployment without the variable; not confirmed.
  - The first load after the `ops-api` Access cookie lapses shows "portfolio — network error"; Retry recovers.
  - cloudflared is 2026.7.2 (2026.9.1 available).

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
- ~~**`test_health_office`** has 5 assertion failures identical on `main`~~ — the date-window checks passed once SCHEDULE-2 moved the program weeks to Sun–Sat; the file is green.

**LAMBDA-DRIFT — the health/CRM Lambda deploys by hand and drifts from `main` — DONE 2026-09-21: options C and B built (#135, #136); A (CI deploy) not built.**
- **C — the deploy preflight** (`scripts/lambda_preflight.py`, run by `api/deploy.sh` before the build): refuses a deploy whose code still names a column that a *pending* migration drops or renames — the 9/20 outage, reproduced as its regression test. `--force "<reason>"` overrides and logs to `~/.artemis_lambda_deploy.log`. First real run: the 2026-09-21 00:39Z deploy, passed.
- **B — the Monday drift check** (`artemis/lambda_drift.py`, on the 08:00 update check): downloads the live package and diffs every `app/**` and `knowledge/**` file against `git show HEAD:<path>` — contents, not `CodeSha256`, so it names the file. **First live run 2026-09-21 08:00:02 CDT: caught `knowledge/secrets.py`** (DIET-1's `get_notion_token()`), which the 08:29 CDT redeploy cleared.
- **IAM:** inline policy `acos-lambda-drift-read` on `acos-ec2-role` — `lambda:GetFunction` + `lambda:GetFunctionConfiguration` on the one function ARN only. Applied 2026-09-20, committed at `infrastructure/iam/acos-lambda-drift-read.json`. The box still has **no Lambda write permission**, by design.
- **Still open:** the Lambda's code hash and matching commit are not yet in `context/CONTEXT.generated.md`.
- History and the original options follow.
 `rdmis-crm-api` only changes when someone runs `api/deploy.sh` from a laptop. Merging to `main` doesn't deploy it, and nothing reports when it's behind. It has drifted twice. Most recently, TEST-DB-GUARD (#106) merged on 9/19 but never reached the Lambda. It shipped unplanned with the 9/19 machine-setup deploy (#114), found only because the pre-deploy diff of the live package against `main` was run by hand.
- **Record of that deploy:** built from `main` 9734401. Its `api/` and `knowledge/` match that commit, and the live package was diffed against `main` before and verified after. The runtime identity (LastModified / CodeSha256) belongs in the generated snapshot, not here.
- **Rollback package (the pre-9/19 build):** on the box, at `~/backups/lambda_2026-09-17_7320a28e6924.zip`.
  - SHA-256: `7320a28e692489ffe7abe469d6280d162bf280081f4e102ea7a8298b13bf68c9`. That's the same digest as the 9/17 build's Lambda `CodeSha256`, `cyCijmkkif/nq+Rp1igNFivygAgfThAup6gpixO/aMk=` (hex vs base64).
  - The box's role (`acos-ec2-role`) has **no Lambda write permissions** (read-only since 2026-09-20, for the drift check), so the rollback runs from a Mac with SSO:
    - `scp rdmis:backups/lambda_2026-09-17_7320a28e6924.zip /tmp/`
    - `aws lambda update-function-code --function-name rdmis-crm-api --zip-file fileb:///tmp/lambda_2026-09-17_7320a28e6924.zip --region us-east-1 --profile rdmis-admin`
  - Then re-run the smoke check.
- **Rollback package (the pre-#148 build, replaced 2026-09-21 15:21:06Z):** on the box, at `~/backups/lambda_2026-09-21_f701d74f8418.zip`.
  - SHA-256: `f701d74f841861d8378b50899879164e45822596f3afbc6a79c7f04e0476547c` = that build's `CodeSha256` `9wHXT4QYYdg3i1CJmHkWTkWCJZbzr7xqecfwTgR2VHw=` (live 13:29:57Z → 15:21:06Z on 9/21). Verified on the box after the copy.
  - Roll back exactly as for the 9/17 package, with this file name.
- **Every deploy saves its rollback first — enforced (2026-09-21).** `api/deploy.sh` runs `scripts/lambda_backup.sh` after the column-grep preflight and before the build. It downloads the LIVE package, checks it against the function's `CodeSha256`, and copies it to the box as `~/backups/lambda_<date the live code was deployed>_<first 12 hex of its SHA-256>.zip` (mode 600), verifying the digest again there; a local copy stays in the Mac's `~/backups`. Any failure — SSO expired, download, digest mismatch, box unreachable — exits non-zero and the deploy stops before uploading. `--force` does not skip it. If that exact package is already on the box it's a no-op. Never save a rollback only to a scratch dir: #148's was, until it was copied here. First run: the live #148 build → `~/backups/lambda_2026-09-21_c31cc8942a20.zip`.
  - **Retention: the newest 5 packages are kept** (by the time they were saved; ~24 MB each, the box root volume is 8 GB). Older ones are deleted only after the new package is verified on the box, and nothing is deleted if the new one isn't among the five. The Mac's `~/backups` is trimmed the same way.
- **Option A — deploy from CI on merge:** a GitHub Action builds the package (same Docker image as `deploy.sh`) and runs `update-function-code` on every merge touching `api/` or `knowledge/`, using a narrowly scoped OIDC role. Then run the post-deploy smoke check (`/plan`, `/status`, `/overview` → 200 with the current key; old key → 401). If the check fails, roll back to the previous code.
- **Option B — flag it in the Monday drift check:** extend SCHEMA-DRIFT's Monday 08:00 check. Download the live package, compare its `app/` + `knowledge/` files with `main`, and post only when they differ, listing the commits in between. It's read-only, and deploying stays manual.
- **Either way:** add the Lambda's code hash and the matching commit to `context/CONTEXT.generated.md`, so drift is a fact on the snapshot instead of something re-derived each time.

**HARDEN-1 follow-ups (open).**
- **Office equipment unconfirmed** — Precor pin-stack step (default 10 lb), Icarian Smith bar weight, and the hex dumbbell range are guesses; `TODO(office)` in gym-display `src/lib/equipment.ts`. Tracked as **OFFICE-WALK** (backlog).
- **Pre-existing test failures — fixed 2026-09-19 (TEST-STUBS).** `test_confirm_dispatch` now runs on an in-memory DB (it had been writing test rows into production `acos.calendar_audit` / `acos.guardrail_violations` whenever run on the box); `test_commitments_rds` matches the PB-010/011 `add_commitment` and `#id` formats, and its live tier applies the 024/028 columns; `test_opsdiag` moved to `tests/` (run from `artemis/`, `artemis/calendar.py` shadowed the stdlib).
- **Lambda deploy drift** — `rdmis-crm-api` is deployed by hand (`api/deploy.sh`); there is no CI deploy and no check that the live function matches `main`.
- **API Gateway has no access logs or detailed metrics** — per-route counts don't exist. On 2026-07-19 (**10:00–15:00 CDT**, 15:00–20:00 UTC — corrected from an earlier 05:00–10:00 misreading of local-labelled CloudWatch buckets) the API served **154,023 authenticated, successful reads** (~10/s, 0 4xx, no writes); cause unattributed, most likely a client refetch loop during that day's OPS-2 dashboard work (10:35–14:45 CDT, ending with a 14:45 fix for silent reload loops). A second flood ran 2026-05-04 23:00 CDT → 05-05 ~15:00 (~394k requests). Since 2026-09-16 the stage is throttled to 5 req/s, burst 10. Enable HTTP API access logging.
- ~~**Machine settings never captured**~~ — **DONE 2026-09-19 (#114, gym-display #15):** the logger records named seat/pad/range positions, the debrief parser extracts them deterministically from Ryan's own words, and `/overview` returns `setup`. The Lambda was deployed 20:57Z.
- **WAKE-2** — the next day-phase slice (not started).
- **TV retirement decision** — the gym-display TV layout is gone; decide whether the TV is retired for good or gets a read-only glance view.

**COLUMN-GREP — the migrate-first rule for drops and renames (2026-09-20, from an outage).** Before any migration that drops or renames a column, grep **every** `SELECT`, `INSERT` and `UPDATE` naming it across `artemis/`, `scripts/` and `api/`, and report the hits. Code that merely *reads the value* is the obvious half; code that merely *names the column* is the half that bites.
- **What happened:** 039 dropped `health.plan.status`. The paired Lambda change removed the `plan.status == 'completed'` branch but left `status` in `_plan_days`' SELECT column list. `/plan` and `/overview` returned 500 (`psycopg2.errors.UndefinedColumn`). `/today`, `/status` and `/log` were unaffected.
- **Timeline, from the instruments (corrected 2026-09-20).** `acos.schema_migrations`: 039 applied **17:54:12Z**. CloudWatch `/aws/lambda/rdmis-crm-api`: exactly **two** `UndefinedColumn` invocations, both at **17:54:21Z**, and none before or after in 17:45–18:15Z. Lambda `LastModified` for the fix: **17:55:52Z**. Exposure **1 min 40 s**; two failed requests.
- **The first write-up of this incident was wrong**, and `d06fb2c`'s commit message still carries it: "from the migration at 17:57Z until this deploy", and 7 minutes in `CLAUDE.md`. Both numbers were recalled, not measured, and the window it names opens *after* the fix was already live. The commit message cannot be corrected without rewriting `main`, so it stands and this entry is the record. The lesson is the one we keep relearning: an incident timeline is data on the box, not memory.
- **Why the pre-drop smoke test was worthless:** it ran while the column still existed, so it passed for the wrong reason. A check that cannot fail before the change is not a check.
- **The check that works** is textual, not behavioural: `grep -rn "<column>" artemis scripts api` and read every hit, plus a scan of every statement against that table.

**YOGA-4 — flow reorder, 40 s holds, transition table, no tones (2026-09-20).** Built. Every hold is 40 s in both rounds; the round-2 doubling is gone. The lunges run high lunge R → crescent R → crescent L → high lunge L so the side switch lands at the top of the crescent; extended puppy moves after all four lunges, immediately before bridge; easy pose closes round 1 only. A per-step `transition_sec` table replaces the posture-derived 3 s / 5 s rule and is now the only source of transition lengths — `posture` stays as description, nothing computes from it. The lead-in moved from 3 s to 7 s before the hold ends, and every beep, chime and tone is gone from the flow player (the workout timer keeps its beeps). Totals: office 40:32, home 32:27, both under the 45 min target. Seated meditation stays 60 s and savasana 180 s, confirmed with Ryan when the spec's "stay at 3 min each" did not match the seeded 1 min.

**YOGA-5 — softer voice, Sanskrit cues, mid-hold cues — LIVE 2026-09-21 (artemis #143, #144; gym-display #18, #19).**
- *Voice* (`src/lib/voice.ts`): Ava (enhanced first) → Allison → Samantha → the platform default, matched on name **and** `voiceURI`. Rate **85 %** and pitch 0.95. iOS returns an empty `getVoices()` on the first call, so it waits for `voiceschanged` (with a timeout), keeps listening for a voice installed mid-session, and re-picks when a stored voice has been uninstalled rather than silently keeping the default.
- *Settings* on the ready screen only: a cycling **Voice** button, the shared **Stepper** for speed, and a **Cues during holds** toggle. The first build used a native `<select>`, range slider and checkbox, which broke the app's no-native-controls rule (`e2e/nav.e2e.ts`); rebuilt on the app's own touch controls. Speed is a whole percentage because the shared Stepper rounds to one decimal — 0.85 displayed as "0.8".
- *Sanskrit*: every pose carries `sanskrit` (shown small beneath the English name) and `sanskrit_spoken` (phonetic, spoken only, never rendered). The move cue is "Move to <phonetic>" with no side. Meditation and the Stretch Trainer have none and keep English. `validate_flow` fails on a pose with no entry.
- *Mid-hold cues*: one short line 12 s into each pose, then silence. **Meditation and savasana are silent for their whole hold** (Ryan's correction — the first build spoke their line at the start). Toggle defaults on; only an explicit off turns it off.
- **The 11 flow rows were reseeded after #143/#144** — seeded before them, they carried neither field, and YOGA-5 was live but inert until the reseed. Verified 11/11.

**GD-STRENGTH-CUES — rest-period voice prompts — LIVE 2026-09-21 (gym-display #21).** (#20 was GitHub-closed when #19 merged and deleted its base branch; #21 is the same commit.)
- *Voice prompt* in the rest before each **new** exercise — once per exercise, never between sets of the same one, never on a round break, never before the first: "Next exercise is leg press. 12 reps at RPE 6. Your last sets averaged RPE 6 at 150 pounds for 12 reps." `promptTarget()` is pure so those rules are unit-tested. **Skipped entirely when the rest is shorter than the sentence**, rather than talking over the next set.
- *"Your last sets averaged"* = the mean across all sets of that exercise in the most recent earlier session; weight to the pound, reps whole, RPE to 0.5; skipped and inferred rows excluded. Computed client-side from `/sessions` — no API change.
- **The average is for the spoken sentence only** (Ryan). `/last_logged`, the stepper prefill and the LAST tile keep the most recent set.
- The on-screen target line this spec also added was **superseded the same day by GD-DISTANCE's tiles**.

**GD-DISTANCE — the active-set screen, readable from ~20 ft — LIVE 2026-09-21 (gym-display #22).**
- **Three tiles across the top** — REPS, RPE (with "stop with ~4 reps left" beneath), LAST (the weight, with ×reps · RPE beneath) — then SET x OF y in the accent, the name, and the timer at exactly **half** the rest timer. LAST is `/last_logged`'s single last set, never the spoken average (tested with a history averaging 150 against a last set of 175).
- **The journey map hides during an active set in landscape only**, and returns for rest, warmup and cooldown. Portrait keeps its strip: its pane is already full width, so hiding it would gain no size, only a vertical jump.
- **Measured (WebKit, 132 CSS px = 1 in, cap height 0.705 em):** REPS/RPE **1.25 in**, LAST **0.98 in** in landscape (0.77 / 0.61 before the map moved); portrait 0.84 / 0.66. **Ryan's 1.5–2 in target is not met** — 1.5 in would need REPS and RPE two across at full width with LAST demoted; 2 in doesn't fit with the controls on screen.
- **The bottom action row is five fixed slots outside the pane** (Prev · Pause · Skip · Busy · More). Inside the pane it followed the pane's width, so hiding the map moved every button at each set/rest boundary — Pause jumped from x 251–474 to 627–802 — and its `auto-fit` columns already reflowed by button count. Every button now holds the same x-range in every mode.
- **Exercise notes** ("log seat + pin setting") moved from the active-set screen to the rest logger, beside Machine setup.
- Tested: no layout shift when `/last_logged` arrives late or the timer changes width; each set/rest switch settles in one animation frame (a test proven to catch an injected 300 ms animation); nothing overflows or clips.

**YOGA-6 — higher-intensity flow. Backlog, unscheduled; do not spec further until the recovery flow has run a few weeks and its player is stable.** A second flow type beside Recovery Flow, aimed at raising heart rate rather than stretching: a new `session_type` (e.g. `power_flow`) sharing the same player, transition-table mechanism, voice cues and hands-free operation — **no second player**. Shorter holds, continuous linking instead of long static holds, likely more rounds; specifics to be decided with Ryan when it is built, not assumed now. Open then: target duration; where it sits in the week (it is cardio-shaped, so it may replace a Z2 day rather than a flow day); whether it is mat-only (which would make it usable at Brown Deer and Richfield, where equipment is thin); office or WI day. **Depends on WATCH-1 workout matching** — "higher heart rate" is a claim worth measuring, and once workouts and HR are flowing the real session HR says whether it does what it is meant to.

**YOGA-JENI — a static flow page for a second person. Backlog, low priority, unscheduled.** A fixed flow definition played by the existing player on its own route (`gym.rdm.is/flow/jeni` or `?flow=jeni`). No plan lookup: it reads nothing from `health.plan` and derives nothing from the cycle, playing the same sequence every time. No recording: no `session_log` writes, no completion logging, no check-in, no pre-fill. Auth is a Cloudflare Access policy edit adding her email to the existing gym app — no code, no user model, no `user_id` anywhere. Same player, voice cues and hands-free behaviour; a missing pose or cue is added to the existing registry the usual way. **Blocked on Ryan supplying her sequence with hold times**, so the flow content is deliberately unspecified. Explicitly out of scope: multi-user support, per-user data, her own logging or reports. One static page, nothing more.
**SECRETS-LP — replace `SecretsManagerReadWrite` on `acos-ec2-role` (2026-09-20). PROPOSED, NOT APPLIED — awaiting Ryan's review.** Policy drafted at `infrastructure/iam/acos-ec2-secrets-least-privilege.json`.

- **What the box has today.** `SecretsManagerReadWrite` is an AWS-managed policy and it is not what its name suggests. Its first statement is `secretsmanager:*` on `*` — read, write, **delete**, and rotation-config on every secret in the account, including `prod/github/pat`, `rdmis/dev/github`, `rdmis/mailerlite/api_key` and `rdmis/dev/twilio`, none of which the box touches. It also carries `cloudformation:CreateChangeSet` / `ExecuteChangeSet` on `*` and `lambda:CreateFunction` / `AddPermission` on `arn:...:function:SecretsManager*`. Found while verifying the LAMBDA-DRIFT permissions: the box could call `lambda:ListFunctions`, which none of its intended policies grant — that call comes from here.
- **What the box actually uses.** Eleven secrets read: the RDS credential (`rds!db-bfe5d90f-…`), `rdmis/dev/anthropic-api-key`, `mattermost`, `gmail-oauth`, `gmail-token`, `calendar-token`, `booking-links`, `crm-api-key`, `openweather-api-key`, `acos/vault-repo`, and — since DIET-1 (#145, 2026-09-21) — `rdmis/dev/notion-token`. That last one matters more than its size: `get_notion_token()` returns `None` on **any** failure, by design, so a policy missing it would not error — the 00:15 pre-fill would just record `prefill_outcome = 'unavailable'` every day. **Any secret added to `knowledge/secrets.py` must be added to this policy in the same PR.**
- **Read-only would break Gmail and Calendar.** `artemis/gmail.py` (:104, :136) and `artemis/calendar.py` (:89, :120) call `put_secret` to store refreshed OAuth tokens. Strictly read-only, both stop working at the next token refresh — silently, on a timer, which is the worst shape of failure. The draft therefore allows `PutSecretValue` on exactly those two secrets and nothing else.
- **What the proposal drops:** every other secret in the account; `DeleteSecret`, `CreateSecret`, `UpdateSecret`, `RestoreSecret`, `TagResource`, rotation config; all CloudFormation; all Lambda write actions. It keeps `GetSecretValue` + `DescribeSecret` on the ten, and `PutSecretValue` on the two token secrets.
- **What else would break, checked:** nothing in `artemis/` or `knowledge/` reads `health-api-key`, `watch-ingest-key` or `zoho-webhook-secret` — those are the Lambda's, which has its own role. `rdmis/dev/twilio` is referenced by `knowledge/secrets.py` but called from nowhere. The provisioning scripts (`scripts/provision_health_api_key.py`, `provision_openweather_key.py`) do write secrets, and after this change must be run from the Mac under `rdmis-admin`, which is where they already belong.
- **Separately, a live bug this surfaced:** `artemis/voice.py` reads `rdmis/dev/deepgram-api-key` and `rdmis/dev/elevenlabs-api-key`. **Neither secret exists in the account.** That path is already broken today; the policy change is not what breaks it, but the policy should not list them either.
- **Rollback** is one command — reattach the managed policy — and the blast radius of getting it wrong is the box losing a secret it needs, which is loud and immediate everywhere except the two OAuth refreshes.

## 7. Operating disciplines (non-negotiable)

Propose-then-confirm · **column-grep before a drop or rename (COLUMN-GREP)** · the Brad Spaits rule (no autonomous external comms; activation gates) · trust-the-data-not-the-report · verify-on-the-live-box · statistics-vs-semantics wall · generated-vs-authored split · CT-anchored "today" · one system of record (RDS) · no-tokens-on-disk (Secrets Manager) · solo-scale (no enterprise patterns) · `feat/*`→PR→`main`, migrate-first deploy. Full detail in `CLAUDE.md`.

---

## 8. Why the order

1. **Unified state (✅ done)** — no trustworthy analytical/cognition layer over a split-brain. Done first, correctly.
2. **Knowledge layer (vault) (✅ v1 — PB-011)** — the semantic surface Ryan authors; the synthesis surfacer needs it populated. Precedes the cognition medallion (the June decision, now reality): the cognition layer reasons *over* adjudicated knowledge, so the knowledge substrate is laid first.
3. **Cognition layer** — learning/self-proposal needs one coherent decision log. (HEALTH-1's confabulating stub is what happens when this is half-built and ungated — build it deliberately.)
4. **Bounded autonomy** — safe only once state is trustworthy, knowledge adjudicated, decisions logged, automations gated.

The migration was load-bearing foundation, now laid. The system has one source of truth; the next layers can be built on solid ground.
