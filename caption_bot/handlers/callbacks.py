import asyncio

from pyrogram import Client

from ..cover import CoverSendError, send_video_with_cover
from ..preview import batch_keyboard, build_preview, finished_keyboard
from ..sequence import detect_episode, next_caption_from_successes, next_season_episode, replace_episode
from ..state import get_state


def _copy_item(client, item, caption, cover_file_id):
    """Return an awaitable that outputs the item without downloading it."""
    if item.media_type == "video" and cover_file_id:
        return send_video_with_cover(
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
    return client.copy_message(
        chat_id=item.chat_id,
        from_chat_id=item.chat_id,
        message_id=item.message_id,
        caption=caption,
    )


def register(app: Client):
    @app.on_callback_query()
    async def callback(client, query):
        if not query.from_user:
            return
        state = get_state(query.from_user.id)
        data = query.data or ""

        if not query.message or query.message.chat.type != "private" or query.message.chat.id != query.from_user.id:
            await query.answer("This button belongs to another user.", show_alert=True)
            return

        if data.startswith(("up:", "down:")):
            try:
                index = int(data.split(":", 1)[1])
            except (ValueError, IndexError):
                await query.answer("Invalid position.", show_alert=True)
                return
            if data.startswith("up:"):
                if not (0 < index < len(state.batch)):
                    await query.answer("Cannot move that item up.")
                    return
                state.batch[index - 1], state.batch[index] = state.batch[index], state.batch[index - 1]
            else:
                if not (0 <= index < len(state.batch) - 1):
                    await query.answer("Cannot move that item down.")
                    return
                state.batch[index], state.batch[index + 1] = state.batch[index + 1], state.batch[index]

            captions, text = build_preview(state)
            if captions is None:
                await query.answer("Could not rebuild preview.", show_alert=True)
                return
            await query.answer("Order updated.")
            await query.message.edit_text(text, reply_markup=batch_keyboard(len(state.batch)))
            return

        if data == "cancel":
            for item in list(state.batch):
                try:
                    await client.delete_messages(item.chat_id, item.message_id)
                except Exception as exc:
                    print(f"Could not delete cancelled source {item.message_id}: {exc}")
            state.batch.clear()
            state.preview_message_id = None
            state.preview_chat_id = None
            await query.answer("Batch cancelled.")
            await query.message.edit_text("❌ Batch cancelled.\n\nPending source messages were removed when possible.")
            return

        if data == "nextseason":
            if state.processing:
                await query.answer("The bot is still processing.", show_alert=True)
                return
            if state.batch:
                await query.answer("Finish the current batch first.", show_alert=True)
                return
            if not state.current_caption:
                await query.answer("No active caption sequence.", show_alert=True)
                return
            episode = detect_episode(state.current_caption)
            if not episode:
                await query.answer("Current caption has no SxxExx.", show_alert=True)
                return
            state.current_caption = replace_episode(
                state.current_caption, next_season_episode(episode)
            )
            state.preview_message_id = None
            await query.answer("Next season started.")
            await query.message.edit_text(
                f"🔄 Next season started.\n\nNext caption:\n{state.current_caption}"
            )
            return

        if data != "confirm":
            await query.answer("Unknown action.", show_alert=True)
            return

        if state.processing:
            await query.answer("Already processing.", show_alert=True)
            return
        if not state.batch:
            await query.answer("Batch is empty.", show_alert=True)
            return
        if not state.current_caption:
            await query.answer("Caption sequence is not active.", show_alert=True)
            return

        captions, _ = build_preview(state)
        if captions is None:
            await query.answer("Could not generate captions.", show_alert=True)
            return

        state.processing = True
        await query.answer("Processing...")
        try:
            cover_status = "\n🖼️ Saved video cover will be applied to videos." if state.cover_file_id else ""
            await query.message.edit_text(
                "⏳ Processing batch...\n\n"
                "Copying all files first. Source messages will be deleted only after the batch copy phase finishes."
                + cover_status
            )
        except Exception:
            pass

        items = list(state.batch)
        successful = []
        failed = []

        # Phase 1: output every item. No source deletion occurs here.
        for item, caption in zip(items, captions):
            try:
                if item.media_type == "video" and state.cover_file_id:
                    # Bot API sendVideo accepts both the existing video file_id
                    # and the existing cover file_id. Neither file is uploaded.
                    await _run_cover_send(
                        item=item,
                        caption=caption,
                        cover_file_id=state.cover_file_id,
                    )
                else:
                    await client.copy_message(
                        chat_id=item.chat_id,
                        from_chat_id=item.chat_id,
                        message_id=item.message_id,
                        caption=caption,
                    )
                successful.append((item, item.assigned))
            except Exception as exc:
                failed.append((item, item.assigned, exc))
                print(f"Error processing {item.display_name}: {exc}")

        # Phase 2: delete only sources whose output was confirmed successful.
        deleted = 0
        for item, _ in successful:
            try:
                await client.delete_messages(item.chat_id, item.message_id)
                deleted += 1
            except Exception as exc:
                print(f"Could not delete source {item.message_id}: {exc}")

        successful_eps = [ep for _, ep in successful if ep is not None]
        failed_eps = [ep for _, ep, _ in failed if ep is not None]

        try:
            state.current_caption = next_caption_from_successes(
                state.current_caption, successful_eps, failed_eps
            )
        except ValueError:
            pass

        state.batch.clear()
        state.preview_message_id = None
        state.preview_chat_id = None
        state.processing = False

        text = (
            "✅ Batch finished.\n\n"
            f"Processed: {len(successful)}\n"
            f"Failed: {len(failed)}\n"
            f"Source messages deleted: {deleted}\n\n"
            f"Next caption:\n{state.current_caption}"
        )
        if state.cover_file_id:
            text += "\n\n🖼️ Video cover: ON"
        if failed:
            text += "\n\n⚠️ Failed source messages were NOT deleted. You can resend them."

        try:
            await query.message.edit_text(text, reply_markup=finished_keyboard())
        except Exception as exc:
            print(f"Could not update batch result: {exc}")


async def _run_cover_send(*, item, caption, cover_file_id):
    # The Bot API helper is intentionally isolated so cover-specific failures
    # are treated exactly like any other batch failure and the source is kept.
    return await asyncio.to_thread(
        send_video_with_cover,
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
