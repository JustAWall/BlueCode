"""Startup banner: one of several ASCII arts, picked at random.

Kept in its own module so the arts are easy to extend and easy to test:
each art must be printable as-is, with no trailing whitespace, and should
fit a standard 80-column terminal.
"""

from __future__ import annotations

import os
import random

_BLUE_CODE_FIGLET = r"""
____  _             ____          _
| __ )| |_   _  ___ / ___|___   __| | ___
|  _ \| | | | |/ _ \ |   / _ \ / _` |/ _ \
| |_) | | |_| |  __/ |__| (_) | (_| |  __/
|____/|_|\__,_|\___|\____\___/ \__,_|\___|
"""

_BLUE_CODE_SLANT = r"""
___    _                  ___               _
(  _`\ (_ )               (  _`\            ( )
| (_) ) | |  _   _    __  | ( (_)   _      _| |   __
|  _ <' | | ( ) ( ) /'__`\| |  _  /'_`\  /'_` | /'__`\
| (_) ) | | | (_) |(  ___/| (_( )( (_) )( (_| |(  ___
(____/'(___)`\___/'`\____)(____/'`\___/'`\__,_)`\____)
"""

_BLUE_CODE_BLOCKS = r"""
______   _        _    _   ______  ______  ______   _____    ______
| |  | \ | |      | |  | | | |     | |     / |  | \ | | \ \  | |
| |--| < | |   _  | |  | | | |---- | |---- | |  | | | |  | | | |----
|_|__|_/ |_|__|_| \_|__|_| |_|____ |_|____ \_|__|_/ |_|_/_/  |_|____
"""

ARTS: tuple[str, ...] = (_BLUE_CODE_FIGLET, _BLUE_CODE_SLANT, _BLUE_CODE_BLOCKS)


def _clean(art: str) -> str:
    lines = [line.rstrip() for line in art.strip("\n").splitlines()]
    # Drop leading/trailing blank lines, keep internal spacing intact.
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def art_width(art: str) -> int:
    return max((len(line) for line in art.splitlines()), default=0)


def all_arts() -> list[str]:
    return [_clean(art) for art in ARTS]


def pick(width: int | None = None, rng: random.Random | None = None) -> str | None:
    """Return a randomly chosen art that fits `width`, or None if disabled.

    Falls back to the narrowest art when the terminal is very small; returns
    None only when art is disabled via BLUECODE_NO_ART.
    """
    if os.environ.get("BLUECODE_NO_ART"):
        return None
    arts = all_arts()
    if width is not None:
        fitting = [a for a in arts if art_width(a) <= width]
        if fitting:
            arts = fitting
        else:
            arts = [min(arts, key=art_width)]
    return (rng or random).choice(arts)
