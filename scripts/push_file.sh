#!/usr/bin/env bash
# Push one local file to the remote `media` branch under a given path.
# Used for user-uploaded videos (Meta fetches video_url from the raw URL)
# and Reel cover frames. Tokens are passed via env, never echoed.
#   usage: push_file.sh <local-file> <remote-path>
set -euo pipefail

LOCAL="${1:?usage: push_file.sh <local-file> <remote-path>}"
REMOTE_PATH="${2:?usage: push_file.sh <local-file> <remote-path>}"
BRANCH="media"
WORK="$(mktemp -d)"
REPO="${GITHUB_REPOSITORY:?GITHUB_REPOSITORY not set}"
TOKEN="${MEDIA_PUSH_TOKEN:?MEDIA_PUSH_TOKEN not set}"

# Branch may already exist on the remote — clone it so previous days' files
# stay live at their URLs.
if git ls-remote "https://x-access-token:${TOKEN}@github.com/${REPO}.git" --heads "$BRANCH" | grep -q "$BRANCH"; then
  git clone -q --depth 1 -b "$BRANCH" "https://x-access-token:${TOKEN}@github.com/${REPO}.git" "$WORK/work" 2>/dev/null
  cd "$WORK/work"
else
  mkdir -p "$WORK/work"
  cd "$WORK/work"
  git init -q .
  git checkout -q -b "$BRANCH"
fi

git config user.name "ig-agent"
git config user.email "actions@github.com"

mkdir -p "$(dirname "$REMOTE_PATH")"
cp "$LOCAL" "$REMOTE_PATH"
git add -f "$REMOTE_PATH"
git commit -q -m "media: add ${REMOTE_PATH}" || echo "nothing new to commit"
git push -q "https://x-access-token:${TOKEN}@github.com/${REPO}.git" "$BRANCH" --force
echo "pushed ${REMOTE_PATH} to ${BRANCH}"
