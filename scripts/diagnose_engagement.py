"""Read-only dump of what the Graph API sees: media, comments, DM threads.

Print-only — no replies, no writes. Exists to tell "nothing arrived" apart
from "the agent cannot see it", which the engagement log alone can't do:
it reports counts, not who wrote what.

Privacy: this workflow's log is PUBLIC (public repo → public Actions logs).
Comment/DM text and usernames are masked by default; set
DIAGNOSE_SHOW_TEXT=1 (env or workflow env) to print them, and only when the
log will not be shared.
"""
import os
import sys

from src import instagram

SHOW_TEXT = os.environ.get("DIAGNOSE_SHOW_TEXT", "") == "1"


def mask(text, width: int = 40) -> str:
    """repr-truncate when opt-in; a fixed placeholder otherwise."""
    if SHOW_TEXT:
        t = str(text or "")
        return repr(t[:width] + "…" if len(t) > width else t)
    return "'<redacted — set DIAGNOSE_SHOW_TEXT=1>'"


def mask_user(name) -> str:
    if SHOW_TEXT:
        return f"@{name}"
    return "@<redacted>"

token = os.environ.get("IG_ACCESS_TOKEN", "")
uid = os.environ.get("IG_USER_ID", "")
if not token or not uid:
    sys.exit("missing IG_ACCESS_TOKEN / IG_USER_ID")

me = instagram.token_info(token)
print(f"account: @{me.get('username')} (id={me.get('user_id')})")
print(f"IG_USER_ID secret: {uid}\n")

media = instagram.list_media(token, uid, limit=10)
print(f"media ({len(media)}):")
for m in media:
    print(" ", m.get("id"))

print("\ncomments:")
for m in media:
    mid = str(m.get("id"))
    try:
        comments = instagram.list_comments(token, mid)
    except Exception as e:
        print(f"  {mid} -> {e}")
        continue
    for c in comments:
        cid = str(c.get("id"))
        print(f"  {mid}  {mask_user(c.get('username'))}  {mask(c.get('text'))}  "
              f"{c.get('timestamp')}")
        try:
            replies = instagram.list_comment_replies(token, cid)
        except Exception as e:
            print(f"      replies -> {e}")
            continue
        for r in replies:
            print(f"      reply: {mask_user(r.get('username'))} {mask(r.get('text'))}")

print("\nconversations:")
threads = instagram.list_conversations(token, uid)
print(f"  count={len(threads)}")
for t in threads:
    users = [u.get("username") for u in t.get("users") or []]
    print(f"  {t.get('id')} users={[mask_user(u) for u in users]} "
          f"updated={t.get('updated_time')}")
    for msg in (t.get("messages") or {}).get("data") or []:
        print(f"     {msg.get('created_time')} "
              f"{mask_user((msg.get('from') or {}).get('username'))}: "
              f"{mask(msg.get('text'))}")
