# RDMIS CRM API

FastAPI + PostgreSQL (RDS) headless CRM. Deployed to AWS Lambda via Mangum.

## Structure
```
rdmis-crm/
├── app/
│   ├── main.py          # FastAPI app + Lambda handler
│   ├── database.py      # SQLAlchemy engine + session
│   ├── models.py        # All 7 entity models
│   └── routers/
│       ├── organizations.py
│       ├── contacts.py
│       ├── deals.py
│       ├── interactions.py
│       ├── commitments.py
│       ├── invoices.py
│       ├── founder_loans.py
│       └── webhooks.py  # Zoho — no API key, uses signature verification
├── migrations/
│   └── versions/
│       └── 001_initial_schema.py
├── requirements.txt
└── README.md
```

## Environment Variables (Lambda)
| Variable | Value |
|---|---|
| `RDS_SECRET_ARN` | ARN of the RDS-managed secret |
| `RDS_HOST` | `rdmis-dev.xxxx.us-east-1.rds.amazonaws.com` |
| `RDS_DB` | `crm` |
| `CRM_API_KEY_SECRET` | `rdmis/dev/crm-api-key` |
| `ZOHO_SECRET_ARN` | `rdmis/dev/zoho-webhook-secret` |

## Deploy (Lambda)
```bash
pip install -r requirements.txt -t package/
cp -r app package/
cd package && zip -r ../function.zip .
aws lambda update-function-code --function-name rdmis-crm-api --zip-file fileb://../function.zip
```

## Run Migrations
```bash
# Requires VPC access or bastion/RDS proxy
alembic upgrade head
```

## Auth
All routes require `X-API-Key` header except `POST /webhooks/zoho`.
