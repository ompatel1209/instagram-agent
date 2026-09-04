"""Auto-reply engagement: answer comments and DMs on the account.

Two flows, both keyword-themed and non-fatal (the reel.py contract):
  - comments: recent published media (from state.json) -> fetch comments
    -> skip already-replied + own comments -> categorize by keywords
    -> reply -> record in state so no comment is ever answered twice.
  - DMs: list conversations -> find threads whose last message is
    incoming (and within the 24h window) -> themed reply -> record,
    at most one reply per thread per day.

Reply text comes from content/replies.json — love / friendship / general
banks in the same girly voice as captions.json. The pick is deterministic
per comment/thread id, so a crash + re-run never changes what we said.

Every permission failure is recorded in state.json under
"engagement" -> "permissions_missing" and reconciled into exactly one
GitHub issue (alerts.ENGAGEMENT_PERMS_TITLE) — the first Actions run is
the live probe of whether the token carries the needed permissions.
"""
import datetime as dt
import json
import random

from . import alerts, instagram, state
from .config import CONTENT_DIR, load_config

# Bounds so an hourly run can never spam: at most this many replies per run.
MAX_COMMENT_REPLIES = 10
MAX_DM_REPLIES = 4
# Meta only allows messaging a user within 24h of their last message.
DM_WINDOW_HOURS = 24
# How many recent published media objects to sweep for comments.
COMMENT_MEDIA_LIMIT = 5


def load_bank() -> dict:
    with open(CONTENT_DIR / "replies.json", encoding="utf-8") as f:
        return json.load(f)


def _own_usernames(bank: dict, cfg: dict) -> set[str]:
    """Lowercase, @-stripped usernames that must never get a reply."""
    names = {str(u).lstrip("@").lower() for u in bank.get("no_reply_users", [])}
    handle = str(cfg.get("handle", "")).lstrip("@").lower()
    if handle:
        names.add(handle)
    return names


def categorize(bank: dict, text: str) -> dict:
    """First category whose keywords appear in the text; general fallback.

    Order in replies.json is the priority order (love before friendship
    before general), so "lets be friends love you" matches love first.
    """
    lowered = text.lower()
    for cat in bank.get("categories", []):
        if any(kw.lower() in lowered for kw in cat.get("keywords", [])):
            return cat
    # Default: the category keyed "general", else the last one.
    for cat in bank.get("categories", []):
        if cat.get("key") == "general":
            return cat
    cats = bank.get("categories", [])
    return cats[-1] if cats else {"key": "general", "comment_replies": [],
                                  "dm_replies": []}


def _pick_replies(cat: dict, kind: str, seed: str) -> list[str]:
    """Deterministic rotation: shuffled by seed so neighbors differ, stable
    across re-runs (same seed -> same order)."""
    entries = cat.get(kind, [])
    if not entries:
        return []
    rng = random.Random(f"{kind}:{seed}")
    order = list(range(len(entries)))
    rng.shuffle(order)
    return [entries[i] for i in order]


def _permission_problem(err_text: str) -> bool:
    """Heuristic: does this API error look like a missing permission?"""
    low = err_text.lower()
    return ("permission" in low or "admin approval" in low
            or "#10" in low or "#200" in low or "authorize" in low)


def _note(state_obj: dict, today_str: str, message: str) -> None:
    print(f"engagement: {message}")
    state.note_failure(state_obj, today_str, "engagement", message)


# --- Comments -------------------------------------------------------------------


def _recent_media_ids(st: dict, limit: int = COMMENT_MEDIA_LIMIT) -> list[str]:
    """Newest-first published media ids (feed/story/reel) from state.json."""
    ids: list[str] = []
    for date_str in sorted(st.get("days", {}).keys(), reverse=True):
        pm = st.get("days", {}).get(date_str, {}).get("published_media")
        if not isinstance(pm, dict):
            continue
        for kind in ("feed", "reel", "story"):
            mid = pm.get(kind)
            if mid and mid not in ids:
                ids.append(str(mid))
        if len(ids) >= limit:
            break
    return ids[:limit]


def reply_to_comments(cfg: dict, st: dict, bank: dict,
                     today_str: str) -> tuple[int, str | None]:
    """Answer new comments on recent posts. Returns (replied, perm_problem)."""
    token = cfg["access_token"]
    replied = set(state.replied_comments(st))
    own = _own_usernames(bank, cfg)
    count = 0

    for mid in _recent_media_ids(st):
        try:
            comments = instagram.list_comments(token, mid)
        except instagram.InstagramError as e:
            msg = str(e)
            _note(st, today_str, f"list comments on {mid} failed — {msg}")
            return count, "comments" if _permission_problem(msg) else None
        for c in comments:
            if count >= MAX_COMMENT_REPLIES:
                return count, None
            cid = str(c.get("id", ""))
            if not cid or cid in replied:
                continue
            if str(c.get("username", "")).lstrip("@").lower() in own:
                continue
            text = str(c.get("text", ""))
            cat = categorize(bank, text)
            rotation = _pick_replies(cat, "comment_replies", cid)
            if not rotation:
                continue
            # rotation is seeded by the comment id, so every comment gets
            # its own (stable, idempotent) pick from the bank.
            reply = rotation[0]
            try:
                instagram.reply_to_comment(token, cid, reply)
            except instagram.InstagramError as e:
                msg = str(e)
                _note(st, today_str, f"reply to comment {cid} failed — {msg}")
                if _permission_problem(msg):
                    return count, "comments"
                continue
            state.mark_comment_replied(st, cid)
            replied.add(cid)
            state.save(st)  # save per reply: crash-safe idempotency
            count += 1
            print(f"engagement: replied to comment {cid} "
                  f"({cat['key']}) — {reply[:40]}…")
    return count, None


# --- Direct messages -------------------------------------------------------------


def _parse_time(value) -> dt.datetime | None:
    """Parse Graph API created_time (ISO 8601, varied shapes)."""
    if not value:
        return None
    text = str(value).replace("Z", "+00:00").replace("+0000", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


def _participant_igsid(thread: dict, own: set[str]) -> str | None:
    """The other party's IGSID from the thread's users list."""
    for user in thread.get("users", []) or []:
        if not isinstance(user, dict):
            continue
        if str(user.get("username", "")).lstrip("@").lower() in own:
            continue
        igsid = user.get("id")
        if igsid:
            return str(igsid)
    return None


def answer_dms(cfg: dict, st: dict, bank: dict,
               today_str: str) -> tuple[int, str | None]:
    """Reply to unanswered incoming DMs (within 24h). Returns (sent, perm)."""
    token = cfg["access_token"]
    own = _own_usernames(bank, cfg)
    done_today = set(state.dm_replied_today(st, today_str))
    count = 0

    try:
        threads = instagram.list_conversations(token, cfg["ig_user_id"])
    except instagram.InstagramError as e:
        msg = str(e)
        _note(st, today_str, f"list conversations failed — {msg}")
        return 0, "messages" if _permission_problem(msg) else None

    for thread in threads:
        if count >= MAX_DM_REPLIES:
            break
        tid = str(thread.get("id", ""))
        if not tid or tid in done_today:
            continue
        msgs = (thread.get("messages", {}) or {}).get("data", []) or []
        if not msgs:
            continue
        last = max(msgs, key=lambda m: str(m.get("created_time", "")))
        # Unanswered = the newest message is incoming, not from us.
        sender = (last.get("from", {}) or {}).get("username", "")
        if str(sender).lstrip("@").lower() in own:
            continue
        # 24h messaging window (Meta hard-rejects anything older).
        created = _parse_time(last.get("created_time"))
        if created is None:
            continue  # can't prove it's fresh — never risk the window
        age_h = (dt.datetime.now(dt.timezone.utc) - created).total_seconds() / 3600
        if age_h > DM_WINDOW_HOURS:
            continue

        igsid = _participant_igsid(thread, own)
        if not igsid:
            _note(st, today_str,
                  f"thread {tid}: no participant IGSID found — skipped")
            continue

        cat = categorize(bank, str(last.get("text", "")))
        rotation = _pick_replies(cat, "dm_replies", tid)
        if not rotation:
            continue
        try:
            instagram.send_message(token, cfg["ig_user_id"], igsid, rotation[0])
        except instagram.InstagramError as e:
            msg = str(e)
            _note(st, today_str, f"reply to thread {tid} failed — {msg}")
            if _permission_problem(msg):
                return count, "messages"
            continue
        state.mark_dm_replied(st, tid)
        state.mark_dm_thread_replied(st, today_str, tid)
        state.save(st)
        done_today.add(tid)
        count += 1
        print(f"engagement: answered DM thread {tid} "
              f"({cat['key']}) — {rotation[0][:40]}…")
    return count, None


# --- Entry point -----------------------------------------------------------------


def run() -> None:
    cfg = load_config()
    if not cfg.get("access_token") or not cfg.get("ig_user_id"):
        print("engagement: IG_ACCESS_TOKEN/IG_USER_ID not set — skipping")
        return
    today_str = dt.date.today().isoformat()
    st = state.load()
    bank = load_bank()

    n_comments, perm_c = reply_to_comments(cfg, st, bank, today_str)
    n_dms, perm_m = answer_dms(cfg, st, bank, today_str)

    permissions = sorted(
        {p for p in (perm_c, perm_m) if p}
    )
    st["engagement"] = {
        "permissions_missing": permissions,
        "last_run": f"{n_comments} comment replies, {n_dms} DM replies",
    }
    state.save(st)
    print(f"engagement: done — {n_comments} comment(s), {n_dms} DM(s)"
          + (f", MISSING: {', '.join(permissions)}" if permissions else ""))

    # Reconcile exactly the one permission issue (never the day-incomplete
    # ones — the hourly cadence must not open those before the 9 AM post).
    if cfg.get("gh_pat"):
        try:
            alerts.reconcile(
                cfg, [alerts.evaluate_engagement(st)], today_str, False)
        except Exception as e:
            print(f"engagement: permission alert skipped — {e}")


if __name__ == "__main__":
    run()
