# CONTEXT.generated.md — DO NOT HAND-EDIT (run scripts/context_snapshot.sh)
_Generated: 2026-11-10T10:27:07Z_

## Git
- Branch: main
- Head: 8d89a8b feat(health): SESSION-OPEN — weekly strength quota; plan rows are suggestions (#283)
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
- 054_health_export.sql
- 055_session_log_row_circuit.sql
- 056_session_log_row_dnf.sql
- 057_session_log_target_bpm.sql
- 058_strength_full.sql

## artemis/ modules
- __init__.py
- availability.py
- away.py
- billing.py
- block2.py
- briefs.py
- bw_circuit.py
- calendar.py
- calendar_cache.py
- cardio_baseline.py
- cardio_detect.py
- cognition_outcomes.py
- commitments.py
- config.py
- crm_client.py
- crm_query.py
- crm_write_guard.py
- crm_writer.py
- cycle.py
- demo_intake.py
- dietitian_report.py
- dossier.py
- drift.py
- email_index.py
- full_body.py
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
- hr_zones.py
- inbox.py
- inbox_cli.py
- intent.py
- interaction_logger.py
- interval_gate.py
- lambda_drift.py
- life_ops.py
- llm_json.py
- log_redaction.py
- main.py
- manual_gap.py
- mattermost.py
- meal_log.py
- meal_nudge.py
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
- prep.py
- prep_notion.py
- program_repeat.py
- prompts.py
- quiet_hours.py
- row_schedule.py
- scheduler.py
- scheduling.py
- schema_drift.py
- session_library.py
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
- week_ahead.py
- week_template.py

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
- session_hr_zones
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
- health_export
- prep_config
- prep_event
- prep_ingredient
- prep_recipe
- prep_recipe_line
- prep_session
- prep_stay
- prep_stay_day
- prep_store
- prep_store_item
- prep_sync
- recipe_step
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
- 2026-11-10: cardio_z2
- 2026-11-11: strength_full
- 2026-11-11: recovery_flow
