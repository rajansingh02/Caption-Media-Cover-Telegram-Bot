"""`/transfer` — bundle finished seasons behind a single deep link.

Flow
----

/transfer shows one toggle button per archived season (populated from
caption_bot.archive, written by callbacks.py every time a batch finishes
processing). Once the user taps "Done", a random token is stored locally
together with the chosen season numbers, and the user gets back:

    https://t.me/<bot_username>?start=xfer_<token>

Opening that link from any account sends that account "/start xfer_<token>",
which looks the token up and resends every archived file for the selected
seasons, season by season, in episode order — using the stored file_id, so
nothing is downloaded or re-uploaded, and the original source chat does not
need to still contain the files.
"""

from __future__ import annotations

import asyncio

from pyrogram import filters
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

from .. import archive
from ..cover import send_video_with_cover
from ..state import get_state


# Delay between messages sent to the receiving chat, to stay well clear of
# Telegram's per-chat flood limits.
SEND_DELAY = 1.2


# ---------------------------------------------------------------------------
# Picker UI
# ---------------------------------------------------------------------------


def _season_keyboard(
    owner_id: int,
    selected: set[int],
) -> InlineKeyboardMarkup:
    seasons = archive.list_seasons(owner_id)

    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []

    for season in seasons:
        count = archive.season_episode_count(owner_id, season)
        mark = "✅" if season in selected else "⬜"

        row.append(
            InlineKeyboardButton(
                f"{mark} S{season:02d} ({count})",
                callback_data=f"xfer_toggle:{season}",
            )
        )

        if len(row) == 3:
            rows.append(row)
            row = []

    if row:
        rows.append(row)

    rows.append(
        [
            InlineKeyboardButton(
                "✅ Done",
                callback_data="xfer_done",
            ),
            InlineKeyboardButton(
                "❌ Cancel",
                callback_data="xfer_cancel",
            ),
        ]
    )

    return InlineKeyboardMarkup(rows)


def _picker_text(selected: set[int]) -> str:
    lines = [
        "📤 /transfer",
        "",
        "Tap the seasons you want to send, then Done.",
    ]

    if selected:
        lines.extend(
            [
                "",
                "Selected: "
                + ", ".join(
                    f"S{season:02d}" for season in sorted(selected)
                ),
            ]
        )

    return "\n".join(lines)


async def _answer(
    query: CallbackQuery,
    text: str = "",
    show_alert: bool = False,
) -> None:
    try:
        await query.answer(text, show_alert=show_alert)
    except Exception as error:
        print(f"Could not answer /transfer callback: {error}")


async def _edit(
    query: CallbackQuery,
    text: str,
    reply_markup=None,
) -> None:
    try:
        await query.edit_message_text(
            text,
            reply_markup=reply_markup,
        )
    except Exception as error:
        print(f"Could not edit /transfer message: {error}")


# ---------------------------------------------------------------------------
# Sending an archived episode
# ---------------------------------------------------------------------------


async def _send_archived_episode(client, chat_id: int, ep) -> None:
    """Resend one archived episode by file_id. No download/upload happens."""

    if ep.media_type == "video" and ep.cover_file_id:
        await asyncio.to_thread(
            send_video_with_cover,
            chat_id=chat_id,
            video_file_id=ep.file_id,
            caption=ep.caption,
            cover_file_id=ep.cover_file_id,
            width=ep.width,
            height=ep.height,
            duration=ep.duration,
            supports_streaming=ep.supports_streaming,
            has_spoiler=ep.has_spoiler,
        )
        return

    if ep.media_type == "video":
        await client.send_video(
            chat_id=chat_id,
            video=ep.file_id,
            caption=ep.caption,
            width=ep.width,
            height=ep.height,
            duration=ep.duration,
            supports_streaming=ep.supports_streaming,
            has_spoiler=ep.has_spoiler,
        )
        return

    if ep.media_type == "document":
        await client.send_document(
            chat_id=chat_id,
            document=ep.file_id,
            caption=ep.caption,
        )
        return

    if ep.media_type == "audio":
        await client.send_audio(
            chat_id=chat_id,
            audio=ep.file_id,
            caption=ep.caption,
        )
        return

    if ep.media_type == "animation":
        await client.send_animation(
            chat_id=chat_id,
            animation=ep.file_id,
            caption=ep.caption,
        )
        return

    if ep.media_type == "photo":
        await client.send_photo(
            chat_id=chat_id,
            photo=ep.file_id,
            caption=ep.caption,
        )
        return

    raise ValueError(f"Unknown media_type: {ep.media_type}")


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register(app) -> None:
    @app.on_message(
        filters.command("transfer") & filters.private
    )
    async def transfer_command(client, message):
        owner_id = message.from_user.id
        state = get_state(owner_id)

        seasons = archive.list_seasons(owner_id)

        if not seasons:
            await message.reply_text(
                "❌ No finished seasons yet.\n\n"
                "/transfer only sends seasons that have already "
                "been processed with /scaption — finish at least "
                "one batch first."
            )
            return

        state.transfer_selection = set()

        sent = await message.reply_text(
            _picker_text(state.transfer_selection),
            reply_markup=_season_keyboard(
                owner_id,
                state.transfer_selection,
            ),
        )

        state.transfer_message_id = sent.id

    # =========================================================
    # /start (including deep-link payloads: /start xfer_<token>)
    # =========================================================

    @app.on_message(
        filters.command("start") & filters.private
    )
    async def start_command(client, message):
        payload = (
            message.command[1]
            if len(message.command) > 1
            else None
        )

        if not payload or not payload.startswith("xfer_"):
            await message.reply_text(
                "👋 Send /scaption S01E01.Name.mkv to begin a "
                "caption sequence, or /transfer to send finished "
                "seasons to another account."
            )
            return

        token = payload[len("xfer_"):]
        record = archive.get_transfer(token)

        if record is None:
            await message.reply_text(
                "❌ This transfer link is invalid or no longer "
                "exists."
            )
            return

        await message.reply_text(
            f"📥 Receiving {len(record.seasons)} season(s)…"
        )

        for season in record.seasons:
            episodes = archive.get_season_episodes(
                record.owner_id,
                season,
            )

            if not episodes:
                await message.reply_text(
                    f"⚠️ Season {season:02d}: nothing archived "
                    "for it anymore, skipping."
                )
                continue

            await message.reply_text(
                f"📁 Season {season:02d} "
                f"({len(episodes)} file(s))"
            )

            sent = 0
            failed = 0

            for ep in episodes:
                try:
                    await _send_archived_episode(
                        client,
                        message.chat.id,
                        ep,
                    )
                    sent += 1

                except Exception as error:
                    failed += 1

                    print(
                        "Could not send archived episode "
                        f"S{ep.season:02d}E{ep.episode:02d}: "
                        f"{error}"
                    )

                await asyncio.sleep(SEND_DELAY)

            summary = f"✅ Season {season:02d}: {sent} sent"

            if failed:
                summary += f", {failed} failed"

            await message.reply_text(summary)

        await message.reply_text("✅ Transfer complete.")


async def handle_callback(
    client,
    query: CallbackQuery,
    data: str,
    state,
) -> None:
    """Entry point called from callbacks.py's dispatcher for every
    callback_data starting with "xfer_"."""

    owner_id = query.from_user.id

    if data.startswith("xfer_toggle:"):
        try:
            season = int(data.split(":", 1)[1])
        except ValueError:
            await _answer(query, "Invalid season.", show_alert=True)
            return

        if season in state.transfer_selection:
            state.transfer_selection.discard(season)
        else:
            state.transfer_selection.add(season)

        await _edit(
            query,
            _picker_text(state.transfer_selection),
            reply_markup=_season_keyboard(
                owner_id,
                state.transfer_selection,
            ),
        )
        await _answer(query)
        return

    if data == "xfer_done":
        if not state.transfer_selection:
            await _answer(
                query,
                "Select at least one season first.",
                show_alert=True,
            )
            return

        seasons = sorted(state.transfer_selection)
        token = archive.create_transfer(owner_id, seasons)

        state.transfer_selection = set()
        state.transfer_message_id = None

        me = client.me or await client.get_me()
        username = me.username if me else None

        if username:
            link = f"https://t.me/{username}?start=xfer_{token}"

            text = (
                "✅ Transfer link ready for "
                + ", ".join(f"S{s:02d}" for s in seasons)
                + f":\n\n{link}\n\n"
                "Open it from any account to receive the files, "
                "season by season."
            )
        else:
            text = (
                f"✅ Transfer token ready: xfer_{token}\n\n"
                "Could not detect the bot's username — from the "
                "other account, open this bot and send:\n"
                f"/start xfer_{token}"
            )

        await _edit(query, text)
        await _answer(query, "Link ready.")
        return

    if data == "xfer_cancel":
        state.transfer_selection = set()
        state.transfer_message_id = None

        await _edit(query, "❌ Transfer cancelled.")
        await _answer(query)
        return

    await _answer(query, "Unknown transfer action.", show_alert=True)
