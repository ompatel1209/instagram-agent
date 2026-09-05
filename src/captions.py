"""Girly caption bank: pick a caption + hashtags by the vibe in a filename.

Files in the uploads queue are named like `selfie1.jpg` or `attitude2.mp4` —
the word before the trailing number is the vibe. Anything that doesn't match
a known vibe falls back to `general`, so no file is ever left captionless.
"""
import datetime as dt
import json
import random

from .config import CONTENT_DIR

# Filename word -> vibe key in content/captions.json (plus common synonyms).
VIBE_ALIASES = {
    "selfie": "selfie",
    "selfy": "selfie",
    "pic": "selfie",
    "photo": "selfie",
    "attitude": "attitude",
    "att": "attitude",
    "sassy": "attitude",
    "cute": "cute",
    "cutie": "cute",
    "ootd": "ootd",
    "outfit": "ootd",
    "dress": "ootd",
    "fashion": "ootd",
    "travel": "travel",
    "trip": "travel",
    "vacation": "travel",
    "vacay": "travel",
    "beach": "travel",
    "selflove": "selflove",
    "love": "selflove",
    "glow": "selflove",
    "general": "general",
}

PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".heic", ".webp"}
VIDEO_EXTS = {".mp4", ".mov", ".m4v"}


def load_bank() -> dict:
    with open(CONTENT_DIR / "captions.json", encoding="utf-8") as f:
        return json.load(f)


def load_parts() -> dict | None:
    """Combinatorial caption parts, or None when absent/corrupt — the
    composer falls back to the static bank caption either way."""
    try:
        with open(CONTENT_DIR / "caption_parts.json", encoding="utf-8") as f:
            parts = json.load(f)
        if parts.get("openers") and parts.get("vibes"):
            return parts
    except Exception:
        pass
    return None


def compose_caption(vibe: str, date: dt.date) -> str | None:
    """Opener x line x closer composed for (vibe, date), or None.

    10 openers x 14 lines x 6 closers = 840 combos per vibe, so effective
    caption variety is far beyond the 15 static lines — the daily refresh
    workflow's job is keeping these parts feeling current. Deterministic:
    same (vibe, date) always composes identically, so re-runs never
    rewrite a published caption. None on any failure (missing/corrupt
    parts file, unknown vibe) — callers fall back to the static bank.
    """
    parts = load_parts()
    if not parts:
        return None
    entry = (parts["vibes"].get(vibe)
             or parts["vibes"].get("general"))
    if not entry:
        return None
    key = f"{date.isoformat()}:{vibe}"
    rng = random.Random(key)
    opener = rng.choice(parts["openers"])
    line = rng.choice(entry["lines"])
    closer = rng.choice(entry["closers"])
    return f"{opener} {line} {closer}"


def vibe_from_filename(filename: str) -> str:
    """`attitude3.mp4` -> `attitude`; unknown -> `general`."""
    stem = filename.rsplit(".", 1)[0].lower().strip()
    stem = "".join(ch for ch in stem if ch.isalpha())  # drop digits
    return VIBE_ALIASES.get(stem, "general")


def is_video(filename: str) -> bool:
    return "." + filename.rsplit(".", 1)[-1].lower() in VIDEO_EXTS


def pick(bank: dict, vibe: str, date: dt.date) -> dict:
    """Deterministically pick the caption for a date (stable on re-runs)."""
    entries = bank.get(vibe) or bank["general"]
    rng = random.Random(f"{date.isoformat()}:{vibe}")
    captions = entries["captions"]
    n = dt.date.fromisoformat(date.isoformat()).toordinal() - dt.date(2026, 9, 2).toordinal()
    caption = captions[n % len(captions)]
    return {"caption": caption, "hashtags": entries["hashtags"], "rng": rng}


def format_caption(caption: str, tags: list[str]) -> str:
    """Caption line + formatted hashtag line — the single formatting point
    every caption path goes through (so trending tags merge in cleanly)."""
    lines = [caption]
    if tags:
        lines += ["", " ".join(f"#{t.lstrip('#').lower()}" for t in tags)]
    return "\n".join(lines)


def caption_text(bank: dict, vibe: str, date: dt.date, extra_tags: list[str]) -> str:
    """Full caption body: girly line + vibe hashtags (+ optional extras)."""
    picked = pick(bank, vibe, date)
    tags = list(picked["hashtags"]) + [t for t in extra_tags if t]
    return format_caption(picked["caption"], tags)
