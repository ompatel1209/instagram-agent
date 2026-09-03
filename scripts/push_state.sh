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
# Actions pushes state commits to main all day (main + backup + safety runs,
# plus manual dispatches); the checkout's origin/main can be behind by one of
# those. Rebase the state commit on top of the freshest remote main before
# pushing — the only file this repo ever commits is state.json, and even a
# clashing mid-air edit of it loses nothing (the state is rebuilt from the
# run's in-memory copy on the next attempt).
if git fetch origin main; then
  if ! git diff --cached --quiet origin/main; then
    git rebase origin/main
  fi
fi
git commit -q -m "state update $(date -u +%Y-%m-%dT%H:%MZ)"
git push origin HEAD:main
echo "state.json pushed"
