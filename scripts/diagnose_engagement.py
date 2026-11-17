"""Read-only dump of what the Graph API sees: media, comments, DM threads.

Print-only — no replies, no writes. Exists to tell "nothing arrived" apart
from "the agent cannot see it", which the engagement log alone can't do:
it reports counts, not who wrote what.

Privacy: this workflow's log is PUBLIC (public repo → public Actions logs).
Comment/DM text and usernames are masked by default; set
DIAGNOSE_SHOW_TEXT=1 (env or workflow env) to print them, and only when the
log will not be shared.
"""
import json
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

# Insights metric probe: find which metric names this token/host actually
# accepts, per media product type (feed image vs reel), so src/insights.py
# can request the right set. Also probes the exact ids state.json records
# as published, to tell "old post, insights gone" from "wrong metrics".
print("\ninsights metric probe (per media type):")
try:
    typed = instagram._get(
        f"{instagram.BASE}/{uid}/media",
        params={"fields": "id,media_type", "limit": 20,
                "access_token": token}, timeout=30)
    media = typed.json().get("data", [])
except Exception as e:
    print(f"  typed media lookup failed: {e}")
    media = []
seen_types = set()
for m in media:
    mtype = str(m.get("media_type", ""))
    if mtype in seen_types:
        continue
    seen_types.add(mtype)
    mid = str(m.get("id"))
    for metric_set in ("reach,likes,comments,saved", "views,reach", "views"):
        try:
            out = instagram.media_insights(token, mid, metric_set)
            print(f"  OK   {mtype} [{metric_set}] -> {out}")
        except Exception as e:
            print(f"  FAIL {mtype} [{metric_set}] -> {e}")

# Per-id probe over the exact ids the agent published (from state.json).
print("\ninsights probe over state-recorded ids:")
try:
    st = json.load(open("state.json", encoding="utf-8"))
except Exception as e:
    st = {}
    print(f"  state.json unreadable: {e}")
for dstr in sorted(st.get("days", {}), reverse=True)[:7]:
    pm = st["days"][dstr].get("published_media")
    if not isinstance(pm, dict):
        continue
    for kind in ("feed", "reel"):
        mid = pm.get(kind)
        if not mid:
            continue
        try:
            meta = instagram._get(
                f"{instagram.BASE}/{mid}",
                params={"fields": "media_type,media_product_type",
                        "access_token": token}, timeout=30).json()
        except Exception as e:
            print(f"  {dstr} {kind} {mid} meta FAIL: {e}")
            continue
        try:
            out = instagram.media_insights(token, str(mid),
                                           "reach,likes,comments,saved")
            print(f"  {dstr} {kind} {mid} {meta} OK -> {out}")
        except Exception as e:
            print(f"  {dstr} {kind} {mid} {meta} FAIL -> {e}")

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
