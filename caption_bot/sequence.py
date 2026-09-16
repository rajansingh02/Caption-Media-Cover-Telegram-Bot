"""Smart season/episode detection and sequence planning.

Performance notes
-----------------

`detect_episode()` is the single hottest function in the bot: it runs for
every incoming file, again for every re-analysis of a burst, again for
every preview rebuild (each ⬆️/⬇️ press) and again for every item during
processing. Two changes keep that cheap:

1. Marker pairing is O(S + E) instead of O(S × E) with a sort. The old
   version built one tuple per (season, episode) combination and sorted
   the whole list; matches arrive in positional order, so a single
   two-pointer pass finds the same winner.

2. Results are memoised per string. Filenames and captions repeat
   constantly within a batch, so the regex scan usually runs once per
   distinct name instead of once per lookup.

Detection and replacement semantics are unchanged, with one fix: a marker
written with three or more digits (S001) now keeps its width, which is
what the docstring below and tests/test_sequence.py always expected.
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Optional

from .models import BatchItem, Episode


# ---------------------------------------------------------
# Independent Sxx / Exx markers
# ---------------------------------------------------------

#
# The leading zeros are captured as their own group purely so the written
# width can be measured with two len() calls instead of a Python-level
# scan over the match. The matching behaviour is identical to the
# original `S\s*0*(\d{1,3})(?!\d)` form.
#
SEASON_RE = re.compile(
    r"(?<![A-Za-z])S\s*(0*)(\d{1,3})(?!\d)",
    re.IGNORECASE,
)

EPISODE_RE = re.compile(
    r"(?<![A-Za-z])E\s*(0*)(\d{1,4})(?!\d)",
    re.IGNORECASE,
)

# Memoisation budget. Each entry is a couple of small tuples, so even a
# full cache costs well under 100 KB.
_CACHE_SIZE = 512


# One Sxx or Exx marker found in a string, as a plain tuple:
#
#     (start, end, value, width)
#
# A NamedTuple reads better but costs ~20% more to build, and two of
# these are constructed for every filename the bot looks at, so the
# layout is documented here and unpacked at each use instead.
_START = 0
_END = 1
_VALUE = 2
_WIDTH = 3

Marker = tuple[int, int, int, int]


def _markers(pattern: re.Pattern[str], text: str) -> list[Marker]:
    found: list[Marker] = []

    for match in pattern.finditer(text):
        zeros, digits = match.group(1, 2)

        value = int(digits)

        if value < 1:
            continue

        # Width is the digit count as written, so S001 stays three wide
        # and S01/S1 stay two wide. Both halves come straight from the
        # regex, so no Python-level scan of the match is needed.
        width = len(zeros) + len(digits)

        found.append(
            (
                match.start(),
                match.end(),
                value,
                width if width > 2 else 2,
            )
        )

    return found


@lru_cache(maxsize=_CACHE_SIZE)
def _best_marker_pair_cached(
    text: str,
) -> Optional[tuple[Marker, Marker]]:
    """
    Find the most sensible Sxx + Exx pair.

    Sxx and Exx do not need to be adjacent and do not need
    to appear in S-before-E order.

    If several markers exist, choose the pair with the smallest
    distance between them; ties prefer the earliest Sxx, then the
    earliest Exx.
    """
    seasons = _markers(SEASON_RE, text)

    if not seasons:
        return None

    episodes = _markers(EPISODE_RE, text)

    if not episodes:
        return None

    # Overwhelmingly the common case: one Sxx and one Exx in the name.
    if len(seasons) == 1 and len(episodes) == 1:
        return seasons[0], episodes[0]

    # Matches come out of finditer in increasing start order, so the
    # closest episode to each season is always one of the two straddling
    # it. Walking both lists once therefore finds the global winner.
    best: Optional[tuple[int, int, int]] = None
    best_pair: Optional[tuple[Marker, Marker]] = None

    index = 0
    total = len(episodes)

    for season in seasons:
        season_start = season[_START]

        while (
            index < total
            and episodes[index][_START] < season_start
        ):
            index += 1

        for candidate in (
            episodes[index - 1] if index else None,
            episodes[index] if index < total else None,
        ):
            if candidate is None:
                continue

            candidate_start = candidate[_START]

            key = (
                abs(season_start - candidate_start),
                season_start,
                candidate_start,
            )

            if best is None or key < best:
                best = key
                best_pair = (season, candidate)

    return best_pair


def _best_marker_pair(
    text: str | None,
) -> Optional[tuple[Marker, Marker]]:
    if not text:
        return None

    return _best_marker_pair_cached(text)


def detect_episode(text: str | None) -> Episode | None:
    pair = _best_marker_pair(text)

    if pair is None:
        return None

    season, episode = pair

    return Episode(season[_VALUE], episode[_VALUE])


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

    season_marker, episode_marker = pair

    season_token = f"S{episode.season:0{season_marker[_WIDTH]}d}"
    episode_token = f"E{episode.episode:0{episode_marker[_WIDTH]}d}"

    # Apply the markers in positional order so both spans stay valid,
    # and build the result with one join instead of repeated slicing.
    if season_marker[_START] <= episode_marker[_START]:
        first, first_token = season_marker, season_token
        second, second_token = episode_marker, episode_token
    else:
        first, first_token = episode_marker, episode_token
        second, second_token = season_marker, season_token

    return "".join(
        (
            text[: first[_START]],
            first_token,
            text[first[_END] : second[_START]],
            second_token,
            text[second[_END] :],
        )
    )


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


def _episode_sort_key(item: BatchItem) -> tuple[int, int]:
    detected = item.detected
    return (detected.season, detected.episode)


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

    # One pass to split explicit from unmarked instead of three
    # separate comprehensions over the batch.
    explicit: list[BatchItem] = []
    unmarked: list[BatchItem] = []

    for item in items:
        if item.detected is not None:
            explicit.append(item)
        else:
            unmarked.append(item)

    # ---------------------------------------------------------
    # Every item has an explicit episode.
    # ---------------------------------------------------------
    if not unmarked:
        ordered = sorted(explicit, key=_episode_sort_key)

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

    # ---------------------------------------------------------
    # Mixed explicit + unmarked files
    # ---------------------------------------------------------
    if explicit:
        anchors = sorted(explicit, key=_episode_sort_key)

        # A newer explicit season/episode becomes the effective
        # starting point instead of blindly continuing the old
        # caption sequence.
        effective_start = current

        for item in anchors:
            detected = item.detected

            if detected is not None and detected > current:
                effective_start = detected
                break

        first_anchor = anchors[0].detected

        before: list[BatchItem] = []
        after: list[BatchItem] = []

        if (
            first_anchor.season == effective_start.season
            and first_anchor.episode > effective_start.episode
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
        assignments = []

        # -----------------------------------------------------
        # Fill before first explicit anchor.
        # -----------------------------------------------------
        if before:
            start_episode = (
                first_anchor.episode
                - len(before)
            )

            if start_episode < 1:
                start_episode = effective_start.episode

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

    assignments = []

    cursor = current

    for index, item in enumerate(ordered):
        episode = cursor if index == 0 else increment(cursor)

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


def cache_info():
    """Expose the detection cache stats (handy when profiling)."""
    return _best_marker_pair_cached.cache_info()


def clear_cache() -> None:
    _best_marker_pair_cached.cache_clear()
