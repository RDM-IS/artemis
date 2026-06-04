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

**Module:** `artemis/health.py` (intent handlers), `artemis/scheduler.py`
(cron jobs), `app/routers/health.py` (read API consumed by gym-display
at `gym.rdm.is`).

**Database:** `health` schema (migration 013) — `health.plan`,
`health.session_log`, `health.daily_state`, `health.adjustments`,
`health.training_rules`, `health.phase_config`. Isolated; no `public`
or `acos` writes.

### Triggers — proactive (scheduled jobs)

All scheduled jobs guard with `self._is_quiet()` at the top. Quiet
hours 22:00-04:00 CT.

**`job_morning_prompt`** — daily morning survey + workout calibration:
- Tue 04:01 CT — strength_a workout day, AM only
- Wed 07:00 CT — logging-only (no workout calibration; PM workout)
- Thu 04:01 CT — workout day, PM allowed
- Fri 04:01 CT — workout day, AM only
- Sat 07:00 CT — logging-only (no workout calibration; PM workout)
- Sun 07:00 CT — workout day, PM allowed
- Mon 07:00 CT — workout day, PM allowed

Posts the morning survey questions (sleep hrs, energy 1-5, soreness
by region, weight, resting HR). User replies with answers via existing
`log_morning_state` intent. After `daily_state` row writes, post the
calibrated workout plan reply (~15 min after first prompt) including
session_type, equipment list, and location (downstairs gym vs outside).

**`job_evening_prompt`** — Wed/Sat at 16:30 CT — same shape as morning
prompt but for the PM workout.

**`job_health_nag`** — 21:00 CT, daily. Fires only if today's plan has
no `session_log` row AND today is not "no PM" (Tue/Fri). Suppressed on
rest_mobility and walk session_types.

**`job_health_inferred_summary`** — 21:50 CT, daily. Backstop. If the
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
- **Bike configuration override** (prior evening: "trainer set indoor"
  or "trainer set outdoor") → stored on tomorrow's `daily_state`
  pre-fill so morning prompt skips the weather-based suggestion

### Equipment & location mapping

Static map by `session_type` until autoregulator (separate ticket)
adds explicit `health.plan.location` and `health.plan.equipment`
columns:

```
strength_a / strength_b / strength_c
  -> location: downstairs gym
  -> equipment: PowerBlock dumbbells, flat bench, curl bar +
     plates (2x 10#, 2x 25#), TRX, resistance bands, exercise ball

cardio_intervals
  -> location: downstairs gym
  -> equipment: water rower OR bike on trainer

cardio_z2
  -> location: downstairs gym (Z2 pace, low impact)
  -> equipment: bike on trainer (default)

walk
  -> location: outside (or treadmill/indoor walk if weather forces)
  -> equipment: shoes

rest_mobility
  -> location: anywhere
  -> equipment: yoga mat, resistance bands (light)
```

Bike indoor/outdoor decision: weather at prompt time decides (rain
or sub-40°F = indoor) UNLESS the user posted a `trainer set
indoor`/`trainer set outdoor` override message the prior evening. The
trainer setup at 04:00 is fixed — Artemis never asks the user to
change tires mid-morning.

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
  Plan rows are seeded for 2026-05-06 → 2026-09-19; future plan
  modifications go through the autoregulator or direct DB update.
- **Wake word ("Hey Artemis" voice mode)** — Picovoice Porcupine
  planned, not built.
- **Explicit `location` and `equipment` columns** on `health.plan`
  — added when autoregulator lands and needs to swap (rain day ->
  indoor walk -> bike).

### Frontend consumer

`gym.rdm.is` (gym-display) reads `GET /api/health/today` with
`X-API-Key` header, displays today's plan on TV/iPad in the gym.
Hosted on Cloudflare Pages, gated by Cloudflare Access OTP/SSO to
`ryan@rdm.is`. Frontend repo: `RDM-IS/gym-display`.

### Testing

- `python3.11 tests/test_health_seed.py` — 13 tests, validates 137
  baseline plan rows, phase distribution 28/42/42/25, day-of-week
  mapping
- `python3.11 tests/test_health_intents.py` — 21 tests, intent
  detection + handlers + nag logic, all DB and Claude calls mocked
- API: 12 tests in `tests/api/test_health.py` (auth envelopes, CORS,
  no_plan envelope, JSONB serialization)
