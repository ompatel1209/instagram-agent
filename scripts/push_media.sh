#!/usr/bin/env bash
# Push rendered media files to the `media` branch (media only, no source).
# Meta's servers fetch image_url from raw.githubusercontent.com, so the JPGs
# must be publicly readable on that branch. Called by src/main.py with the
# MEDIA_PUSH_TOKEN env var (PAT or the runner's github.token).
set -euo pipefail

DATE="${1:?usage: push_media.sh YYYY-MM-DD}"
SRC_DIR="media_out"
BRANCH="media"
REPO="${GITHUB_REPOSITORY:?GITHUB_REPOSITORY not set}"
TOKEN="${MEDIA_PUSH_TOKEN:?MEDIA_PUSH_TOKEN not set}"

cd "$SRC_DIR"

git init -q .
git checkout -q -b "$BRANCH" 2>/dev/null || git checkout -q "$BRANCH"
git config user.name "ig-agent"
git config user.email "actions@github.com"
# Scope credentials to this push only (avoid the checkout's persisted creds,
# which force-push to the wrong branch remote when re-used inside media_out/).
git remote remove origin 2>/dev/null || true
git add -f "${DATE}-feed.jpg" "${DATE}-story.jpg"

# Branch may already exist on the remote — stack today's files on top so
# previous days' media URLs keep resolving.
if git ls-remote "https://x-access-token:${TOKEN}@github.com/${REPO}.git" --heads "$BRANCH" | grep -q "$BRANCH"; then
  git pull "https://x-access-token:${TOKEN}@github.com/${REPO}.git" "$BRANCH" --allow-unrelated-histories -q || true
  git add -f "${DATE}-feed.jpg" "${DATE}-story.jpg"
fi

git commit -q -m "media ${DATE}" || echo "nothing new to commit"
git push "https://x-access-token:${TOKEN}@github.com/${REPO}.git" "${BRANCH}" --force
echo "pushed ${DATE}-feed.jpg ${DATE}-story.jpg to ${BRANCH}"
