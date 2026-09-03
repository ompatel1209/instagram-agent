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


# --- Uploads queue bookkeeping ------------------------------------------------

def posted_files(state: dict) -> list[str]:
    """Upload files already published (top-level, date-independent)."""
    return state.get("posted_files", [])


def mark_file_posted(state: dict, filename: str) -> None:
    """Record an uploaded file as published so the queue never repeats it."""
    files = state.setdefault("posted_files", [])
    if filename not in files:
        files.append(filename)


def media_of_day(state: dict, date_str: str) -> str | None:
    """The uploads-queue file that was (or will be) posted on a date."""
    return state.get("days", {}).get(date_str, {}).get("media_of_day")


def set_media_of_day(state: dict, date_str: str, filename: str) -> None:
    day(state, date_str)["media_of_day"] = filename


# --- Stock-photo (Pexels) bookkeeping ------------------------------------------


def stock_of_day(state: dict, date_str: str) -> dict | None:
    """The stock photo that was (or will be) posted on a date."""
    return state.get("days", {}).get(date_str, {}).get("stock_of_day")


def set_stock_of_day(state: dict, date_str: str, photo: dict) -> None:
    """Pin the day's stock photo (idempotent re-runs pick the same photo)."""
    day(state, date_str)["stock_of_day"] = {
        "id": photo["id"], "vibe": photo["vibe"],
        "photographer": photo["photographer"],
    }


def used_stock_ids(state: dict) -> list[int]:
    """All stock-photo ids ever posted, so no stock photo repeats."""
    ids = []
    for d in state.get("days", {}).values():
        stock = d.get("stock_of_day")
        if isinstance(stock, dict) and isinstance(stock.get("id"), int):
            ids.append(stock["id"])
    return ids
