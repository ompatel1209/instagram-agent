"""Read-only dump of what the Graph API sees: media, comments, DM threads.

Print-only — no replies, no writes. Exists to tell "nothing arrived" apart
from "the agent cannot see it", which the engagement log alone can't do:
it reports counts, not who wrote what.
"""
import os
import sys

from src import instagram

token = os.environ.get("IG_ACCESS_TOKEN", "")
uid = os.environ.get("IG_USER_ID", "")
if not token or not uid:
    sys.exit("missing IG_ACCESS_TOKEN / IG_USER_ID")

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
        print(f"  {mid}  @{c.get('username')}  {c.get('text')!r}  "
              f"{c.get('timestamp')}")
        try:
            replies = instagram.list_comment_replies(token, cid)
        except Exception as e:
            print(f"      replies -> {e}")
            continue
        for r in replies:
            print(f"      reply: @{r.get('username')} {r.get('text')!r}")

print("\nconversations:")
threads = instagram.list_conversations(token, uid)
print(f"  count={len(threads)}")
for t in threads:
    users = [u.get("username") for u in t.get("users") or []]
    print(f"  {t.get('id')} users={users} updated={t.get('updated_time')}")
    for msg in (t.get("messages") or {}).get("data") or []:
        print(f"     {msg.get('created_time')} "
              f"{(msg.get('from') or {}).get('username')}: "
              f"{msg.get('text')!r}")
