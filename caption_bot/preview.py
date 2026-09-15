from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .config import MAX_CAPTION_LENGTH
from .sequence import plan_batch


def build_preview(state):
    try:
        ordered, episodes = plan_batch(state.batch, state.current_caption)
    except ValueError as exc:
        return None, str(exc)

    # Keep the same state object but make the final order explicit before confirmation.
    state.batch[:] = ordered
    captions = []
    for episode in episodes:
        from .sequence import replace_episode
        caption = replace_episode(state.current_caption, episode)
        if len(caption) > MAX_CAPTION_LENGTH:
            return None, "Generated caption exceeds Telegram's 1024-character limit."
        captions.append(caption)

    lines = [
        f"📦 Batch: {len(state.batch)} file(s)",
        "🧠 Smart season/episode detection enabled.",
        "📌 Filename SxxExx markers override the active sequence.",
    ]
    if state.cover_file_id:
        lines.append("🖼️ Video cover: ON (applies to videos only).")
    else:
        lines.append("🖼️ Video cover: OFF.")
    lines.append("")
    for index, (item, caption, episode) in enumerate(zip(state.batch, captions, episodes), 1):
        source = "detected" if item.detected else "inferred"
        lines.append(
            f"{index}. {item.display_name}\n"
            f"   → {caption}\n"
            f"   ({source}: S{episode.season:02d}E{episode.episode:02d})"
        )
    lines.append("\nReview the order before confirming.")
    return captions, "\n".join(lines)


def batch_keyboard(count: int) -> InlineKeyboardMarkup:
    keyboard = []
    for index in range(count):
        row = []
        if index > 0:
            row.append(InlineKeyboardButton(f"⬆️ {index + 1}", callback_data=f"up:{index}"))
        if index < count - 1:
            row.append(InlineKeyboardButton(f"⬇️ {index + 1}", callback_data=f"down:{index}"))
        if row:
            keyboard.append(row)
    keyboard.append([InlineKeyboardButton("✅ Confirm & process", callback_data="confirm")])
    keyboard.append([InlineKeyboardButton("❌ Cancel batch", callback_data="cancel")])
    return InlineKeyboardMarkup(keyboard)


def finished_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Start Next Season", callback_data="nextseason")]
    ])
