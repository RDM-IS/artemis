# Artemis Playbooks

## PB-001: Demo Access Notification (v2)

> **Note:** v2 replaces the legacy flat-contact approach. All CRM writes
> now flow through the CRM Write Guard (PB-008) for dedup before insert.

**Module:** `artemis/demo_intake.py`

**Trigger:** Email from `demo@rdm.is` with subject containing
"Lucint demo accessed" (case-insensitive). Scanned every 5 minutes
via scheduler job `demo_intake`. Also triggered from triage if matched.

**Extraction from email body:**
- `Name:` line
- `Email:` line (domain extracted automatically)
- `Company:` line (set to None if "not provided", "no company", or empty)
- `Time:` line

**Actions:**
1. Apply Gmail labels:
   - `@artemis`
   - `@artemis/pipeline`
   - `@artemis/pipeline/demo-request`
2. CRM Write Guard — company:
   - `entity_type="company"`, domain match or fuzzy name
   - `confidence="high"` if company name extracted, `"low"` if domain-only
   - `types=["Prospect"]`
3. CRM Write Guard — person:
   - `entity_type="person"`, email exact match
   - `source="lucint-demo"`, `confidence="high"`
4. CRM Write Guard — relationship:
   - person + company, `role="Contact"`, `is_primary=True`
5. CRM Write Guard — engagement:
   - company, `type="Pilot"`, `gate=0`, `status="Active"`
6. CRM Write Guard — touch_event:
   - Inbound email, `playbook="PB-001"`
7. Create dynamic Gmail label `@artemis/pipeline/[company]`:
   - Sanitized: lowercase, spaces/special chars replaced with hyphens
8. Create commitment: "Follow up with [name] re: Lucint demo"
   - `due_date` = next business day, `effort_days` = 1
9. Mark message as processed (shared `acos.processed_billing` table)
10. Post to #artemis-ryan:
    - Lead name, company, gate, email, CRM status, follow-up date

**Label output state:**
- `@artemis`
- `@artemis/pipeline`
- `@artemis/pipeline/demo-request`
- `@artemis/pipeline/[company]` (dynamic per-company)

**Error Handling:**
- Name or email not extractable: apply `@artemis/needs-review`,
  post to Mattermost, halt (do not write to CRM)
- Any CRM write guard returns "flagged": continue remaining steps,
  note flagged entities in Mattermost post
- Commitment creation fails: log warning, do not halt
- Fatal error: post raw failure to Mattermost for manual handling
- Mark as processed ONLY after successful Mattermost post

**Testing:** `python -m artemis.demo_intake --dry-run`

## PB-002: Meeting Follow-up with Action Items

**Trigger:** Email from a known contact after a meeting that contains
"next steps", "action items", "follow up", or a date for a next meeting

**Actions:**
1. Extract all action items (bullet points or numbered lists)
2. For each action item create a commitment:
   - due_date = 2 days before next meeting date (if mentioned),
     else 5 days from today
   - effort = 2 days default
   - client = sender's company or domain
3. Create a follow-up commitment: "Send deliverables to [sender]"
   due_date = 1 day before next meeting, effort = 1
4. Mark email as NEEDS_ACTION with due_date = earliest commitment due_date
5. Post to #artemis-commitments with all extracted items
6. Post to #artemis-ops: ":clipboard: [sender] follow-up processed —
   [N] commitments created, next meeting [date]"

## PB-003: Survey / Feedback Request

**Trigger:** Email containing "survey", "feedback", "2 minutes",
"fill out", "rate your experience"

**Actions:**
1. Mark as NEEDS_ACTION with due_date = 2 days from today, effort = 1
2. Add note: "Quick task — estimated 2-5 minutes"
3. Post to #artemis-ops only if sender is a known important contact,
   otherwise batch into morning brief

## PB-004: Meeting Request / Calendar Invite

**Trigger:** Email containing a proposed meeting time or calendar invite

**Actions:**
1. Mark as NEEDS_ACTION immediately
2. Post to #artemis-ops: ":calendar: Meeting request from [sender] —
   needs response"
3. Include proposed time in the post

## PB-005: Commitment Deadline Reminder Chain

**Trigger:** Scheduled — runs against all active commitments

**Actions:**
1. 5 days before due_date: post to #artemis-commitments if not started
2. effort_days before due_date: ":warning: Start today" alert
3. 1 day before due_date: ":red_circle: Due tomorrow" alert
4. On due_date: "TODAY" alert, escalate to #artemis-ops
5. When commitment marked done AND a "forward deliverables"
   follow-up exists: post reminder to #artemis-ops

## PB-006: Availability Request

**Trigger:** Email containing "when are you free", "schedule a call",
"find a time", "what times work", "send me your availability",
"when works for you", "do you have time", "are you available",
"set up a meeting", "book a time"

**Actions:**
1. Extract requested timeframe from email (default: next 5 business days)
2. Query calendar for the timeframe period
3. Find 4-6 open slots based on meeting preferences:
   - Respect MEETING_HOURS_START / MEETING_HOURS_END
   - Apply MEETING_BUFFER_MINUTES between events
   - Exclude focus blocks ("focus", "deep work", "work session")
   - Prefer spreading slots across multiple days
4. Post formatted availability to #artemis-ops with numbered slots:
   - Include sender name, company, subject, and original quote
   - Include `send [numbers]` / `send all` / `edit` / `cancel` instructions
5. On `send [numbers]`:
   - Generate professional reply draft via Claude
   - Include selected time slots and BOOKING_LINK (if configured)
   - Post draft to #artemis-ops for approval
6. On `confirm`:
   - Send reply via Gmail API
   - Mark original email as WAITING in inbox zero
7. NEVER auto-reply — all sends require explicit user confirmation

## PB-007: Billing Intake

**Trigger:** Email has Gmail label `@artemis/billing` (applied to emails
arriving at billing@rdm.is)

**OAuth Requirements:** spreadsheets scope (added to
setup_oauth.py — re-run if missing)

**Actions:**
1. Fetch full email body and detect attachments via Gmail API
2. Extract: sender name, sender domain, subject, date, dollar amounts
   (regex: `\$[\d,]+\.?\d*` or `[\d,]+\.\d{2}`)
   - Amounts are deduplicated before processing (forwarding artifacts)
2a. Vendor entity lookup via `crm_write_guard` — see PB-008.
   If flagged, add review note to expense but never drop the billing record.
3. Classify expense category by keyword matching on subject + sender:
   - Infrastructure (AWS, Azure, etc.)
   - SaaS / Software (GitHub, Notion, Anthropic, etc.)
   - Legal, Insurance, Hardware, Sales & Outreach, or Misc
4. Generate Gmail deep link for the message:
   `https://mail.google.com/mail/u/0/#inbox/{message_id}`
   Attachment filenames (if any) are listed in the Notes field.
5. Append row to expense tracking Google Sheet:
   [Date, Vendor, Description, Category, Amount, Payment Method,
    Founder Loan?, Reimbursed?, Reimbursed Date, Document Link, Notes]
   - Founder Loan = "Yes" by default (pre-MSA)
   - Notes = "Auto-logged by Artemis. Review required." if uncertain
   - Document Link = Gmail deep link
6. Mark message ID as processed in Postgres (prevents re-processing)
7. Post to #artemis-ryan:
   Billing intake logged — sender, amount, category, Gmail link
   React with checkmark if correct or reply to correct fields

**Error Handling:**
- Sheets append fails → post all data to Mattermost for manual entry
- Multiple distinct amounts found → use largest, note all in Notes field
- Forwarded founder loans from ryan@rdm.is → suppress ambiguity flags
- Never silently drop an expense

**Testing:** `python -m artemis.test_billing --dry-run` (no writes)
**Unit tests:** `python -m artemis.test_billing --unit`

## PB-008: CRM Write Guard

**Trigger:** Any playbook that creates or references a CRM entity
(companies, persons, relationships, engagements, touch events).

**Module:** `artemis/crm_write_guard.py`

**Entry point:**
```python
crm_write_guard(entity_type, data, confidence, source_pb,
                gmail_message_id=None, gmail_client=None, mm_client=None)
# Returns: {"status": "written"|"exists"|"flagged", "entity_id": UUID|None, "flag_reason": str|None}
```

**Match algorithm:**
- **Company:** domain exact match → exists. Name Levenshtein ≤ 2 →
  high confidence = auto-merge, low = flag. No match → create.
- **Person:** email exact match → exists. Name fuzzy + same company →
  high = merge, low = flag. Name fuzzy + different company → ALWAYS flag
  (potential org change). No match → create.
- **Relationship:** active match + same role → exists. Different role →
  end old, create new. No match → create.
- **Engagement:** active match → update gate/status. No match → create.
- **Touch event:** always write, no dedup.

**Flag routing (ambiguous matches):**
1. Write proposed data to `acos.pending_crm_writes` (expires after 7 days)
2. Apply Gmail label `@artemis/needs-review` if gmail_message_id provided
3. Post to #artemis-ryan with candidate comparison and confirm/reject commands:
   `@artemis crm confirm [id]` or `@artemis crm reject [id]`
4. Return `{"status": "flagged"}` — caller must handle gracefully

**Mattermost commands:**
- `@artemis crm confirm [pending_id]` — execute the pending write, remove from queue
- `@artemis crm reject [pending_id]` — discard pending write
- `@artemis crm pending` — list all unresolved pending writes

**Tables (migration 012):**
- `public.persons`, `public.companies`, `public.relationships`,
  `public.engagements`, `public.touch_events`
- `acos.pending_crm_writes`, `acos.funding_events`

**Constraints:**
- Never drop a billing expense — if CRM write fails, billing continues
- All successful CRM writes post confirmation to #artemis-ryan
- API keys never logged or echoed
- Quiet hours respected for proactive notifications

## PB-009: Personal Training

**Trigger:** Multiple — see below. All routing isolated to channel
`#artemis-ryan` (DM only, never broadcast).

**Module:** `artemis/health.py` (intent handlers), `artemis/health_office.py`
(office gym inventory + program builders), `artemis/scheduler.py` (cron jobs),
`api/app/routers/health.py` (read API consumed by gym-display at `gym.rdm.is`).

**Database:** `health` schema (migration 013) — `health.plan`,
`health.session_log`, `health.daily_state`, `health.adjustments`,
`health.training_rules`, `health.phase_config`. Isolated; no `public`
or `acos` writes.

### Triggers — proactive (scheduled jobs)

Health-tier jobs post in the **wake** and **open** phases; in **quiet** they
are held durably and flushed at the next wake (WAKE-1 — see the day-phase
section in `docs/ARTEMIS_STATE.md`). All times are local wall-clock in the
**active** timezone, which follows `set timezone to <place>`.

**The wake post (04:30 local, `job_wake` → `artemis/wake.py`)** — there is no
separate morning-prompt cron any more. One post carries: today's session
(name, duration, `Where:` from `blocks.location`, equipment, first lift,
warmup), the morning survey, any held health notices, and pre-departure
(checklist, first event, weather, `depart:` commitments).

The survey variant comes from the **plan row**, never the weekday
(`wake.prompt_type_for`): `rest_mobility`/`walk` → logging-only, everything
else → `workout_am`. Ryan replies via the existing `log_morning_state` intent;
after the `daily_state` row writes, the calibrated plan posts ~15 min later
(`job_health_calibration_followup`, scheduled as a one-shot at wake time).

> The old weekday table (Tue 04:01 / Wed 07:00 / …) is gone: it encoded the
> home-gym AM/PM split. Workouts are now AM at the office gym, so the plan row
> is the only thing that decides.

**`job_health_nag`** — 16:30 local, daily. Fires only if today's plan has no
`session_log` row. Suppressed on `rest_mobility` and `walk`. It moved off
21:00 because 21:00 now sits inside the quiet window (17:00–04:30) and would
never post.

**`job_health_inferred_summary`** — 21:50 local, daily. Backstop, writes
only — it posts nothing, so the quiet window does not apply. If the
plan exists, isn't rest/walk/skipped, and still has no log, write a
placeholder `session_summary` row with `logged_via='inferred'` and
`notes='no debrief — assumed at baseline'`. Autoregulator treats these
as low-confidence signal.

### Triggers — reactive (Mattermost messages in #artemis-ryan)

Routed via `detect_health_intent()` regex pre-check, then Claude
intent classifier (rules 9-11 in `artemis/intent.py`):

- **Morning state** ("slept 6h, energy 3/5, legs sore", "feel great",
  "RHR 58") → `log_morning_state` → upserts `health.daily_state` on
  `state_date` (COALESCE preserves earlier-filled fields)
- **Workout debrief** ("did 3 rounds of...", "burpees 15 reps RPE 8",
  "done", per-exercise reports) → `log_workout_debrief` → inserts N
  `session_log` exercise rows + 1 summary row, all with
  `logged_via='mattermost'`
- **Edit grammar** (`fix burpees rpe 9`) → `handle_fix_intent` updates
  the most recent matching `session_log` row via LIKE search; falls
  through to debrief handler if no match
- **Modality swap** ("swap today to elliptical") → propose-then-confirm swap
  of a cardio/walk session to another office machine: treadmill, elliptical,
  upright bike, recumbent bike, or stepmill. Same stimulus; `swap revert` undoes.
- **Retired: bike trainer override** (`trainer set indoor` / `trainer set
  outdoor`) — HEALTH-2 retired the home bike. The phrase is still matched
  deterministically and gets an honest "retired — nothing changed" reply; it
  never reaches a bike handler or the LLM.

### Equipment & location mapping

**Location is plan data (HEALTH-2).** Each `health.plan` row carries
`blocks.location` and `blocks.equipment`; `resolve_equipment_and_location`
prefers them. The static `_EQUIPMENT_MAP` (built from
`artemis/health_office.py`) is the fallback for a row that lacks them. The home
gym still exists — a row that trains there says so in `blocks.location`.

Office gym inventory (all Precor): pulldown/seated row, rear delt/pec fly, leg
extension/leg curl, leg press/calf extension, abdominal/back extension machines;
S3.23 functional trainer (rope + handles); Icarian Smith machine; hex DBs,
Olympic bar + plates, 2 flat benches, 1 adjustable bench; captain's chair/dip
tower, 45° back extension; treadmills, ellipticals, upright bike, recumbent bike,
stepmill, Stretch Trainer; stability balls, mats. The rower and outdoor bike are
retired from the plan.

Fallback map:

```
strength_a       -> office gym: leg press, DBs + flat bench, pulldown, leg curl,
                    functional trainer (rope), captain's chair   (first lift: Leg press)
strength_b       -> office gym: DBs, seated row, adjustable bench, leg extension,
                    rear delt fly, functional trainer, 45° back ext (first: DB goblet squat)
strength_c       -> office gym: DBs, pec fly, functional trainer, adjustable bench,
                    calf press, ab machine                        (first: DB Romanian deadlift)
cardio_z2        -> office gym: treadmill / elliptical / recumbent / upright bike
cardio_intervals -> office gym: stepmill / upright bike
walk             -> outside: walking shoes (rain or <40°F -> indoor walk)
rest_mobility    -> office gym: mat / Stretch Trainer
```

Weather is consulted for `walk` only. There is no bike indoor/outdoor decision
and no `trainer set` override any more.

**Office program (seeded 2026-09-16 → 2026-11-08)** —
`scripts/reseed_health_plan_v2.py --office` (dry-run default, `--commit` to
write), validated by `scripts/validate_health_plan.py`. Ramp-up 9/16-9/20
(Z2 20 min, Strength A 2 sets, Z2 20 min, recovery walk, rest), then weeks 1-7
(phase 1): Mon A · Tue Z2 · Wed rest · Thu B · Fri C · Sat rest · Sun walk.
Sets/RPE/Z2: wk1-2 2/6/20 min · wk3-4 3/7/30 · wk5-6 3/7.5/35-40 (Fri C
stepmill/upright-bike finisher 6×30s/90s) · wk7 deload 2/6/30. Warmup 5 min
elliptical, cooldown 5 min Stretch Trainer; loads null weeks 1-2; week 1
machine exercises note "log seat + pin setting".

The feat/health-ramp nightly slide/evaluate job and its `--ramp` reseed are
retired (their 7/25-9/11 window passed undeployed and would overwrite this plan).

### Trainer voice

All confirm-back replies must use the trainer voice template at the
top of `artemis/health.py`: short, direct, no fluff, no shame, no
fake hype. Example morning confirm-back:

> Logged: 6.5h sleep, energy 3/5, legs sore (3), RHR 58. Anything to fix?

Example debrief confirm-back:

> Logged 4 exercises:
> • Burpees: 15 reps, RPE 10, peak HR 159
> • RDL: 10 reps @ 50lb, RPE 6
> • Rows: completed, "felt strong"
> • Plank: SKIPPED (knee was off)
> Overall RPE 8.
> Noted: "rest too easy on Z2 recovery, try 60s"
> Reply 'fix burpees rpe 9' or 'good' / nothing.

### Error handling

- Parse failure (Claude intent classifier returns malformed) -> trainer
  voice error reply, no DB write, do not lose the message
- DB write failure -> warn user with the failed payload echoed back so
  they can retry; never silently drop a session
- `fix <exercise>` non-match -> fall through to debrief handler (treat
  as new debrief)
- Idempotency: `_idempotency_key()` hashes the Mattermost message ID;
  duplicate webhook deliveries are no-ops

### Recovery override

When the calibrated plan is built (`build_calibrated_plan_post`),
the prompt prepends a recovery override message if **sleep < 5h
AND energy ≤ 2** on today's `daily_state` row. The override changes
the Mattermost prompt text only — it does not modify the
`health.plan` row, does not set `daily_state.is_override`, and does
NOT update what `/api/health/today` returns. Consequence: the
gym-display TV at `gym.rdm.is` will show the original scheduled
workout while the trainer voice prompt on your phone says "drop
to mobility + walk." Mattermost is authoritative when the two
disagree.

The 5h / energy 2 thresholds are conservative defaults from sports
science guidance — protecting against compound-lift injury risk
when both sleep deprivation and CNS fatigue stack. They are NOT
configurable in T4; tuning will land with the autoregulator
ticket which moves the rules into `health.training_rules` config
reads instead of hardcoded prompt-builder checks.

User can override the override by replying "do it anyway" — the
debrief intent will fire normally and log against the original plan.

### Out of scope (deferred to future tickets)

- **Autoregulator** (separate ticket) — adjusts `health.plan` rows
  based on rolling RPE / recovery signal. Will write to
  `health.adjustments` audit table.
- **Workout creation/editing from chat** — only logging is supported.
  The office program is seeded 2026-09-16 → 2026-11-08; future plan
  modifications go through the autoregulator or a reviewed reseed.
- **Wake word ("Hey Artemis" voice mode)** — Picovoice Porcupine
  planned, not built.
- **Explicit `location` and `equipment` columns** on `health.plan` — not
  needed for now: both live in `blocks` (HEALTH-2).

### Frontend consumer

`gym.rdm.is` (gym-display) reads `GET /api/health/today` with
`X-API-Key` header, displays today's plan on TV/iPad in the gym.
Hosted on Cloudflare Pages, gated by Cloudflare Access OTP/SSO to
`ryan@rdm.is`. Frontend repo: `RDM-IS/gym-display`.

### Testing

- `python3.11 tests/test_health_seed.py` — 13 tests, validates 137
  baseline plan rows, phase distribution 28/42/42/25, day-of-week
  mapping
- `python3.11 tests/test_health_intents.py` — intent detection +
  handlers + nag logic, all DB and Claude calls mocked
- `python3.11 tests/test_health_office.py` — office schedule/ramp, location
  from blocks, retired bike/trainer/ramp, regression: no row from 2026-09-21
  onward references a rower or bike on trainer
- API: 12 tests in `tests/api/test_health.py` (auth envelopes, CORS,
  no_plan envelope, JSONB serialization)

---

## PB-010: Meeting Intelligence / Colleague Dossiers

**Module:** `artemis/dossier.py` · **Migration:** `024_dossier.sql`
**Intent:** deterministic short-circuit in `intent.detect_dossier_intent`,
wired ahead of the LLM classifier in `main._handle_dossier_command`.

**Concept.** One dossier per person, five sections: (1) Position & terrain,
(2) What they need from me — both Ryan-authored; (3) Interaction log
(append-only, draft→approve); (4) Open loops (undated `dossier_loop` watch-items +
dated `acos.commitments` carrying a `dossier_id`); (5) Idea bank with
cross-pollination provenance.

**The wall.** Artemis extracts/connects/drafts (statistics); nothing enters the
record until Ryan approves (semantics). Every autonomous write lands in a
draft/proposed state. Confirmations always render from the re-read written row
(no-fabrication gate). Org-agnostic schema (FCA is the first tenant).

**Commands.**
- `met with <names> [about <topic>] [on YYYY-MM-DD]` + notes (or a text
  attachment) → immutable `dossier_meeting` capture, attendee linking (unknowns
  become inactive stubs), then autonomous draft extraction.
- `dossier review` → numbered pending drafts grouped by person;
  `approve all` · `approve 1-4` · `approve 1 & 3` · `edit 2: <text>` · `drop 4`.
- `brief <x> [about <y>]` / `prepare a meeting package` / `i'm meeting with <x>`
  → read-only pre-brief (open loops, needs, strongest idea, recent context;
  ⚠️ flags an empty idea bank). Multi-person packages dedupe shared loops/ideas.
- `remind me to <task> <when>` → immediate commitment (explicit, no approval);
  attaches a `dossier_id` if a known name appears; an "I emailed X …" phrasing
  also drafts a one-line log touch (inferred).
- `what's on the to dos today | this week` → CT-anchored to-dos (overdue → today
  → rest of week), dossier-linked items attributed; draft to-dos listed separately.
- `dossier show <name> [--drafts]` · `dossier new <name>` ·
  `dossier set <name> position:|needs: <text>` (propose-then-confirm).

**Seed:** `scripts/seed_dossiers.py` (idempotent) parses
`scripts/seed_data/*.md` — see the README there for the format.

**Coverage note.** Canonical to-do home is `acos.commitments` (migration 020),
extended by 024 with nullable `due_date` + `dossier_id`/`meeting_id`. Dossier
action items are `status='draft'` commitments, invisible to the reminder radar
until approved.

### PB-010c — Org assignments & org chart (migration 026)

People are org-independent; **employment** is the org-scoped fact
(`acos.org_assignment`, effective-dated). The org tag moved off `acos.dossier`
onto this table so reorgs/job-changes accrue history. Reporting edges are FACTS
Ryan approves — the LLM never asserts structure; renders read only approved
current rows (drafts invisible).

- **Set (propose-then-confirm):** `dossier set jennifer title: …` ·
  `… reports_to: <slug>` · `… org: fca-odae` · `… org_root[: false]`. Multiple in
  one line: `dossier set sarah org: fdic, title: Senior Examiner` (a comma inside
  a title value is kept — a comma only splits fields before a `key:`). Each change
  is close-and-insert (close current row `valid_to=today`, insert new current
  carrying forward unchanged fields); same-value → no-op. `reports_to` to an
  unknown slug is refused with a `dossier new` offer.
- **Queries (read-only, deterministic-routed):** `org <person>` (title/org, chain
  up via recursive CTE with a depth-20 cycle guard, directs, peers) ·
  `org <orgname>` (roster as a tree where edges exist, flat where not) ·
  `org history <person>`. Natural forms route too: *where does X fit*, *who does
  X report to*, *who reports to X*. Bare `org` → usage hint (never LLM). Person
  slug wins over an org of the same name (the alternative is noted).
- **Extraction:** an optional `org_signals` array drafts stated edges only
  (`{dossier_slug, field, value, evidence}` — no quote → no signal; structure is
  never inferred from a title). Signals enter the review queue as `[org]` items;
  approve runs the same close-and-insert as `dossier set`.
- **Surfaces:** `dossier show` / `brief` headers gain `<title> · <org>`;
  multi-person briefs group attendees under their org; a reporting change within
  60 days adds a "recent reorg" line.

Two migration-026 corrections vs the spec DDL (both surfaced): the
`one_current_assignment` unique index is scoped `… AND status='approved'` so
draft rows (also `valid_to IS NULL`) coexist without collision; and a nullable
`evidence` column stores the draft signal's provenance quote for the review item.

### PB-010d — Org profiles (migration 027)

Organizations get profiles parallel to person dossiers, split by authorship:
- **Authored sections** (`org set <orgkey> overview:|active_work:|opportunities:|display_name: <text>`) — Ryan writes them, propose-then-confirm; prose is terminal (an embedded `key:` never splits). Artemis never edits these.
- **Org notes** — append-only, drafted by extraction from an `org_notes` schema field (org-level facts only, evidence-gated; person facts are never org notes and the EXT-1 critique pass enforces category fit). Reviewed as `[org-note <org>]` items, approved with meeting provenance.

`org <orgname>` render: display_name → overview → active work → opportunities → people tree → recent approved notes (cap 5, overflow → `org notes <org>`). Empty sections omitted; a bare auto-created profile shows the roster only. Extraction auto-creates a bare profile (statistics act) so an org note's FK holds — 3rd-party intel ("FDIC is hiring" from an FCA meeting) is valid. Briefs prepend the shared org's overview first-sentence when all attendees share a profiled org.

**Deferred (PB-010b / later):** staleness nudges, calendar-triggered auto-briefs,
Obsidian `generated/dossiers/*` projection, approval-queue web UI, pgvector
search over raw notes. **PB-010c-deferred:** matrix/dotted-line orgs (edges
table), relationship-type taxonomy (§1 prose carries it), org-chart web viz.
**PB-010d-deferred:** org-to-org relationships, org-level open-loops, editing/
retiring individual approved notes (append-only for now).

**Testing:** `python3.11 -m artemis.test_dossier` — mocked tier (parsing,
resolution, deterministic routing, commitment origin, CT windows, malformed-LLM
safety) always runs; LIVE Postgres tier (migrations + real state machine) runs
when a local PG is reachable.

## PB-011: Vault / Second Brain Ingest (v1)

**Module:** `artemis/vault.py` (schema `vault`, migration 028)

Ryan's Obsidian vault (RDM-IS/vault, private) is the canonical human-authored
knowledge store. Artemis ingests it into Postgres (schema `vault`), runs one
extraction pass per new note, and surfaces everything as proposals through the
existing approval gates. The vault FILE is canon; Postgres is a rebuildable
projection. **THE WALL** (statistics vs semantics): Artemis parses, counts, links,
detects, and PROPOSES; Ryan approves, names, blesses. Nothing extraction produces
auto-writes to a system-of-record table — on approval only, a proposal is written
through the EXISTING creation paths (commitment creation, dossier draft-approval).

**Sync (one job, two triggers):** 03:30 local cron + on-demand `vault sync` /
`digest`. It moved off 04:00 so the ingest finishes before the 04:30 wake post
reads from it; it writes only and posts nothing.
Read-only git mirror (shallow clone, fetch + reset --hard); PAT used at fetch time
only, never on disk. Upsert notes → recompute `[[wikilinks]]` → throttled extraction
pass → proposals.

**Commands:** `vault sync` · `vault status` (note counts, proposal/queue counts, PAT
expiry, running version footer) · `digest` / `today's digest` · `proposals` /
`proposals expired` · adjudicate a live digest with `approve 1-3` / `approve all` /
`reject 2`.

**Morning brief:** pending-proposals digest, yesterday's journal diff (coverage),
yesterday's capture-coverage line, and a **vault PAT expiry warning** (≤14 days →
rotate-now block; OPS-1). **Coverage nudge:** weekday 16:30 CT, at most one post when
real meetings outnumber captures.

**OPS-1 self-diagnosis:** a failed `vault sync` renders a deterministic runbook
(`artemis/opsdiag.py`) — the failure class, the literal error, and exact remediation
commands — and writes an `acos.audit_log` row; unknown failures surface the raw error
labeled `unclassified`.

**Testing:** `python3.11 -m artemis.test_vault` (mocked + LIVE Postgres tiers) and
`python3.11 -m artemis.test_opsdiag` (runbook classification + version truth).
