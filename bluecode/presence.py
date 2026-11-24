"""Presence: who is online right now.

Bluetooth Classic has no broadcast, so presence is:
  1. ping peers we already know (parallel, short timeout) -> fast path;
  2. optionally run an inquiry scan to discover new devices -> slow path.

A peer is "online" if it answers HELLO on the BlueCode RFCOMM channel.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from . import config
from . import protocol as P
from .transport import rfcomm


@dataclass
class Presence:
    mac: str
    nick: str
    dev: str = ""

    @property
    def label(self) -> str:
        return self.nick or self.mac


async def _probe(
    mac: str, channel: int, nick: str, dev: str, timeout: float
) -> Presence | None:
    try:
        link = await rfcomm.connect(mac, channel, timeout=timeout)
    except (OSError, asyncio.TimeoutError):
        return None
    try:
        await link.send(P.hello(nick, dev))
        while True:
            frame = await asyncio.wait_for(link.recv(), timeout=timeout)
            if frame["t"] in ("hello_ack", "hello"):
                if frame["t"] == "hello":
                    await link.send(P.hello_ack(nick, dev))
                return Presence(
                    mac=mac.upper(),
                    nick=str(frame.get("nick", "")).strip() or mac.upper(),
                    dev=str(frame.get("dev", "")),
                )
    except (OSError, asyncio.TimeoutError, ConnectionError, P.ProtocolError):
        return None
    finally:
        await link.close()


async def find_online(
    *,
    nick: str,
    dev: str,
    channel: int,
    cache: config.PeerCache,
    scan: bool = False,
    scan_seconds: int = 8,
    timeout: float = 2.0,
) -> list[Presence]:
    """Return currently reachable BlueCode peers, de-duplicated by MAC.

    Only cached BlueCode peers are probed by default: blindly trying every
    device BlueZ has ever seen means opening RFCOMM to random headphones.
    A scan adds freshly discovered devices to the candidate set.
    """
    candidates: set[str] = set(cache.all())

    if scan:
        # discover() returns only devices that answered in this window, so we
        # do not waste time probing every headset BlueZ remembers.
        seen_now = await asyncio.to_thread(config.discover, scan_seconds)
        candidates.update(seen_now)
        candidates.update(mac for mac, _ in await asyncio.to_thread(config.known_devices))

    own = set(await asyncio.to_thread(rfcomm.own_macs))
    candidates = {mac.upper() for mac in candidates if mac} - own

    results = await asyncio.gather(
        *(_probe(mac, channel, nick, dev, timeout) for mac in sorted(candidates))
    )
    online: list[Presence] = []
    seen: set[str] = set()
    for item in results:
        if item is None or item.mac in seen:
            continue
        seen.add(item.mac)
        cache.remember(item.mac, item.nick)
        online.append(item)
    online.sort(key=lambda p: p.nick.lower())
    return online
