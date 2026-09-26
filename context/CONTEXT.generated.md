# CONTEXT.generated.md — DO NOT HAND-EDIT (run scripts/context_snapshot.sh)
_Generated: 2026-09-26T20:05:06Z_

## Git
- Branch: main
- Head: 4c336a1 feat(cycle): the script owns the whole leave window — 9/29 was the last carve-out (#203)
- Origin: https://github.com/RDM-IS/artemis.git

## Runtime (only meaningful when run ON EC2)
- Public IP: 3.227.229.186
- Host: ip-172-31-2-193.ec2.internal
- Python: Python 3.11.14 (/usr/bin/python3.11)
- acos.service: active | ExecStart: ExecStart=/usr/bin/python3.11 -m artemis.main

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
- PB-011: Vault / Second Brain Ingest (v1)

## Migrations (latest 5)
- 039_drop_plan_status_debris.sql
- 040_nutrition_diet1.sql
- 041_watch_hourly.sql
- 042_plan_slot.sql
- 043_session_log_modality.sql

## artemis/ modules
- __init__.py
- availability.py
- billing.py
- briefs.py
- calendar.py
- calendar_cache.py
- cardio_baseline.py
- commitments.py
- config.py
- crm_client.py
- crm_query.py
- crm_write_guard.py
- crm_writer.py
- cycle.py
- demo_intake.py
- dossier.py
- drift.py
- email_index.py
- gmail.py
- google_drive.py
- google_sheets.py
- guardrails.py
- health.py
- health_checkin.py
- health_eval.py
- health_guard.py
- health_office.py
- health_patterns.py
- health_regions.py
- help_registry.py
- inbox.py
- inbox_cli.py
- intent.py
- interaction_logger.py
- lambda_drift.py
- life_ops.py
- log_redaction.py
- main.py
- mattermost.py
- monitors.py
- morning_brief.py
- notion_meal_plan.py
- nutrition.py
- ops_access.py
- ops_api.py
- opsdiag.py
- parser.py
- playbook_rules.py
- posting.py
- prompts.py
- quiet_hours.py
- scheduler.py
- scheduling.py
- schema_drift.py
- test_billing.py
- test_dossier.py
- test_guardrails.py
- test_mattermost.py
- test_polish1.py
- test_stability.py
- test_vault.py
- utils.py
- vault.py
- version.py
- voice.py
- wake.py
- watch_prefill.py
- weather.py

## Database (live RDS)

### schema `acos`
- action_items
- audit_log
- calendar_audit
- circuit_breaker_status
- commitments
- cycle_day_overrides
- data_vault_satellites
- dossier
- dossier_entry
- dossier_idea
- dossier_loop
- dossier_meeting
- dossier_meeting_attendee
- email_index
- engagement
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
- velocity_ledger

### schema `health`
- adjustments
- daily_state
- meal
- nutrition_log
- nutrition_target
- pain_pattern
- phase_config
- plan
- reflection
- session_log
- training_rules
- watch_heart_rate
- watch_hourly
- watch_sample
- watch_workout

### schema `nutrition`
- day
- entry
- food
- target

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

### schema `vault`
- extraction_proposal
- ingest_state
- note_links
- note_metadata
- notes

### health.plan — next 3 days
- 2026-09-26: recovery_flow
- 2026-09-26: rest
- 2026-09-27: recovery_flow
