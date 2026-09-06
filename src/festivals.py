"""Festival calendar: today's festival greeting + tags, deterministically.

content/festivals.json holds the calendar: fixed-date entries ("date":
"MM-DD", same every year) and lunar/moving entries ("years": explicit ISO
date per year — panchang dates shift by region/moon-sighting, so explicit
per-year dates beat formulas and any correction is a one-line edit). When
a new year approaches, extend each "years" map; a year with no entry just
means no-festival that day (graceful degradation, never an error).

On a festival day:
  - the greeting line LEADS the caption (greet() prefixes it) — festival
    content first, the day's vibe caption right behind it
  - the festival tags ride right behind the static vibe bank: callers pass
    festival_tags() into trending.caption_tags' fest_tags slot, ahead of
    the day's trending window. trending's _clean (inside merge_tags)
    lowercases/dedupes and drops anything in trending.BLOCKED, so this
    module never needs to import trending — no circular dependency.

Everything is deterministic per (date, festival id): the same day always
greets with the same line and tags, so a re-run never rewrites a published
caption. Never raises — a missing/corrupt calendar degrades to no-festival
and the day publishes exactly as a normal day would. A festival must
never be the reason a post fails.

"vibe" in an entry is informational (the refresh day-plan log mentions
it); the day's media keeps its own vibe. First matching entry wins on a
same-day collision, so majors are listed first in the calendar.
"""
import datetime as dt
import json
import random

from .config import CONTENT_DIR


def _load() -> list[dict]:
    """The festival list, or [] when absent/corrupt (never raises)."""
    try:
        with open(CONTENT_DIR / "festivals.json", encoding="utf-8") as f:
            data = json.load(f)
        entries = data.get("festivals")
        if isinstance(entries, list):
            return [e for e in entries if isinstance(e, dict)]
    except Exception:
        pass
    return []


def festival_for(date: dt.date) -> dict | None:
    """Today's festival entry, or None when the calendar has no match.

    Fixed entries match on MM-DD (every year); lunar entries match their
    explicit ISO date from the "years" map (mapped years only). First
    match wins — majors are listed first in the calendar.
    """
    mmdd = date.strftime("%m-%d")
    iso = date.isoformat()
    year = str(date.year)
    for entry in _load():
        if entry.get("date") == mmdd:
            return entry
        years = entry.get("years")
        if isinstance(years, dict) and years.get(year) == iso:
            return entry
    return None


def festival_line(festival: dict | None, date: dt.date) -> str:
    """The day's greeting line, deterministic per (date, id). "" without a
    festival (or lines) — callers then have nothing to prefix."""
    if not festival:
        return ""
    lines = festival.get("lines")
    if not isinstance(lines, list) or not lines:
        return ""
    rng = random.Random(f"{date.isoformat()}:{festival.get('id', '?')}")
    return str(rng.choice(lines))


def festival_tags(festival: dict | None) -> list[str]:
    """The festival's tags ([] without a festival) — cleaned here so the
    refresh log/state read consistently; trending's _clean re-checks them
    (dedupe + BLOCKED) when they merge into a caption."""
    if not festival:
        return []
    tags = festival.get("tags")
    if not isinstance(tags, list):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for t in tags:
        t = str(t).lstrip("#").strip().lower()
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def greet(caption: str, festival: dict | None, date: dt.date) -> str:
    """Prefix the festival greeting onto a caption body ("" line = no-op),
    so every tier can call this unconditionally."""
    line = festival_line(festival, date)
    if not line:
        return caption
    return f"{line}\n\n{caption}"
