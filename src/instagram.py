"""Instagram Graph API client (Instagram Login host: graph.instagram.com).

Implements the official Content Publishing flow:
  1. POST /{ig-user-id}/media           -> container id
  2. GET  /{container-id}?fields=status_code  (poll once/min, <=5 min)
  3. POST /{ig-user-id}/media_publish   -> published media id
"""
import time

import requests

GRAPH_HOST = "https://graph.instagram.com"
API_VERSION = "v23.0"
BASE = f"{GRAPH_HOST}/{API_VERSION}"
TIMEOUT = 30


class InstagramError(Exception):
    pass


def _check(resp: requests.Response) -> dict:
    if resp.status_code >= 400:
        try:
            err = resp.json().get("error", {})
        except ValueError:
            err = {}
        raise InstagramError(
            f"HTTP {resp.status_code}: {err.get('message', resp.text[:300])}"
        )
    return resp.json()


def get_user_id(token: str) -> str:
    """Resolve the IG professional account id from /me."""
    r = requests.get(
        f"{BASE}/me",
        params={"fields": "user_id", "access_token": token},
        timeout=TIMEOUT,
    )
    data = _check(r)
    uid = data.get("user_id") or data.get("id")
    if not uid:
        raise InstagramError(f"/me returned no user id: {data}")
    return str(uid)


def create_container(token: str, ig_user_id: str, image_url: str | None = None,
                     video_url: str | None = None,
                     media_type: str | None = None,
                     caption: str | None = None) -> str:
    """Step 1: create a media container.

    media_type STORIES for stories, REELS for reels (video_url then)."""
    if not image_url and not video_url:
        raise InstagramError("create_container needs image_url or video_url")
    params: dict = {"access_token": token}
    if video_url:
        params["video_url"] = video_url
    else:
        params["image_url"] = image_url
    if media_type:
        params["media_type"] = media_type
    if caption:
        params["caption"] = caption
    r = requests.post(f"{BASE}/{ig_user_id}/media", data=params, timeout=TIMEOUT)
    data = _check(r)
    cid = data.get("id")
    if not cid:
        raise InstagramError(f"container creation returned no id: {data}")
    return str(cid)


def wait_finished(token: str, container_id: str,
                 max_wait_s: int = 300, poll_s: int = 30) -> str:
    """Step 2: poll status_code until FINISHED (or raise).

    Video containers (Reels/long Story video) can take several minutes to
    transcode, so callers pass a longer max_wait_s for video media.
    """
    deadline = time.time() + max_wait_s
    last = ""
    while time.time() < deadline:
        r = requests.get(
            f"{BASE}/{container_id}",
            params={"fields": "status_code", "access_token": token},
            timeout=TIMEOUT,
        )
        status = _check(r).get("status_code", "")
        last = status
        if status == "FINISHED":
            return status
        if status in ("EXPIRED", "ERROR"):
            raise InstagramError(f"container {container_id} status {status}")
        time.sleep(poll_s)
    raise InstagramError(
        f"container {container_id} not FINISHED after {max_wait_s}s (last={last})"
    )


def publish(token: str, ig_user_id: str, container_id: str) -> str:
    """Step 3: publish a finished container."""
    r = requests.post(
        f"{BASE}/{ig_user_id}/media_publish",
        data={"creation_id": container_id, "access_token": token},
        timeout=TIMEOUT,
    )
    data = _check(r)
    mid = data.get("id")
    if not mid:
        raise InstagramError(f"publish returned no media id: {data}")
    return str(mid)


def video_duration_seconds(url: str, local_path=None) -> float | None:
    """ffprobe a video's duration via the system ffmpeg (preinstalled on
    ubuntu runners). Returns None if probing fails (caller decides)."""
    import subprocess as _sp
    target = ["-i", str(local_path)] if local_path else ["-i", url]
    try:
        out = _sp.run(
            ["ffprobe", "-v", "error", *target, "-show_entries",
             "format=duration", "-of", "default=noprint_wrappers=1:nokey=1"],
            capture_output=True, text=True, timeout=60, check=True,
        ).stdout.strip()
        return float(out)
    except Exception:
        return None


def publishing_limit(token: str, ig_user_id: str) -> dict:
    """GET content_publishing_limit — quota info for the 24h window."""
    r = requests.get(
        f"{BASE}/{ig_user_id}/content_publishing_limit",
        params={"access_token": token},
        timeout=TIMEOUT,
    )
    return _check(r)


def refresh_token(token: str) -> str:
    """Refresh a long-lived Instagram User token. Returns the new token."""
    r = requests.get(
        f"{GRAPH_HOST}/refresh_access_token",
        params={"grant_type": "th_refresh_token", "access_token": token},
        timeout=TIMEOUT,
    )
    data = _check(r)
    new = data.get("access_token")
    if not new:
        raise InstagramError(f"refresh returned no token: {list(data.keys())}")
    return new


def token_info(token: str) -> dict:
    """GET /me?fields=expires — token metadata for expiry tracking."""
    r = requests.get(
        f"{GRAPH_HOST}/me",  # unversioned works for token introspection
        params={"fields": "expires,expires_in,user_id", "access_token": token},
        timeout=TIMEOUT,
    )
    return _check(r)
