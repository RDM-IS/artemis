#!/bin/bash
set -e

REPO="$(cd "$(dirname "$0")/.." && pwd)"
OVERRIDE_LOG="$HOME/.artemis_lambda_deploy.log"

# COLUMN-GREP preflight — before the build, so a refusal costs nothing.
#
# It refuses when the code being shipped still names a column that a PENDING
# migration drops or renames. On 2026-09-20 that combination took /plan and
# /overview down for 1 min 40 s: the smoke test passed because the column
# still existed at the time it ran.
#
# Override: bash deploy.sh --force "why this deploy cannot wait". The reason is
# required, printed, and appended to $OVERRIDE_LOG with the sha being deployed.
python3 "$REPO/scripts/lambda_preflight.py" "$@"

if [ "$1" = "--force" ]; then
  printf '%s  %s  FORCED: %s\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    "$(git -C "$REPO" rev-parse --short HEAD)" \
    "${2:-<no reason given>}" >> "$OVERRIDE_LOG"
  echo "[deploy] override recorded in $OVERRIDE_LOG"
fi

# ROLLBACK FIRST — the live package goes to ~/backups on the box, verified
# against its CodeSha256, before anything is built or uploaded. No override:
# --force skips the column-grep refusal, never this. (2026-09-21: the rollback
# for #148 had been saved only to a session scratch dir.)
bash "$REPO/scripts/lambda_backup.sh"

echo "Building Lambda package for Python 3.12 (linux/amd64)..."
rm -rf package function.zip

docker run --rm \
  --platform linux/amd64 \
  --entrypoint /bin/bash \
  -v "$PWD":/var/task \
  -w /var/task \
  public.ecr.aws/lambda/python:3.12 \
  -c "pip install -r requirements.txt -t ./package --quiet"

echo "Zipping..."
cd package && zip -r ../function.zip . -x "*.pyc" -x "*/__pycache__/*" > /dev/null
cd ..
zip -r function.zip app/ -x "*.pyc" -x "*/__pycache__/*" > /dev/null
zip -r function.zip ../migrations/ -x "*.pyc" -x "*/__pycache__/*" > /dev/null
zip -r function.zip ../tests/ -x "*.pyc" -x "*/__pycache__/*" > /dev/null
zip -r function.zip ../knowledge/ -x "*.pyc" -x "*/__pycache__/*" > /dev/null

export PATH="$PATH:/usr/local/bin:$HOME/.local/bin"
echo "Deploying..."
aws lambda update-function-code \
  --function-name rdmis-crm-api \
  --zip-file fileb://function.zip \
  --region us-east-1

# DRIFT-ALARM (2026-09-25): record WHICH COMMIT this package was built from on
# the function itself. Nothing recorded it before, so answering "is the live
# Lambda current?" meant downloading the 24 MB package and diffing it file by
# file. The Description is read by lambda:GetFunctionConfiguration, which the
# box role already has. Keep `sha=<40 hex>` first — the drift check parses it.
DEPLOYED_SHA="$(git -C "$REPO" rev-parse HEAD)"
DEPLOYED_BRANCH="$(git -C "$REPO" rev-parse --abbrev-ref HEAD)"
aws lambda wait function-updated-v2 --function-name rdmis-crm-api --region us-east-1
aws lambda update-function-configuration \
  --function-name rdmis-crm-api \
  --region us-east-1 \
  --description "sha=${DEPLOYED_SHA} branch=${DEPLOYED_BRANCH} deployed=$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --query '[Description,LastUpdateStatus]' --output text

echo "Cleaning up..."
rm -rf package function.zip

echo "Done."
