#!/usr/bin/env bash
# Push state.json (and any tracked changes) back to main so the next run —
# including the safety re-run — knows which steps already succeeded.
set -euo pipefail

git config user.name "ig-agent"
git config user.email "actions@github.com"
git add state.json
if git diff --cached --quiet; then
  echo "state.json unchanged — nothing to push"
  exit 0
fi
git commit -q -m "state update $(date -u +%Y-%m-%dT%H:%MZ)"
git push origin HEAD:main
echo "state.json pushed"
