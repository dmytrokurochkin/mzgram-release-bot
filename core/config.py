import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

# Variables set by systemd (or the shell) win over .env
load_dotenv(BASE_DIR / ".env", override=False)


def _list(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _bool(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes", "on")


def _chat_id(value: str) -> int | str:
    value = value.strip()
    return int(value) if value.lstrip("-").isdigit() else value


BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHANNEL_ID = _chat_id(os.getenv("CHANNEL_ID", ""))
# The local telegram-bot-api server: files up to 2000 MB instead of 50 MB
BOT_API_URL = os.getenv("BOT_API_URL", "http://127.0.0.1:8081").strip()
ADMIN_IDS = [int(item) for item in _list(os.getenv("ADMIN_IDS", "")) if item.isdigit()]

REPOS = _list(os.getenv("REPOS", "dmytrokurochkin/MZGram-Android,dmytrokurochkin/MZGram-Desktop"))
POLL_INTERVAL = max(1, int(os.getenv("POLL_INTERVAL", "300")))
PUBLISH_PRERELEASES = _bool(os.getenv("PUBLISH_PRERELEASES", "false"))
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip() or None
GITHUB_API_URL = os.getenv("GITHUB_API_URL", "https://api.github.com").strip().rstrip("/")

DATA_DIR = Path(os.getenv("DATA_DIR", str(BASE_DIR / "data")))
DB_PATH = DATA_DIR / "releases.db"
DOWNLOAD_DIR = DATA_DIR / "downloads"
LOCK_PATH = DATA_DIR / "bot.lock"

# Upload of up to 10 files of up to 2000 MB each to the local Bot API server
UPLOAD_TIMEOUT = int(os.getenv("UPLOAD_TIMEOUT", "7200"))


def missing() -> list[str]:
    names = []
    if not BOT_TOKEN:
        names.append("BOT_TOKEN")
    if CHANNEL_ID == "":
        names.append("CHANNEL_ID")
    if not REPOS:
        names.append("REPOS")
    return names
