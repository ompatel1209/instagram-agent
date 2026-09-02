"""Token lifecycle: refresh + write the fresh token back to GitHub Secrets."""
import subprocess

from . import instagram
from .state import note_token_expiry


def days_left(token: str) -> tuple[int | None, str]:
    """Best-effort expiry lookup; returns (days, iso) or (None, '')."""
    try:
        info = instagram.token_info(token)
    except Exception:
        return None, ""
    expires_in = info.get("expires_in")
    if expires_in is None:
        return None, ""
    import datetime as dt

    exp = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=int(expires_in))
    days = int(expires_in // 86400)
    return days, exp.date().isoformat()


def refresh_and_store(token: str, gh_pat: str, repo: str) -> tuple[str, bool]:
    """Refresh the long-lived token; if a PAT is available, update the secret.

    Returns (token_to_use, secret_updated). Falls back to the old token if
    refresh fails, so publishing is never blocked by token housekeeping.
    """
    try:
        new_token = instagram.refresh_token(token)
    except Exception:
        # Refresh failures must not stop publishing; run continues with old.
        return token, False

    secret_updated = False
    if gh_pat:
        try:
            subprocess.run(
                ["gh", "secret", "set", "IG_ACCESS_TOKEN", "--repo", repo,
                 "--body", new_token],
                check=True, timeout=60,
                env={"GH_TOKEN": gh_pat, "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", "HOME": "/home/runner"},
            )
            secret_updated = True
        except Exception:
            secret_updated = False
    return new_token, secret_updated
