"""Application entry point.

Performance notes
-----------------

Pyrogram's defaults assume a busy multi-user bot: it starts
`min(32, cpu_count + 4)` update-dispatcher tasks and reserves several
up/download transmission slots. This bot is single-user and never
transfers file bytes, so both are pinned to the minimum (see WORKERS /
MAX_CONCURRENT_TRANSMISSIONS in config.py). On a 1-core box that alone
removes most of the idle memory and all of the pointless task switching.

uvloop is used when it is already installed, and silently ignored when it
is not, so nothing new has to be compiled on a small device.
"""

import atexit

from pyrogram import Client

from . import archive, cover
from .config import (
    API_HASH,
    API_ID,
    BOT_TOKEN,
    MAX_CONCURRENT_TRANSMISSIONS,
    SESSION_IN_MEMORY,
    SESSION_NAME,
    USE_UVLOOP,
    WORKERS,
)
from .log import get as get_logger, setup as setup_logging
from .handlers import callbacks, commands, media, transfer

setup_logging()

logger = get_logger(__name__)


def _install_uvloop() -> None:
    if not USE_UVLOOP:
        return

    try:
        import uvloop  # type: ignore
    except ImportError:
        return

    try:
        uvloop.install()
        logger.info("uvloop enabled")
    except Exception as error:  # pragma: no cover - defensive
        logger.warning("Could not enable uvloop: %s", error)


def _build_client() -> Client:
    base = {
        "api_id": API_ID,
        "api_hash": API_HASH,
        "bot_token": BOT_TOKEN,
        "workers": WORKERS,
        "in_memory": SESSION_IN_MEMORY,
    }

    optional = {
        "max_concurrent_transmissions": MAX_CONCURRENT_TRANSMISSIONS,
    }

    try:
        return Client(SESSION_NAME, **base, **optional)
    except TypeError:
        # Older/newer Pyrogram builds may not expose every knob.
        return Client(SESSION_NAME, **base)


_install_uvloop()

app = _build_client()

commands.register(app)
media.register(app)
transfer.register(app)
callbacks.register(app)


def _cleanup() -> None:
    cover.shutdown()
    archive.close()


atexit.register(_cleanup)


def run() -> None:
    logger.info(
        "Caption bot starting (workers=%s, uvloop=%s)",
        WORKERS,
        USE_UVLOOP,
    )
    print("Caption bot is running...")
    app.run()


if __name__ == "__main__":
    run()
