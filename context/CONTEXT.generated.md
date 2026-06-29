# CONTEXT.generated.md — DO NOT HAND-EDIT (run scripts/context_snapshot.sh)
_Generated: 2026-06-29T01:45:52Z_

## Git
- Branch: main
- Head: 9e4580b fix(gmail): default list_inbox_message_ids query to in:inbox
- Origin: https://github.com/RDM-IS/artemis.git

## Runtime (only meaningful when run ON EC2)
- Public IP: 3.227.229.186
- Host: ip-172-31-2-193.ec2.internal
- Python: Python 3.9.25

## Playbooks (PLAYBOOKS.md)
- PB-001: Demo Access Notification (v2)
- PB-002: Meeting Follow-up with Action Items
- PB-003: Survey / Feedback Request
- PB-004: Meeting Request / Calendar Invite
- PB-005: Commitment Deadline Reminder Chain
- PB-006: Availability Request
- PB-007: Billing Intake
- PB-008: CRM Write Guard
- PB-009: Personal Training

## Migrations (latest 5)
- 017_grocery_to_acos.sql
- 018_inbox_threads.sql
- 019_quiet_hours_state.sql
- 020_commitments.sql
- 021_email_index.sql

## artemis/ modules
- __init__.py
- availability.py
- billing.py
- briefs.py
- calendar.py
- calendar_cache.py
- commitments.py
- config.py
- crm_client.py
- crm_query.py
- crm_write_guard.py
- crm_writer.py
- demo_intake.py
- email_index.py
- gmail.py
- google_drive.py
- google_sheets.py
- guardrails.py
- health.py
- inbox.py
- inbox_cli.py
- intent.py
- interaction_logger.py
- life_ops.py
- main.py
- mattermost.py
- monitors.py
- parser.py
- prompts.py
- quiet_hours.py
- scheduler.py
- scheduling.py
- test_billing.py
- test_guardrails.py
- utils.py
- version.py
- voice.py
- weather.py

## Database (live RDS)

### schema `acos`
- action_items
- audit_log
- calendar_audit
- circuit_breaker_status
- commitments
- data_vault_satellites
- email_index
- entities
- expenses
- founder_loans
- funding_events
- grocery_list
- guardrail_violations
- inbox_threads
- mrr_snapshots
- osint_signals
- pending_crm_writes
- pipeline_events
- processed_billing
- quiet_state
- relationships
- schema_migrations
- system_state
- timezone_overrides
- v_gold_contacts
- velocity_ledger

### schema `health`
- adjustments
- daily_state
- meal
- nutrition_log
- nutrition_target
- phase_config
- plan
- session_log
- training_rules

### schema `public`
- commitments
- companies
- contacts
- deals
- engagements
- founder_loans
- interactions
- invoices
- monthly_financials
- organizations
- persons
- planned_expenses
- processed_billing
- relationships
- touch_events
- v_budget_vs_actual
- v_founder_loan_balance

### health.plan — next 3 days
- 2026-06-29: strength_a
- 2026-06-30: rest_mobility
- 2026-07-01: cardio_intervals
