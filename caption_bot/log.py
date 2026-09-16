"""Central logging.

The bot used to `print()` from every error path. Logging costs the same
when enabled but can be silenced with LOG_LEVEL=WARNING, which matters on
machines writing to an SD card.
"""

import logging

from .config import LOG_LEVEL

_configured = False


def setup() -> None:
    global _configured

    if _configured:
        return

    logging.basicConfig(
        level=getattr(logging, LOG_LEVEL, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # Pyrogram is very chatty at INFO and each record costs CPU + I/O.
    logging.getLogger("pyrogram").setLevel(logging.WARNING)

    _configured = True


def get(name: str) -> logging.Logger:
    return logging.getLogger(name)
