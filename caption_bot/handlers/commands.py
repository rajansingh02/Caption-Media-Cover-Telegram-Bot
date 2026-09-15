from pyrogram import filters

from ..preview import batch_keyboard, build_preview
from ..sequence import detect_episode, next_season_episode, replace_episode
from ..state import get_state, reset_state


def register(app):
    @app.on_message(filters.command("scaption") & filters.private)
    async def scaption(client, message):
        state = get_state(message.from_user.id)
        if len(message.command) < 2:
            await message.reply_text("Usage:\n/scaption S01E01.Trip.mkv")
            return
        caption = message.text.split(None, 1)[1].strip()
        if state.batch:
            await message.reply_text("⚠️ Finish or cancel the current batch first.")
            return
        if not detect_episode(caption):
            await message.reply_text("❌ Caption must contain SxxExx, e.g. S01E01 or S01 E01.")
            return
        state.current_caption = caption
        state.preview_message_id = None
        state.preview_chat_id = None
        await message.reply_text(
            f"✅ Caption sequence set:\n\n{caption}\n\n"
            "Send your files. When finished, use /preview."
        )

    @app.on_message(filters.command("preview") & filters.private)
    async def preview(client, message):
        state = get_state(message.from_user.id)
        if not state.current_caption:
            await message.reply_text("❌ No caption sequence. Use /scaption S01E01.Name.mkv")
            return
        if not state.batch:
            await message.reply_text("❌ No files are waiting in the batch.")
            return
        captions, text = build_preview(state)
        if captions is None:
            await message.reply_text(f"❌ {text}")
            return
        if state.preview_message_id:
            try:
                await client.edit_message_text(
                    message.chat.id,
                    state.preview_message_id,
                    text,
                    reply_markup=batch_keyboard(len(state.batch)),
                )
                return
            except Exception:
                state.preview_message_id = None
        sent = await message.reply_text(text, reply_markup=batch_keyboard(len(state.batch)))
        state.preview_message_id = sent.id
        state.preview_chat_id = message.chat.id

    @app.on_message(filters.command("cover") & filters.private)
    async def cover_command(client, message):
        state = get_state(message.from_user.id)
        args = message.text.split(maxsplit=1)
        arg = args[1].strip().lower() if len(args) > 1 else ""

        if arg in {"off", "disable", "remove", "none"}:
            state.cover_file_id = None
            state.cover_message_id = None
            state.awaiting_cover = False
            await message.reply_text("🖼️ Video cover disabled.")
            return

        if arg in {"status", "show"}:
            if state.cover_file_id:
                await message.reply_text("🖼️ Video cover: ON\n\nNew video outputs will use the saved cover.")
            else:
                await message.reply_text("🖼️ Video cover: OFF")
            return

        # `/cover` as a reply to an existing photo is the fastest setup path.
        if message.reply_to_message and message.reply_to_message.photo:
            state.cover_file_id = _photo_file_id(message.reply_to_message)
            state.cover_message_id = message.reply_to_message.id
            state.awaiting_cover = False
            await message.reply_text(
                "✅ Video cover saved.\n\n"
                "It will be applied to video files in future batches without downloading or re-uploading the video."
            )
            return

        state.awaiting_cover = True
        await message.reply_text(
            "🖼️ Send the cover image now.\n\n"
            "The image will be saved as a Telegram file_id and reused for future videos.\n"
            "No video will be downloaded or re-uploaded.\n\n"
            "Use /cover off to disable it."
        )

    @app.on_message(filters.command("nextseason") & filters.private)
    async def nextseason(client, message):
        state = get_state(message.from_user.id)
        if state.batch:
            await message.reply_text("⚠️ Finish or cancel the current batch first.")
            return
        if not state.current_caption:
            await message.reply_text("❌ No caption sequence is active. Use /scaption first.")
            return
        episode = detect_episode(state.current_caption)
        if not episode:
            await message.reply_text("❌ No SxxExx sequence is active.")
            return
        state.current_caption = replace_episode(
            state.current_caption, next_season_episode(episode)
        )
        await message.reply_text(
            f"🔄 Next season started.\n\nNext caption:\n{state.current_caption}"
        )

    @app.on_message(filters.command("stop") & filters.private)
    async def stop(client, message):
        state = get_state(message.from_user.id)
        for item in list(state.batch):
            try:
                await client.delete_messages(item.chat_id, item.message_id)
            except Exception as exc:
                print(f"Could not delete pending source {item.message_id}: {exc}")
        reset_state(message.from_user.id)
        await message.reply_text(
            "🛑 Caption sequence stopped.\n\nPending batch cancelled."
        )
