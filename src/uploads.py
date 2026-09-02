"""Uploads queue: the user's own photos/videos, queued on the `media` branch.

The user drops files into an `uploads/` folder (ingested to the media branch
as `uploads/<filename>` by scripts/ingest_uploads.sh). Files post one per day:
the queue is listed deterministically (name order), and state.json tracks which
files have already been posted, so the next run always picks the first
unposted file. Photos publish as feed post + Story; videos as Reels + Story.
"""
import json
import os


def _repo_slug() -> str:
    slug = os.environ.get("GITHUB_REPOSITORY", "")
    if not slug:
        import pathlib
        cfg_path = pathlib.Path(__file__).resolve().parent.parent / "config.json"
        with open(cfg_path, encoding="utf-8") as f:
            cfg = json.load(f)
        slug = f"{cfg.get('repo_owner', '')}/{cfg.get('repo_name', '')}"
    return slug


def _token() -> str:
    tok = os.environ.get("MEDIA_PUSH_TOKEN") or os.environ.get("GH_PAT", "")
    if not tok:
        raise RuntimeError("MEDIA_PUSH_TOKEN/GH_PAT not set — cannot list uploads")
    return tok


def list_queue() -> list[str]:
    """All queued upload filenames on the media branch (sorted, no tokens).

    Uses the GitHub contents API so no token-bearing URL ever hits a log;
    returns [] on any failure (bad branch, empty queue, no network).
    """
    import urllib.request
    url = (f"https://api.github.com/repos/{_repo_slug()}/contents/"
           f"uploads?ref=media")
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {_token()}",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            entries = json.load(r)
    except Exception:
        return []
    return sorted(e["name"] for e in entries
                  if isinstance(e, dict) and e.get("type") == "file")


def next_file(posted_files: list[str]) -> str | None:
    """First queued file not yet posted, or None when the queue is empty."""
    posted = set(posted_files)
    for filename in list_queue():
        if filename not in posted:
            return filename
    return None


def raw_url(filename: str, cfg: dict) -> str:
    """Public raw.githubusercontent URL for a queued upload."""
    return (f"https://raw.githubusercontent.com/{cfg['repo_owner']}/"
            f"{cfg['repo_name']}/media/uploads/{filename}")
