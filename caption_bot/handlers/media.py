import asyncio
from collections import Counter

from pyrogram import filters
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from ..config import MAX_BATCH_SIZE
from ..models import BatchItem, Episode
from ..sequence import detect_episode
from ..state import get_state


# Telegram normally delivers a multi-file burst very quickly.
# Wait for a short quiet period before analyzing the incoming files.
COLLECTION_DELAY = 2.5


MEDIA_FILTER = filters.private & (
    filters.video
    | filters.document
    | filters.audio
    | filters.photo
    | filters.animation
)


def get_filename(message):
    """
    Return the actual Telegram media filename.

    Existing Telegram captions are intentionally ignored.
    """
    for media in (
        message.document,
        message.video,
        message.audio,
        message.animation,
    ):
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


def build_batch_item(message) -> BatchItem:
    """
    Convert one Telegram media message into a BatchItem.

    No source message is deleted here.
    """

    filename = get_filename(message)

    media_type = None
    file_id = None

    width = None
    height = None
    duration = None

    supports_streaming = False
    has_spoiler = False

    if message.video:
        media_type = "video"
        file_id = message.video.file_id

        width = message.video.width
        height = message.video.height
        duration = message.video.duration

        supports_streaming = bool(
            message.video.supports_streaming
        )

        has_spoiler = bool(
            getattr(
                message.video,
                "has_spoiler",
                False,
            )
        )

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

    return BatchItem(
        message_id=message.id,
        chat_id=message.chat.id,
        filename=filename,
        display_name=get_display_name(message),
        caption=message.caption,
        detected=detect_episode(filename),
        media_type=media_type,
        file_id=file_id,
        width=width,
        height=height,
        duration=duration,
        supports_streaming=supports_streaming,
        has_spoiler=has_spoiler,
    )


def _expected_episode(state) -> Episode | None:
    """
    Determine the next expected episode.

    Empty active batch:
        use current_caption.

    Existing batch:
        continue after the highest detected/assigned episode.

    This is important because current_caption is the START of the
    current processing sequence and must not remain the expected
    episode after files have already been added to the batch.
    """

    current = detect_episode(
        state.current_caption
    )

    if current is None:
        return None

    existing = []

    for item in state.batch:
        episode = (
            item.assigned
            if item.assigned is not None
            else item.detected
        )

        if episode is not None:
            existing.append(episode)

    if not existing:
        return current

    highest = max(existing)

    return Episode(
        highest.season,
        highest.episode + 1,
    )


def _dominant_season(
    items: list[BatchItem],
    expected: Episode | None,
) -> int | None:
    """
    Determine which explicit season most likely represents the
    incoming batch.

    Primary rule:
        highest number of explicit files.

    Tie breakers:
        1. highest number of unique episodes
        2. closest season to the currently expected season
        3. lowest season number for deterministic behavior
    """

    explicit = [
        item.detected
        for item in items
        if item.detected is not None
    ]

    if not explicit:
        return None

    counts = Counter(
        episode.season
        for episode in explicit
    )

    highest_count = max(
        counts.values()
    )

    candidates = [
        season
        for season, count in counts.items()
        if count == highest_count
    ]

    if len(candidates) > 1:
        unique_counts = {
            season: len(
                {
                    episode.episode
                    for episode in explicit
                    if episode.season == season
                }
            )
            for season in candidates
        }

        highest_unique = max(
            unique_counts.values()
        )

        candidates = [
            season
            for season in candidates
            if unique_counts[season] == highest_unique
        ]

    if (
        len(candidates) > 1
        and expected is not None
    ):
        candidates.sort(
            key=lambda season: (
                abs(
                    season
                    - expected.season
                ),
                season,
            )
        )

    return candidates[0]


def _analyse_incoming(
    state,
    items: list[BatchItem],
):
    """
    Analyze the entire incoming burst as one batch.

    Returns:

        reason
            None
            "next_season"
            "wrong_batch"

        valid_items
            Files that belong to the detected batch.

        rejected_items
            Black sheep / duplicate explicit files.

        dominant_season
            Detected batch season.

        expected
            Episode expected before this incoming batch.
    """

    expected = _expected_episode(state)

    explicit = [
        item
        for item in items
        if item.detected is not None
    ]

    # No explicit SxxExx anywhere.
    #
    # Keep the existing inference behavior.
    if not explicit:
        return (
            None,
            list(items),
            [],
            None,
            expected,
        )

    dominant = _dominant_season(
        items,
        expected,
    )

    valid: list[BatchItem] = []
    rejected: list[BatchItem] = []

    seen_episodes: set[Episode] = set()

    for item in items:
        detected = item.detected

        # No explicit marker.
        #
        # Do not call it a black sheep. The existing sequence planner
        # can infer its position later.
        if detected is None:
            valid.append(item)
            continue

        # Explicit file belongs to another season.
        if detected.season != dominant:
            rejected.append(item)
            continue

        # Same episode twice in one incoming batch.
        #
        # Keep the first occurrence and mark later duplicates red.
        if detected in seen_episodes:
            rejected.append(item)
            continue

        seen_episodes.add(detected)

        valid.append(item)

    reason = None

    if (
        expected is not None
        and dominant is not None
    ):
        if dominant == expected.season:
            # Same season.
            #
            # The FIRST explicit episode should match the expected
            # episode. Internal gaps are allowed.
            #
            # Example:
            #
            # expected S03E01
            # S03E01
            # S03E02
            # S03E04
            #
            # is valid. E03 is simply missing.
            explicit_valid = [
                item.detected
                for item in valid
                if item.detected is not None
            ]

            if explicit_valid:
                first_episode = min(
                    explicit_valid
                )

                if (
                    first_episode.episode
                    != expected.episode
                ):
                    reason = "wrong_batch"

        elif dominant == expected.season + 1:
            # Next season.
            reason = "next_season"

        else:
            # Jumped multiple seasons, went backwards,
            # or otherwise doesn't belong to the active sequence.
            reason = "wrong_batch"

    # Any black sheep makes this require confirmation.
    if rejected:
        if reason is None:
            reason = "wrong_batch"

    return (
        reason,
        valid,
        rejected,
        dominant,
        expected,
    )


def _format_episode(
    episode: Episode | None,
) -> str:
    if episode is None:
        return "unknown"

    return (
        f"S{episode.season:02d}"
        f"E{episode.episode:02d}"
    )


def _short_name(
    name: str,
    maximum: int = 90,
) -> str:
    """
    Keep the review message safely below Telegram's message
    size limit even with large batches and long filenames.
    """

    if len(name) <= maximum:
        return name

    return (
        name[: maximum - 3]
        + "..."
    )


def _build_review_text(
    state,
    items,
    valid,
    rejected,
    dominant,
    expected,
    reason,
):
    current = detect_episode(
        state.current_caption
    )

    if reason == "next_season":
        title = "⚠️ Next season detected"
    else:
        title = (
            "⚠️ Are you sure you sent "
            "the correct batch?"
        )

    lines = [
        title,
        "",
        f"Current sequence: "
        f"{_format_episode(current)}",
        f"Next expected file: "
        f"{_format_episode(expected)}",
    ]

    valid_explicit = [
        item.detected
        for item in valid
        if item.detected is not None
    ]

    if (
        dominant is not None
        and valid_explicit
    ):
        first = min(valid_explicit)
        last = max(valid_explicit)

        if first == last:
            lines.append(
                "Detected batch: "
                f"{_format_episode(first)}"
            )
        else:
            lines.append(
                "Detected batch: "
                f"{_format_episode(first)}"
                " → "
                f"{_format_episode(last)}"
            )

    lines.extend(
        [
            "",
            f"Received files "
            f"({len(items)}):",
        ]
    )

    for item in items:
        if item in rejected:
            marker = "🔴"
        elif item.detected is not None:
            marker = "🟢"
        else:
            marker = "🟡"

        if item.detected is not None:
            detected_text = (
                f" — {_format_episode(item.detected)}"
            )
        else:
            detected_text = (
                " — SxxExx not detected"
            )

        lines.append(
            f"{marker} "
            f"{_short_name(item.display_name)}"
            f"{detected_text}"
        )

    if rejected:
        lines.extend(
            [
                "",
                (
                    f"🔴 {len(rejected)} file(s) "
                    "are black sheep and will NOT "
                    "be added."
                ),
            ]
        )

    # Explicit valid files all belong to one season.
    if valid and all(
        item.detected is not None
        for item in valid
    ):
        seasons = {
            item.detected.season
            for item in valid
            if item.detected is not None
        }

        if len(seasons) == 1:
            lines.append(
                ""
                f"🧠 All {len(valid)} valid "
                f"files belong to "
                f"S{dominant:02d}."
            )

    # Explain gaps without treating them as errors.
    if valid_explicit:
        episode_numbers = sorted(
            {
                episode.episode
                for episode in valid_explicit
            }
        )

        if len(episode_numbers) >= 2:
            missing = []

            for number in range(
                episode_numbers[0],
                episode_numbers[-1] + 1,
            ):
                if number not in episode_numbers:
                    missing.append(number)

            if missing:
                if len(missing) <= 8:
                    missing_text = ", ".join(
                        f"E{number:02d}"
                        for number in missing
                    )
                else:
                    missing_text = (
                        f"{len(missing)} episodes"
                    )

                lines.extend(
                    [
                        "",
                        (
                            "ℹ️ Missing episode(s) in "
                            "this batch: "
                            f"{missing_text}"
                        ),
                        (
                            "Missing episodes are not "
                            "treated as errors."
                        ),
                    ]
                )

    lines.extend(
        [
            "",
            "Continue with the detected batch?",
        ]
    )

    return "\n".join(lines)


async def _finalize_incoming(
    client,
    user_id: int,
):
    """
    Wait for the incoming burst to become quiet, then classify
    the entire burst once.
    """

    try:
        await asyncio.sleep(
            COLLECTION_DELAY
        )
    except asyncio.CancelledError:
        return

    state = get_state(user_id)

    # A newer task has replaced this one.
    if (
        state.collection_task
        is not asyncio.current_task()
    ):
        return

    state.collection_task = None

    if not state.incoming_batch:
        return

    items = list(
        state.incoming_batch
    )
    state.incoming_batch.clear()

    # Never lose files if /preview or processing happened while
    # the collection timer was active.
    if (
        state.processing
        or state.preview_message_id
    ):
        state.incoming_batch.extend(items)
        return

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

    # Completely normal incoming batch.
    if reason is None:
        if (
            len(state.batch)
            + len(valid)
            > MAX_BATCH_SIZE
        ):
            state.incoming_batch.extend(
                items
            )

            await client.send_message(
                user_id,
                (
                    f"⚠️ Batch limit reached "
                    f"({MAX_BATCH_SIZE}).\n\n"
                    "Use /preview before adding "
                    "more files."
                ),
            )

            return

        state.batch.extend(valid)

        ack = await client.send_message(
            user_id,
            (
                f"➕ Added {len(valid)} "
                "file(s) to batch.\n\n"
                + "\n".join(
                    f"• {_short_name(item.display_name, 100)}"
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
    # One confirmation for the WHOLE incoming burst.
    # ---------------------------------------------------------

    state.pending_items = list(items)
    state.pending_valid_items = list(valid)
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
        user_id,
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


def _cover_only_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ Confirm & apply cover",
                    callback_data="cover_confirm",
                ),
                InlineKeyboardButton(
                    "❌ Cancel",
                    callback_data="cover_cancel",
                ),
            ]
        ]
    )


def _build_cover_preview_text(
    state,
    items: list[BatchItem],
) -> str:
    videos = sum(
        1
        for item in items
        if item.media_type == "video"
    )

    lines = [
        "🖼️ Cover-only mode "
        "(no caption sequence active).",
        "",
        f"📦 Batch: {len(items)} file(s), "
        f"{videos} video(s) will get the "
        "saved cover.",
        "Other files are passed through "
        "unchanged.",
        "",
    ]

    for index, item in enumerate(items, 1):
        lines.append(
            f"{index}. "
            f"{_short_name(item.display_name, 100)}"
        )

    lines.extend(
        [
            "",
            "Confirm to process?",
        ]
    )

    return "\n".join(lines)


async def _finalize_cover_only(
    client,
    user_id: int,
):
    """Wait for the incoming burst to become quiet, then show a
    cover-only confirmation for the whole burst."""

    try:
        await asyncio.sleep(
            COLLECTION_DELAY
        )
    except asyncio.CancelledError:
        return

    state = get_state(user_id)

    if (
        state.cover_collection_task
        is not asyncio.current_task()
    ):
        return

    state.cover_collection_task = None

    if not state.cover_batch:
        return

    # A caption sequence or normal preview started meanwhile —
    # let that flow take over instead.
    if (
        state.current_caption
        or state.processing
        or state.cover_preview_message_id
    ):
        return

    items = list(state.cover_batch)

    # Sort into a sensible sequence when every file carries a
    # detectable SxxExx marker; otherwise keep arrival order.
    if items and all(item.detected for item in items):
        items.sort(
            key=lambda item: (
                item.detected.season,
                item.detected.episode,
            )
        )

    state.cover_batch = items

    review = await client.send_message(
        user_id,
        _build_cover_preview_text(
            state,
            items,
        ),
        reply_markup=_cover_only_keyboard(),
    )

    state.cover_preview_message_id = review.id


async def _receive_cover_only(
    client,
    message,
    state,
    user_id: int,
):
    """Collect media for cover-only mode (no /scaption active)."""

    if state.processing:
        await message.reply_text(
            "⏳ The current batch is still "
            "processing."
        )
        return

    if state.cover_preview_message_id:
        await message.reply_text(
            "⚠️ A cover batch is waiting for "
            "confirmation.\n\n"
            "Confirm or cancel it first."
        )
        return

    if (
        len(state.cover_batch)
        >= MAX_BATCH_SIZE
    ):
        await message.reply_text(
            f"⚠️ Batch limit reached "
            f"({MAX_BATCH_SIZE})."
        )
        return

    state.cover_batch.append(
        build_batch_item(message)
    )

    if (
        state.cover_collection_task
        and not state.cover_collection_task.done()
    ):
        state.cover_collection_task.cancel()

    state.cover_collection_task = (
        asyncio.create_task(
            _finalize_cover_only(
                client,
                user_id,
            )
        )
    )


def register(app):
    @app.on_message(MEDIA_FILTER)
    async def receive(client, message):
        if not message.from_user:
            return

        user_id = message.from_user.id
        state = get_state(user_id)

        # -----------------------------------------------------
        # Cover upload mode
        # -----------------------------------------------------

        if state.awaiting_cover:
            if not message.photo:
                await message.reply_text(
                    "❌ Please send a photo for "
                    "the cover, or use /cover off."
                )
                return

            state.cover_file_id = (
                message.photo.file_id
            )

            state.cover_message_id = (
                message.id
            )

            state.awaiting_cover = False

            await message.reply_text(
                "✅ Video cover saved.\n\n"
                "It will be applied automatically "
                "to video files in future batches."
            )

            return

        # -----------------------------------------------------
        # No active caption sequence
        #
        # If a cover is saved, run in cover-only mode instead of
        # dropping the media: just re-send with the cover applied,
        # no caption rewriting, no /scaption required.
        # -----------------------------------------------------

        if not state.current_caption:
            if state.cover_file_id:
                await _receive_cover_only(
                    client,
                    message,
                    state,
                    user_id,
                )
            return

        # -----------------------------------------------------
        # Currently processing
        # -----------------------------------------------------

        if state.processing:
            await message.reply_text(
                "⏳ The current batch is still "
                "processing."
            )
            return

        # -----------------------------------------------------
        # Existing normal preview
        # -----------------------------------------------------

        if state.preview_message_id:
            await message.reply_text(
                "⚠️ A batch is waiting for "
                "confirmation.\n\n"
                "Confirm or cancel it first."
            )
            return

        # -----------------------------------------------------
        # Existing batch-level warning
        #
        # Normally this means the user should answer the
        # confirmation instead of sending more files.
        #
        # Do NOT produce one spam message per incoming file.
        # -----------------------------------------------------

        if state.pending_items:
            await message.reply_text(
                "⚠️ A batch confirmation is "
                "waiting.\n\n"
                "Please answer it before "
                "sending another batch."
            )
            return

        # -----------------------------------------------------
        # Add this file to the temporary incoming burst.
        # -----------------------------------------------------

        active_count = (
            len(state.batch)
            + len(state.incoming_batch)
        )

        if active_count >= MAX_BATCH_SIZE:
            await message.reply_text(
                f"⚠️ Batch limit reached "
                f"({MAX_BATCH_SIZE}).\n\n"
                "Use /preview."
            )
            return

        item = build_batch_item(
            message
        )

        state.incoming_batch.append(
            item
        )

        # Cancel the previous quiet-period timer.
        if (
            state.collection_task
            and not state.collection_task.done()
        ):
            state.collection_task.cancel()

        # Start a fresh quiet-period timer.
        state.collection_task = (
            asyncio.create_task(
                _finalize_incoming(
                    client,
                    user_id,
                )
            )
        )
