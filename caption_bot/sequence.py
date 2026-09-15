"""Smart season/episode detection and sequence planning."""

from __future__ import annotations

import re
from typing import Iterable

from .models import BatchItem, Episode

# S01E02, S01.E02, S01 E02, S01-E02, and even S01 [other text] E02.
# The important rule is that Sxx and Exx do not have to touch.
EPISODE_RE = re.compile(
    r"(?<![A-Za-z0-9])S\s*0*(\d{1,3})(?!\d).*?E\s*0*(\d{1,4})(?!\d)",
    re.IGNORECASE,
)


def detect_episode(text: str | None) -> Episode | None:
    if not text:
        return None
    match = EPISODE_RE.search(text)
    if not match:
        return None
    season = int(match.group(1))
    episode = int(match.group(2))
    if season < 1 or episode < 1:
        return None
    return Episode(season, episode)


def _episode_widths(text: str) -> tuple[int, int]:
    match = EPISODE_RE.search(text or "")
    if not match:
        return 2, 2
    return max(2, len(match.group(1))), max(2, len(match.group(2)))


def replace_episode(text: str, episode: Episode) -> str:
    match = EPISODE_RE.search(text or "")
    if not match:
        raise ValueError("No SxxExx marker found in caption")
    season_width, episode_width = _episode_widths(text)
    token = f"S{episode.season:0{season_width}d}E{episode.episode:0{episode_width}d}"
    return text[: match.start()] + token + text[match.end() :]


def increment(episode: Episode) -> Episode:
    return Episode(episode.season, episode.episode + 1)


def next_season_episode(episode: Episode) -> Episode:
    return Episode(episode.season + 1, 1)


def _distance(a: Episode, b: Episode) -> int:
    # Used only as a consistency heuristic; season boundaries are deliberately
    # kept distinct so S01E99 -> S02E01 is not treated as S01E100.
    if a.season != b.season:
        return 10_000 + abs(a.season - b.season) * 1_000
    return abs(a.episode - b.episode)


def plan_batch(items: list[BatchItem], current_caption: str) -> tuple[list[BatchItem], list[Episode]]:
    """Return final batch order and one exact Episode assignment per item.

    Explicit filename markers are authoritative. If any filename says S02E01,
    the planner will not blindly continue an old S01 sequence. Unmarked files
    are inferred around explicit anchors and otherwise from the active caption.
    """
    current = detect_episode(current_caption)
    if current is None:
        raise ValueError("Current caption must contain SxxExx")

    # If every file is explicit, ordering by season/episode is unambiguous.
    if items and all(item.detected for item in items):
        ordered = sorted(items, key=lambda item: (item.detected.season, item.detected.episode))
        return ordered, [item.detected for item in ordered]  # type: ignore[arg-type]

    # With mixed explicit/unmarked names, first create a stable list. Explicit
    # markers define the anchors; unmarked files are then filled in sequence.
    explicit = [item for item in items if item.detected]
    if explicit:
        # Sort explicit anchors. Keep duplicate episodes stable by original order.
        anchors = sorted(explicit, key=lambda item: (item.detected.season, item.detected.episode))  # type: ignore[union-attr]

        # If there is a clear explicit season/episode newer than the active
        # caption, that explicit marker wins. This is the automatic next-season
        # detection the bot needs.
        effective_start = current
        newer = [item.detected for item in anchors if item.detected and item.detected > current]
        if newer:
            effective_start = min(newer)

        # Infer unmarked files from the active/new explicit context. If the
        # number of unmarked files can fit before the first explicit anchor,
        # place them immediately before it; otherwise append after the last
        # explicit anchor. This avoids inventing a season transition.
        unmarked = [item for item in items if not item.detected]
        if not unmarked:
            return anchors, [item.detected for item in anchors]  # type: ignore[arg-type]

        first_anchor = anchors[0].detected  # type: ignore[assignment]
        before: list[BatchItem] = []
        after: list[BatchItem] = []
        cursor = first_anchor
        if first_anchor.season == effective_start.season and first_anchor.episode > effective_start.episode:
            capacity = first_anchor.episode - effective_start.episode
            before = unmarked[:capacity]
            after = unmarked[capacity:]
        else:
            after = unmarked

        result: list[BatchItem] = []
        assignments: list[Episode] = []

        for offset, item in enumerate(before):
            ep = Episode(first_anchor.season, first_anchor.episode - len(before) + offset)
            if ep.episode < 1:
                ep = increment(current)
            item.assigned = ep
            result.append(item)
            assignments.append(ep)

        for item in anchors:
            ep = item.detected  # type: ignore[assignment]
            item.assigned = ep
            result.append(item)
            assignments.append(ep)

        cursor = assignments[-1] if assignments else effective_start
        for item in after:
            cursor = increment(cursor)
            item.assigned = cursor
            result.append(item)
            assignments.append(cursor)

        return result, assignments

    # No filename has a marker: simply continue the active caption.
    ordered = list(items)
    assignments = []
    cursor = current
    for index, item in enumerate(ordered):
        ep = cursor if index == 0 else increment(cursor)
        item.assigned = ep
        assignments.append(ep)
        cursor = ep
    return ordered, assignments


def next_caption_from_successes(current_caption: str, successful_episodes: Iterable[Episode], failed_episodes: Iterable[Episode]) -> str:
    """Choose the safest next caption.

    If anything failed, resume from the earliest failed assignment so a failed
    source can be retried without silently skipping its episode. Otherwise,
    continue after the highest processed episode.
    """
    successes = list(successful_episodes)
    failures = list(failed_episodes)
    if failures:
        target = min(failures)
    elif successes:
        target = increment(max(successes))
    else:
        target = detect_episode(current_caption)
        if target is None:
            raise ValueError("Current caption has no SxxExx")
    return replace_episode(current_caption, target)
