# Artemis Playbooks

## PB-001: Demo Access Notification

**Trigger:** Email from Artemis demo system containing "Demo access confirmed"

**Actions:**
1. Extract: visitor name, email, company (if provided)
2. Create contact in CRM (upsert — don't duplicate if email exists)
   Fields: name, email, company, source="artemis-demo",
   first_seen=today, status="lead"
3. Create commitment: "Follow up with [name] re: demo access"
   due_date = next business day, effort = 1, client = extracted company or "Prospect"
4. Post to #artemis-ops: ":dart: New demo lead: [name] ([company]) —
   follow-up scheduled for [date]"
5. Mark email as NEEDS_ACTION in inbox zero with due_date = next business day

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
