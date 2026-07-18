# CONTEXT.generated.md — DO NOT HAND-EDIT (run scripts/context_snapshot.sh)
_Generated: 2026-07-18T21:23:48Z_

## Git
- Branch: main
- Head: a30845f feat(dossier): PB-010d org profiles (authored sections + org notes) (#76)
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
- PB-010: Meeting Intelligence / Colleague Dossiers

## Migrations (latest 5)
- 023_playbook_rules.sql
- 024_dossier.sql
- 025_dossier_approve.sql
- 026_org_assignment.sql
- 027_org_profile.sql

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
- dossier.py
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
- playbook_rules.py
- prompts.py
- quiet_hours.py
- scheduler.py
- scheduling.py
- test_billing.py
- test_dossier.py
- test_guardrails.py
- test_mattermost.py
- test_stability.py
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
- dossier
- dossier_entry
- dossier_idea
- dossier_loop
- dossier_meeting
- dossier_meeting_attendee
- email_index
- entities
- expenses
- founder_loans
- funding_events
- grocery_list
- guardrail_violations
- inbox_threads
- mrr_snapshots
- org_assignment
- org_note
- org_profile
- osint_signals
- pending_crm_writes
- pipeline_events
- playbook_rules
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
- 2026-07-18: cardio_z2
- 2026-07-19: cardio_z2
- 2026-07-20: strength_a
