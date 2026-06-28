#!/usr/bin/env bash
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
OUT=context/CONTEXT.generated.md
mkdir -p context
{
  echo "# CONTEXT.generated.md — DO NOT HAND-EDIT (run scripts/context_snapshot.sh)"
  echo "_Generated: $(date -u +%Y-%m-%dT%H:%M:%SZ)_"
  echo
  echo "## Git"
  echo "- Branch: $(git rev-parse --abbrev-ref HEAD)"
  echo "- Head: $(git log -1 --oneline)"
  echo "- Origin: $(git remote get-url origin)"
  echo
  echo "## Runtime (only meaningful when run ON EC2)"
  TOK=$(curl -s --max-time 2 -X PUT "http://169.254.169.254/latest/api/token" -H "X-aws-ec2-metadata-token-ttl-seconds: 60" 2>/dev/null); IP=$(curl -s --max-time 2 -H "X-aws-ec2-metadata-token: $TOK" http://169.254.169.254/latest/meta-data/public-ipv4 2>/dev/null || echo "n/a")
  echo "- Public IP: ${IP}"
  echo "- Host: $(hostname)"
  echo "- Python: $(python3 --version 2>&1)"
  if systemctl list-units --type=service 2>/dev/null | grep -q acos; then
    echo "- acos.service: $(systemctl is-active acos 2>/dev/null) | ExecStart: $(systemctl cat acos 2>/dev/null | grep -E '^ExecStart=' | head -1)"
  fi
  echo
  echo "## Playbooks (PLAYBOOKS.md)"
  grep -E "^## PB-" PLAYBOOKS.md | sed 's/^## /- /' || echo "- (none found)"
  echo
  echo "## Migrations (latest 5)"
  ls migrations/*.sql | sort | tail -5 | xargs -n1 basename | sed 's/^/- /'
  echo
  echo "## artemis/ modules"
  ls artemis/*.py | xargs -n1 basename | sed 's/^/- /'
  echo
  echo "## Database (live RDS)"
  ( set -a; [ -f .env ] && . ./.env; set +a; PYTHONPATH="$PWD" /usr/bin/python3.11 scripts/context_db.py ) 2>/dev/null || echo "- (db dump skipped — run on a host with RDS access + secrets)"
} > "$OUT"
echo "Wrote $OUT"
