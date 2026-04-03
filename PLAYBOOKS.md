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
