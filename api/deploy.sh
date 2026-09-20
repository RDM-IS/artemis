#!/bin/bash
set -e

REPO="$(cd "$(dirname "$0")/.." && pwd)"
OVERRIDE_LOG="$HOME/.artemis_lambda_deploy.log"

# COLUMN-GREP preflight — before the build, so a refusal costs nothing.
#
# It refuses when the code being shipped still names a column that a PENDING
# migration drops or renames. On 2026-09-20 that combination took /plan and
# /overview down for seven minutes: the smoke test passed because the column
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

echo "Cleaning up..."
rm -rf package function.zip

echo "Done."
