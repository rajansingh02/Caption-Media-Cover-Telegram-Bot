"""Telegram video-cover support.

Telegram's current Bot API exposes `cover` on sendVideo.  A Telegram file_id
can be supplied, so the video and cover remain on Telegram's servers; the bot
does not download either file.

Performance notes
-----------------

The previous version called `urlopen()` through `asyncio.to_thread()`. That
meant, for every single video in a batch:

* a DNS lookup and a fresh TLS handshake to api.telegram.org — by far the
  most expensive thing the bot does on a small ARM board; and
* a thread taken from asyncio's default executor, which sizes itself to
  `min(32, cpu_count + 4)` threads and keeps them alive afterwards.

This version keeps one HTTPS connection open with keep-alive and runs every
request on one dedicated worker thread. A 20-video batch goes from 20
handshakes to 1, and the bot's thread count stays flat no matter how large
the batch is. Only the standard library is used.
"""

from __future__ import annotations

import http.client
import json
import ssl
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.parse import urlencode

import asyncio

from .config import BOT_TOKEN
from .log import get as get_logger

logger = get_logger(__name__)

_API_HOST = "api.telegram.org"
_TIMEOUT = 30

_HEADERS = {
    "Content-Type": "application/x-www-form-urlencoded",
    "Connection": "keep-alive",
    "Accept-Encoding": "identity",
}


class CoverSendError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# One connection, one thread — both created on first use.
# ---------------------------------------------------------------------------

_connection: http.client.HTTPSConnection | None = None
_connection_lock = threading.Lock()
_ssl_context: ssl.SSLContext | None = None

_executor: ThreadPoolExecutor | None = None
_executor_lock = threading.Lock()


def _get_ssl_context() -> ssl.SSLContext:
    global _ssl_context

    if _ssl_context is None:
        # Building the context loads the CA bundle from disk; doing it
        # once instead of per request saves both I/O and memory.
        _ssl_context = ssl.create_default_context()

    return _ssl_context


def _get_connection() -> http.client.HTTPSConnection:
    global _connection

    if _connection is None:
        _connection = http.client.HTTPSConnection(
            _API_HOST,
            timeout=_TIMEOUT,
            context=_get_ssl_context(),
        )

    return _connection


def _drop_connection() -> None:
    global _connection

    if _connection is not None:
        try:
            _connection.close()
        except Exception:
            pass

        _connection = None


def _get_executor() -> ThreadPoolExecutor:
    global _executor

    if _executor is None:
        with _executor_lock:
            if _executor is None:
                _executor = ThreadPoolExecutor(
                    max_workers=1,
                    thread_name_prefix="botapi",
                )

    return _executor


def shutdown() -> None:
    """Close the pooled connection and stop the worker thread."""
    with _connection_lock:
        _drop_connection()

    global _executor

    if _executor is not None:
        _executor.shutdown(wait=False)
        _executor = None


# ---------------------------------------------------------------------------
# Bot API request
# ---------------------------------------------------------------------------


def _request_once(method: str, body: bytes) -> bytes:
    connection = _get_connection()

    connection.request(
        "POST",
        f"/bot{BOT_TOKEN}/{method}",
        body=body,
        headers=_HEADERS,
    )

    response = connection.getresponse()

    # The body must always be drained, otherwise the socket cannot be
    # reused for the next request.
    payload = response.read()

    if response.status != 200:
        text = payload.decode("utf-8", errors="replace")

        # A non-200 still carries a JSON description; surface it the same
        # way the old HTTPError branch did.
        raise CoverSendError(
            f"Telegram Bot API HTTP {response.status}: {text}"
        )

    return payload


def _post_form(method: str, data: dict[str, Any]) -> dict[str, Any]:
    body = urlencode(data).encode("utf-8")

    with _connection_lock:
        try:
            raw = _request_once(method, body)

        except CoverSendError:
            raise

        except (
            http.client.HTTPException,
            ConnectionError,
            TimeoutError,
            OSError,
        ) as exc:
            # A pooled connection can be closed by the far end between
            # requests. Rebuild it once and retry before giving up.
            logger.debug("Retrying Bot API request: %s", exc)

            _drop_connection()

            try:
                raw = _request_once(method, body)

            except CoverSendError:
                raise

            except Exception as retry_error:
                _drop_connection()

                raise CoverSendError(
                    f"Telegram Bot API request failed: {retry_error}"
                ) from retry_error

    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise CoverSendError("Telegram returned invalid JSON.") from exc

    if not payload.get("ok"):
        description = payload.get("description", "Unknown Telegram error")
        raise CoverSendError(description)

    return payload["result"]


def _build_payload(
    *,
    chat_id: int,
    video_file_id: str,
    caption: str,
    cover_file_id: str,
    width: int | None,
    height: int | None,
    duration: int | None,
    supports_streaming: bool,
    has_spoiler: bool,
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "chat_id": str(chat_id),
        "video": video_file_id,
        "cover": cover_file_id,
        "caption": caption,
        "supports_streaming": "true" if supports_streaming else "false",
        "has_spoiler": "true" if has_spoiler else "false",
    }

    if width:
        data["width"] = str(width)
    if height:
        data["height"] = str(height)
    if duration:
        data["duration"] = str(duration)

    return data


def send_video_with_cover(
    *,
    chat_id: int,
    video_file_id: str,
    caption: str,
    cover_file_id: str,
    width: int | None = None,
    height: int | None = None,
    duration: int | None = None,
    supports_streaming: bool = False,
    has_spoiler: bool = False,
) -> dict[str, Any]:
    """Send an existing Telegram video with an existing Telegram cover.

    Both `video` and `cover` are file_ids. No media bytes are downloaded by
    this bot and no multipart upload is performed.

    Blocking; prefer `send_video_with_cover_async()` from handlers.
    """
    return _post_form(
        "sendVideo",
        _build_payload(
            chat_id=chat_id,
            video_file_id=video_file_id,
            caption=caption,
            cover_file_id=cover_file_id,
            width=width,
            height=height,
            duration=duration,
            supports_streaming=supports_streaming,
            has_spoiler=has_spoiler,
        ),
    )


async def send_video_with_cover_async(
    *,
    chat_id: int,
    video_file_id: str,
    caption: str,
    cover_file_id: str,
    width: int | None = None,
    height: int | None = None,
    duration: int | None = None,
    supports_streaming: bool = False,
    has_spoiler: bool = False,
) -> dict[str, Any]:
    """Await the blocking call on the dedicated worker thread.

    The event loop stays free while the request is in flight, and no new
    thread is spawned per call.
    """
    payload = _build_payload(
        chat_id=chat_id,
        video_file_id=video_file_id,
        caption=caption,
        cover_file_id=cover_file_id,
        width=width,
        height=height,
        duration=duration,
        supports_streaming=supports_streaming,
        has_spoiler=has_spoiler,
    )

    loop = asyncio.get_running_loop()

    return await loop.run_in_executor(
        _get_executor(),
        _post_form,
        "sendVideo",
        payload,
    )
