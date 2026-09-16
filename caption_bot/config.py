"""Configuration.

Everything that affects CPU/RAM usage is exposed as an environment
variable so the bot can be tuned down for very small machines without
touching the code.
"""

import os
from pathlib import Path

from dotenv import load_dotenv


# Project root:
# ~/Caption-Media-Cover-Telegram-Bot/
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Always load the root .env regardless of where the bot is started from.
ENV_FILE = PROJECT_ROOT / ".env"
load_dotenv(ENV_FILE)


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)

    if raw is None:
        return default

    return raw.strip().lower() in {"1", "true", "yes", "on"}


API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]

MAX_BATCH_SIZE = _int("MAX_BATCH_SIZE", 100)
MAX_CAPTION_LENGTH = 1024
SESSION_NAME = os.getenv("SESSION_NAME", "caption_bot")

# ---------------------------------------------------------------------------
# Runtime tuning (low-hardware knobs)
# ---------------------------------------------------------------------------

# Pyrogram update-dispatcher tasks. This bot is single-user and every
# handler is I/O bound, so one worker is enough and keeps both the task
# count and the peak memory down. Raise it only if you run heavy
# concurrent batches.
WORKERS = max(1, _int("WORKERS", 1))

# Pyrogram's parallel up/download slots. Nothing is ever downloaded or
# uploaded here (everything is file_id based), so this can stay at the
# minimum instead of allocating session slots we never use.
MAX_CONCURRENT_TRANSMISSIONS = max(
    1,
    _int("MAX_CONCURRENT_TRANSMISSIONS", 1),
)

# How long to wait for an incoming media burst to go quiet before
# analysing it as one batch.
COLLECTION_DELAY = _float("COLLECTION_DELAY", 2.5)

# Pause between messages while /transfer resends an archived season.
SEND_DELAY = _float("SEND_DELAY", 1.2)

# Logging: WARNING keeps disk/console I/O near zero on SD-card setups.
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

# Use uvloop when it happens to be installed (optional, not required).
USE_UVLOOP = _bool("USE_UVLOOP", True)

# Keep the Pyrogram session in RAM instead of a SQLite file. Saves disk
# writes, but the peer cache is rebuilt on every restart.
SESSION_IN_MEMORY = _bool("SESSION_IN_MEMORY", False)

# ---------------------------------------------------------------------------
# Local archive
# ---------------------------------------------------------------------------

# Local SQLite file backing the /transfer archive (finished seasons/episodes
# and generated transfer links). Everything lives on disk next to the bot —
# no external database required.
DB_PATH = Path(
    os.getenv(
        "DB_PATH",
        str(PROJECT_ROOT / "bot_data.sqlite3"),
    )
)

# SQLite page cache in KiB. Negative values mean "KiB" to SQLite; the
# archive is tiny, so 2 MiB is plenty and avoids the 20 MiB default-ish
# growth on bigger builds.
DB_CACHE_KIB = _int("DB_CACHE_KIB", 2048)
