"""Smart season/episode detection and sequence planning."""

from __future__ import annotations

import re
from typing import Iterable

from .models import BatchItem, Episode


# ---------------------------------------------------------
# Independent Sxx / Exx markers
# ---------------------------------------------------------

SEASON_RE = re.compile(
    r"(?<![A-Za-z])S\s*0*(\d{1,3})(?!\d)",
    re.IGNORECASE,
)

EPISODE_RE = re.compile(
    r"(?<![A-Za-z])E\s*0*(\d{1,4})(?!\d)",
    re.IGNORECASE,
)


def _find_markers(text: str | None):
    if not text:
        return [], []

    seasons = [
        match
        for match in SEASON_RE.finditer(text)
        if int(match.group(1)) >= 1
    ]

    episodes = [
        match
        for match in EPISODE_RE.finditer(text)
        if int(match.group(1)) >= 1
    ]

    return seasons, episodes


def _best_marker_pair(text: str | None):
    """
    Find the most sensible Sxx + Exx pair.

    Sxx and Exx do not need to be adjacent and do not need
    to appear in S-before-E order.

    If several markers exist, choose the pair with the smallest
    distance between them.
    """
    seasons, episodes = _find_markers(text)

    if not seasons or not episodes:
        return None

    candidates = []

    for season_match in seasons:
        for episode_match in episodes:
            distance = abs(
                season_match.start() - episode_match.start()
            )

            candidates.append(
                (
                    distance,
                    season_match.start(),
                    episode_match.start(),
                    season_match,
                    episode_match,
                )
            )

    candidates.sort(
        key=lambda value: (
            value[0],
            value[1],
            value[2],
        )
    )

    return candidates[0][3], candidates[0][4]


def detect_episode(text: str | None) -> Episode | None:
    pair = _best_marker_pair(text)

    if pair is None:
        return None

    season_match, episode_match = pair

    season = int(season_match.group(1))
    episode = int(episode_match.group(1))

    if season < 1 or episode < 1:
        return None

    return Episode(season, episode)


def _marker_width(match) -> int:
    """
    Preserve at least two digits.

    S01 -> width 2
    S001 -> width 3
    S1 -> width 2
    """
    return max(2, len(match.group(1)))


def replace_episode(text: str, episode: Episode) -> str:
    """
    Replace Sxx and Exx independently.

    Text between the Sxx and Exx markers is preserved.

    Examples:

        S01E09
        ->
        S02E01

        S01 - 1080p - E09
        ->
        S02 - 1080p - E01

        E09 - 1080p - S01
        ->
        E01 - 1080p - S02
    """
    pair = _best_marker_pair(text)

    if pair is None:
        raise ValueError(
            "No Sxx/Exx markers found in caption"
        )

    season_match, episode_match = pair

    season_width = _marker_width(season_match)
    episode_width = _marker_width(episode_match)

    season_token = (
        f"S{episode.season:0{season_width}d}"
    )

    episode_token = (
        f"E{episode.episode:0{episode_width}d}"
    )

    replacements = [
        (
            season_match.start(),
            season_match.end(),
            season_token,
        ),
        (
            episode_match.start(),
            episode_match.end(),
            episode_token,
        ),
    ]

    # Replace from right to left so the original match
    # positions remain valid.
    result = text

    for start, end, replacement in sorted(
        replacements,
        reverse=True,
    ):
        result = (
            result[:start]
            + replacement
            + result[end:]
        )

    return result


def increment(episode: Episode) -> Episode:
    return Episode(
        episode.season,
        episode.episode + 1,
    )


def next_season_episode(episode: Episode) -> Episode:
    return Episode(
        episode.season + 1,
        1,
    )


def plan_batch(
    items: list[BatchItem],
    current_caption: str,
) -> tuple[list[BatchItem], list[Episode]]:
    """
    Return final batch order and one exact Episode assignment
    per item.

    Explicit filename markers are authoritative.

    If a filename contains a newer season such as S02E01 while
    the active caption is S01E11, S02 wins automatically.
    """

    current = detect_episode(current_caption)

    if current is None:
        raise ValueError(
            "Current caption must contain Sxx/Exx"
        )

    if not items:
        return [], []

    # ---------------------------------------------------------
    # Every item has an explicit episode.
    # ---------------------------------------------------------
    if all(item.detected for item in items):
        ordered = sorted(
            items,
            key=lambda item: (
                item.detected.season,
                item.detected.episode,
            ),
        )

        assignments: list[Episode] = []

        for item in ordered:
            # IMPORTANT:
            # Store the detected episode on the item itself.
            # callbacks.py uses item.assigned after successful
            # processing to calculate the next caption.
            episode = item.detected

            item.assigned = episode
            assignments.append(episode)

        return ordered, assignments  # type: ignore[arg-type]

    explicit = [
        item
        for item in items
        if item.detected
    ]

    # ---------------------------------------------------------
    # Mixed explicit + unmarked files
    # ---------------------------------------------------------
    if explicit:
        anchors = sorted(
            explicit,
            key=lambda item: (
                item.detected.season,
                item.detected.episode,
            ),
        )

        unmarked = [
            item
            for item in items
            if not item.detected
        ]

        # A newer explicit season/episode becomes the effective
        # starting point instead of blindly continuing the old
        # caption sequence.
        newer = [
            item.detected
            for item in anchors
            if item.detected and item.detected > current
        ]

        effective_start = (
            min(newer)
            if newer
            else current
        )

        first_anchor = anchors[0].detected

        before: list[BatchItem] = []
        after: list[BatchItem] = []

        if (
            first_anchor.season
            == effective_start.season
            and first_anchor.episode
            > effective_start.episode
        ):
            capacity = (
                first_anchor.episode
                - effective_start.episode
            )

            before = unmarked[:capacity]
            after = unmarked[capacity:]

        else:
            after = unmarked

        result: list[BatchItem] = []
        assignments: list[Episode] = []

        # -----------------------------------------------------
        # Fill before first explicit anchor.
        # -----------------------------------------------------
        if before:
            start_episode = (
                first_anchor.episode
                - len(before)
            )

            if start_episode < 1:
                start_episode = (
                    effective_start.episode
                )

            for offset, item in enumerate(before):
                episode = Episode(
                    first_anchor.season,
                    start_episode + offset,
                )

                item.assigned = episode
                result.append(item)
                assignments.append(episode)

        # -----------------------------------------------------
        # Explicit anchors.
        # -----------------------------------------------------
        for item in anchors:
            episode = item.detected

            item.assigned = episode

            result.append(item)
            assignments.append(episode)

        # -----------------------------------------------------
        # Remaining unmarked files continue after the final
        # explicit anchor.
        # -----------------------------------------------------
        if assignments:
            cursor = assignments[-1]
        else:
            cursor = effective_start

        for item in after:
            cursor = increment(cursor)

            item.assigned = cursor

            result.append(item)
            assignments.append(cursor)

        return result, assignments

    # ---------------------------------------------------------
    # No explicit filenames.
    #
    # Continue directly from the active caption.
    # ---------------------------------------------------------
    ordered = list(items)

    assignments: list[Episode] = []

    cursor = current

    for index, item in enumerate(ordered):
        episode = (
            cursor
            if index == 0
            else increment(cursor)
        )

        item.assigned = episode

        assignments.append(episode)

        cursor = episode

    return ordered, assignments


def next_caption_from_successes(
    current_caption: str,
    successful_eps: list[Episode],
    failed_eps: list[Episode] | None = None,
) -> str:
    """
    Calculate the next caption from the episodes that were actually
    assigned/sent successfully.

    The important rule is:

        NEXT = highest successfully processed episode + 1

    This means the function does NOT blindly increment the original
    /scaption value.

    Examples:

        current_caption = S01E01
        successful = [S01E01 ... S01E13]
        -> S01E14

        current_caption = S01E01
        successful = [S02E01 ... S02E05]
        -> S02E06

    This also preserves the rest of the user's caption template.
    """

    if not successful_eps:
        return current_caption

    # The last/highest successfully assigned episode is the real
    # source of truth for the next sequence.
    last_episode = max(successful_eps)

    next_episode = increment(last_episode)

    return replace_episode(
        current_caption,
        next_episode,
    )
