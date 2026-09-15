import asyncio

from pyrogram import filters

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


async def _delete_ack_messages(
    client,
    state,
    chat_id,
):
    """
    Delete the temporary "Added to batch" messages.

    Source media messages are NEVER deleted here.
    """

    for message_id in list(
        state.batch_ack_message_ids
    ):
        try:
            await client.delete_messages(
                chat_id,
                message_id,
            )

        except Exception as exc:
            print(
                "Could not delete batch "
                f"acknowledgement "
                f"{message_id}: {exc}"
            )

    state.batch_ack_message_ids.clear()


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

    # Reuse the exact same analysis code used by media.py.
    from .media import (
        _analyse_incoming,
        _build_review_text,
    )

    from pyrogram.types import (
        InlineKeyboardButton,
        InlineKeyboardMarkup,
    )

    items = list(
        state.incoming_batch
    )

    state.incoming_batch.clear()

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
        from ..config import MAX_BATCH_SIZE

        if (
            len(state.batch)
            + len(valid)
            > MAX_BATCH_SIZE
        ):
            # Do not lose the sources.
            state.incoming_batch.extend(
                items
            )

            return

        state.batch.extend(valid)

        ack = await client.send_message(
            items[0].chat_id,
            (
                f"➕ Added {len(valid)} "
                "file(s) to batch.\n\n"
                + "\n".join(
                    f"• {item.display_name}"
                    for item in valid
                )
                + "\n\nWhen finished, use /preview."
            ),
        )

        state.batch_ack_message_ids.append(
            ack.id
        )

        return

    # ---------------------------------------------------------
    # One review for the entire incoming burst.
    # ---------------------------------------------------------

    state.pending_items = list(items)
    state.pending_valid_items = list(
        valid
    )
    state.pending_rejected_items = list(
        rejected
    )

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ Yes, continue",
                    callback_data="seq_batch_yes",
                ),
                InlineKeyboardButton(
                    "❌ No",
                    callback_data="seq_batch_no",
                ),
            ]
        ]
    )

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
        reply_markup=keyboard,
    )

    state.pending_review_message_id = (
        review.id
    )


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
        state = get_state(
            message.from_user.id
        )

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
                "⚠️ Finish or cancel the "
                "current batch first."
            )
            return

        if not detect_episode(
            caption
        ):
            await message.reply_text(
                "❌ Caption must contain "
                "SxxExx.\n\n"
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
        state = get_state(
            message.from_user.id
        )

        if not state.current_caption:
            await message.reply_text(
                "❌ No caption sequence.\n\n"
                "Use:\n"
                "/scaption S01E01.Name.mkv"
            )
            return

        if state.processing:
            await message.reply_text(
                "⏳ The current batch is "
                "still processing."
            )
            return

        # If files arrived moments before /preview,
        # analyze them immediately instead of waiting 2.5 seconds.
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
                "⚠️ Answer the batch "
                "confirmation first."
            )
            return

        if not state.batch:
            await message.reply_text(
                "❌ No files are waiting "
                "in the batch."
            )
            return

        # Delete temporary acknowledgements.
        await _delete_ack_messages(
            client,
            state,
            message.chat.id,
        )

        captions, text = build_preview(
            state
        )

        if captions is None:
            await message.reply_text(
                f"❌ {text}"
            )
            return

        if state.preview_message_id:
            try:
                await client.edit_message_text(
                    message.chat.id,
                    state.preview_message_id,
                    text,
                    reply_markup=batch_keyboard(
                        len(state.batch)
                    ),
                )

                return

            except Exception as exc:
                print(
                    "Could not edit existing "
                    f"preview: {exc}"
                )

                state.preview_message_id = None
                state.preview_chat_id = None

        sent = await message.reply_text(
            text,
            reply_markup=batch_keyboard(
                len(state.batch)
            ),
        )

        state.preview_message_id = sent.id
        state.preview_chat_id = (
            message.chat.id
        )

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
        state = get_state(
            message.from_user.id
        )

        args = message.text.split(
            maxsplit=1
        )

        arg = (
            args[1].strip().lower()
            if len(args) > 1
            else ""
        )

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
                    "New video outputs will use "
                    "the saved cover."
                )
            else:
                await message.reply_text(
                    "🖼️ Video cover: OFF"
                )

            return

        # /cover replied to an existing photo.
        if (
            message.reply_to_message
            and message.reply_to_message.photo
        ):
            state.cover_file_id = (
                _photo_file_id(
                    message.reply_to_message
                )
            )

            state.cover_message_id = (
                message.reply_to_message.id
            )

            state.awaiting_cover = False

            await message.reply_text(
                "✅ Video cover saved.\n\n"
                "It will be applied to video "
                "files in future batches without "
                "downloading or re-uploading "
                "the video."
            )

            return

        state.awaiting_cover = True

        await message.reply_text(
            "🖼️ Send the cover image now.\n\n"
            "The image will be saved as a "
            "Telegram file_id and reused for "
            "future videos.\n\n"
            "No video will be downloaded or "
            "re-uploaded.\n\n"
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
        state = get_state(
            message.from_user.id
        )

        if state.processing:
            await message.reply_text(
                "⏳ The current batch is "
                "still processing."
            )
            return

        if (
            state.batch
            or state.incoming_batch
            or state.pending_items
        ):
            await message.reply_text(
                "⚠️ Finish the current "
                "batch first."
            )
            return

        if not state.current_caption:
            await message.reply_text(
                "❌ No caption sequence "
                "is active.\n\n"
                "Use /scaption first."
            )
            return

        episode = detect_episode(
            state.current_caption
        )

        if not episode:
            await message.reply_text(
                "❌ No SxxExx sequence "
                "is active."
            )
            return

        state.current_caption = replace_episode(
            state.current_caption,
            next_season_episode(
                episode
            ),
        )

        state.preview_message_id = None
        state.preview_chat_id = None

        await message.reply_text(
            "🔄 Next season started.\n\n"
            f"Next caption:\n"
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
        state = get_state(
            message.from_user.id
        )

        # Cancel collection timer.
        if (
            state.collection_task
            and not state.collection_task.done()
        ):
            state.collection_task.cancel()

            try:
                await state.collection_task
            except asyncio.CancelledError:
                pass

        if (
            state.cover_collection_task
            and not state.cover_collection_task.done()
        ):
            state.cover_collection_task.cancel()

            try:
                await state.cover_collection_task
            except asyncio.CancelledError:
                pass

        # IMPORTANT:
        # /stop cancels the workflow but DOES NOT delete source
        # media. Source deletion is reserved for successful
        # processing or explicit "delete sources" confirmation.
        await _delete_ack_messages(
            client,
            state,
            message.chat.id,
        )

        reset_state(
            message.from_user.id
        )

        await message.reply_text(
            "🛑 Caption sequence stopped.\n\n"
            "Pending batch cancelled.\n"
            "Source files were kept."
        )
