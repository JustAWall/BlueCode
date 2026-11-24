"""Transport abstraction.

A Link is a bidirectional, message-oriented channel to one peer.
The only backend right now is RFCOMM (Bluetooth Classic, BlueZ), but the
session code below only depends on this interface, which keeps an eventual
BLE backend a drop-in.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from .. import protocol as P


@dataclass
class Link:
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    mac: str = ""
    nick: str = ""
    dev: str = ""
    extras: dict[str, Any] = field(default_factory=dict)

    async def send(self, obj: dict[str, Any]) -> None:
        await P.write_frame(self.writer, obj)

    async def recv(self) -> dict[str, Any]:
        return await P.read_frame(self.reader)

    async def close(self) -> None:
        try:
            self.writer.close()
            await asyncio.wait_for(self.writer.wait_closed(), timeout=2)
        except (OSError, asyncio.TimeoutError, RuntimeError):
            pass

    @property
    def alive(self) -> bool:
        return not self.writer.is_closing()
