#!/usr/bin/env bash
# STAB-1 A5 — the ONE safe deploy path for the box. Atomic migrate-first:
#   pull → run migrations → restart the service.
#
# WHY this exists: box diagnosis (2026-07-17) found NO auto-pull mechanism — no
# crontab (not even installed), no cron.d/systemd-timer git pull, no ExecStartPre
# hook, no runner. The git reflog shows manual `pull --ff-only` by the operator.
# So the "half-deploy" (code on disk ≠ running process, migrations unapplied) was
# a *manual* pull-without-restart, not an automation to disable. The fix is to
# make the manual deploy a single command that can't half-finish: never a bare
# `git pull`. `job_update_check` now points here.
#
# Usage (on the box):
#   bash scripts/deploy.sh
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"
BRANCH="$(git rev-parse --abbrev-ref HEAD)"
echo "==> Deploying on branch: ${BRANCH}"

echo "==> Pull (fast-forward only)"
git pull --ff-only

echo "==> Load env"
set -a; [ -f .env ] && . ./.env; set +a

echo "==> Migrate-first (apply any new migrations to RDS)"
PYTHONPATH="$PWD" /usr/bin/python3.11 migrations/run_migrations.py

echo "==> Restart acos (bounded — STAB-1 A3 SIGTERM handler exits < 5s)"
time sudo systemctl restart acos

echo "==> Post-restart status"
sleep 2
systemctl is-active acos
systemctl show acos -p ActiveEnterTimestamp --value

echo "==> Done. Running commit:"
git log -1 --oneline
