import random

from bluecode import art


def test_all_arts_are_clean_and_fit_80_columns():
    arts = art.all_arts()
    assert len(arts) == 3
    for banner in arts:
        assert banner == banner.strip("\n")
        assert not any(line != line.rstrip() for line in banner.splitlines())
        assert art.art_width(banner) <= 80, art.art_width(banner)


def test_arts_are_distinct():
    assert len(set(art.all_arts())) == 3


def test_pick_returns_one_of_the_arts():
    for seed in range(20):
        banner = art.pick(width=200, rng=random.Random(seed))
        assert banner in art.all_arts()


def test_pick_actually_varies():
    seen = {art.pick(width=200, rng=random.Random(seed)) for seed in range(50)}
    assert len(seen) == 3, "random selection should reach every art"


def test_pick_falls_back_to_narrowest_on_small_terminal():
    narrow = art.pick(width=10, rng=random.Random(0))
    assert narrow is not None
    assert art.art_width(narrow) == min(map(art.art_width, art.all_arts()))


def test_pick_respects_width_when_something_fits():
    narrowest = min(art.all_arts(), key=art.art_width)
    limit = art.art_width(narrowest)
    for seed in range(20):
        banner = art.pick(width=limit, rng=random.Random(seed))
        assert art.art_width(banner) <= limit


def test_art_can_be_disabled(monkeypatch):
    monkeypatch.setenv("BLUECODE_NO_ART", "1")
    assert art.pick(width=200) is None
