from pyrogram import filters

from ..config import MAX_BATCH_SIZE
from ..models import BatchItem
from ..sequence import detect_episode
from ..state import get_state

MEDIA_FILTER = filters.private & (
    filters.video | filters.document | filters.audio | filters.photo | filters.animation
)


def get_filename(message):
    for media in (message.document, message.video, message.audio, message.animation):
        if media:
            return media.file_name
    return None


def get_display_name(message):
    filename = get_filename(message)
    if filename:
        return filename
    if message.photo:
        return "Photo"
    if message.video:
        return "Video"
    if message.audio:
        return "Audio"
    if message.document:
        return "Document"
    if message.animation:
        return "Animation"
    return "Media"


def register(app):
    @app.on_message(MEDIA_FILTER)
    async def receive(client, message):
        state = get_state(message.from_user.id)

        # A photo immediately following `/cover` becomes the reusable cover
        # instead of entering the caption batch.
        if state.awaiting_cover:
            if not message.photo:
                await message.reply_text("❌ Please send a photo for the cover, or use /cover off.")
                return
            state.cover_file_id = message.photo.file_id
            state.cover_message_id = message.id
            state.awaiting_cover = False
            await message.reply_text(
                "✅ Video cover saved.\n\n"
                "It will be applied automatically to video files in future batches."
            )
            return

        if not state.current_caption:
            return
        if state.processing:
            await message.reply_text("⏳ The current batch is still processing.")
            return
        if state.preview_message_id:
            await message.reply_text("⚠️ A batch is waiting for confirmation. Confirm or cancel it first.")
            return
        if len(state.batch) >= MAX_BATCH_SIZE:
            await message.reply_text(f"⚠️ Batch limit reached ({MAX_BATCH_SIZE}). Use /preview.")
            return

        filename = get_filename(message)
        detected = detect_episode(filename)

        media_type = None
        file_id = None
        width = height = duration = None
        supports_streaming = False
        has_spoiler = False

        if message.video:
            media_type = "video"
            file_id = message.video.file_id
            width = message.video.width
            height = message.video.height
            duration = message.video.duration
            supports_streaming = bool(message.video.supports_streaming)
            has_spoiler = bool(getattr(message.video, "has_spoiler", False))
        elif message.document:
            media_type = "document"
            file_id = message.document.file_id
        elif message.audio:
            media_type = "audio"
            file_id = message.audio.file_id
        elif message.animation:
            media_type = "animation"
            file_id = message.animation.file_id
        elif message.photo:
            media_type = "photo"
            file_id = message.photo.file_id

        item = BatchItem(
            message_id=message.id,
            chat_id=message.chat.id,
            filename=filename,
            display_name=get_display_name(message),
            detected=detected,
            media_type=media_type,
            file_id=file_id,
            width=width,
            height=height,
            duration=duration,
            supports_streaming=supports_streaming,
            has_spoiler=has_spoiler,
        )
        state.batch.append(item)

        if detected:
            hint = f"🧠 Detected S{detected.season:02d}E{detected.episode:02d}."
        else:
            hint = "🧠 No SxxExx marker; I'll infer its episode from the batch."

        cover_hint = "\n🖼️ Saved video cover: ON." if state.cover_file_id and media_type == "video" else ""
        await message.reply_text(
            f"➕ Added to batch: {len(state.batch)}\n\n"
            f"{item.display_name}\n{hint}{cover_hint}\n\n"
            "When finished, use /preview."
        )
