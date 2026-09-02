#!/usr/bin/env bash
# Ingest the user's uploads/ folder (on the runner or locally staged) into the
# remote `media` branch as uploads/<filename>. Files must already be in the
# working tree under uploads/ (gitignored on main; they live on media only).
#   usage: ingest_uploads.sh <uploads-dir>
set -euo pipefail

SRC_DIR="${1:?usage: ingest_uploads.sh <uploads-dir>}"
BRANCH="media"
WORK="$(mktemp -d)"
REPO="${GITHUB_REPOSITORY:?GITHUB_REPOSITORY not set}"
TOKEN="${MEDIA_PUSH_TOKEN:?MEDIA_PUSH_TOKEN not set}"

shopt -s nullglob nocaseglob
files=("$SRC_DIR"/*)
shopt -u nullglob nocaseglob
if [ ${#files[@]} -eq 0 ]; then
  echo "uploads: no files in $SRC_DIR — queue unchanged"
  exit 0
fi

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

mkdir -p uploads
copied=0
for f in "${files[@]}"; do
  name="$(basename "$f")"
  # Skip macOS junk and anything already queued.
  case "$name" in
    .DS_Store|._*) continue ;;
  esac
  if [ -f "uploads/$name" ]; then continue; fi
  cp "$f" "uploads/$name"
  git add -f "uploads/$name"
  copied=$((copied + 1))
done

if [ "$copied" -eq 0 ]; then
  echo "uploads: nothing new to queue"
  exit 0
fi

git commit -q -m "uploads: queue ${copied} file(s)"
git push -q "https://x-access-token:${TOKEN}@github.com/${REPO}.git" "$BRANCH" --force
echo "uploads: queued ${copied} file(s) on ${BRANCH}"
