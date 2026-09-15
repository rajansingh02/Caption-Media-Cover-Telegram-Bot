"""Telegram video-cover support.

Telegram's current Bot API exposes `cover` on sendVideo.  A Telegram file_id
can be supplied, so the video and cover remain on Telegram's servers; the bot
does not download either file.
"""

import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .config import BOT_TOKEN


class CoverSendError(RuntimeError):
    pass


def _post_form(method: str, data: dict[str, Any]) -> dict[str, Any]:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    encoded = urlencode(data).encode("utf-8")
    request = Request(
        url,
        data=encoded,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            body = str(exc)
        raise CoverSendError(f"Telegram Bot API HTTP {exc.code}: {body}") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise CoverSendError(f"Telegram Bot API request failed: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise CoverSendError("Telegram returned invalid JSON.") from exc

    if not payload.get("ok"):
        description = payload.get("description", "Unknown Telegram error")
        raise CoverSendError(description)
    return payload["result"]


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
    """
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

    return _post_form("sendVideo", data)
