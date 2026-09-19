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
`health.training_rules`, `health.phase_config`; `health.pain_pattern` and
`health.reflection` (migration 032). Isolated; no `public`
or `acos` writes.

### Triggers — proactive (scheduled jobs)

Health-tier jobs post in the **wake** and **open** phases; in **quiet** they
are held durably and flushed at the next wake (WAKE-1 — see the day-phase
section in `docs/ARTEMIS_STATE.md`). All times are local wall-clock in the
**active** timezone, which follows `set timezone to <place>`.

### Check-in-driven morning (FRIDAY-1)

The morning is **event-driven**; the fixed 04:45 calibration post and its
"Recovery override" line are gone.

| When | What |
|---|---|
| 04:30 (Sat/Sun 07:30) | **Wake post** (`job_wake` → `artemis/wake.py`): today's session rendered **plan-exact** from `health.plan` (every exercise, sets×reps, RPE cap, load), warmup/cooldown, the check-in prompt, held health notices, pre-departure. Sets `checkin_open:<date>`. |
| on the check-in reply | Parsed **deterministically** (`artemis/health_checkin.py`, no LLM) and stored in `health.daily_state`. **No sets logged today** → the rules run and today's plan row is rewritten; the reply is the plan-exact diff. **Sets already logged** → stored only, reply `Logged.` |
| 05:15 (Sat/Sun 08:15) | `job_checkin_nudge` (WAKE + `CHECKIN_NUDGE_OFFSET_MIN`): **training days only**, if no check-in and no sets — one post, "No check-in yet — run Session X as written." Nothing on rest/walk days; never repeats. |

Prompt text:

> Reply with: sleep hrs, energy 0–5, soreness by area 0–5 (0 = none), weight, RHR.
> Example: `slept 7 energy 4 sore 0 weight 283`

**Routing.** The `morning_flow` handler sits **ahead of `nutrition`** and the
LLM. It claims check-in-shaped text, ack words (`nope`, `all good`), done words
(`done`, `workout completed`, `logged in app`) and `original` — nothing else
(`ok`, `thanks`, `done <thread-id>` are not claimed). A claimed message never
reaches an LLM or Gmail. Before 06:30 — Sat/Sun 08:30 — (phase ≠ open) the general LLM fallback
gets **no** Gmail, calendar, inbox or notes context.

**Scale — every rating is 0–5** (0 = none, 5 = can't use it): energy, soreness
and pain. Sleep stays in hours.

- `x/10` is halved and rounded up (8/10 → 4, 5/10 → 3); `x/5` is taken as is.
- A bare number above 5 (or an `x/5` above 5) → reply **"Ratings are 0–5."** —
  nothing stored, nothing changed.
- `sore 0` is stored as `{"overall": 0}`, never NULL.
- Several regions per message; a score carries across "and" within a phrase,
  never into the next comma-separated clause. A region named without a number
  is stored unscored (`null`) and changes nothing.
- Regions: shoulder, chest, back, low back, arms, biceps, triceps, legs, quads,
  hamstrings, calves, core, knee, hip, neck (aliases in
  `artemis/health_regions.py`). An unknown region is stored and named in the
  reply ("didn't recognize region X"), with no change.
- Pain words (pain, sharp, injury, tweaked, strain…) make that region **pain**
  rather than soreness — only the region they're attached to. Stored as
  `soreness.pain = {region: score}`.

**Rules — the pain ladder (PAIN-1).** Highest priority first. A **day-level**
rule (1–4) ends the ladder; the region rules (5–8) apply to whatever exercises
are left, then recovery (9). Nothing adds volume, load or RPE; high energy or
long sleep never adds work — progression belongs to the program.

| # | When | Change |
|---|---|---|
| 1 | **pain 4–5** in any region | **Day off**: the row becomes `rest_mobility`, blocks `{type: mobility, display_name: "Day off", duration_min: 0}`, no RPE, 0 min. No nudge. |
| 2 | **rising pain** starting at ≥ 1 (below) | **Day off**, same as 1. The reply names the trend. |
| 3 | **pain 3** in the **primary** region of **≥ 50%** of today's exercises (a Z2/walk block counts as one, by its primary region) | **Mobility / Yoga** day: `rest_mobility`, 30 min, Stretch Trainer + mat, `mobility_focus` = the region(s). Secondary use doesn't count toward the 50%: shoulder pain 3 on Session B (primary on 2 of 7) is rule 5, not a mobility day. |
| 4 | 2+ **soreness** regions at 4–5 | Day swap → Recovery Z2 (20–30 min, recumbent bike) + 10 min mobility. |
| 5 | **pain 3** (under 50%) | Exercises with the region as **primary** are removed and replaced by a **mobility block** for it: `mobility_focus`, `mobility_min` (10 min for one region, 15 for several), `mobility_notes` from the region→mobility map in `health_regions.py`; Stretch Trainer + mat added to equipment. Exercises with it as **secondary only** are **swapped** for the first pool exercise (list in rule 6) that avoids every sore and painful region and isn't already in the session; if none fits, that exercise goes to the mobility block instead. A finisher using the region is dropped. |
| 6 | **soreness 4–5** | Remove every exercise with that region as primary **or** secondary (`health_regions.py`); refill to the same count from the pool (leg press, seated leg curl, leg extension, calf press, captain's chair knee raise, seated back extension, Pallof press), avoiding every sore **and painful** region and anything rule 5 removed or added. When the pool runs out the reply says how many slots stayed empty. A finisher using the region is dropped. |
| 7 | **pain 2** | Exercises with that **primary** region get a lighter target: `target_load_lbs` = 80% of the **last logged load** (top set of the exercise's most recent earlier session; skipped and inferred rows ignored), rounded **down** to a load the office can make (DBs 5–45 by 5; machines/cables by 10; Smith/bar = bar + 2 × a subset of 45/35/25/10/5 per side), never above the last load; `load_from` records it. No history → target stays null and the exercise notes say "go lighter than last time". Bodyweight work is left alone. |
| 8 | **soreness 2–3** | Exercises with that **primary** region: −1 set (min 1), RPE cap −1. |
| 9 | sleep < 6 or energy ≤ 2 | RPE cap −1 on everything (stacks with rule 8, floor 1), sets capped at 2, Z2 duration −25%. |
| — | **pain 0–1**, soreness 0–1, or nothing qualifying | No change. Pain is stored and noted: "Pain shoulder 1/5 — noted." then "Check-in logged — run Session X as written." Pain 2 with no primary exercise to lighten is noted the same way. |

Overall priority: pain day off > rising day off > pain whole-day mobility >
soreness day swap > pain-3 region mobility > soreness replace > pain-2 lighter >
soreness lighten > recovery. FRIDAY-1's "pain 4–5 replaces" and "pain 1–3:
load −20% + mobility" rules are gone.

**Rising pain.** A chain for a region needs morning check-ins on **D−2, D−1
and today** (three consecutive local days) that **each give that region a pain
number**, strictly increasing. A missing day, a check-in that doesn't mention
the region, or a region named without a number breaks the chain. Only morning
check-ins (`health.daily_state`) count — in-session notes never do.

- Chain starts at **≥ 1** (`1→2→3`) → **day off** (rule 2).
- Chain starts at **0** (`0→1→2`) → **no day off**; the normal pain rules
  apply to today's number, and the reply adds one line `rising: shoulder 0→1→2`.
- `1→2`, `2→2→3`, `3→2→1`, `(unmentioned)→1→2` → nothing.

> Pain shoulder 1→2→3 (rising) → day off. Reply `original` to undo.

> Pain shoulder 2/5 → incline DB press: go lighter than last time; rear delt fly: go lighter than last time.
> rising: shoulder 0→1→2
> Reply `original` to undo.

Rest days are never adjusted. A walk day only yields to the day-off rules
(1–2). A second check-in the same day recomputes from `blocks.original` (never
stacks); a second check-in that no longer warrants a change restores the plan
as written.

**Storage and undo.** Only when `CHECKIN_ADJUST` is on (set explicitly to `1`
in the box `.env`): the adjusted blocks are written to today's `health.plan`
row with `blocks.original` (the untouched blocks and row fields) and
`blocks.adjustment` = `{reason, rules_fired, checkin_id, at, removed, added,
eased, summary}` — no migration. `/api/health/today` serves the adjusted row,
so gym-display shows it. Replying **`original`** restores `blocks.original` and
clears the adjustment. Every write and restore is audited in `acos.audit_log`.
With the flag off the check-in is stored and the would-be change is audited as
`checkin_adjust_suppressed`.

**Replies are the plan diff only — never advice or health text, pain included:**

> Shoulder 4/5 → removed DB goblet squat, seated cable row, incline DB press, rear delt fly. Added leg press, seated leg curl, calf press, captain's chair knee raise.
> Reply `original` to undo.

> Pain shoulder 4/5 → day off. Reply `original` to undo.

> Pain shoulder 3/5 → removed incline DB press and rear delt fly. Added 10 min shoulder mobility (Stretch Trainer + mat). Swapped DB goblet squat → leg press, seated cable row → seated leg curl.
> Reply `original` to undo.

> Pain shoulder 2/5 → incline DB press 20 lb (last 25); rear delt fly: go lighter than last time.
> Reply `original` to undo.

### In-session pain (gym-display chip)

The logger's quick flags include **Pain**: it opens a region picker (the
check-in vocabulary above) and a 0–5 chip row — no keyboard. Each pick adds
`pain=<region>:<n>` to that set's `session_log.notes`; several are allowed,
`;`-separated (`setting=7; pain=shoulder:2; felt off`). The Status page's pain
detector (`/api/health/sessions` → `outliers.pain_notes`, keyword "pain") still
flags them. Artemis reads `pain=` notes **only** for pattern surfacing — never
for today's rules.

### Pain patterns and the Sunday review (PAIN-1)

`artemis/health_patterns.py`, tables `health.pain_pattern` and
`health.reflection` (migration 032). It counts and reports; it never states a
cause and never changes a plan.

- **Exposure:** exercise E logged on day D (a non-skipped set, or a set with a
  pain note).
- **Hit:** an in-session `pain=` note on E with any region ≥ 2 (counts for that
  region, with or without a check-in), **or** the D+1 morning check-in has pain
  ≥ 2 in a region E uses (primary or secondary, families included). At most one
  hit per exposure per region.
- **Candidate:** E × region with **≥ 3 hits and ≥ 60%** of exposures over the
  last **8 weeks**, reported as hits/exposures. Other exercises logged on the
  hit days that use the same region are named ("also that day") — co-occurrence
  only.

| When | What |
|---|---|
| 21:55 daily | `job_pain_pattern_recompute` — silent; refreshes `health.pain_pattern`. New rows are written only for candidates; existing rows are kept current (`qualifies` can go false). |
| Sun 08:30 | `job_health_review` (health tier, OPEN phase only) — one post per open candidate that is **new or changed** since it was last posted (`surfaced_hits/_exposures`); nothing when nothing changed. |
| Sun 08:35 | `job_weekly_eval` (health tier, OPEN phase only) — EVAL-1: the week so far (Wed–Sat, labelled partial): sessions done vs due, missed, average session RPE vs cap, load change vs last week, adjustments. Data only, no recommendations. |
| on the check-in | The check-in whose pain completes a candidate (it qualifies now, didn't before, and yesterday is one of its hit days) gets **one extra line**, once ever (`mentioned_at`). |

> Pattern: shoulder pain ≥2 after **incline DB press** — 3 of 4 sessions (also that day: rear delt fly). What do you notice?

**Replies in a pattern thread** (`pattern_thread` handler, after `morning_flow`):

- anything → stored **verbatim** in `health.reflection` (linked to the pattern
  when the thread is about exactly one; redelivered posts are no-ops), reply
  "Noted.", **no plan change**. If it asks for a workout change, it is stored
  and then handed to the existing swap flow (propose → confirm).
- `dismiss` → hidden until the pattern gains **2 more hits**.
- `resolved` → closed; it reopens only when new data (a hit after the
  resolution) re-qualifies it.

Sunday posts are threads of their own (`post_ids`). For the check-in mention,
the next unclaimed reply in that morning thread **the same day** is the
reflection (one-shot, `pain_pattern_thread:<root>` in `acos.system_state`).

**Plan-claim guard.** Any LLM text about workouts is checked by
`artemis/health_guard.py`: an exercise not in the plan window (±7 days,
including `blocks.original`), a load not in any plan/log/body-weight row, or a
"last session(s)" figure not in the logs replaces the draft with the
deterministic plan detail and logs a guardrail violation. Equipment words
("mat", "DBs") and greetings are not exercises; business replies aren't
checked.

Rest and walk days get the same check-in prompt — no "Today's workout is
later" line.

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

- **Morning state** ("slept 6 energy 3 legs sore 3", "sore shoulder 4",
  "RHR 58") → the deterministic `morning_flow` check-in (see above) → upserts
  `health.daily_state` on `state_date` (COALESCE preserves earlier-filled fields)
  and may adjust today's plan
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
extension/leg curl, leg press/calf extension, abdominal/back extension (seated —
it covers back extensions; programmed as "Seated back extension", equipment
class `machine`); S3.23 functional trainer (rope + handles); Icarian Smith
machine; hex DBs, Olympic bar + plates, 2 flat benches, 1 adjustable bench;
captain's chair/dip tower; treadmills, ellipticals, upright bike, recumbent
bike, stepmill, Stretch Trainer; stability balls, mats. There is **no** 45° back
extension / roman chair. The rower and outdoor bike are retired from the plan.
This list is Ryan's description, not yet confirmed in person — see OFFICE-WALK
in `docs/ARTEMIS_STATE.md`.

Fallback map:

```
strength_a       -> office gym: leg press, DBs + flat bench, pulldown, leg curl,
                    functional trainer (rope), captain's chair   (first lift: Leg press)
strength_b       -> office gym: DBs, seated row, adjustable bench, leg extension,
                    rear delt fly, functional trainer, back ext machine (first: DB goblet squat)
strength_c       -> office gym: DBs, pec fly, functional trainer, adjustable bench,
                    calf press, ab machine                        (first: DB Romanian deadlift)
cardio_z2        -> office gym: treadmill / elliptical / recumbent / upright bike
cardio_intervals -> office gym: stepmill / upright bike
walk             -> outside: walking shoes (rain or <40°F -> indoor walk)
rest_mobility    -> office gym: mat / Stretch Trainer
```

Weather is consulted for `walk` only. There is no bike indoor/outdoor decision
and no `trainer set` override any more.

**Office program — phase 1, anchored Wed 2026-09-16, seeded 9/16 → 11/03** —
`scripts/reseed_health_plan_v2.py --office` (dry-run default, `--commit` to
write; also deletes orphan rows past the program end), validated by
`scripts/validate_health_plan.py`. All sessions are at the **office gym**
(`blocks.location`); the Sunday walk is outside.

Week windows run **Wed–Tue** from 2026-09-16 (week 1 = 9/16–9/22 … week 7 =
10/28–11/03). There are no ramp-up days: 9/16 is week 1, day 1.

| Wed | Thu | Fri | Sat | Sun | Mon | Tue |
|---|---|---|---|---|---|---|
| Strength A | Recovery Flow (office) | Strength B | Recovery Flow (home) | walk | Strength C | Z2 |

Thu and Sat are Recovery Flow days (YOGA-1, `session_type = recovery_flow`,
replacing `rest_mobility`), so no two strength days are adjacent and every
strength day falls on a weekday, when the office gym is open.

### Recovery Flow (YOGA-1)

A guided, hands-free mobility flow. `blocks.type = recovery_flow`, `target_rpe`
2, built by `health_office._recovery_flow`.

| # | Pose | Side | Hold (round 1) | Mirror group |
|---|---|---|---|---|
| 1 | Child's pose | — | 30 s | |
| 2 | Cobra | — | 30 s | |
| 3 | Downward dog (easier: dolphin) | — | 60 s | |
| 4 | Standing forward bend | — | 30 s | |
| 5 | High lunge (easier: knee down) | R | 30 s | lunge-unit |
| 6 | Crescent lunge (easier: knee down) | R | 30 s | lunge-unit |
| 7 | Extended puppy | — | 30 s | (transition) |
| 8 | High lunge | L | 30 s | lunge-unit |
| 9 | Crescent lunge | L | 30 s | lunge-unit |
| 10 | Bridge | — | 30 s | |
| 11a/b | Supine twist | R / L | 30 / 30 s | twist-supine |
| 12a/b | Wind release | R / L | 30 / 30 s | wind |
| 13a/b | Seated side bend | lean L / lean R | 30 / 30 s | side-bend |
| 14a/b | Seated twist | L / R | 30 / 30 s | twist-seated |
| 15 | Seated mountain | — | 30 s | |
| 16 | Easy pose | — | 30 s | |

- **Rounds:** 2 full rounds of every step; round 2 doubles the holds of steps
  10–16. Then 3 min easy-pose breathing.
- **Thursday (office)** starts with an 8 min Stretch Trainer block ("Follow the
  8 placard stretches"); **Saturday (home)** doesn't.
- **Totals:** office 37:30 (`est_duration_min` 38), home 29:30 (30). Round 1
  10:30, round 2 16:00.
- **Step fields:** `step`, `name`, `side` (null/R/L), `side_label` ("Right leg
  forward", "Lean left"…), `duration_sec`, `mirror_group`, `cue` (our own
  wording), `easier`. No external links.
- **Side validator** (`health_office.validate_flow`, run on every seed): each
  mirror group needs R and L entries with equal total hold in every round. A
  lunge unit compares as a group (high + crescent), not pose by pose.
- **Check-in rules:** only the day-off rules apply — pain 4–5 or rising pain
  turns the flow into a day off (`original` restores it). Pain 2–3 changes
  nothing (it's already mobility); soreness and sleep/energy rules don't apply.
  Reply: "Check-in logged — Recovery Flow as planned."
- **Nudge:** the 05:15 check-in nudge applies. No 16:30 debrief nag and no
  21:50 inferred summary on flow days — gym-display logs the flow itself, and a
  missed flow is never a missed session.
- **Wake post:** plan-exact — the Stretch Trainer block, every round-1 step with
  side and hold, the round-2 doubling, the close and the total. The check-in
  prompt has no "workout is later" wording.
- **gym-display:** one Start tap, then hands-free — every step auto-advances
  with a 5 s "next" preview and chime, a large "Switch sides" screen and
  distinct tone between R and L, round progress (1/2, 2/2), and on-device voice
  cues (`speechSynthesis`, mute toggle). Tap to pause/resume, swipe to skip.
  Completion is logged automatically as a `session_summary`
  (`notes = "recovery_flow: complete …"`); closing or backgrounding mid-flow
  logs `recovery_flow: partial <n> of <m> min`, which counts as done.
- **Reseed:** `reseed_health_plan_v2.py --office --flow-days --from <date>`
  rewrites only the flow days (needs migration 033; refuses logged dates).

| Weeks | Sets | RPE | Z2 | Notes |
|---|---|---|---|---|
| 1–2 | 2 | 6 | 20 min | `target_load_lbs` null (finding weights); week 1 machine exercises note "log seat + pin setting" |
| 3–4 | 3 | 7 | 30 min | |
| 5–6 | 3 | 7.5 | 35–40 min | Mon Strength C adds the stepmill/upright-bike finisher, 6 × (30s hard / 90s easy) |
| 7 | 2 | 6 | 30 min | deload |

Warmup 5 min elliptical, cooldown 5 min Stretch Trainer.

> ⚠️ `health.phase_config` caps phase 1 at `max_session_rpe = 7.0`, below the
> weeks 5–6 target of 7.5. Nothing enforces the cap today; resolve before 10/14.

**Progression is manual.** The weekly evaluation (EVAL-1, Sunday post + weekly report) reports; Ryan decides any repeat, restart or change. The ramp engine was deleted in RAMP-RETIRE (2026-09-19).

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

### Recovery override (retired)

The old "Recovery override" line in the timed calibrated post never changed the
plan and is gone with that post. Recovery is now rule 9 of the check-in
adjustment above, and it does change `health.plan` (and therefore gym-display).

### Out of scope (deferred to future tickets)

- **Autoregulator** (separate ticket) — adjusts `health.plan` rows
  based on rolling RPE / recovery signal. Will write to
  `health.adjustments` audit table.
- **Workout creation/editing from chat** — only logging is supported.
  The office program is seeded 2026-09-16 → 2026-11-03; future plan
  modifications go through the autoregulator or a reviewed reseed.
- **Wake word ("Hey Artemis" voice mode)** — Picovoice Porcupine
  planned, not built.
- **Explicit `location` and `equipment` columns** on `health.plan` — not
  needed for now: both live in `blocks` (HEALTH-2).

### Frontend consumer

`gym.rdm.is` (gym-display) is an iPad Pro 11" Safari app (the TV layout is
retired). The browser calls the **same-origin** `/api/*` Pages Function, never
the Lambda directly; the Function requires a Cloudflare Access JWT, answers
only on `gym.rdm.is` and `*.gym-display.pages.dev`, and attaches `X-API-Key`
server-side (see gym-display `docs/API_AUTH.md`). A lapsed Access session shows
a "Session expired — tap to sign in" banner; unsynced sets stay queued.
**Tomorrow / Week (GD-WEEK).** `GET /api/health/plan?from=&to=` (inclusive,
≤ 14 days, dates in the active timezone; defaults today..today+6) returns each
day's stored blocks (adjustment included), `location`, `adjusted`, the
per-exercise sets actually logged (past/today), and a derived `status`:
`done` (real summary — flow "complete" — all planned sets, plan.status
completed, or a past rest day), `partial` (some real logs; flow "partial"),
`missed` (past training day, no real logs — inferred rows don't count, a
missed flow is missed), `today`, `upcoming`. gym-display shows it read-only
from the Setup screen's Tomorrow and Week buttons.
**Status page (STATUS-1).** `GET /api/health/overview` returns the whole page
in one read, in the active timezone: `program` (name, phase, week of
weeks_total, anchor, deload week, sessions done/planned this Wed–Tue week),
`week_days` (the `/plan` status derivation), `today` (session, progress in
sets / minutes / rest, this morning's check-in, the adjustment), a
`strength_progress` row per program exercise (last / previous / best top set
by load × reps, trend ↑→↓ at ±2%, latest `setting=`), `checkins_14d`, open
`patterns` (empty until migration 032), plain-language `flags` (missed,
partial, RPE over cap, pain chips and pain-keyword notes), and `weight_30d`
with a first/latest/change summary. Everything except body weight is scoped
to `plan_date >= program.anchor`. The program comes from
`acos.system_state.health_program` (written by every office reseed), else it
is derived from the plan rows. `/status` stays for the Today→Status redirect.
Hosted on Cloudflare Pages, gated by Cloudflare Access OTP/SSO to
`ryan@rdm.is`. Frontend repo: `RDM-IS/gym-display`.

### Testing

- `python3.11 tests/test_health_seed.py` — 13 tests, validates 137
  baseline plan rows, phase distribution 28/42/42/25, day-of-week
  mapping
- `python3.11 tests/test_health_intents.py` — intent detection +
  handlers + nag logic, all DB and Claude calls mocked
- `python3.11 tests/test_health_office.py` — office schedule and weekly progression, location
  from blocks, retired bike/trainer/ramp, regression: no row from 2026-09-21
  onward references a rower or bike on trainer
- `python3.11 tests/test_checkin_adjust.py` — FRIDAY-1 parser, rules, flows,
  routing, claim guard (FakeDB, no RDS)
- `python3.11 tests/test_pain_ladder.py` — PAIN-1 ladder, rising pain, `pain=`
  notes, pattern tally/recompute/lifecycle, reflections, jobs
- API: `tests/api/test_health.py` (auth envelopes, CORS, no_plan envelope,
  JSONB serialization, pain-note outliers)

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
`python3.11 tests/test_opsdiag.py` (runbook classification + version truth).
