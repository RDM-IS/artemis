#!/bin/bash
set -e

# Fetch Mattermost DB credentials from Secrets Manager and start containers.
# Run from ~/mattermost on EC2.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

echo "Fetching Mattermost credentials from Secrets Manager..."
SECRET_JSON=$(aws secretsmanager get-secret-value \
  --secret-id rdmis/dev/mattermost \
  --region us-east-1 \
  --query SecretString \
  --output text)

DB_USER=$(echo "$SECRET_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin)['db_user'])")
DB_PASS=$(echo "$SECRET_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin)['db_password'])")

# RDS endpoint for Mattermost DB
RDS_HOST=$(aws secretsmanager get-secret-value \
  --secret-id "$(printenv RDS_SECRET_ARN 2>/dev/null || echo 'rds!db-bfe5d90f-9ab3-4646-8c98-45e1d3e7aa72')" \
  --region us-east-1 \
  --query SecretString \
  --output text | python3 -c "import sys,json; print(json.load(sys.stdin).get('host','rdmis-dev.cx0mwwykgidb.us-east-1.rds.amazonaws.com'))")

# Write .env for docker-compose (not committed — gitignored)
cat > .env <<EOF
MM_SQLSETTINGS_DATASOURCE=postgres://${DB_USER}:${DB_PASS}@${RDS_HOST}:5432/mattermost?sslmode=require
EOF

echo "Starting Mattermost..."
docker compose up -d

echo "Waiting for Mattermost to be healthy..."
for i in $(seq 1 30); do
  if curl -sf http://localhost:8065/api/v4/system/ping >/dev/null 2>&1; then
    echo "Mattermost is healthy."
    rm -f .env
    exit 0
  fi
  sleep 2
done

echo "ERROR: Mattermost did not become healthy within 60s"
rm -f .env
exit 1
