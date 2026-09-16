"""Command handlers: /scaption, /preview, /cover, /nextseason, /stop.

Performance notes
-----------------

* `_flush_incoming` used to import media.py, pyrogram.types and config on
  every call, from inside the function. Those are module-level imports now
  (there is no import cycle: media.py does not import this module).
* Acknowledgement messages are removed with grouped delete calls, so
  /preview and /stop no longer fire one API request per stale message.
"""

import asyncio

from pyrogram import filters

from ..config import MAX_BATCH_SIZE
from ..log import get as get_logger
from ..preview import (
    batch_keyboard,
    build_preview,
)
from ..sequence import (
    detect_episode,
    next_season_episode,
    replace_episode,
)
from ..state import (
    get_state,
    reset_state,
)
from .media import (
    _analyse_incoming,
    _build_review_text,
    build_ack_text,
    review_keyboard,
)

logger = get_logger(__name__)

_DELETE_CHUNK = 100


async def _delete_ack_messages(
    client,
    state,
    chat_id,
):
    """
    Delete the temporary "Added to batch" messages.

    Source media messages are NEVER deleted here.
    """

    message_ids = list(dict.fromkeys(state.batch_ack_message_ids))

    state.batch_ack_message_ids.clear()

    if not message_ids:
        return

    for start in range(0, len(message_ids), _DELETE_CHUNK):
        chunk = message_ids[start : start + _DELETE_CHUNK]

        try:
            await client.delete_messages(chat_id, chunk)
            continue
        except Exception as exc:
            logger.warning(
                "Could not delete %s batch acknowledgement(s): %s",
                len(chunk),
                exc,
            )

        for message_id in chunk:
            try:
                await client.delete_messages(chat_id, message_id)
            except Exception as exc:
                logger.warning(
                    "Could not delete batch acknowledgement %s: %s",
                    message_id,
                    exc,
                )


async def _flush_incoming(
    client,
    state,
):
    """
    Immediately finalize the current incoming burst.

    Used by /preview so the user does not have to wait for
    the normal collection delay.
    """

    task = state.collection_task

    if (
        task
        and not task.done()
        and task is not asyncio.current_task()
    ):
        task.cancel()

        try:
            await task
        except asyncio.CancelledError:
            pass

    state.collection_task = None

    if not state.incoming_batch:
        return

    items = list(state.incoming_batch)

    state.incoming_batch.clear()

    # Reuse the exact same analysis code used by media.py.
    (
        reason,
        valid,
        rejected,
        dominant,
        expected,
    ) = _analyse_incoming(
        state,
        items,
    )

    # Normal incoming batch.
    if reason is None:
        if len(state.batch) + len(valid) > MAX_BATCH_SIZE:
            # Do not lose the sources.
            state.incoming_batch.extend(items)

            return

        state.batch.extend(valid)

        ack = await client.send_message(
            items[0].chat_id,
            build_ack_text(valid, maximum=10_000),
        )

        state.batch_ack_message_ids.append(ack.id)

        return

    # ---------------------------------------------------------
    # One review for the entire incoming burst.
    # ---------------------------------------------------------

    state.pending_items = list(items)
    state.pending_valid_items = list(valid)
    state.pending_rejected_items = list(rejected)

    review = await client.send_message(
        items[0].chat_id,
        _build_review_text(
            state=state,
            items=items,
            valid=valid,
            rejected=rejected,
            dominant=dominant,
            expected=expected,
            reason=reason,
        ),
        reply_markup=review_keyboard(),
    )

    state.pending_review_message_id = review.id


def _photo_file_id(message):
    if not message.photo:
        return None

    return message.photo.file_id


def _clear_pending_state(state):
    state.pending_items.clear()
    state.pending_valid_items.clear()
    state.pending_rejected_items.clear()
    state.pending_review_message_id = None


def register(app):
    # =========================================================
    # /scaption
    # =========================================================

    @app.on_message(
        filters.command("scaption")
        & filters.private
    )
    async def scaption(
        client,
        message,
    ):
        state = get_state(message.from_user.id)

        if len(message.command) < 2:
            await message.reply_text(
                "Usage:\n"
                "/scaption S01E01.Trip.mkv"
            )
            return

        caption = message.text.split(
            None,
            1,
        )[1].strip()

        if not caption:
            await message.reply_text(
                "❌ Caption cannot be empty."
            )
            return

        if (
            state.batch
            or state.incoming_batch
            or state.pending_items
        ):
            await message.reply_text(
                "⚠️ Finish or cancel the current batch first."
            )
            return

        if not detect_episode(caption):
            await message.reply_text(
                "❌ Caption must contain SxxExx.\n\n"
                "Examples:\n"
                "S01E01\n"
                "S01 E01\n"
                "S01.E01"
            )
            return

        state.current_caption = caption

        state.preview_message_id = None
        state.preview_chat_id = None

        await message.reply_text(
            "✅ Caption sequence set:\n\n"
            f"{caption}\n\n"
            "Send your files.\n"
            "When finished, use /preview."
        )

    # =========================================================
    # /preview
    # =========================================================

    @app.on_message(
        filters.command("preview")
        & filters.private
    )
    async def preview(
        client,
        message,
    ):
        state = get_state(message.from_user.id)

        if not state.current_caption:
            await message.reply_text(
                "❌ No caption sequence.\n\n"
                "Use:\n"
                "/scaption S01E01.Name.mkv"
            )
            return

        if state.processing:
            await message.reply_text(
                "⏳ The current batch is still processing."
            )
            return

        # If files arrived moments before /preview,
        # analyze them immediately instead of waiting for the
        # collection delay.
        if state.incoming_batch:
            await _flush_incoming(
                client,
                state,
            )

            # A suspicious batch now needs its confirmation.
            if state.pending_items:
                return

        if state.pending_items:
            await message.reply_text(
                "⚠️ Answer the batch confirmation first."
            )
            return

        if not state.batch:
            await message.reply_text(
                "❌ No files are waiting in the batch."
            )
            return

        # Delete temporary acknowledgements.
        await _delete_ack_messages(
            client,
            state,
            message.chat.id,
        )

        captions, text = build_preview(state)

        if captions is None:
            await message.reply_text(f"❌ {text}")
            return

        keyboard = batch_keyboard(len(state.batch))

        if state.preview_message_id:
            try:
                await client.edit_message_text(
                    message.chat.id,
                    state.preview_message_id,
                    text,
                    reply_markup=keyboard,
                )

                return

            except Exception as exc:
                logger.warning(
                    "Could not edit existing preview: %s", exc
                )

                state.preview_message_id = None
                state.preview_chat_id = None

        sent = await message.reply_text(
            text,
            reply_markup=keyboard,
        )

        state.preview_message_id = sent.id
        state.preview_chat_id = message.chat.id

    # =========================================================
    # /cover
    # =========================================================

    @app.on_message(
        filters.command("cover")
        & filters.private
    )
    async def cover_command(
        client,
        message,
    ):
        state = get_state(message.from_user.id)

        args = message.text.split(maxsplit=1)

        arg = args[1].strip().lower() if len(args) > 1 else ""

        if arg in {
            "off",
            "disable",
            "remove",
            "none",
        }:
            state.cover_file_id = None
            state.cover_message_id = None
            state.awaiting_cover = False

            await message.reply_text(
                "🖼️ Video cover disabled."
            )

            return

        if arg in {
            "status",
            "show",
        }:
            if state.cover_file_id:
                await message.reply_text(
                    "🖼️ Video cover: ON\n\n"
                    "New video outputs will use the saved cover."
                )
            else:
                await message.reply_text(
                    "🖼️ Video cover: OFF"
                )

            return

        # /cover replied to an existing photo.
        replied = message.reply_to_message

        if replied and replied.photo:
            state.cover_file_id = _photo_file_id(replied)
            state.cover_message_id = replied.id
            state.awaiting_cover = False

            await message.reply_text(
                "✅ Video cover saved.\n\n"
                "It will be applied to video files in future "
                "batches without downloading or re-uploading "
                "the video."
            )

            return

        state.awaiting_cover = True

        await message.reply_text(
            "🖼️ Send the cover image now.\n\n"
            "The image will be saved as a Telegram file_id and "
            "reused for future videos.\n\n"
            "No video will be downloaded or re-uploaded.\n\n"
            "Use /cover off to disable it."
        )

    # =========================================================
    # /nextseason
    # =========================================================

    @app.on_message(
        filters.command("nextseason")
        & filters.private
    )
    async def nextseason(
        client,
        message,
    ):
        state = get_state(message.from_user.id)

        if state.processing:
            await message.reply_text(
                "⏳ The current batch is still processing."
            )
            return

        if (
            state.batch
            or state.incoming_batch
            or state.pending_items
        ):
            await message.reply_text(
                "⚠️ Finish the current batch first."
            )
            return

        if not state.current_caption:
            await message.reply_text(
                "❌ No caption sequence is active.\n\n"
                "Use /scaption first."
            )
            return

        episode = detect_episode(state.current_caption)

        if not episode:
            await message.reply_text(
                "❌ No SxxExx sequence is active."
            )
            return

        state.current_caption = replace_episode(
            state.current_caption,
            next_season_episode(episode),
        )

        state.preview_message_id = None
        state.preview_chat_id = None

        await message.reply_text(
            "🔄 Next season started.\n\n"
            "Next caption:\n"
            f"{state.current_caption}"
        )

    # =========================================================
    # /stop
    # =========================================================

    @app.on_message(
        filters.command("stop")
        & filters.private
    )
    async def stop(
        client,
        message,
    ):
        state = get_state(message.from_user.id)

        # Cancel collection timers.
        for task in (
            state.collection_task,
            state.cover_collection_task,
        ):
            if task is not None and not task.done():
                task.cancel()

                try:
                    await task
                except asyncio.CancelledError:
                    pass

        state.collection_task = None
        state.cover_collection_task = None

        # IMPORTANT:
        # /stop cancels the workflow but DOES NOT delete source
        # media. Source deletion is reserved for successful
        # processing or explicit "delete sources" confirmation.
        await _delete_ack_messages(
            client,
            state,
            message.chat.id,
        )

        reset_state(message.from_user.id)

        await message.reply_text(
            "🛑 Caption sequence stopped.\n\n"
            "Pending batch cancelled.\n"
            "Source files were kept."
        )
