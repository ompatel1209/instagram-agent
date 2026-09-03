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
# to a list of Pexels search queries in priority order: an Indian-aesthetic
# query first (the account's theme), then a generic fallback with the same
# mood so an empty result can never break the tier. The photo and the girly
# caption always tell the same story.
VIBES = {
    "selfie":   ["indian woman portrait natural light",
                 "woman portrait natural light"],
    "attitude": ["indian girl attitude style",
                 "confident woman fashion"],
    "cute":     ["indian girl aesthetic pink",
                 "pink aesthetic cute"],
    "ootd":     ["indian outfit traditional fashion",
                 "outfit street style fashion"],
    "travel":   ["indian travel aesthetic",
                 "travel wanderlust aesthetic"],
    "selflove": ["indian woman self care cozy",
                 "self care cozy morning"],
    "general":  ["indian girl aesthetic",
                 "aesthetic pink flowers"],
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

    The vibe's queries are tried in priority order (Indian-aesthetic first,
    generic fallback after) and the first one with usable results wins.
    Candidates are sorted by photo id, already-posted ids are skipped, and
    the date + query seed a stable pick so an idempotent re-run of the same
    day chooses the same photo again. Returns a dict with `id`, `src`
    (download URL), `photographer` and `vibe`, or None when nothing is
    available (including a missing/invalid key).
    """
    vibe = vibe_of_day(date)
    used = set(used_ids)
    for query in VIBES[vibe]:
        photos = _search(api_key, query)
        candidates = sorted(
            (p for p in photos if p.get("id") not in used
             and p.get("src", {}).get("portrait")),
            key=lambda p: p["id"],
        )
        if not candidates:
            continue
        rng = random.Random(f"{date.isoformat()}:{query}")
        photo = rng.choice(candidates)
        return {
            "id": photo["id"],
            "src": photo["src"]["portrait"],  # 800x1200, pads cleanly to 4:5/9:16
            "photographer": photo.get("photographer", "Pexels"),
            "vibe": vibe,
        }
    return None


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


# --- Stock-video (Pexels) tier: daily Reels --------------------------------------
#
# A separate function set from the photo tier because videos live on a
# different API tree (/videos/, not /v1/) and have their own pick rules
# (portrait orientation, 5–60s duration, a downloadable mp4 rendition).

VIDEO_API_BASE = "https://api.pexels.com/videos/"

# One visual-mood query per photo vibe, aligned with the Indian-aesthetic
# theme. Used to pick the day's Reel video; the queries were validated live
# against the API (all return 18+ portrait clips of 5–60s).
REEL_VIBES = {
    "selfie":   ["indian woman portrait", "woman portrait natural light"],
    "attitude": ["indian girl aesthetic", "confident woman fashion"],
    "cute":     ["indian aesthetic flowers", "pink aesthetic cute"],
    "ootd":     ["indian outfit fashion", "outfit street style fashion"],
    "travel":   ["indian travel aesthetic", "travel wanderlust aesthetic"],
    "selflove": ["self care aesthetic", "self care cozy morning"],
    "general":  ["indian aesthetic", "aesthetic pink flowers"],
}


def _search_videos(api_key: str, query: str, per_page: int = 40) -> list[dict]:
    """Portrait videos for a query; [] on any failure (never raises)."""
    try:
        r = requests.get(
            f"{VIDEO_API_BASE}search",
            params={"query": query, "orientation": "portrait",
                    "per_page": per_page},
            headers={"Authorization": api_key},
            timeout=30,
        )
        if r.status_code != 200:
            return []
        return r.json().get("videos", [])
    except requests.RequestException:
        return []


def _pick_video_file(video: dict) -> dict | None:
    """The mp4 rendition of a video to download.

    Renditions with a height between 720 and 1920 are eligible (Instagram
    Reels want at least 720p); among them the one closest to 1080p wins —
    the platform's preferred Reel resolution — with the smallest eligible
    file breaking ties so the mux stays fast.
    """
    files = [
        f for f in video.get("video_files", [])
        if f.get("file_type") == "video/mp4"
        and 720 <= (f.get("height") or 0) <= 1920
    ]
    if not files:
        return None
    # Closest to 1080p wins; smallest height breaks exact ties (smaller
    # file = faster mux, and download size is the runner-time bottleneck).
    return min(files, key=lambda f: (abs((f.get("height") or 0) - 1080),
                                     f.get("height") or 0))


def pick_video(api_key: str, date: dt.date,
               used_ids: list[int]) -> dict | None:
    """Choose today's stock video deterministically.

    Mirrors pick_photo: the vibe's queries are tried in priority order,
    candidates are filtered to 5–60s clips with a downloadable mp4
    rendition, already-posted ids are skipped, and the date + query seed
    a stable pick so an idempotent re-run chooses the same video. Returns
    a dict with `id`, `src` (mp4 URL), `photographer`, `vibe` and
    `duration`, or None when nothing is available.
    """
    vibe = vibe_of_day(date)
    used = set(used_ids)
    for query in REEL_VIBES[vibe]:
        videos = _search_videos(api_key, query)
        candidates = []
        for v in videos:
            if v.get("id") in used or not (5 <= (v.get("duration") or 0) <= 60):
                continue
            f = _pick_video_file(v)
            if f:
                candidates.append((v, f))
        if not candidates:
            continue
        candidates.sort(key=lambda vf: vf[0]["id"])
        rng = random.Random(f"{date.isoformat()}:{query}")
        v, f = rng.choice(candidates)
        return {
            "id": v["id"],
            "src": f["link"],
            "photographer": v.get("user", {}).get("name", "Pexels"),
            "vibe": vibe,
            "duration": v.get("duration"),
        }
    return None


def video_by_id(api_key: str, video_id: int) -> dict | None:
    """Re-fetch a pinned video's metadata (for idempotent re-runs).

    Like photo_by_id: the pick for a day is pinned in state.json without
    its temporary download URL, so a re-run that needs to redo a failed
    step looks the video up by id. None on any failure.
    """
    try:
        r = requests.get(f"{VIDEO_API_BASE}videos/{video_id}",
                         headers={"Authorization": api_key}, timeout=30)
        if r.status_code != 200:
            return None
        v = r.json()
        f = _pick_video_file(v)
        if not f:
            return None
        return {
            "id": v["id"],
            "src": f["link"],
            "photographer": v.get("user", {}).get("name", "Pexels"),
            "vibe": None,  # the caller fills this from the pinned record
            "duration": v.get("duration"),
        }
    except requests.RequestException:
        return None


def download_video_file(video: dict, dest) -> bool:
    """Fetch the chosen video's mp4 to `dest`; False on any failure."""
    try:
        r = requests.get(video["src"], timeout=180)
        if r.status_code == 200 and r.content:
            dest.write_bytes(r.content)
            return True
    except requests.RequestException:
        pass
    return False
