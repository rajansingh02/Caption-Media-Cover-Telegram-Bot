"""Preview text and inline keyboards.

Performance notes
-----------------

* `replace_episode` was imported inside the caption loop; it is now a
  normal module-level import.
* The preview text is assembled from a list of chunks and joined once.
* `batch_keyboard()` is memoised for the last few batch sizes. Every
  ⬆️/⬇️ press rebuilds the preview with the *same* number of files, so
  the two-buttons-per-row markup (up to ~200 objects for a large batch)
  is now built once per batch size instead of once per press.
"""

from functools import lru_cache

from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .config import MAX_CAPTION_LENGTH
from .sequence import plan_batch, replace_episode


_CONFIRM_ROW = [
    InlineKeyboardButton("✅ Confirm & process", callback_data="confirm")
]
_CANCEL_ROW = [
    InlineKeyboardButton("❌ Cancel batch", callback_data="cancel")
]


def build_preview(state):
    try:
        ordered, episodes = plan_batch(state.batch, state.current_caption)
    except ValueError as exc:
        return None, str(exc)

    # Keep the same state object but make the final order explicit before confirmation.
    state.batch[:] = ordered

    captions = []

    for episode in episodes:
        caption = replace_episode(state.current_caption, episode)

        if len(caption) > MAX_CAPTION_LENGTH:
            return None, "Generated caption exceeds Telegram's 1024-character limit."

        captions.append(caption)

    lines = [
        f"📦 Batch: {len(state.batch)} file(s)",
        "🧠 Smart season/episode detection enabled.",
        "📌 Filename SxxExx markers override the active sequence.",
        (
            "🖼️ Video cover: ON (applies to videos only)."
            if state.cover_file_id
            else "🖼️ Video cover: OFF."
        ),
        "",
    ]

    append = lines.append

    for index, (item, caption, episode) in enumerate(
        zip(state.batch, captions, episodes),
        1,
    ):
        source = "detected" if item.detected else "inferred"
        append(
            f"{index}. {item.display_name}\n"
            f"   → {caption}\n"
            f"   ({source}: S{episode.season:02d}E{episode.episode:02d})"
        )

    append("\nReview the order before confirming.")

    return captions, "\n".join(lines)


@lru_cache(maxsize=4)
def batch_keyboard(count: int) -> InlineKeyboardMarkup:
    keyboard = []

    for index in range(count):
        row = []

        if index > 0:
            row.append(
                InlineKeyboardButton(
                    f"⬆️ {index + 1}",
                    callback_data=f"up:{index}",
                )
            )

        if index < count - 1:
            row.append(
                InlineKeyboardButton(
                    f"⬇️ {index + 1}",
                    callback_data=f"down:{index}",
                )
            )

        if row:
            keyboard.append(row)

    keyboard.append(_CONFIRM_ROW)
    keyboard.append(_CANCEL_ROW)

    return InlineKeyboardMarkup(keyboard)


_FINISHED_KEYBOARD = InlineKeyboardMarkup(
    [[InlineKeyboardButton("🔄 Start Next Season", callback_data="nextseason")]]
)


def finished_keyboard() -> InlineKeyboardMarkup:
    return _FINISHED_KEYBOARD
