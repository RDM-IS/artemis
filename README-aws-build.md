# Artemis — AWS Build (v3.1)

This branch is the v3.1 architectural rebuild targeting AWS EC2.
The `main` branch contains the original tower/WSL2 build and remains
production until this branch is promoted.

## Branch strategy
- `main` — tower build, current prod, do not modify
- `aws-build` — this branch, AWS rebuild in progress

## Repo structure
```
artemis/
├── api/            CRM Lambda (FastAPI + Mangum)
│   ├── app/        Routers, models, database
│   ├── deploy.sh   Lambda packaging and deployment
│   └── requirements.txt
├── artemis/        Core bot (Mattermost, Gmail, Calendar, briefs)
├── knowledge/      ACOS knowledge layer
│   ├── db.py       Connection pool + entity/relationship ops
│   └── secrets.py  Centralized Secrets Manager access (single source of truth)
├── migrations/     SQL migrations (shared across platform)
└── tests/          Schema validation tests (shared across platform)
```

## Secrets policy
**All credentials come from AWS Secrets Manager.**
The only env vars in the system are:

| Variable | Purpose |
|----------|---------|
| `RDS_SECRET_ARN` | ARN of the RDS secret (not the password itself) |
| `RDS_HOST` | RDS endpoint hostname |
| `RDS_DB` | Database name (default: `crm`) |
| `AWS_REGION` | AWS region (default: `us-east-1`) |
| `ENVIRONMENT` | `dev` or `prod` |

Everything else (Anthropic key, Mattermost token, Twilio creds, Gmail
OAuth, CRM API key, Zoho webhook secret) is fetched at runtime from
Secrets Manager via `knowledge/secrets.py`.

### Secrets in AWS Secrets Manager

| Secret Name | Keys | Status |
|-------------|------|--------|
| `rds!db-bfe5d90f-...` | `username`, `password` | exists |
| `rdmis/dev/crm-api-key` | `api_key` | exists |
| `rdmis/dev/zoho-webhook-secret` | `webhook_secret` | exists |
| `rdmis/dev/anthropic-api-key` | `api_key` | needs creation |
| `rdmis/dev/mattermost` | `url`, `token`, `channel_id` | needs creation |
| `rdmis/dev/twilio` | `account_sid`, `auth_token`, `from_number` | needs creation |
| `rdmis/dev/gmail-oauth` | OAuth credentials JSON | needs creation |

## Database schema
- **RDS instance:** `rdmis-dev`
- **`public` schema:** CRM tables (contacts, deals, interactions, commitments, organizations, founder_loans)
- **`acos` schema:** ACOS tables (entities, relationships, osint_signals, data_vault_satellites, audit_log, velocity_ledger, circuit_breaker_status, guardrail_violations, schema_migrations)

No `crm.` schema. `public.` and `acos.` only.

## Environment setup
Copy `.env.example.aws` to `.env` and populate. Never commit `.env`.

## Status
Phase 1 — Schema + secrets centralization complete
