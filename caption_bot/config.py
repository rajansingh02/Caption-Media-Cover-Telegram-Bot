import os
from pathlib import Path

from dotenv import load_dotenv


# Project root:
# ~/Caption-Media-Cover-Telegram-Bot/
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Always load the root .env regardless of where the bot is started from.
ENV_FILE = PROJECT_ROOT / ".env"
load_dotenv(ENV_FILE)


API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]

MAX_BATCH_SIZE = int(os.getenv("MAX_BATCH_SIZE", "100"))
MAX_CAPTION_LENGTH = 1024
SESSION_NAME = os.getenv("SESSION_NAME", "caption_bot")

# Local SQLite file backing the /transfer archive (finished seasons/episodes
# and generated transfer links). Everything lives on disk next to the bot —
# no external database required.
DB_PATH = Path(
    os.getenv(
        "DB_PATH",
        str(PROJECT_ROOT / "bot_data.sqlite3"),
    )
)
