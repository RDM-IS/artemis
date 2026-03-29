# Infrastructure

AWS deployment configs for the ACOS platform.

## EC2 Instance

- **Name:** acos-primary
- **Region:** us-east-1
- **OS:** Amazon Linux 2023
- **Python:** 3.11

## Services

### Mattermost — https://artemis.rdm.is

Team chat for ACOS notifications and @artemis bot.

```bash
# Start (fetches DB creds from Secrets Manager, launches Docker)
cd ~/mattermost && ./start.sh

# Logs
docker logs -f mattermost
```

- Runs in Docker via `docker-compose.yml`
- Postgres DB on RDS (shared instance `rdmis-dev`)
- Nginx reverse proxy with TLS (Let's Encrypt)
- WebSocket at `wss://artemis.rdm.is/api/v4/websocket`

### ACOS Orchestrator — systemd service

The AI Chief of Staff scheduler, Gmail/Calendar polling, and Mattermost bot.

```bash
# Install service (first time)
bash ~/artemis/infrastructure/install_service.sh

# Start / stop / restart
sudo systemctl start acos
sudo systemctl stop acos
sudo systemctl restart acos

# Logs (live tail)
journalctl -u acos -f

# Status
sudo systemctl status acos
```

- Runs as `ec2-user` from `/home/ec2-user/artemis`
- Auto-starts on boot, restarts on crash (10s delay)
- Health check at `http://localhost:5001/health`

### CRM Lambda — rdmis-crm-api

FastAPI app deployed as AWS Lambda behind function URL.

```bash
# Deploy
cd ~/artemis/api && bash deploy.sh
```

## Required Secrets (AWS Secrets Manager)

| Secret Name | Keys | Purpose |
|---|---|---|
| `rds!db-bfe5d90f-...` | username, password, host | RDS managed credentials |
| `rdmis/dev/mattermost` | url, int_url, token, channel_id, db_user, db_password | Mattermost bot + DB |
| `rdmis/dev/crm-api-key` | api_key | CRM API authentication |
| `rdmis/dev/zoho-webhook-secret` | webhook_secret | Zoho webhook verification |
| `rdmis/dev/anthropic-api-key` | api_key | Claude API |
| `rdmis/dev/gmail-oauth` | (credentials.json content) | Gmail OAuth client config |
| `rdmis/dev/gmail-token` | (token.json content) | Gmail OAuth token |
| `rdmis/dev/calendar-token` | (calendar_token.json content) | Calendar OAuth token |
| `rdmis/dev/twilio` | account_sid, auth_token, from_number | Twilio SMS |
| `rdmis/dev/booking-links` | 30min, 60min, 90min | Google Calendar booking URLs |

## Booking Links

Store Google Calendar appointment schedule URLs in Secrets Manager:

```json
{
  "30min": "PASTE_GOOGLE_CALENDAR_30MIN_URL",
  "60min": "PASTE_GOOGLE_CALENDAR_60MIN_URL",
  "90min": "PASTE_GOOGLE_CALENDAR_90MIN_URL"
}
```

Create via: Calendar > Create > Appointment schedule > set duration > copy booking link.
Secret name: `rdmis/dev/booking-links`

## Required Environment Variables

Only these five env vars are needed — everything else comes from Secrets Manager:

```
RDS_SECRET_ARN    — ARN of the RDS managed secret
RDS_HOST          — RDS endpoint hostname
RDS_DB            — Database name (default: crm)
AWS_REGION        — us-east-1
ENVIRONMENT       — dev / prod
```

## Directory Layout on EC2

```
/home/ec2-user/
  artemis/              ← this repo (aws-build branch)
    artemis/            ← orchestrator code
    api/                ← CRM Lambda code
    knowledge/          ← shared secrets + DB modules
    migrations/         ← SQL migrations
    tests/              ← test suite
    infrastructure/     ← this directory
  mattermost/           ← Mattermost Docker (data, config, logs)
```
