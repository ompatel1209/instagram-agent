"""state.json: per-date idempotency + no-repeat tracking.

The workflow commits this file back to main after every run so a re-run
(or the safety re-run) completes only the steps that are still missing —
never double-posts.
"""
import json

from .config import STATE_PATH


def load() -> dict:
    if not STATE_PATH.exists():
        return {"days": {}}
    with open(STATE_PATH, encoding="utf-8") as f:
        return json.load(f)


def save(state: dict) -> None:
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
        f.write("\n")


def day(state: dict, date_str: str) -> dict:
    return state.setdefault("days", {}).setdefault(date_str, {})


def mark(state: dict, date_str: str, step: str, value=None) -> None:
    """Record a successful step (idempotency flag)."""
    d = day(state, date_str)
    d[step] = value if value is not None else True
    if "published_media" not in d:
        d["published_media"] = {}
    if step.startswith("publish_"):
        d.setdefault("published_media", {})


def record_media_id(state: dict, date_str: str, kind: str, media_id: str) -> None:
    day(state, date_str).setdefault("published_media", {})[kind] = media_id


def done(state: dict, date_str: str, step: str) -> bool:
    return bool(state.get("days", {}).get(date_str, {}).get(step))


def both_published(state: dict, date_str: str) -> bool:
    d = state.get("days", {}).get(date_str, {})
    return bool(d.get("publish_feed") and d.get("publish_story"))


def note_failure(state: dict, date_str: str, where: str, message: str) -> None:
    """Non-fatal bookkeeping for the log/history."""
    d = day(state, date_str)
    history = d.setdefault("failures", [])
    history.append({"where": where, "message": str(message)[:500]})


def note_token_expiry(state: dict, expires_iso: str, days_left: int) -> None:
    state["token"] = {"expires": expires_iso, "days_left": days_left}
