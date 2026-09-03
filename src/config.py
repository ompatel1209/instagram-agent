"""Configuration loading: config.json merged with environment secrets."""
import json
import os
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.json"
STATE_PATH = ROOT / "state.json"
CONTENT_DIR = ROOT / "content"
FONTS_DIR = ROOT / "assets" / "fonts"
PREVIEW_DIR = ROOT / "preview"


def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = json.load(f)

    # Secrets come from the environment (GitHub Actions / local shell),
    # never from config.json — that file is committed to a public repo.
    cfg["access_token"] = os.environ.get("IG_ACCESS_TOKEN", "")
    cfg["ig_user_id"] = os.environ.get("IG_USER_ID", "")
    cfg["gh_pat"] = os.environ.get("GH_PAT", "")
    # Stock-photo tier (Pexels). Empty key = tier silently disabled.
    cfg["pexels_api_key"] = os.environ.get("PEXELS_API_KEY", "")

    # GitHub repo coordinates, used to build public media URLs and push paths.
    cfg.setdefault("repo_owner", os.environ.get("GITHUB_REPOSITORY_OWNER", ""))
    cfg.setdefault("repo_name", os.environ.get("GITHUB_REPOSITORY", "").split("/")[-1] or "")
    return cfg


def media_url(cfg: dict, date_str: str, kind: str) -> str:
    """Public raw.githubusercontent URL for a rendered media file."""
    return (
        f"https://raw.githubusercontent.com/{cfg['repo_owner']}/{cfg['repo_name']}"
        f"/media/{date_str}-{kind}.jpg"
    )
