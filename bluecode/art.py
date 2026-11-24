"""Startup banner: one of several ASCII arts, picked at random.

Kept in its own module so the arts are easy to extend and easy to test:
each art must be printable as-is, with no trailing whitespace, and the picker
filters by terminal width/height so a huge banner never eats the menu.
"""

from __future__ import annotations

import os
import random

_BLOCKY = r"""
___         __
| o )||   _ / _|_  ||_
| o \|||U/o( (_/o\/o/o\
|___/L|\_|( \__\_/\_\(
"""

_SMALL = r"""
___  _            ___         _
| . >| | _ _  ___ |  _> ___  _| | ___
| . \| || | |/ ._>| <__/ . \/ . |/ ._>
|___/|_|`___|\___.`___/\___/\___|\___.
"""

_SCRIPT = r"""
__  ,   ,  ,  _, _, _, ,_   _,
 '|_) |   |  | /_,/  / \,| \,/_,
 _|_)'|__'\__|'\_'\_'\_/_|_/'\_
'       '    `   `  `' '       `
"""

_BUBBLES = r"""
O~~ O~~    O~~                      O~~                  O~~
O~    O~~  O~~                   O~~   O~~               O~~
O~     O~~ O~~O~~  O~~   O~~    O~~          O~~         O~~   O~~
O~~~ O~    O~~O~~  O~~ O~   O~~ O~~        O~~  O~~  O~~ O~~ O~   O~~
O~     O~~ O~~O~~  O~~O~~~~~ O~~O~~       O~~    O~~O~   O~~O~~~~~ O~~
O~      O~ O~~O~~  O~~O~         O~~   O~~ O~~  O~~ O~   O~~O~
O~~~~ O~~ O~~~  O~~O~~  O~~~~      O~~~~     O~~     O~~ O~~  O~~~~
"""

_SLANT_BS = r"""
______ _            _____           _
| ___ \ |          /  __ \         | |
| |_/ / |_   _  ___| /  \/ ___   __| | ___
| ___ \ | | | |/ _ \ |    / _ \ / _` |/ _ \
| |_/ / | |_| |  __/ \__/\ (_) | (_| |  __/
\____/|_|\__,_|\___|\____/\___/ \__,_|\___|
"""

_BARS = r"""
_____  _    __ __  ____  ____  ____  ____  ____
| () )| |__|  |  || ===|/ (__`/ () \| _) \| ===|
|_()_)|____|\___/ |____|\____)\____/|____/|____|
"""

# Tall figlet banners: only shown when the terminal is big enough.
_TALL_A = r"""
.-. .-')                           ('-.                          _ .-') _
\  ( OO )                        _(  OO)                        ( (  OO) )
 ;-----.\  ,--.     ,--. ,--.   (,------.   .-----.  .-'),-----. \     .'_
 | .-.  |  |  |.-') |  | |  |    |  .---'  '  .--./ ( OO'  .-.  ',`'--..._)
 | '-' /_) |  | OO )|  | | .-')  |  |      |  |('-. /   |  | |  ||  |  \  '
 | .-. `.  |  |`-' ||  |_|( OO )(|  '--.  /_) |OO  )\_) |  |\|  ||  |   ' |
 | |  \  |(|  '---.'|  | | `-' / |  .--'  ||  |`-'|   \ |  | |  ||  |   / :
 | '--'  / |      |('  '-'(_.-'  |  `---.(_'  '--'\    `'  '-'  '|  '--'  /
 `------'  `------'  `-----'     `------'   `-----'      `-----' `-------'
   ('-.
 _(  OO)
(,------.
 |  .---'
 |  |
(|  '--.
 |  .--'
 |  `---.
 `------'
"""

_TALL_B = r"""
<-.(`-')                      (`-')  _                     _(`-')    (`-')  _
 __( OO)    <-.        .->    ( OO).-/ _             .->  ( (OO ).-> ( OO).-/
'-'---.\  ,--. )  ,--.(,--.  (,------. \-,-----.(`-')----. \    .'_ (,------.
| .-. (/  |  (`-')|  | |(`-') |  .---'  |  .--./( OO).-.  ''`'-..__) |  .---'
| '-' `.) |  |OO )|  | |(OO )(|  '--.  /_) (`-')( _) | |  ||  |  ' |(|  '--.
| /`'.  |(|  '__ ||  | | |  \ |  .--'  ||  |OO ) \|  |)|  ||  |  / : |  .--'
| '--'  / |     |'\  '-'(_ .' |  `---.(_'  '--'\  '  '-'  '|  '-'  / |  `---.
`------'  `-----'  `-----'    `------'   `-----'   `-----' `------'  `------'
"""

ARTS: tuple[str, ...] = (
    _BLOCKY,
    _SMALL,
    _SCRIPT,
    _BUBBLES,
    _SLANT_BS,
    _BARS,
    _TALL_A,
    _TALL_B,
)


def _clean(art: str) -> str:
    lines = [line.rstrip() for line in art.strip("\n").splitlines()]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def art_width(art: str) -> int:
    return max((len(line) for line in art.splitlines()), default=0)


def art_height(art: str) -> int:
    return len(art.splitlines())


def all_arts() -> list[str]:
    seen: set[str] = set()
    arts: list[str] = []
    for raw in ARTS:
        cleaned = _clean(raw)
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            arts.append(cleaned)
    return arts


def pick(
    width: int | None = None,
    height: int | None = None,
    rng: random.Random | None = None,
) -> str | None:
    """Return a random art that fits the terminal, or None if disabled.

    Prefers arts fitting both dimensions; if none fit, falls back to the
    narrowest (or shortest) art rather than printing something broken.
    """
    if os.environ.get("BLUECODE_NO_ART"):
        return None
    arts = all_arts()
    if not arts:
        return None

    def fits(art: str) -> bool:
        return (width is None or art_width(art) <= width) and (
            height is None or art_height(art) <= height
        )

    fitting = [a for a in arts if fits(a)]
    if fitting:
        return (rng or random).choice(fitting)
    if width is not None and not any(art_width(a) <= width for a in arts):
        return min(arts, key=art_width)
    return min(arts, key=art_height)
