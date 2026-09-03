"""Licensed stock-photo tier (Pexels API) — the middle content source.

Content priority in src/main.py: uploads queue (user's own photos/videos)
-> Pexels stock photo -> rendered quote/tip graphics. Pexels provides an
official API whose photos are free to use (attribution appreciated), so
this tier is ToS- and copyright-safe where scraping Pinterest would not be.

Every failure mode is non-fatal: missing API key, rate limit, network
error, or an empty search result returns None and the caller falls back to
the quote/tip graphics — the daily schedule can never break because of
this module.
"""
import datetime as dt
import random

import requests

API_BASE = "https://api.pexels.com/v1/"
EPOCH = dt.date(2026, 9, 2)

# Daily vibe rotation — one vibe per day, cycling forever. Each vibe maps
# to a Pexels search query chosen to match the caption bank's mood, so the
# photo and the girly caption always tell the same story.
VIBES = {
    "selfie":   "woman portrait natural light",
    "attitude": "confident woman fashion",
    "cute":     "pink aesthetic cute",
    "ootd":     "outfit street style fashion",
    "travel":   "travel wanderlust aesthetic",
    "selflove": "self care cozy morning",
    "general":  "aesthetic pink flowers",
}
VIBE_ORDER = ["general", "selfie", "ootd", "cute", "travel", "attitude",
              "selflove"]


def vibe_of_day(date: dt.date) -> str:
    """Deterministic vibe rotation, one per day, cycling every 7 days."""
    n = date.toordinal() - EPOCH.toordinal()
    return VIBE_ORDER[n % len(VIBE_ORDER)]


def _search(api_key: str, query: str, per_page: int = 40) -> list[dict]:
    """Portrait photos for a query; [] on any failure (never raises)."""
    try:
        r = requests.get(
            f"{API_BASE}search",
            params={"query": query, "orientation": "portrait",
                    "per_page": per_page},
            headers={"Authorization": api_key},
            timeout=30,
        )
        if r.status_code != 200:
            return []
        return r.json().get("photos", [])
    except requests.RequestException:
        return []


def pick_photo(api_key: str, date: dt.date,
               used_ids: list[int]) -> dict | None:
    """Choose today's stock photo deterministically.

    Candidates are sorted by photo id, already-posted ids are skipped, and
    the date + query seed a stable pick so an idempotent re-run of the same
    day chooses the same photo again. Returns a dict with `id`, `src`
    (download URL), `photographer` and `vibe`, or None when nothing is
    available (including a missing/invalid key).
    """
    vibe = vibe_of_day(date)
    photos = _search(api_key, VIBES[vibe])
    if not photos:
        return None
    used = set(used_ids)
    candidates = sorted(
        (p for p in photos if p.get("id") not in used
         and p.get("src", {}).get("portrait")),
        key=lambda p: p["id"],
    )
    if not candidates:
        return None
    rng = random.Random(f"{date.isoformat()}:{VIBES[vibe]}")
    photo = rng.choice(candidates)
    return {
        "id": photo["id"],
        "src": photo["src"]["portrait"],  # 800x1200, pads cleanly to 4:5/9:16
        "photographer": photo.get("photographer", "Pexels"),
        "vibe": vibe,
    }


def photo_by_id(api_key: str, photo_id: int) -> dict | None:
    """Re-fetch a pinned photo's metadata (for idempotent re-runs).

    The pick for a day is stored in state.json without its temporary
    download URL, so a re-run that needs to redo a failed step looks the
    photo up by id instead of re-picking. None on any failure.
    """
    try:
        r = requests.get(f"{API_BASE}photos/{photo_id}",
                         headers={"Authorization": api_key}, timeout=30)
        if r.status_code != 200:
            return None
        p = r.json()
        if not p.get("src", {}).get("portrait"):
            return None
        return {
            "id": p["id"],
            "src": p["src"]["portrait"],
            "photographer": p.get("photographer", "Pexels"),
            "vibe": None,  # the caller fills this from the pinned record
        }
    except requests.RequestException:
        return None


def download(photo: dict, dest) -> bool:
    """Fetch the chosen photo's JPG to `dest`; False on any failure."""
    try:
        r = requests.get(photo["src"], timeout=120)
        if r.status_code == 200 and r.content:
            dest.write_bytes(r.content)
            return True
    except requests.RequestException:
        pass
    return False
