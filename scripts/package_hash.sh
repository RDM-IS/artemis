#!/usr/bin/env bash
# CI-2 (2026-09-25) — the identity of the LAMBDA PACKAGE, not of the repo.
#
# DRIFT-ALARM used to compare the deployed commit sha with origin/main, so every
# artemis merge reported Lambda drift even when the shipped code was identical —
# most merges touch artemis/ or docs/, which the package does not contain. A
# false positive every day teaches you to ignore the alarm.
#
# This hashes only what api/deploy.sh actually zips:
#     api/  knowledge/  migrations/
#
# PACKAGE-IDENTITY (2026-09-26): tests/ was in this list and in the zip, so a
# test-only commit changed the package identity and DRIFT-ALARM reported Lambda
# drift for a change that CANNOT affect runtime. Measured over the 25 commits
# before the fix: 13 touched a hashed path and 8 of those were tests-only. An
# alarm that fires on non-events trains you to ignore it, which is the same
# reasoning that replaced the commit-sha comparison with this hash (CI-2).
#
# The list must stay exactly what deploy.sh zips. If they diverge, the hash
# stops being the package's identity and starts being a guess about it.
# Git already hashes a directory's full contents as its tree object, so the
# tree ids of those four paths ARE the package's identity. No file walking, no
# ordering questions, no timestamps.
#
# ONE implementation, called from both sides: api/deploy.sh writes the result
# into the Lambda Description at deploy time, artemis/drift.py computes it for
# origin/main at check time. If this formula ever changes, both change together.
#
# Usage:  scripts/package_hash.sh [ref]     (default HEAD)
set -euo pipefail

REF="${1:-HEAD}"
cd "$(git rev-parse --show-toplevel)"

if command -v sha256sum >/dev/null 2>&1; then     # Amazon Linux
    SHA=(sha256sum)
else                                              # macOS
    SHA=(shasum -a 256)
fi

git rev-parse "${REF}:api" "${REF}:knowledge" "${REF}:migrations" \
    | "${SHA[@]}" | cut -c1-16
