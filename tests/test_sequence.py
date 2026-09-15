from caption_bot.models import BatchItem, Episode
from caption_bot.sequence import detect_episode, plan_batch, replace_episode


def item(name):
    return BatchItem(1, 1, name, name, detect_episode(name))


def test_detection_formats():
    assert detect_episode('Show.S01E02.mkv') == Episode(1, 2)
    assert detect_episode('Show.S01.E02.mkv') == Episode(1, 2)
    assert detect_episode('Show.S01 E02.mkv') == Episode(1, 2)
    assert detect_episode('Show.S01 - 1080p - E02.mkv') == Episode(1, 2)
    assert detect_episode('show.s02.anything.anything.e10.mkv') == Episode(2, 10)


def test_new_season_overrides_old_active_sequence():
    items = [item('x.S02.E01.mkv'), item('x.S02 E02.mkv')]
    ordered, eps = plan_batch(items, 'S01E11.Name.mkv')
    assert eps == [Episode(2, 1), Episode(2, 2)]


def test_all_explicit_are_sorted():
    items = [item('x.S02E02.mkv'), item('x.S01E10.mkv'), item('x.S02E01.mkv')]
    _, eps = plan_batch(items, 'S01E09.Name.mkv')
    assert eps == [Episode(1, 10), Episode(2, 1), Episode(2, 2)]


def test_unmarked_sequence():
    items = [item('one.mkv'), item('two.mkv'), item('three.mkv')]
    _, eps = plan_batch(items, 'S01E07.Name.mkv')
    assert eps == [Episode(1, 7), Episode(1, 8), Episode(1, 9)]


def test_replace_preserves_width():
    assert replace_episode('S01E09.Show.mkv', Episode(2, 1)) == 'S02E01.Show.mkv'
