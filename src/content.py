"""Content bank: date-seeded deterministic selection with no repeats.

Each calendar date maps to a stable (quote, tip) pair. Selection walks the
bank by day-count since the epoch start date, so the same date always picks
the same content and the pair never repeats until the bank fully cycles.
"""
import datetime as dt
import json
import random

from .config import CONTENT_DIR

# The date the automation goes live. Day N picks quotes[N % len] and
# tips[N % len]; changing this shifts every day's mapping, so fix it once.
EPOCH = dt.date(2026, 9, 2)


def load_banks() -> tuple[list[dict], list[dict]]:
    with open(CONTENT_DIR / "quotes.json", encoding="utf-8") as f:
        quotes = json.load(f)
    with open(CONTENT_DIR / "tips.json", encoding="utf-8") as f:
        tips = json.load(f)
    return quotes, tips


def day_index(date: dt.date) -> int:
    """0-based days since EPOCH. Negative-safe: clamps pre-epoch dates."""
    delta = (date - EPOCH).days
    return delta if delta >= 0 else 0


def pick(quotes: list[dict], tips: list[dict], date: dt.date) -> dict:
    """Deterministically pick the (quote, tip) pair for a date."""
    n = day_index(date)
    quote = quotes[n % len(quotes)]
    tip = tips[n % len(tips)]
    # Per-date seed keeps any tie-breaking stable without repeats.
    rng = random.Random(f"{date.isoformat()}")
    return {
        "date": date.isoformat(),
        "quote": quote,
        "tip": tip,
        "seed": f"{date.isoformat()}",
        "_rng_shuffled": rng.random(),  # reserved; selection is bank-order based
    }


def caption_for(quote: dict, hashtags: list[str]) -> str:
    """Build the feed-post caption from the day's quote plus hashtags."""
    author = quote["author"]
    lines = [f"“{quote['text']}”", "", f"— {author}"]
    if hashtags:
        lines += ["", " ".join(f"#{tag.lstrip('#')}" for tag in hashtags)]
    return "\n".join(lines)
