"""Self-monitoring: turn run outcomes into GitHub issues the user can see.

The agent runs unattended at 9 AM; when something breaks — a day never
completes, the token ages toward death, the token refresh stops reaching
the GitHub secret, the uploads queue runs dry, or the access token
disappears entirely — state.json records it but nobody reads state.json.
This module reconciles a small set of `ig-agent:` issues instead: each
alert condition opens (or keeps open) exactly one issue, and the condition
resolving closes it with a note, so recovery needs no human cleanup.

Everything here is best-effort: sync() never raises, and alerting can never
kill a publishing run. Bodies carry only state.json data (which the public
repo already exposes) — never the access token.
"""
import datetime as dt

import requests

from . import state, uploads

TITLE_PREFIX = "ig-agent: "
TOKEN_EXPIRY_TITLE = TITLE_PREFIX + "IG token expires soon"
TOKEN_REFRESH_TITLE = TITLE_PREFIX + "daily token refresh is failing"
QUEUE_LOW_TITLE = TITLE_PREFIX + "uploads queue running low"
NOT_CONFIGURED_TITLE = TITLE_PREFIX + "agent is not configured"

TOKEN_ALERT_DAYS = 7    # alert when the token has fewer days left than this
QUEUE_LOW_FILES = 2     # alert when at most this many uploads remain
RECENT_REFRESH_DAYS = 2 # refresh problems this recent still count as current


def _log(msg: str) -> None:
    print(f"[ig-agent] {msg}", flush=True)


def _incomplete_title(date_str: str) -> str:
    return f"{TITLE_PREFIX}{date_str} post incomplete"


def _incomplete_date(title: str) -> str | None:
    """Extract the date from an incomplete-day issue title, if it is one."""
    if not (title.startswith(TITLE_PREFIX)
            and title.endswith(" post incomplete")):
        return None
    mid = title[len(TITLE_PREFIX):-len(" post incomplete")]
    try:
        dt.date.fromisoformat(mid)
    except ValueError:
        return None
    return mid


def _refreshed_recently(st: dict, date_str: str) -> bool:
    """True when a token refresh succeeded on this day or the last two.

    A refresh hands out a fresh ~60-day token, so a stale pre-refresh
    days_left value must not trigger a false "expires soon" alarm.
    """
    today = dt.date.fromisoformat(date_str)
    for n in range(RECENT_REFRESH_DAYS + 1):
        day = (today - dt.timedelta(days=n)).isoformat()
        if state.token_refreshed_today(st, day):
            return True
    return False


def evaluate(st: dict, date_str: str, queue: list[str] | None,
             token_configured: bool = True) -> list[dict]:
    """Compute the desired state of each alert (pure: no network, no writes).

    Every entry is {title, open, body?, resolve_note?}: `open` says whether
    an issue with that title SHOULD exist; reconcile() makes GitHub match.
    """
    out = []

    # 0. Agent not configured — the completely-silent death. If the
    # IG_ACCESS_TOKEN secret is ever deleted, runs exit 0 with no state
    # changes, so nothing else would ever alert. This condition is the tripwire.
    a = {"title": NOT_CONFIGURED_TITLE, "open": not token_configured}
    if token_configured:
        a["resolve_note"] = "IG_ACCESS_TOKEN secret is set again."
    else:
        a["body"] = (
            "The IG_ACCESS_TOKEN secret is not set — the agent is skipping "
            "every run (posting nothing) while still exiting green.\n\n"
            "A GitHub Actions log line 'IG_ACCESS_TOKEN secret is not set' "
            "confirms it. Fix: generate a long-lived token from the Meta "
            "Graph API (the original setup flow) and set it as the "
            "IG_ACCESS_TOKEN secret."
        )
    out.append(a)

    # 1. Day incomplete — one issue per date; closes when the day completes.
    drec = st.get("days", {}).get(date_str, {})
    complete = state.both_published(st, date_str)
    a = {"title": _incomplete_title(date_str), "open": not complete}
    if complete:
        a["resolve_note"] = f"{date_str} recovered — feed + story published."
    else:
        missing = [s for s in ("publish_feed", "publish_story")
                   if not drec.get(s)]
        lines = [f"The automatic Instagram post for {date_str} did not "
                 "finish.", "",
                 f"Missing steps: {', '.join(missing)}.", ""]
        failures = [f for f in drec.get("failures", [])
                    if isinstance(f, dict)][-5:]
        if failures:
            lines.append("Recent failures (from state.json):")
            for f in failures:
                lines.append(f"- {f.get('where', '?')}: "
                             f"{f.get('message', '')}")
        tr = drec.get("token_refresh")
        if isinstance(tr, dict) and tr.get("reason"):
            lines.append(f"- token refresh: {tr['reason']}")
        lines += ["",
                  "The 9:18 AM backup and 3:04 PM safety runs retry "
                  "automatically; this issue closes itself if the day "
                  "completes. To retry sooner, use the workflow's "
                  "'Run workflow' button with this date."]
        a["body"] = "\n".join(lines)
    out.append(a)

    # 2. Token expiry — one stable issue; opens under a week of runway.
    tok = st.get("token") if isinstance(st.get("token"), dict) else {}
    days_left = tok.get("days_left")
    a = {"title": TOKEN_EXPIRY_TITLE, "open": False}
    if (isinstance(days_left, int) and days_left < TOKEN_ALERT_DAYS
            and not _refreshed_recently(st, date_str)):
        a["open"] = True
        a["body"] = (f"The Instagram access token expires in {days_left} "
                     f"day(s) (on {tok.get('expires', 'unknown')}).\n\n"
                     "The daily auto-refresh should keep this near ~55; "
                     "when it keeps dropping instead, the refresh is "
                     "failing — see the token-refresh issue and state.json."
                     "\n\n"
                     "If the token dies completely, a new one must be "
                     "generated from the Meta Graph API (the original setup "
                     "flow) and set as the IG_ACCESS_TOKEN secret.")
    else:
        note = "token healthy"
        if isinstance(days_left, int):
            note += f" ({days_left} days left)"
        a["resolve_note"] = note + "."
    out.append(a)

    # 3. Token refresh failing to reach the secret — the fixed-death-date risk.
    # Only dangerous failures count: a refresh that SUCCEEDED at Meta but
    # could not be written back to the IG_ACCESS_TOKEN secret ("refreshed but
    # …"). Meta rejecting a <24h-old token is routine and harmless.
    cutoff = (dt.date.fromisoformat(date_str)
              - dt.timedelta(days=RECENT_REFRESH_DAYS)).isoformat()
    recent = [i for i in state.token_refresh_issues(st)
              if i.get("date", "") >= cutoff
              and str(i.get("reason", "")).startswith("refreshed but")]
    a = {"title": TOKEN_REFRESH_TITLE, "open": False}
    if recent and not state.token_refreshed_today(st, date_str):
        a["open"] = True
        lines = ["Meta issued a fresh token but it could NOT be written "
                 "back to the IG_ACCESS_TOKEN GitHub secret — future runs "
                 "keep using the aging token, so the agent will stop on a "
                 "fixed date.", ""]
        for i in recent[-3:]:
            lines.append(f"- {i.get('date')}: {i.get('reason', '')}")
        lines += ["",
                  "Check that the GH_PAT secret is still valid and can "
                  "write secrets (classic PAT: repo scope)."]
        a["body"] = "\n".join(lines)
    else:
        a["resolve_note"] = ("token refresh reached the IG_ACCESS_TOKEN "
                            "secret again.")
    out.append(a)

    # 4. Uploads queue running low — the user's own photos are first priority.
    if queue is not None:
        posted = set(state.posted_files(st))
        remaining = [f for f in queue if f not in posted]
        a = {"title": QUEUE_LOW_TITLE, "open": False}
        if len(remaining) <= QUEUE_LOW_FILES:
            a["open"] = True
            a["body"] = (f"The uploads queue has {len(remaining)} file(s) "
                         "left to post. When it runs out, the agent falls "
                         "back to stock photos (Pexels) or quote graphics."
                         "\n\n"
                         "To keep your own photos/videos posting daily: "
                         "add files to the uploads/ folder and run "
                         "scripts/ingest_uploads.sh.")
        else:
            a["resolve_note"] = f"queue restocked ({len(remaining)} files)."
        out.append(a)

    return out


# --- GitHub issue reconciliation -----------------------------------------------


def _scrub(text: str, secrets: list[str]) -> str:
    """Replace any secret occurrence with [REDACTED] before it reaches the
    GitHub API (defense in depth — issue bodies are built from state.json,
    which is already public, but a leak must never get a second audience)."""
    for s in secrets:
        if s:
            text = text.replace(s, "[REDACTED]")
    return text


def _gh(cfg: dict, method: str, path: str, payload: dict | None = None):
    """Call the GitHub API as the PAT user. Header auth — the token never
    appears in a URL, an argv, or a log. Raises on any failure."""
    r = requests.request(
        method, f"https://api.github.com{path}",
        headers={"Accept": "application/vnd.github+json",
                 "Authorization": f"Bearer {cfg['gh_pat']}",
                 "X-GitHub-Api-Version": "2022-11-28"},
        json=payload, timeout=30,
    )
    r.raise_for_status()
    return r.json() if r.content else None


def _list_open(cfg: dict) -> list[dict]:
    """Open `ig-agent:` issues as [{number, title}] (PRs never included)."""
    slug = f"{cfg['repo_owner']}/{cfg['repo_name']}"
    entries = _gh(cfg, "GET",
                  f"/repos/{slug}/issues?state=open&per_page=100")
    if not isinstance(entries, list):
        return []
    return [{"number": e["number"], "title": e["title"]}
            for e in entries
            if isinstance(e, dict) and "pull_request" not in e
            and str(e.get("title", "")).startswith(TITLE_PREFIX)]


def _create(cfg: dict, title: str, body: str) -> None:
    slug = f"{cfg['repo_owner']}/{cfg['repo_name']}"
    _gh(cfg, "POST", f"/repos/{slug}/issues", {"title": title, "body": body})


def _close(cfg: dict, number: int, note: str) -> None:
    slug = f"{cfg['repo_owner']}/{cfg['repo_name']}"
    if note:
        _gh(cfg, "POST", f"/repos/{slug}/issues/{number}/comments",
            {"body": note[:2000]})
    _gh(cfg, "PATCH", f"/repos/{slug}/issues/{number}", {"state": "closed"})


def reconcile(cfg: dict, desired: list[dict], today_str: str,
              today_complete: bool) -> None:
    """Make the repo's open `ig-agent:` issues match the desired alert state."""
    issues = _list_open(cfg)
    for a in desired:
        match = next((i for i in issues if i["title"] == a["title"]), None)
        if a["open"] and match is None:
            _create(cfg, a["title"],
                    _scrub(a.get("body", ""), [cfg.get("gh_pat", ""),
                                               cfg.get("access_token", "")]))
        elif not a["open"] and match is not None:
            _close(cfg, match["number"],
                   _scrub(a.get("resolve_note", ""), [cfg.get("gh_pat", ""),
                                                      cfg.get("access_token", "")]))

    # A completed later day supersedes an older incomplete one: close it so
    # failed days don't pile up as open issues forever.
    if today_complete:
        for i in issues:
            d = _incomplete_date(i["title"])
            if d is not None and d < today_str:
                _close(cfg, i["number"],
                       _scrub(f"{today_str} posted successfully — closing. {d} was "
                              f"never completed; re-run the workflow with date {d} "
                              "if that post is still wanted.",
                              [cfg.get("gh_pat", ""),
                               cfg.get("access_token", "")]))


def sync(cfg: dict, date_str: str, enabled: bool = True,
         token_configured: bool = True) -> None:
    """Entry point from main.run()'s finally: evaluate + reconcile.

    Never raises — alerting must not kill (or mask the exit code of) a run.
    token_configured mirrors the secret check main() performed BEFORE its
    early-returns, so a deleted IG_ACCESS_TOKEN still alerts even though
    state.json was never touched that day.
    """
    if not enabled:
        return
    try:
        if not cfg["gh_pat"]:
            _log("alerts skipped: no GH_PAT — issues cannot be managed")
            return
        st = state.load()
        try:
            queue = uploads.list_queue_checked()
        except Exception:
            queue = None  # unknown queue -> no queue alert either way
        desired = evaluate(st, date_str, queue, token_configured)
        reconcile(cfg, desired, date_str,
                  state.both_published(st, date_str))
    except Exception as e:
        _log(f"alerts skipped: {e}")
