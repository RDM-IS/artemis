#!/bin/bash
# Save the LIVE rdmis-crm-api package as the rollback before anything replaces it.
#
# Called by api/deploy.sh before the build, so every deploy leaves its rollback
# in ~/backups on the box — never in a scratch dir that disappears. Also safe
# to run on its own.
#
#   ~/backups/lambda_<date live code was deployed>_<first 12 hex of CodeSha256>.zip
#
# The download is checked against the Lambda's own CodeSha256 (base64 of the
# zip's SHA-256), locally and again on the box after the copy. Any failure
# exits non-zero, and deploy.sh stops before uploading. There is no override:
# a deploy without a rollback is the thing this exists to prevent.
#
# Rollback (from a Mac with SSO; the box role has no Lambda write permission):
#   scp rdmis:backups/<file> /tmp/
#   aws lambda update-function-code --function-name rdmis-crm-api \
#     --zip-file fileb:///tmp/<file> --region us-east-1 --profile rdmis-admin
set -euo pipefail

FUNCTION="rdmis-crm-api"
REGION="us-east-1"
BOX="${ARTEMIS_BOX:-rdmis}"
LOCAL_DIR="$HOME/backups"

fail() { echo "[backup] FAILED: $*" >&2; exit 1; }

info=$(aws lambda get-function --function-name "$FUNCTION" --region "$REGION" \
        --query '[Configuration.CodeSha256, Configuration.LastModified, Code.Location]' \
        --output text) || fail "could not read the live function (SSO expired? aws sso login --profile rdmis-admin)"
read -r sha_b64 modified url <<<"$info"
[ -n "$sha_b64" ] && [ -n "$url" ] || fail "empty CodeSha256 or download URL"

hex=$(python3 -c 'import base64, sys; print(base64.b64decode(sys.argv[1]).hex())' "$sha_b64")
name="lambda_${modified:0:10}_${hex:0:12}.zip"

# Already on the box with the right digest (e.g. a second deploy of the same
# live code): nothing to do.
remote_hex=$(ssh -o ConnectTimeout=20 "$BOX" "sha256sum ~/backups/$name 2>/dev/null | cut -d' ' -f1" || true)
if [ "$remote_hex" = "$hex" ]; then
  echo "[backup] rollback already on the box: ~/backups/$name (sha256 $hex)"
  exit 0
fi

mkdir -p "$LOCAL_DIR"
curl -fsS -o "$LOCAL_DIR/$name" "$url" || fail "download of the live package failed"
got=$(shasum -a 256 "$LOCAL_DIR/$name" | cut -d' ' -f1)
[ "$got" = "$hex" ] || fail "downloaded package sha256 $got != live CodeSha256 $hex"

ssh -o ConnectTimeout=20 "$BOX" 'mkdir -p ~/backups' || fail "box unreachable ($BOX) — no rollback saved"
scp -q "$LOCAL_DIR/$name" "$BOX:backups/$name" || fail "copy to the box failed"
remote_hex=$(ssh "$BOX" "chmod 600 ~/backups/$name && sha256sum ~/backups/$name | cut -d' ' -f1") \
  || fail "could not verify the copy on the box"
[ "$remote_hex" = "$hex" ] || fail "box copy sha256 $remote_hex != $hex"

echo "[backup] rollback saved: $BOX:~/backups/$name (sha256 $hex, live since $modified; local copy $LOCAL_DIR/$name)"
