#!/usr/bin/env bash
# Push rendered media files to the `media` branch (orphan-style: media only).
# Meta's servers fetch image_url from raw.githubusercontent.com, so the JPGs
# must be publicly readable on that branch. Uses GITHUB_TOKEN auth.
set -euo pipefail

DATE="${1:?usage: push_media.sh YYYY-MM-DD}"
SRC_DIR="media_out"
BRANCH="media"

cd "$SRC_DIR"

git init -q .
git checkout -q -b "$BRANCH" 2>/dev/null || git checkout -q "$BRANCH"
git config user.name "ig-agent"
git config user.email "actions@github.com"
git add -f "${DATE}-feed.jpg" "${DATE}-story.jpg"

# Branch may not exist yet on the remote — handle both first push and updates.
if git ls-remote --heads origin "$BRANCH" | grep -q "$BRANCH"; then
  git fetch -q origin "$BRANCH"
  git reset -q --soft "origin/${BRANCH}" 2>/dev/null || true
  git add -f "${DATE}-feed.jpg" "${DATE}-story.jpg"
fi

git commit -q -m "media ${DATE}" || echo "nothing new to commit"
git push -q origin "${BRANCH}" --force
echo "pushed ${DATE}-feed.jpg ${DATE}-story.jpg to ${BRANCH}"
