"""Token lifecycle: refresh + write the fresh token back to GitHub Secrets.

The refresh endpoint returns a NEW long-lived token (valid ~60 days) while
the OLD one in the IG_ACCESS_TOKEN secret keeps its own countdown — so a
refresh only helps future runs if the secret is actually updated. Both
halves happen here and their outcomes travel in the returned dict so the
run can record/alert on a half-done refresh (today works, the secret ages,
the agent dies on a fixed date).
"""
import datetime as dt
import os
import subprocess

from . import instagram


def days_left(token: str) -> tuple[int | None, str]:
    """Best-effort expiry lookup; returns (days, iso) or (None, '').

    /me reports `expires` as a unix timestamp; the floor of the remaining
    timedelta alarms slightly early, which is the safe direction.
    """
    try:
        info = instagram.token_info(token)
    except Exception:
        return None, ""
    expires_ts = info.get("expires")
    if expires_ts is None:
        return None, ""
    exp = dt.datetime.fromtimestamp(int(expires_ts), tz=dt.timezone.utc)
    days = (exp - dt.datetime.now(dt.timezone.utc)).days
    return days, exp.date().isoformat()


def _write_secret(gh_pat: str, repo: str, new_token: str) -> tuple[bool, str]:
    """Update the IG_ACCESS_TOKEN secret via `gh secret set`.

    The token travels over stdin (gh reads the body from a pipe), never in
    argv or logs. Returns (ok, reason) — the reason is safe to print: gh's
    stderr reports the failing API call, never the secret body.
    """
    try:
        r = subprocess.run(
            ["gh", "secret", "set", "IG_ACCESS_TOKEN", "--repo", repo],
            input=new_token, capture_output=True, text=True, timeout=90,
            env={"GH_TOKEN": gh_pat,
                 "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
                 "HOME": os.environ.get("HOME") or "/home/runner"},
        )
    except FileNotFoundError:
        return False, "gh CLI not found"
    except subprocess.TimeoutExpired:
        return False, "gh secret set timed out (90s)"
    if r.returncode == 0:
        return True, ""
    err = (r.stderr or r.stdout or "").strip()
    reason = err.splitlines()[-1] if err else f"exit code {r.returncode}"
    return False, f"gh secret set failed: {reason[:300]}"


def refresh_and_store(token: str, gh_pat: str, repo: str) -> dict:
    """Refresh the long-lived token; update the IG_ACCESS_TOKEN secret.

    Never raises — publishing is never blocked by token housekeeping.
    Returns an outcome dict:
      token          token to use this run (the old one if refresh failed)
      refreshed      Meta returned a new token
      secret_updated the new token was written to GitHub Secrets
      reason         failure note for state/alerting ("" when healthy)
    """
    out = {"token": token, "refreshed": False, "secret_updated": False,
           "reason": ""}

    try:
        new_token = instagram.refresh_token(token)
    except instagram.InstagramError as e:
        # Meta rejected the refresh — usually an expired/invalid token.
        # Publishing will fail downstream; the reason is recorded for alerting.
        out["reason"] = f"refresh rejected: {e}"[:300]
        return out
    except Exception as e:  # network blip etc. — the run continues on the old token
        out["reason"] = f"refresh failed: {e}"[:300]
        return out

    out["token"] = new_token
    out["refreshed"] = True

    if not gh_pat:
        out["reason"] = "refreshed but GH_PAT unset — IG_ACCESS_TOKEN secret NOT updated"
        return out
    ok, why = _write_secret(gh_pat, repo, new_token)
    out["secret_updated"] = ok
    if not ok:
        out["reason"] = f"refreshed but secret NOT updated — {why}"[:300]
    return out
