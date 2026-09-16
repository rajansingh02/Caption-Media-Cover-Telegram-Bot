"""Callback handlers for preview, batch review, processing and season controls.

Performance notes
-----------------

* Source/acknowledgement messages are deleted in grouped calls (up to 100
  message IDs per request, per chat) instead of one API round trip per
  message. A finished 20-file batch drops from 20+ delete calls to 1, and
  per-message accuracy is preserved by falling back to single deletes only
  if a grouped call fails.
* Finished episodes are written to the archive in one SQLite transaction
  after the send pass instead of one commit per file.
* The confirmed batch is planned once. `confirm` used to run `plan_batch`
  and then `_process_batch` ran it again over the same items.
* Cover sends go through the pooled keep-alive Bot API connection, so a
  batch of videos performs one TLS handshake rather than one per video.
"""

from __future__ import annotations

from typing import Optional

from pyrogram import Client
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

from .. import archive
from ..cover import CoverSendError, send_video_with_cover_async
from ..log import get as get_logger
from ..preview import (
    batch_keyboard,
    build_preview,
    finished_keyboard,
)
from ..sequence import (
    next_caption_from_successes,
    plan_batch,
    replace_episode,
    detect_episode,
    next_season_episode,
)
from ..state import get_state
from . import transfer

logger = get_logger(__name__)


# Telegram accepts at most 100 message IDs per delete request.
_DELETE_CHUNK = 100

# Built once instead of on every rejection.
_DELETE_SOURCES_KEYBOARD = InlineKeyboardMarkup(
    [
        [
            InlineKeyboardButton(
                "🗑️ Yes, delete",
                callback_data="seq_batch_no_delete_yes",
            ),
            InlineKeyboardButton(
                "Keep them",
                callback_data="seq_batch_no_delete_no",
            ),
        ]
    ]
)

_REVIEW_CALLBACKS = frozenset(
    {
        # Actual buttons sent by media.py / commands.py.
        "seq_batch_yes",
        "seq_batch_no",
        # Follow-up "delete these source messages?" confirmation
        # shown after "seq_batch_no".
        "seq_batch_no_delete_yes",
        "seq_batch_no_delete_no",
        # Legacy / alternate aliases kept for compatibility.
        "review_yes",
        "review_no",
        "yes",
        "no",
        "review_confirm",
        "review_cancel",
    }
)

_FINISHED_CALLBACKS = frozenset(
    {
        "nextseason",
        "next_season",
        "finished_nextseason",
    }
)

_COVER_BATCH_CALLBACKS = frozenset({"cover_confirm", "cover_cancel"})

_ACCEPT_CALLBACKS = frozenset(
    {"seq_batch_yes", "review_yes", "yes", "review_confirm"}
)

_REJECT_CALLBACKS = frozenset(
    {"seq_batch_no", "review_no", "no", "review_cancel"}
)

_DELETE_DECISION_CALLBACKS = frozenset(
    {"seq_batch_no_delete_yes", "seq_batch_no_delete_no"}
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _answer(
    query: CallbackQuery,
    text: str = "",
    *,
    show_alert: bool = False,
) -> None:
    """Safely answer a callback query."""
    try:
        await query.answer(
            text,
            show_alert=show_alert,
        )
    except Exception:
        pass


async def _edit(
    query: CallbackQuery,
    text: str,
    reply_markup=None,
) -> bool:
    """Safely edit the callback message."""
    try:
        await query.message.edit_text(
            text,
            reply_markup=reply_markup,
        )
        return True
    except Exception as error:
        logger.warning("Callback message edit failed: %s", error)
        return False


async def _delete_message(
    client: Client,
    chat_id: int,
    message_id: Optional[int],
) -> None:
    """Safely delete one message."""
    if message_id is None:
        return

    try:
        await client.delete_messages(
            chat_id,
            message_id,
        )
    except Exception:
        pass


async def _delete_ids(
    client: Client,
    chat_id: int,
    message_ids: list[int],
) -> tuple[int, int]:
    """Delete message IDs in one chat using as few API calls as possible.

    Returns (deleted_count, failed_count). If a grouped call fails the
    chunk is retried one message at a time so the reported counts stay
    exact.
    """
    deleted = 0
    failed = 0

    for start in range(0, len(message_ids), _DELETE_CHUNK):
        chunk = message_ids[start : start + _DELETE_CHUNK]

        try:
            await client.delete_messages(chat_id, chunk)
            deleted += len(chunk)
            continue

        except Exception as error:
            logger.warning(
                "Grouped delete failed in %s (%s message(s)): %s",
                chat_id,
                len(chunk),
                error,
            )

        for message_id in chunk:
            try:
                await client.delete_messages(chat_id, message_id)
                deleted += 1
            except Exception as error:
                failed += 1
                logger.warning(
                    "Could not delete message %s in %s: %s",
                    message_id,
                    chat_id,
                    error,
                )

    return deleted, failed


def _group_by_chat(items) -> dict[int, list[int]]:
    by_chat: dict[int, list[int]] = {}

    for item in items:
        bucket = by_chat.get(item.chat_id)

        if bucket is None:
            by_chat[item.chat_id] = [item.message_id]
        else:
            bucket.append(item.message_id)

    return by_chat


async def _delete_items_messages(
    client: Client,
    items,
) -> tuple[int, int]:
    """
    Delete the source messages for a list of BatchItems, grouped by
    chat so each chat only needs one delete_messages call.

    Returns (deleted_count, failed_count).
    """
    deleted = 0
    failed = 0

    for chat_id, message_ids in _group_by_chat(items).items():
        chunk_deleted, chunk_failed = await _delete_ids(
            client,
            chat_id,
            message_ids,
        )

        deleted += chunk_deleted
        failed += chunk_failed

    return deleted, failed


def _restore_finished_screen(state) -> tuple[str, object]:
    """
    Text + keyboard to fall back to after handling a rejected
    incoming burst: the last completed batch summary if one
    exists, otherwise a generic cancellation notice.
    """
    if state.last_finished_text:
        return state.last_finished_text, finished_keyboard()

    return (
        "❌ Incoming batch cancelled.\n\n"
        "No source files were processed or deleted.",
        None,
    )


async def _delete_ack_messages(
    client: Client,
    state,
) -> None:
    """
    Delete temporary "Added to batch" acknowledgement messages.

    Failures are intentionally ignored.
    """
    chat_id = state.preview_chat_id

    if chat_id is None:
        return

    if not state.batch_ack_message_ids:
        return

    # dict.fromkeys keeps order while removing duplicates.
    message_ids = list(dict.fromkeys(state.batch_ack_message_ids))

    state.batch_ack_message_ids.clear()

    await _delete_ids(client, chat_id, message_ids)


def _callback_message_id(
    query: CallbackQuery,
) -> Optional[int]:
    """Return the message ID containing the pressed button."""
    if query.message is None:
        return None

    return query.message.id


def _state_user_id(
    query: CallbackQuery,
) -> int:
    """
    Return the Telegram user who pressed the button.

    IMPORTANT:
    This must never be replaced with query.message.chat.id.
    """
    return query.from_user.id


def _is_review_callback(data: str) -> bool:
    return data in _REVIEW_CALLBACKS


def _is_finished_callback(data: str) -> bool:
    return data in _FINISHED_CALLBACKS


def _is_preview_callback(data: str) -> bool:
    return (
        data == "confirm"
        or data == "cancel"
        or data.startswith("up:")
        or data.startswith("down:")
    )


def _is_cover_batch_callback(data: str) -> bool:
    return data in _COVER_BATCH_CALLBACKS


def _preview_result(state):
    """
    Build the actual preview using the project's real preview API.

    build_preview(state) returns:

        (captions, text)

    rather than an object.
    """
    result = build_preview(state)

    if not result:
        return None, None

    if not isinstance(result, tuple):
        return None, None

    if len(result) != 2:
        return None, None

    captions, text = result

    if captions is None or text is None:
        return None, None

    return captions, text


async def _refresh_preview(
    query: CallbackQuery,
    state,
) -> bool:
    """Rebuild and edit the active preview message."""
    try:
        captions, text = _preview_result(state)

        if captions is None or text is None:
            return False

        await query.message.edit_text(
            text,
            reply_markup=batch_keyboard(len(state.batch)),
        )

        state.preview_message_id = query.message.id

        return True

    except Exception as error:
        logger.warning("Could not refresh preview: %s", error)
        return False


# ---------------------------------------------------------------------------
# Ownership / stale-button validation
# ---------------------------------------------------------------------------


def _validate_stateful_callback(
    query: CallbackQuery,
    state,
    data: str,
) -> bool:
    """
    Validate that a stateful button still belongs to the user's
    current state.

    Ownership comes from query.from_user.id.

    Message IDs then prevent old buttons from modifying a newer state.
    """
    message_id = _callback_message_id(query)

    if message_id is None:
        return False

    # ---------------------------------------------------------------
    # Pending batch review
    # ---------------------------------------------------------------

    if _is_review_callback(data):
        return (
            state.pending_review_message_id is not None
            and message_id == state.pending_review_message_id
        )

    # ---------------------------------------------------------------
    # Normal preview
    # ---------------------------------------------------------------

    if _is_preview_callback(data):
        return (
            state.preview_message_id is not None
            and message_id == state.preview_message_id
        )

    # ---------------------------------------------------------------
    # Cover-only batch (no caption sequence active)
    # ---------------------------------------------------------------

    if _is_cover_batch_callback(data):
        return (
            state.cover_preview_message_id is not None
            and message_id == state.cover_preview_message_id
        )

    # ---------------------------------------------------------------
    # Final "Start Next Season" button.
    #
    # This is handled separately because preview_message_id is cleared
    # when processing finishes.
    # ---------------------------------------------------------------

    if _is_finished_callback(data):
        return True

    return False


# ---------------------------------------------------------------------------
# Processing
# ---------------------------------------------------------------------------


def _archive_row(
    owner_id: int,
    item,
    episode,
    caption: str,
    cover_file_id: Optional[str],
) -> dict:
    return {
        "owner_id": owner_id,
        "season": episode.season,
        "episode": episode.episode,
        "caption": caption,
        "media_type": item.media_type,
        "file_id": item.file_id,
        "width": item.width,
        "height": item.height,
        "duration": item.duration,
        "supports_streaming": item.supports_streaming,
        "has_spoiler": item.has_spoiler,
        "cover_file_id": cover_file_id,
    }


async def _process_batch(
    client: Client,
    query: CallbackQuery,
    state,
    preplanned: Optional[tuple[list, list]] = None,
) -> None:
    """
    Process every valid item in the confirmed batch.

    Source messages are NOT deleted during processing.

    They are deleted only after the complete processing pass finishes.

    `preplanned` carries the (ordered_items, assignments) pair when the
    caller already ran plan_batch, so the batch is never planned twice.
    """

    if state.processing:
        await _answer(
            query,
            "⏳ This batch is already processing.",
        )
        return

    if not state.batch:
        await _answer(
            query,
            "Batch is empty.",
            show_alert=True,
        )
        return

    if not state.current_caption:
        await _answer(
            query,
            "Caption sequence is no longer active.",
            show_alert=True,
        )
        return

    state.processing = True

    # Freeze the confirmed batch.
    items = list(state.batch)

    starting_caption = state.current_caption

    try:
        # ---------------------------------------------------------------
        # Final planning
        # ---------------------------------------------------------------

        if preplanned is not None:
            ordered_items, assignments = preplanned
        else:
            try:
                ordered_items, assignments = plan_batch(
                    items,
                    starting_caption,
                )
            except Exception as error:
                logger.error(
                    "Could not plan confirmed batch: %s", error
                )

                state.processing = False

                await _answer(
                    query,
                    "Could not prepare this batch.",
                    show_alert=True,
                )
                return

        if not ordered_items:
            state.processing = False

            await _answer(
                query,
                "There are no valid files to process.",
                show_alert=True,
            )
            return

        state.batch = ordered_items

        # ---------------------------------------------------------------
        # Disable the preview buttons while processing.
        # ---------------------------------------------------------------

        await _edit(
            query,
            "⏳ Processing batch...\n\n"
            "Please wait until all files are finished.",
        )

        await _answer(
            query,
            "Processing...",
        )

        # ---------------------------------------------------------------
        # Process every item.
        # ---------------------------------------------------------------

        owner_id = query.from_user.id
        cover_file_id = state.cover_file_id

        processed_items = []
        failed_items = []
        successful_eps = []
        archive_rows = []

        for item in ordered_items:
            episode = item.assigned or item.detected

            if episode is None:
                failed_items.append(
                    (
                        item,
                        "No episode assignment",
                    )
                )
                continue

            # -----------------------------------------------------------
            # Build the caption directly from the actual sequence API.
            # -----------------------------------------------------------

            try:
                caption = replace_episode(
                    starting_caption,
                    episode,
                )
            except Exception as error:
                failed_items.append(
                    (
                        item,
                        f"Could not build caption: {error}",
                    )
                )

                logger.error(
                    "Caption generation failed for %s: %s",
                    item.display_name,
                    error,
                )
                continue

            # -----------------------------------------------------------
            # Send/copy media.
            # -----------------------------------------------------------

            use_cover = bool(cover_file_id) and item.media_type == "video"

            try:
                # -------------------------------------------------------
                # Video cover
                #
                # The Bot API call runs on cover.py's dedicated worker
                # thread over a pooled keep-alive connection, so the
                # event loop is never blocked and no handshake is repeated
                # per video.  Per the documented contract, a rejected
                # cover operation counts this item as FAILED — it must
                # NOT silently fall back to a cover-less copy, or the
                # user would never learn the cover wasn't applied.
                # -------------------------------------------------------

                if use_cover:
                    if not item.file_id:
                        raise CoverSendError(
                            "Video is missing a file_id."
                        )

                    await send_video_with_cover_async(
                        chat_id=item.chat_id,
                        video_file_id=item.file_id,
                        caption=caption,
                        cover_file_id=cover_file_id,
                        width=item.width,
                        height=item.height,
                        duration=item.duration,
                        supports_streaming=item.supports_streaming,
                        has_spoiler=item.has_spoiler,
                    )

                else:
                    # -----------------------------------------------
                    # Normal Telegram copy (no cover requested, or
                    # non-video media).
                    # -----------------------------------------------
                    await client.copy_message(
                        chat_id=item.chat_id,
                        from_chat_id=item.chat_id,
                        message_id=item.message_id,
                        caption=caption,
                    )

                processed_items.append(item)
                successful_eps.append(episode)

                # -------------------------------------------------------
                # Archive for /transfer.
                #
                # Rows are collected here and written in a single
                # transaction once the send pass is over. It must never
                # fail the batch itself — /transfer simply won't see these
                # episodes if writing the archive fails.
                # -------------------------------------------------------

                archive_rows.append(
                    _archive_row(
                        owner_id,
                        item,
                        episode,
                        caption,
                        cover_file_id if use_cover else None,
                    )
                )

            except Exception as error:
                failed_items.append(
                    (
                        item,
                        str(error),
                    )
                )

                logger.error(
                    "Error processing %s: %s",
                    item.display_name,
                    error,
                )

        # ---------------------------------------------------------------
        # One archive transaction for the whole batch.
        # ---------------------------------------------------------------

        if archive_rows:
            try:
                archive.record_episodes(archive_rows)
            except Exception as error:
                logger.error(
                    "Could not archive episodes for /transfer: %s",
                    error,
                )

        # ---------------------------------------------------------------
        # IMPORTANT:
        #
        # The entire processing pass is now finished.
        #
        # Only successful outputs have their source deleted.
        # Failed source messages remain available.
        # ---------------------------------------------------------------

        deleted, delete_failed = await _delete_items_messages(
            client,
            processed_items,
        )

        # ---------------------------------------------------------------
        # Advance caption from the highest ACTUALLY successful episode.
        #
        # Example:
        #
        # S03E01
        # S03E02
        # S03E03
        # S03E04
        # S03E06
        #
        # => S03E07
        #
        # E05 is never invented.
        # ---------------------------------------------------------------

        if successful_eps:
            try:
                state.current_caption = next_caption_from_successes(
                    starting_caption,
                    successful_eps,
                    [
                        item.assigned
                        for item, _ in failed_items
                        if item.assigned is not None
                    ],
                )

            except Exception as error:
                logger.error(
                    "Could not advance caption sequence: %s", error
                )

                state.current_caption = starting_caption

        next_caption = state.current_caption

        processed = len(processed_items)
        failed = len(failed_items)
        total = len(ordered_items)

        # ---------------------------------------------------------------
        # Clear completed batch state.
        # ---------------------------------------------------------------

        state.batch.clear()

        state.preview_message_id = None
        state.pending_review_message_id = None

        # Pending review data must also be cleared after processing.
        state.pending_items.clear()
        state.pending_valid_items.clear()
        state.pending_rejected_items.clear()

        state.processing = False

        # ---------------------------------------------------------------
        # Keep the cover for reuse.
        # ---------------------------------------------------------------

        state.awaiting_cover = False

        # ---------------------------------------------------------------
        # Delete temporary acknowledgement messages.
        # ---------------------------------------------------------------

        await _delete_ack_messages(
            client,
            state,
        )

        # ---------------------------------------------------------------
        # FINAL MESSAGE
        #
        # This is deliberately a NEW message.
        #
        # We do NOT edit the processing/preview message into the
        # finished message.
        # ---------------------------------------------------------------

        result_lines = [
            "✅ Batch finished.",
            "",
            f"Total: {total}",
            f"Processed: {processed}",
            f"Failed: {failed}",
            f"Sources deleted: {deleted}",
        ]

        if delete_failed:
            result_lines.append(
                f"Source deletion failed: {delete_failed}"
            )

        result_lines.extend(
            [
                "",
                "Next caption:",
                next_caption,
            ]
        )

        if failed_items:
            result_lines.append("")
            result_lines.append("❌ Failed files:")

            for item, _ in failed_items[:20]:
                result_lines.append(f"• {item.display_name}")

            if failed > 20:
                result_lines.append(f"• ...and {failed - 20} more")

        finished_text = "\n".join(result_lines)

        # Remembered so a later rejected incoming burst ("No") can
        # restore this screen instead of a generic cancel message.
        state.last_finished_text = finished_text

        try:
            await client.send_message(
                chat_id=owner_id,
                text=finished_text,
                reply_markup=finished_keyboard(),
            )

        except Exception as error:
            logger.error(
                "Could not send final batch message: %s", error
            )

    except Exception as error:
        logger.exception("Unexpected batch processing error: %s", error)

        state.processing = False

        await _answer(
            query,
            "An unexpected error occurred. "
            "Your source files were not intentionally deleted.",
            show_alert=True,
        )


# ---------------------------------------------------------------------------
# Cover-only processing (no caption sequence active)
# ---------------------------------------------------------------------------


async def _process_cover_batch(
    client: Client,
    query: CallbackQuery,
    state,
) -> None:
    """
    Apply the saved cover to videos and pass everything else
    through untouched — original captions are always preserved,
    nothing rewrites SxxExx here.
    """

    if state.processing:
        await _answer(
            query,
            "⏳ This batch is already processing.",
        )
        return

    if not state.cover_batch:
        await _answer(
            query,
            "Batch is empty.",
            show_alert=True,
        )
        return

    state.processing = True
    items = list(state.cover_batch)

    await _edit(
        query,
        "⏳ Applying cover...\n\n"
        "Please wait until all files are finished.",
    )
    await _answer(query, "Processing...")

    processed_items = []
    failed_items = []

    cover_file_id = state.cover_file_id

    try:
        for item in items:
            try:
                if item.media_type == "video":
                    if not item.file_id:
                        raise CoverSendError(
                            "Video is missing a file_id."
                        )

                    await send_video_with_cover_async(
                        chat_id=item.chat_id,
                        video_file_id=item.file_id,
                        caption=item.caption or "",
                        cover_file_id=cover_file_id,
                        width=item.width,
                        height=item.height,
                        duration=item.duration,
                        supports_streaming=item.supports_streaming,
                        has_spoiler=item.has_spoiler,
                    )
                else:
                    # caption omitted -> pyrogram keeps the original.
                    await client.copy_message(
                        chat_id=item.chat_id,
                        from_chat_id=item.chat_id,
                        message_id=item.message_id,
                    )

                processed_items.append(item)

            except Exception as error:
                failed_items.append((item, str(error)))
                logger.error(
                    "Cover-only processing failed for %s: %s",
                    item.display_name,
                    error,
                )

        deleted, delete_failed = await _delete_items_messages(
            client,
            processed_items,
        )

        state.cover_batch.clear()
        state.cover_preview_message_id = None
        state.processing = False

        failed = len(failed_items)

        result_lines = [
            "✅ Cover batch finished.",
            "",
            f"Total: {len(items)}",
            f"Processed: {len(processed_items)}",
            f"Failed: {failed}",
            f"Sources deleted: {deleted}",
        ]

        if delete_failed:
            result_lines.append(
                f"Source deletion failed: {delete_failed}"
            )

        if failed_items:
            result_lines.append("")
            result_lines.append("❌ Failed files:")

            for item, _ in failed_items[:20]:
                result_lines.append(f"• {item.display_name}")

            if failed > 20:
                result_lines.append(f"• ...and {failed - 20} more")

        try:
            await client.send_message(
                chat_id=query.from_user.id,
                text="\n".join(result_lines),
            )
        except Exception as error:
            logger.error(
                "Could not send cover batch result: %s", error
            )

    except Exception as error:
        logger.exception(
            "Unexpected cover batch processing error: %s", error
        )
        state.processing = False
        await _answer(
            query,
            "An unexpected error occurred. "
            "Your source files were not intentionally deleted.",
            show_alert=True,
        )


# ---------------------------------------------------------------------------
# Main callback handler
# ---------------------------------------------------------------------------


async def callback_handler(
    client: Client,
    query: CallbackQuery,
) -> None:
    """Handle every inline-button callback for the bot."""

    if query is None:
        return

    if query.from_user is None:
        await _answer(
            query,
            "Unable to identify the user.",
            show_alert=True,
        )
        return

    if query.message is None:
        await _answer(
            query,
            "This button is no longer available.",
            show_alert=True,
        )
        return

    data_raw = query.data

    if isinstance(data_raw, bytes):
        try:
            data = data_raw.decode("utf-8")
        except UnicodeDecodeError:
            await _answer(
                query,
                "Invalid button data.",
                show_alert=True,
            )
            return
    elif isinstance(data_raw, str):
        data = data_raw
    else:
        data = str(data_raw or "")

    if not data:
        await _answer(
            query,
            "Invalid button.",
            show_alert=True,
        )
        return

    # ---------------------------------------------------------------
    # State always belongs to the user who pressed the button.
    # ---------------------------------------------------------------

    user_id = _state_user_id(query)
    state = get_state(user_id)

    # ---------------------------------------------------------------
    # /transfer picker (season toggle / done / cancel)
    # ---------------------------------------------------------------

    if data.startswith("xfer_"):
        await transfer.handle_callback(
            client,
            query,
            data,
            state,
        )
        return

    # ---------------------------------------------------------------
    # NEXT SEASON
    # ---------------------------------------------------------------

    if _is_finished_callback(data):
        if state.processing:
            await _answer(
                query,
                "The previous batch is still processing.",
                show_alert=True,
            )
            return

        if state.batch:
            await _answer(
                query,
                "You still have files waiting in the batch.",
                show_alert=True,
            )
            return

        if state.pending_items or state.pending_valid_items:
            await _answer(
                query,
                "You still have a batch waiting for review.",
                show_alert=True,
            )
            return

        if not state.current_caption:
            await _answer(
                query,
                "No caption sequence is active.",
                show_alert=True,
            )
            return

        try:
            current_episode = detect_episode(state.current_caption)

            if current_episode is None:
                await _answer(
                    query,
                    "I couldn't detect Sxx/Exx in the current caption.",
                    show_alert=True,
                )
                return

            next_episode = next_season_episode(current_episode)

            state.current_caption = replace_episode(
                state.current_caption,
                next_episode,
            )

            state.preview_message_id = None
            state.pending_review_message_id = None

            await _answer(
                query,
                "Next season started.",
            )

            await _edit(
                query,
                "🔄 Next season started.\n\n"
                "Next caption:\n"
                f"{state.current_caption}",
            )

        except Exception as error:
            logger.error("Next season callback failed: %s", error)

            await _answer(
                query,
                "Could not start the next season.",
                show_alert=True,
            )

        return

    # ---------------------------------------------------------------
    # Validate all remaining stateful callbacks.
    # ---------------------------------------------------------------

    if not _validate_stateful_callback(
        query,
        state,
        data,
    ):
        await _answer(
            query,
            "This button is no longer active.",
            show_alert=True,
        )
        return

    # ---------------------------------------------------------------
    # Prevent interaction while processing.
    # ---------------------------------------------------------------

    if state.processing:
        await _answer(
            query,
            "⏳ The batch is already processing.",
        )
        return

    # ===============================================================
    # REVIEW YES
    # ===============================================================

    if data in _ACCEPT_CALLBACKS:
        # -----------------------------------------------------------
        # The pending valid list is the authoritative list for the
        # incoming burst.
        #
        # Do not use pending_items because that can contain rejected
        # black-sheep files.
        # -----------------------------------------------------------

        valid_items = list(state.pending_valid_items)

        # Compatibility fallback:
        # If an older command path already placed the valid files into
        # state.batch, preserve that behavior.
        if not valid_items and state.batch:
            valid_items = list(state.batch)

        if not valid_items:
            state.pending_review_message_id = None
            state.pending_items.clear()
            state.pending_valid_items.clear()
            state.pending_rejected_items.clear()

            await _answer(
                query,
                "There are no valid files to add.",
                show_alert=True,
            )
            return

        # -----------------------------------------------------------
        # Append the accepted incoming burst to the active batch.
        #
        # Existing confirmed files are preserved.
        # -----------------------------------------------------------

        existing_ids = {
            (item.chat_id, item.message_id) for item in state.batch
        }

        for item in valid_items:
            key = (item.chat_id, item.message_id)

            if key not in existing_ids:
                state.batch.append(item)
                existing_ids.add(key)

        # -----------------------------------------------------------
        # Review is consumed exactly once.
        # -----------------------------------------------------------

        state.pending_review_message_id = None
        state.pending_items.clear()
        state.pending_valid_items.clear()
        state.pending_rejected_items.clear()

        await _answer(
            query,
            "Batch accepted.",
        )

        # The review message becomes the active preview message.
        state.preview_message_id = query.message.id
        state.preview_chat_id = query.message.chat.id

        # -----------------------------------------------------------
        # Build the real preview using build_preview(state).
        # -----------------------------------------------------------

        try:
            captions, text = _preview_result(state)

        except Exception as error:
            logger.error("Could not build accepted preview: %s", error)

            await _answer(
                query,
                "Could not prepare the batch.",
                show_alert=True,
            )
            return

        if captions is None or text is None:
            await _answer(
                query,
                "Could not prepare the batch.",
                show_alert=True,
            )
            return

        try:
            await query.message.edit_text(
                text,
                reply_markup=batch_keyboard(len(state.batch)),
            )

        except Exception as error:
            logger.warning("Could not update review message: %s", error)

        return

    # ===============================================================
    # REVIEW NO
    # ===============================================================

    if data in _REJECT_CALLBACKS:
        # -----------------------------------------------------------
        # Cancel only the pending incoming burst.
        #
        # Do not destroy an already-confirmed active batch.
        # -----------------------------------------------------------

        # If the incoming burst was not separately populated, fall
        # back to cancelling the current batch as older behavior did.
        has_pending = bool(
            state.pending_items
            or state.pending_valid_items
            or state.pending_rejected_items
        )

        if has_pending:
            # Do NOT clear pending_* yet — the follow-up delete
            # confirmation still needs the item list.
            count = len(state.pending_items)

            await _answer(
                query,
                "Incoming batch rejected.",
            )

            await _edit(
                query,
                "❌ Incoming batch rejected.\n\n"
                f"Delete these {count} source message(s) "
                "from the chat?",
                _DELETE_SOURCES_KEYBOARD,
            )

            return

        state.batch.clear()
        state.pending_review_message_id = None
        state.preview_message_id = None

        await _delete_ack_messages(
            client,
            state,
        )

        await _answer(
            query,
            "Batch cancelled.",
        )

        await _edit(
            query,
            "❌ Batch cancelled.\n\n"
            "No source files were processed or deleted.",
        )

        return

    # ===============================================================
    # REVIEW NO -> DELETE CONFIRMATION
    #
    # Follow-up to "seq_batch_no": the user chose whether to delete
    # the rejected incoming burst's source messages. Either way we
    # restore the previous "Batch finished" screen if one exists.
    # ===============================================================

    if data in _DELETE_DECISION_CALLBACKS:
        items = list(state.pending_items)

        state.pending_items.clear()
        state.pending_valid_items.clear()
        state.pending_rejected_items.clear()
        state.pending_review_message_id = None

        deleted = 0
        delete_failed = 0

        wants_delete = data == "seq_batch_no_delete_yes"

        if wants_delete and items:
            deleted, delete_failed = await _delete_items_messages(
                client,
                items,
            )

        text, keyboard = _restore_finished_screen(state)

        if wants_delete:
            toast = f"Deleted {deleted} source message(s)."
            if delete_failed:
                toast += f" {delete_failed} could not be deleted."
        else:
            toast = "Kept the source messages."

        await _answer(
            query,
            toast,
        )

        await _edit(
            query,
            text,
            keyboard,
        )

        return

    # ===============================================================
    # MOVE UP / MOVE DOWN
    # ===============================================================

    if data.startswith("up:") or data.startswith("down:"):
        if not state.batch:
            await _answer(
                query,
                "Batch is empty.",
                show_alert=True,
            )
            return

        moving_up = data[0] == "u"

        try:
            index = int(data.split(":", 1)[1])
        except (ValueError, IndexError):
            await _answer(
                query,
                "Invalid position.",
                show_alert=True,
            )
            return

        size = len(state.batch)

        if moving_up:
            if index <= 0 or index >= size:
                await _answer(
                    query,
                    "Cannot move that item up.",
                )
                return

            first, second = index - 1, index
            toast = "Moved up."

        else:
            if index < 0 or index >= size - 1:
                await _answer(
                    query,
                    "Cannot move that item down.",
                )
                return

            first, second = index, index + 1
            toast = "Moved down."

        batch = state.batch
        batch[first], batch[second] = batch[second], batch[first]

        await _answer(
            query,
            toast,
        )

        if not await _refresh_preview(
            query,
            state,
        ):
            await _answer(
                query,
                "Could not refresh the preview.",
                show_alert=True,
            )

        return

    # ===============================================================
    # CANCEL NORMAL PREVIEW
    # ===============================================================

    if data == "cancel":
        state.batch.clear()

        state.preview_message_id = None
        state.pending_review_message_id = None

        state.pending_items.clear()
        state.pending_valid_items.clear()
        state.pending_rejected_items.clear()

        await _delete_ack_messages(
            client,
            state,
        )

        await _answer(
            query,
            "Batch cancelled.",
        )

        await _edit(
            query,
            "❌ Batch cancelled.\n\n"
            "No source files were changed or deleted.",
        )

        return

    # ===============================================================
    # CONFIRM NORMAL PREVIEW
    # ===============================================================

    if data == "confirm":
        if not state.batch:
            state.preview_message_id = None

            await _answer(
                query,
                "Batch is empty.",
                show_alert=True,
            )
            return

        if not state.current_caption:
            state.preview_message_id = None

            await _answer(
                query,
                "Caption sequence is no longer active.",
                show_alert=True,
            )
            return

        # -----------------------------------------------------------
        # Validate final assignments one more time.
        #
        # The result is handed to _process_batch so the plan is not
        # computed twice for the same batch.
        # -----------------------------------------------------------

        try:
            ordered_items, assignments = plan_batch(
                list(state.batch),
                state.current_caption,
            )

        except Exception as error:
            logger.error("Final batch planning failed: %s", error)

            await _answer(
                query,
                "Could not validate the batch.",
                show_alert=True,
            )
            return

        if not ordered_items:
            await _answer(
                query,
                "No valid files remain.",
                show_alert=True,
            )
            return

        state.batch = ordered_items

        # -----------------------------------------------------------
        # _process_batch sets processing itself.
        # -----------------------------------------------------------

        await _process_batch(
            client,
            query,
            state,
            preplanned=(ordered_items, assignments),
        )

        return

    # ===============================================================
    # COVER-ONLY BATCH (no caption sequence active)
    # ===============================================================

    if data == "cover_cancel":
        state.cover_batch.clear()
        state.cover_preview_message_id = None

        await _answer(
            query,
            "Cover batch cancelled.",
        )

        await _edit(
            query,
            "❌ Cover batch cancelled.\n\n"
            "No source files were changed or deleted.",
        )

        return

    if data == "cover_confirm":
        await _process_cover_batch(
            client,
            query,
            state,
        )

        return

    # ===============================================================
    # UNKNOWN CALLBACK
    # ===============================================================

    await _answer(
        query,
        "Unknown or expired button.",
        show_alert=True,
    )


# ---------------------------------------------------------------------------
# Registration helper
# ---------------------------------------------------------------------------


def register(app: Client) -> None:
    """
    Register callback handler on the Pyrogram application.
    """
    app.on_callback_query()(callback_handler)


# Backward-compatible alias.
register_callbacks = register
