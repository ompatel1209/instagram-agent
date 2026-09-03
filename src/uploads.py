"""Uploads queue: the user's own photos/videos, queued on the `media` branch.

The user drops files into an `uploads/` folder (ingested to the media branch
as `uploads/<filename>` by scripts/ingest_uploads.sh). Files post one per day:
the queue is listed deterministically (name order), and state.json tracks which
files have already been posted, so the next run always picks the first
unposted file. Photos publish as feed post + Story; videos as Reels + Story.
"""
import json
import os
import pathlib


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


def list_queue_checked() -> list[str] | None:
    """All queued upload filenames on the media branch, or None when the
    lookup itself failed (bad branch, no network, bad token).

    Uses the GitHub contents API so no token-bearing URL ever hits a log.
    Only recognized photo/video extensions are listed, so stray non-media
    files on the branch can never block the queue head. Alerting uses this
    directly: a failed lookup (None) must not masquerade as an empty queue
    and open a false "running low" alarm.
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
        return None
    media_exts = {".jpg", ".jpeg", ".png", ".heic", ".webp",
                  ".mp4", ".mov", ".m4v"}
    return sorted(e["name"] for e in entries
                  if isinstance(e, dict) and e.get("type") == "file"
                  and pathlib.Path(e["name"]).suffix.lower() in media_exts)


def list_queue() -> list[str]:
    """Queue filenames as the picking path sees it: [] when empty OR when
    the lookup failed. Picking treats both the same (nothing to post);
    anything that needs the distinction uses list_queue_checked()."""
    return list_queue_checked() or []


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
