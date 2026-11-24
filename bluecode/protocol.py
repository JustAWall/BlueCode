"""Wire protocol: length-prefixed JSON frames.

Frame layout:  uint32 big-endian length | UTF-8 JSON object

Every object has a "t" (type) field. Adding fields is backwards compatible;
bumping PROTO_VERSION is only needed for breaking changes.
"""

from __future__ import annotations

import asyncio
import json
import struct
import time
from typing import Any

PROTO_VERSION = 1
MAX_FRAME = 64 * 1024

class ProtocolError(Exception):
    """Peer sent something we cannot parse."""


def encode(obj: dict[str, Any]) -> bytes:
    body = json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(body) > MAX_FRAME:
        raise ProtocolError(f"frame too large: {len(body)} bytes")
    return struct.pack("!I", len(body)) + body


async def read_frame(reader: asyncio.StreamReader) -> dict[str, Any]:
    head = await reader.readexactly(4)
    (size,) = struct.unpack("!I", head)
    if size > MAX_FRAME:
        raise ProtocolError(f"frame too large: {size} bytes")
    body = await reader.readexactly(size)
    try:
        obj = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError(f"bad json: {exc}") from exc
    if not isinstance(obj, dict) or not isinstance(obj.get("t"), str):
        raise ProtocolError("frame must be an object with a string 't'")
    return obj


async def write_frame(writer: asyncio.StreamWriter, obj: dict[str, Any]) -> None:
    writer.write(encode(obj))
    await writer.drain()


# --- message constructors -------------------------------------------------


def hello(nick: str, dev: str) -> dict[str, Any]:
    return {"t": "hello", "v": PROTO_VERSION, "nick": nick, "dev": dev}


def hello_ack(nick: str, dev: str) -> dict[str, Any]:
    return {"t": "hello_ack", "v": PROTO_VERSION, "nick": nick, "dev": dev}


def invite(nick: str, dev: str) -> dict[str, Any]:
    return {"t": "invite", "nick": nick, "dev": dev}


def invite_reply(accept: bool, reason: str = "") -> dict[str, Any]:
    return {"t": "invite_reply", "accept": accept, "reason": reason}


def text(text: str) -> dict[str, Any]:
    return {"t": "msg", "ts": time.time(), "text": text}


def typing() -> dict[str, Any]:
    return {"t": "typing"}


def bye(reason: str = "left") -> dict[str, Any]:
    return {"t": "bye", "reason": reason}
