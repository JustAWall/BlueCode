"""Session logic: listener, invitations and an active chat.

The UI layer drives all decisions (accept/decline); this module only knows
about links and frames. Keeping the protocol state machine here makes it
testable over `socketpair()` without any Bluetooth hardware.
"""

from __future__ import annotations

import asyncio
from collections import deque
from contextlib import suppress
from dataclasses import dataclass

from . import config
from . import protocol as P
from .transport import Link, rfcomm

INVITE_WAIT = 30.0
# The caller must outlive the callee's decision window, otherwise both sides
# give up at the same instant and the accept is lost.
CALLER_WAIT = INVITE_WAIT + 10.0


class ChatError(Exception):
    pass


def _refusal_text(reason: str) -> str:
    return {
        "declined": "declined the invite",
        "busy": "is busy in another chat",
        "collision": "both of you invited at once, try again",
        "timeout": "did not answer",
    }.get(reason, reason or "declined the invite")


def _connect_error_text(exc: BaseException) -> str:
    """Turn kernel errnos into something a human can act on."""
    if isinstance(exc, asyncio.TimeoutError):
        return "no answer (out of range, not running BlueCode, or unpaired)"
    if isinstance(exc, P.ProtocolError):
        return "peer speaks a different/incompatible BlueCode protocol"
    code = getattr(exc, "errno", None)
    if code in (111, 113):  # ECONNREFUSED, EHOSTUNREACH
        return "cannot reach peer (out of range, not running BlueCode, or unpaired)"
    if code == 112:  # EHOSTDOWN
        return "peer is off or out of range"
    if code in (16, 98, 114, 115):  # EBUSY, EADDRINUSE, EALREADY, EINPROGRESS
        return "channel is busy, pick another one in mysettings"
    if code == 13:  # EACCES
        return "permission denied: pair the device first (bluetoothctl pair <MAC>)"
    if isinstance(exc, ConnectionError):
        return "connection dropped"
    return str(exc) or type(exc).__name__


@dataclass
class Incoming:
    """Something that happened in the active chat."""

    kind: str  # "msg" | "typing" | "closed"
    text: str = ""
    ts: float = 0.0


@dataclass
class PendingInvite:
    link: Link
    nick: str
    mac: str
    ts: float = 0.0
    decided: asyncio.Future[bool] | None = None


class ChatSession:
    def __init__(self, link: Link, nick: str, mac: str) -> None:
        self.link = link
        self.nick = nick
        self.mac = mac
        self.queue: asyncio.Queue[Incoming] = asyncio.Queue()
        self.closed = asyncio.Event()
        self._reader = asyncio.create_task(self._read_loop())

    async def _read_loop(self) -> None:
        reason = "connection lost"
        try:
            while True:
                frame = await self.link.recv()
                kind = frame["t"]
                if kind == "msg":
                    await self.queue.put(
                        Incoming("msg", str(frame.get("text", "")), float(frame.get("ts", 0.0)))
                    )
                elif kind == "typing":
                    await self.queue.put(Incoming("typing"))
                elif kind == "bye":
                    reason = str(frame.get("reason", "left"))
                    break
                # ignore unknown frame types for forwards compatibility
        except (OSError, asyncio.IncompleteReadError, ConnectionError, P.ProtocolError):
            pass
        finally:
            await self.queue.put(Incoming("closed", reason))
            self.closed.set()
            await self.link.close()

    async def send_text(self, text: str) -> None:
        await self.link.send(P.text(text))

    async def send_typing(self) -> None:
        try:
            await self.link.send(P.typing())
        except (OSError, ConnectionError):
            pass

    async def close(self, reason: str = "left") -> None:
        if self.closed.is_set():
            return
        try:
            await self.link.send(P.bye(reason))
        except (OSError, ConnectionError):
            pass
        self._reader.cancel()
        await self.link.close()
        self.closed.set()


class Hub:
    """Owns the RFCOMM listener and the single active chat."""

    def __init__(self, cfg: config.Config, cache: config.PeerCache) -> None:
        self.cfg = cfg
        self.cache = cache
        self.chat: ChatSession | None = None
        self.invites: deque[PendingInvite] = deque()
        self.server: asyncio.AbstractServer | None = None
        self.channel = cfg.channel
        self.dev = ""
        self._tasks: set[asyncio.Task] = set()
        self._dialing = False
        self._dialing_mac = ""

    async def start(self) -> int:
        self.server, self.channel = await rfcomm.listen(self.channel, self._handle)
        return self.channel

    async def stop(self) -> None:
        if self.chat:
            await self.chat.close("stopping")
            self.chat = None
        for inv in list(self.invites):
            if inv.decided is not None and not inv.decided.done():
                inv.decided.set_result(False)
            await inv.link.close()
        self.invites.clear()
        if self.server:
            self.server.close()
            await self.server.wait_closed()
            self.server = None
        for task in list(self._tasks):
            task.cancel()

    @property
    def busy(self) -> bool:
        return self.chat is not None

    # --- inbound -----------------------------------------------------

    async def _handle(self, link: Link) -> None:
        """Read frames from a freshly accepted connection."""
        owned = False
        try:
            while True:
                frame = await link.recv()
                kind = frame["t"]
                if kind == "hello":
                    link.nick = str(frame.get("nick", "")).strip()
                    link.dev = str(frame.get("dev", ""))
                    await link.send(P.hello_ack(self.cfg.nick, self.dev))
                    continue
                if kind == "invite":
                    nick = str(frame.get("nick", "")).strip() or link.mac
                    link.nick = nick
                    await self._on_invite(link, nick)
                    owned = bool(link.extras.get("owned"))
                    return
                # Unknown / out-of-place frame: ignore.
        except (asyncio.IncompleteReadError, ConnectionError, OSError, P.ProtocolError):
            pass
        finally:
            if not owned:
                await link.close()

    async def _on_invite(self, link: Link, nick: str) -> None:
        mac = link.mac.upper()
        # Both sides pressed "chat" at the same time: reject the incoming link
        # and let the dialing side surface a clear "try again" error. Creating
        # a pending invite here would deadlock the UI, which is blocked in
        # open_chat() at this point.
        if self._dialing_mac == mac:
            try:
                await link.send(P.invite_reply(False, "collision"))
            except (OSError, ConnectionError):
                pass
            await link.close()
            return
        if self.busy or self.invites or self._dialing:
            await link.send(P.invite_reply(False, "busy"))
            await link.close()
            return
        inv = PendingInvite(link=link, nick=nick, mac=link.mac)
        inv.decided = asyncio.get_running_loop().create_future()
        self.invites.append(inv)
        timed_out = False
        try:
            # Let the peer wait; the UI decides.
            accept = await asyncio.wait_for(inv.decided, INVITE_WAIT)
        except asyncio.TimeoutError:
            accept = False
            timed_out = True
            # Remove the dead invite, otherwise the hub counts itself busy
            # forever and rejects every later incoming invite.
            with suppress(ValueError):
                self.invites.remove(inv)
        if not accept:
            reason = "timeout" if timed_out else "declined"
            try:
                await link.send(P.invite_reply(False, reason))
            except (OSError, ConnectionError):
                pass
            await link.close()
            return
        await link.send(P.invite_reply(True))
        link.extras["owned"] = True
        self._adopt(ChatSession(link, nick, link.mac))
        self.cache.remember(link.mac, nick)

    def _adopt(self, chat: ChatSession) -> None:
        self.chat = chat
        task = asyncio.create_task(self._clear_when_closed(chat))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _clear_when_closed(self, chat: ChatSession) -> None:
        await chat.closed.wait()
        if self.chat is chat:
            self.chat = None

    def resolve_invite(self, accept: bool) -> PendingInvite | None:
        if not self.invites:
            return None
        inv = self.invites.popleft()
        if inv.decided is not None and not inv.decided.done():
            inv.decided.set_result(accept)
        return inv

    # --- outbound ----------------------------------------------------

    async def open_chat(self, mac: str, nick: str) -> ChatSession:
        if self.busy:
            raise ChatError("already in a chat")
        mac = mac.upper()
        self._dialing = True
        self._dialing_mac = mac
        link: Link | None = None
        try:
            # connect() itself can fail (unpaired, out of range, wrong channel),
            # so it must live inside the try or the dialing flag leaks.
            link = await rfcomm.connect(mac, self.channel, timeout=5.0)
            await link.send(P.hello(self.cfg.nick, self.dev))
            while True:
                frame = await asyncio.wait_for(link.recv(), timeout=INVITE_WAIT)
                if frame["t"] == "hello_ack":
                    break
            await link.send(P.invite(self.cfg.nick, self.dev))
            frame = await asyncio.wait_for(link.recv(), timeout=CALLER_WAIT)
            if frame["t"] != "invite_reply":
                raise ChatError("unexpected reply")
            if not frame.get("accept"):
                raise ChatError(_refusal_text(str(frame.get("reason", ""))))
        except (asyncio.TimeoutError, P.ProtocolError, OSError, ConnectionError) as exc:
            if link is not None:
                await link.close()
            raise ChatError(_connect_error_text(exc)) from exc
        finally:
            self._dialing = False
            self._dialing_mac = ""
        self.cache.remember(mac, nick)
        chat = ChatSession(link, nick, mac)
        self._adopt(chat)
        return chat

    async def close_chat(self) -> None:
        if self.chat:
            await self.chat.close()
            self.chat = None
