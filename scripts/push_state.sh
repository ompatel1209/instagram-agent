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
# Commit FIRST: `git rebase` refuses to start while the index holds staged
# but uncommitted changes ("Your index contains uncommitted changes"), so the
# old rebase-then-commit order failed on every run and state never reached
# main. With the commit in place the tree is clean and the rebase is legal.
git commit -q -m "state update $(date -u +%Y-%m-%dT%H:%MZ)"

# Actions pushes state commits to main all day (main + backup + safety runs,
# plus manual dispatches); the checkout's origin/main can be behind by one of
# those. Rebase the state commit on top of the freshest remote main before
# pushing — the only file this repo ever commits is state.json, and even a
# clashing mid-air edit of it loses nothing (the state is rebuilt from the
# run's in-memory copy on the next attempt).
if git fetch origin main; then
  if ! git diff --quiet HEAD origin/main; then
    if ! git rebase origin/main; then
      git rebase --abort 2>/dev/null || true
      echo "ERROR: state commit could not rebase onto origin/main — NOT pushed" >&2
      exit 1
    fi
  fi
fi
git push origin HEAD:main
echo "state.json pushed"
