from caption_bot.models import BatchItem, Episode
from caption_bot.sequence import (
    detect_episode,
    plan_batch,
    replace_episode,
)


def item(name):
    return BatchItem(
        1,
        1,
        name,
        name,
        detect_episode(name),
    )


def test_detection_formats():
    assert detect_episode("Show.S01E02.mkv") == Episode(1, 2)
    assert detect_episode("Show.S01.E02.mkv") == Episode(1, 2)
    assert detect_episode("Show.S01 E02.mkv") == Episode(1, 2)
    assert detect_episode("Show.S01 - E02.mkv") == Episode(1, 2)
    assert detect_episode("Show.S01 - 1080p - E02.mkv") == Episode(1, 2)
    assert detect_episode("show.s02.anything.anything.e10.mkv") == Episode(2, 10)


def test_s_and_e_are_independent():
    assert detect_episode("Show.E01.s02.mkv") == Episode(2, 1)
    assert detect_episode("Show.E01 - 1080p - S02.mkv") == Episode(2, 1)
    assert detect_episode("Show.E01.anything.S02.anything.mkv") == Episode(2, 1)


def test_s_and_e_can_be_far_apart():
    assert detect_episode(
        "Show.S02.2160p.WEB-DL.MULTI-AUDIO.E15.mkv"
    ) == Episode(2, 15)


def test_new_season_overrides_old_active_sequence():
    items = [
        item("x.S02.E01.mkv"),
        item("x.S02 E02.mkv"),
    ]

    ordered, eps = plan_batch(
        items,
        "S01E11.Name.mkv",
    )

    assert eps == [
        Episode(2, 1),
        Episode(2, 2),
    ]


def test_all_explicit_are_sorted():
    items = [
        item("x.S02E02.mkv"),
        item("x.S01E10.mkv"),
        item("x.S02E01.mkv"),
    ]

    _, eps = plan_batch(
        items,
        "S01E09.Name.mkv",
    )

    assert eps == [
        Episode(1, 10),
        Episode(2, 1),
        Episode(2, 2),
    ]


def test_unmarked_sequence():
    items = [
        item("one.mkv"),
        item("two.mkv"),
        item("three.mkv"),
    ]

    _, eps = plan_batch(
        items,
        "S01E07.Name.mkv",
    )

    assert eps == [
        Episode(1, 7),
        Episode(1, 8),
        Episode(1, 9),
    ]


def test_replace_preserves_width():
    assert (
        replace_episode(
            "S01E09.Show.mkv",
            Episode(2, 1),
        )
        == "S02E01.Show.mkv"
    )


def test_replace_preserves_text_between_markers():
    assert (
        replace_episode(
            "Show.S01 - 1080p - E09.mkv",
            Episode(2, 1),
        )
        == "Show.S02 - 1080p - E01.mkv"
    )


def test_replace_reversed_markers():
    assert (
        replace_episode(
            "Show.E09 - 1080p - S01.mkv",
            Episode(2, 1),
        )
        == "Show.E01 - 1080p - S02.mkv"
    )


def test_replace_three_digit_width():
    assert (
        replace_episode(
            "Show.S001E009.mkv",
            Episode(2, 12),
        )
        == "Show.S002E012.mkv"
    )
