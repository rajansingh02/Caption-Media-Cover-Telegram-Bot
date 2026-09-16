"""Incoming media: burst collection, classification and review screens.

Performance notes
-----------------

* Membership tests used to be `if item in rejected` against a list of
  BatchItems. Each test ran the dataclass `__eq__` over 14 fields for
  every candidate, i.e. O(files × rejected × 14) per review screen. They
  are now identity lookups in a set.
* Classification walks the burst once instead of building three separate
  filtered lists.
* Review/acknowledgement text is appended to one list and joined once.
* States created by a stray message from an unrelated user are released
  again instead of being kept forever.
"""

import asyncio

from pyrogram import filters
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from ..config import COLLECTION_DELAY, MAX_BATCH_SIZE
from ..models import BatchItem, Episode
from ..sequence import detect_episode
from ..state import get_state, release_if_idle


MEDIA_FILTER = filters.private & (
    filters.video
    | filters.document
    | filters.audio
    | filters.photo
    | filters.animation
)


# Built once instead of per review screen.
_REVIEW_KEYBOARD = InlineKeyboardMarkup(
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

_COVER_KEYBOARD = InlineKeyboardMarkup(
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


def review_keyboard() -> InlineKeyboardMarkup:
    return _REVIEW_KEYBOARD


def get_filename(message):
    """
    Return the actual Telegram media filename.

    Existing Telegram captions are intentionally ignored.
    """
    media = (
        message.document
        or message.video
        or message.audio
        or message.animation
    )

    if media is not None:
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

    video = message.video

    if video:
        media_type = "video"
        file_id = video.file_id

        width = video.width
        height = video.height
        duration = video.duration

        supports_streaming = bool(video.supports_streaming)

        has_spoiler = bool(
            getattr(
                video,
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

    current = detect_episode(state.current_caption)

    if current is None:
        return None

    # One pass, no intermediate list: track the highest episode already
    # sitting in the confirmed batch.
    highest: Episode | None = None

    for item in state.batch:
        episode = item.assigned or item.detected

        if episode is not None and (
            highest is None or episode > highest
        ):
            highest = episode

    if highest is None:
        return current

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

    # counts: season -> number of explicit files
    # uniques: season -> set of episode numbers
    counts: dict[int, int] = {}
    uniques: dict[int, set[int]] = {}

    for item in items:
        detected = item.detected

        if detected is None:
            continue

        season = detected.season

        counts[season] = counts.get(season, 0) + 1

        bucket = uniques.get(season)

        if bucket is None:
            uniques[season] = {detected.episode}
        else:
            bucket.add(detected.episode)

    if not counts:
        return None

    highest_count = max(counts.values())

    candidates = [
        season
        for season, count in counts.items()
        if count == highest_count
    ]

    if len(candidates) > 1:
        highest_unique = max(
            len(uniques[season]) for season in candidates
        )

        candidates = [
            season
            for season in candidates
            if len(uniques[season]) == highest_unique
        ]

    if len(candidates) == 1:
        return candidates[0]

    if expected is not None:
        expected_season = expected.season

        return min(
            candidates,
            key=lambda season: (
                abs(season - expected_season),
                season,
            ),
        )

    return min(candidates)


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

    has_explicit = False

    for item in items:
        if item.detected is not None:
            has_explicit = True
            break

    # No explicit SxxExx anywhere.
    #
    # Keep the existing inference behavior.
    if not has_explicit:
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

    # Lowest explicit episode among the accepted files, tracked here so
    # the check below does not need a second pass.
    first_valid: Episode | None = None

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

        if first_valid is None or detected < first_valid:
            first_valid = detected

        valid.append(item)

    reason = None

    if expected is not None and dominant is not None:
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
            if (
                first_valid is not None
                and first_valid.episode != expected.episode
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
    if rejected and reason is None:
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

    return f"S{episode.season:02d}E{episode.episode:02d}"


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

    return name[: maximum - 3] + "..."


def _build_review_text(
    state,
    items,
    valid,
    rejected,
    dominant,
    expected,
    reason,
):
    current = detect_episode(state.current_caption)

    if reason == "next_season":
        title = "⚠️ Next season detected"
    else:
        title = "⚠️ Are you sure you sent the correct batch?"

    lines = [
        title,
        "",
        f"Current sequence: {_format_episode(current)}",
        f"Next expected file: {_format_episode(expected)}",
    ]

    append = lines.append

    # Identity set: avoids running BatchItem.__eq__ over every field for
    # every file in the burst.
    rejected_ids = {id(item) for item in rejected}

    first: Episode | None = None
    last: Episode | None = None
    episode_numbers: set[int] = set()

    valid_count = len(valid)
    valid_seasons: set[int] = set()
    all_valid_explicit = True

    # Single pass over the accepted files gathers everything the summary
    # lines below need.
    for item in valid:
        detected = item.detected

        if detected is None:
            all_valid_explicit = False
            continue

        valid_seasons.add(detected.season)
        episode_numbers.add(detected.episode)

        if first is None or detected < first:
            first = detected

        if last is None or detected > last:
            last = detected

    if dominant is not None and first is not None:
        if first == last:
            append(f"Detected batch: {_format_episode(first)}")
        else:
            append(
                "Detected batch: "
                f"{_format_episode(first)} → {_format_episode(last)}"
            )

    append("")
    append(f"Received files ({len(items)}):")

    for item in items:
        detected = item.detected

        if id(item) in rejected_ids:
            marker = "🔴"
        elif detected is not None:
            marker = "🟢"
        else:
            marker = "🟡"

        if detected is not None:
            detected_text = f" — {_format_episode(detected)}"
        else:
            detected_text = " — SxxExx not detected"

        append(f"{marker} {_short_name(item.display_name)}{detected_text}")

    if rejected:
        append("")
        append(
            f"🔴 {len(rejected)} file(s) are black sheep "
            "and will NOT be added."
        )

    # Explicit valid files all belong to one season.
    if valid_count and all_valid_explicit and len(valid_seasons) == 1:
        append(
            f"🧠 All {valid_count} valid files belong to S{dominant:02d}."
        )

    # Explain gaps without treating them as errors.
    if len(episode_numbers) >= 2:
        low = min(episode_numbers)
        high = max(episode_numbers)

        missing = [
            number
            for number in range(low, high + 1)
            if number not in episode_numbers
        ]

        if missing:
            if len(missing) <= 8:
                missing_text = ", ".join(
                    f"E{number:02d}" for number in missing
                )
            else:
                missing_text = f"{len(missing)} episodes"

            append("")
            append(
                f"ℹ️ Missing episode(s) in this batch: {missing_text}"
            )
            append("Missing episodes are not treated as errors.")

    append("")
    append("Continue with the detected batch?")

    return "\n".join(lines)


def build_ack_text(valid, maximum: int = 100) -> str:
    """"Added to batch" acknowledgement, built with a single join."""
    parts = [f"➕ Added {len(valid)} file(s) to batch.\n"]

    for item in valid:
        parts.append(f"• {_short_name(item.display_name, maximum)}")

    parts.append("\nWhen finished, use /preview.")

    return "\n".join(parts)


async def _finalize_incoming(
    client,
    user_id: int,
):
    """
    Wait for the incoming burst to become quiet, then classify
    the entire burst once.
    """

    try:
        await asyncio.sleep(COLLECTION_DELAY)
    except asyncio.CancelledError:
        return

    state = get_state(user_id)

    # A newer task has replaced this one.
    if state.collection_task is not asyncio.current_task():
        return

    state.collection_task = None

    if not state.incoming_batch:
        return

    items = list(state.incoming_batch)
    state.incoming_batch.clear()

    # Never lose files if /preview or processing happened while
    # the collection timer was active.
    if state.processing or state.preview_message_id:
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
        if len(state.batch) + len(valid) > MAX_BATCH_SIZE:
            state.incoming_batch.extend(items)

            await client.send_message(
                user_id,
                (
                    f"⚠️ Batch limit reached ({MAX_BATCH_SIZE}).\n\n"
                    "Use /preview before adding more files."
                ),
            )

            return

        state.batch.extend(valid)

        ack = await client.send_message(
            user_id,
            build_ack_text(valid),
        )

        state.batch_ack_message_ids.append(ack.id)

        return

    # ---------------------------------------------------------
    # One confirmation for the WHOLE incoming burst.
    # ---------------------------------------------------------

    state.pending_items = list(items)
    state.pending_valid_items = list(valid)
    state.pending_rejected_items = list(rejected)

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
        reply_markup=_REVIEW_KEYBOARD,
    )

    state.pending_review_message_id = review.id


def _cover_only_keyboard() -> InlineKeyboardMarkup:
    return _COVER_KEYBOARD


def _build_cover_preview_text(
    state,
    items: list[BatchItem],
) -> str:
    videos = 0

    for item in items:
        if item.media_type == "video":
            videos += 1

    lines = [
        "🖼️ Cover-only mode (no caption sequence active).",
        "",
        f"📦 Batch: {len(items)} file(s), "
        f"{videos} video(s) will get the saved cover.",
        "Other files are passed through unchanged.",
        "",
    ]

    append = lines.append

    for index, item in enumerate(items, 1):
        append(f"{index}. {_short_name(item.display_name, 100)}")

    append("")
    append("Confirm to process?")

    return "\n".join(lines)


async def _finalize_cover_only(
    client,
    user_id: int,
):
    """Wait for the incoming burst to become quiet, then show a
    cover-only confirmation for the whole burst."""

    try:
        await asyncio.sleep(COLLECTION_DELAY)
    except asyncio.CancelledError:
        return

    state = get_state(user_id)

    if state.cover_collection_task is not asyncio.current_task():
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
    if all(item.detected is not None for item in items):
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
        reply_markup=_COVER_KEYBOARD,
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
            "⏳ The current batch is still processing."
        )
        return

    if state.cover_preview_message_id:
        await message.reply_text(
            "⚠️ A cover batch is waiting for confirmation.\n\n"
            "Confirm or cancel it first."
        )
        return

    if len(state.cover_batch) >= MAX_BATCH_SIZE:
        await message.reply_text(
            f"⚠️ Batch limit reached ({MAX_BATCH_SIZE})."
        )
        return

    state.cover_batch.append(build_batch_item(message))

    task = state.cover_collection_task

    if task is not None and not task.done():
        task.cancel()

    state.cover_collection_task = asyncio.create_task(
        _finalize_cover_only(
            client,
            user_id,
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
                    "❌ Please send a photo for the cover, "
                    "or use /cover off."
                )
                return

            state.cover_file_id = message.photo.file_id
            state.cover_message_id = message.id
            state.awaiting_cover = False

            await message.reply_text(
                "✅ Video cover saved.\n\n"
                "It will be applied automatically to video files "
                "in future batches."
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
            else:
                # Nothing is configured for this user: do not keep an
                # empty state object around.
                release_if_idle(user_id)

            return

        # -----------------------------------------------------
        # Currently processing
        # -----------------------------------------------------

        if state.processing:
            await message.reply_text(
                "⏳ The current batch is still processing."
            )
            return

        # -----------------------------------------------------
        # Existing normal preview
        # -----------------------------------------------------

        if state.preview_message_id:
            await message.reply_text(
                "⚠️ A batch is waiting for confirmation.\n\n"
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
                "⚠️ A batch confirmation is waiting.\n\n"
                "Please answer it before sending another batch."
            )
            return

        # -----------------------------------------------------
        # Add this file to the temporary incoming burst.
        # -----------------------------------------------------

        active_count = len(state.batch) + len(state.incoming_batch)

        if active_count >= MAX_BATCH_SIZE:
            await message.reply_text(
                f"⚠️ Batch limit reached ({MAX_BATCH_SIZE}).\n\n"
                "Use /preview."
            )
            return

        state.incoming_batch.append(build_batch_item(message))

        # Cancel the previous quiet-period timer.
        task = state.collection_task

        if task is not None and not task.done():
            task.cancel()

        # Start a fresh quiet-period timer.
        state.collection_task = asyncio.create_task(
            _finalize_incoming(
                client,
                user_id,
            )
        )
