# Artemis — AI Chief of Staff

Artemis is a Python backend that reads your Gmail and Google Calendar, then posts intelligent briefs and alerts to a self-hosted Mattermost instance. It responds to @mentions with context-aware answers.

## Prerequisites

- Docker & Docker Compose
- Python 3.11+
- Google Cloud Console project with Gmail API and Calendar API enabled
- An Anthropic API key

## Setup

### 1. Start Mattermost

```bash
docker compose up -d
```

Mattermost will be available at `http://localhost:8065`.

### 2. Configure Mattermost

1. Open `http://localhost:8065` and create your admin account
2. Create a team (note the team ID from the URL or System Console)
3. Create three public channels:
   - `artemis-ops`
   - `artemis-briefs`
   - `artemis-commitments`
4. Create a bot account:
   - Go to **System Console > Integrations > Bot Accounts** and enable bot accounts
   - Go to **Integrations > Bot Accounts > Add Bot Account**
   - Username: `artemis`
   - Role: System Admin (so it can post to all channels)
   - Copy the **Access Token** — this is your `MATTERMOST_BOT_TOKEN`
5. Get your team ID:
   - Go to **System Console > Teams** and click your team
   - The ID is in the URL or shown on the page

### 3. Google OAuth Setup

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a new project (or use existing)
3. Enable **Gmail API** and **Google Calendar API**
4. Go to **Credentials > Create Credentials > OAuth client ID**
5. Application type: **Desktop app**
6. Download the JSON and save it as `credentials.json` in the project root
7. On first run, Artemis will open a browser for OAuth consent — approve both Gmail (read-only) and Calendar (read-only) scopes

### 4. Install Python Dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 5. Configure Environment

```bash
cp .env.example .env
```

Edit `.env` with your values:
- `ANTHROPIC_API_KEY` — from [console.anthropic.com](https://console.anthropic.com/)
- `MATTERMOST_BOT_TOKEN` — from step 2
- `MATTERMOST_TEAM_ID` — from step 2
- `PRIORITY_CONTACTS` — comma-separated emails or domains (e.g., `tti.com,jane@acme.com`)
- `MONITORED_DOMAINS` — domains to check SSL certs for
- `DOMAIN_EXPIRY_DATES` — format: `domain:YYYY-MM-DD,domain:YYYY-MM-DD`

### 6. Seed Commitments

```bash
python -m artemis.commitments add "Pilot scope doc" --due 2026-03-20 --effort 3 --client TTI
python -m artemis.commitments add "Q2 proposal" --due 2026-03-25 --effort 2 --client Acme
python -m artemis.commitments list
```

### 7. Start Artemis

```bash
python -m artemis.main
```

Artemis will:
- Connect to Mattermost via websocket (listening for @mentions)
- Start all scheduled jobs (inbox triage, briefs, monitoring)
- Listen on port 5000 for uptime webhooks

## Remote Access with Tailscale

If running on a home server or VPS, install [Tailscale](https://tailscale.com/) for secure remote access to Mattermost without exposing ports publicly:

```bash
# On the server running Mattermost
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
```

Then access Mattermost at `http://<tailscale-ip>:8065` from any device on your tailnet.

## Uptime Robot Webhook

1. Sign up at [uptimerobot.com](https://uptimerobot.com/)
2. Add monitors for your sites
3. Go to **My Settings > Alert Contacts > Add Alert Contact**
   - Type: Webhook
   - URL: `http://<your-server>:5000/webhook/uptime`
   - POST value: `{"monitorFriendlyName":"*monitorFriendlyName*","alertType":"*alertType*","monitorURL":"*monitorURL*"}`
4. Attach the alert contact to your monitors

When a site goes down, Artemis posts to `#artemis-ops`.

## Commitments CLI

```bash
# Add a commitment
python -m artemis.commitments add "Task title" --due 2026-04-01 --effort 5 --client "Client Name"

# List active commitments
python -m artemis.commitments list

# Mark as done
python -m artemis.commitments done 1

# Mark as blocked
python -m artemis.commitments block 2
```

## Scheduled Jobs

| Job | Schedule | Channel |
|-----|----------|---------|
| Inbox triage | Every 5 min | #artemis-ops |
| Triage batch | Every 30 min | #artemis-ops |
| Pre-meeting brief | 90 min before meetings | #artemis-briefs |
| Morning brief | Daily (default 7:30am) | #artemis-ops |
| SSL check | Daily 8:00am | #artemis-ops |
| Domain expiry check | Daily 8:05am | #artemis-ops |

## @Mention Examples

In any Mattermost channel:
- `@artemis what's open with TTI?`
- `@artemis what do I need before the 2pm call?`
- `@artemis what's due this week?`

Artemis will reply in-thread using context from your email, calendar, and commitments.
