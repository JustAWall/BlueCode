import random

from bluecode import art


def test_all_arts_are_clean():
    arts = art.all_arts()
    assert len(arts) == 8, f"expected 8 unique arts, got {len(arts)}"
    for banner in arts:
        assert banner == banner.strip("\n")
        assert not any(line != line.rstrip() for line in banner.splitlines())
        assert art.art_width(banner) > 0


def test_arts_are_distinct():
    arts = art.all_arts()
    assert len(set(arts)) == len(arts)


def test_duplicates_are_collapsed():
    """Two pasted pairs were identical; the pool must not be inflated."""
    cleaned = [art._clean(raw) for raw in art.ARTS]
    assert len(set(cleaned)) == len(cleaned) == len(art.all_arts())


def test_pick_returns_one_of_the_arts():
    for seed in range(50):
        banner = art.pick(width=200, height=60, rng=random.Random(seed))
        assert banner in art.all_arts()


def test_pick_reaches_every_art_on_a_big_terminal():
    seen = {
        art.pick(width=200, height=60, rng=random.Random(seed))
        for seed in range(300)
    }
    assert len(seen) == len(art.all_arts())


def test_pick_falls_back_to_narrowest_on_tiny_width():
    chosen = art.pick(width=10, height=60, rng=random.Random(0))
    assert art.art_width(chosen) == min(map(art.art_width, art.all_arts()))


def test_pick_respects_width():
    for seed in range(30):
        banner = art.pick(width=45, height=60, rng=random.Random(seed))
        assert art.art_width(banner) <= 45


def test_pick_respects_height():
    for seed in range(30):
        banner = art.pick(width=200, height=5, rng=random.Random(seed))
        assert art.art_height(banner) <= 5


def test_tall_art_never_shown_in_short_terminal():
    tall = max(art.all_arts(), key=art.art_height)
    for seed in range(100):
        banner = art.pick(width=200, height=10, rng=random.Random(seed))
        assert banner != tall


def test_art_can_be_disabled(monkeypatch):
    monkeypatch.setenv("BLUECODE_NO_ART", "1")
    assert art.pick(width=200, height=60) is None
