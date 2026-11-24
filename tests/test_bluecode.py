import asyncio
import json
import socket
from pathlib import Path

import pytest

from bluecode import config, protocol as P
from bluecode.config import Config
from bluecode.session import ChatError, ChatSession, Hub
from bluecode.transport import Link


# --- helpers --------------------------------------------------------------


class Pipe:
    """Both ends of a socketpair; keeps every stream object referenced."""

    def __init__(self, reader, writer, peer_reader, peer_writer) -> None:
        self.reader = reader
        self.writer = writer
        self.peer_reader = peer_reader
        self.peer_writer = peer_writer

    async def close(self) -> None:
        for w in (self.writer, self.peer_writer):
            try:
                w.close()
            except Exception:
                pass
        await asyncio.sleep(0)


async def _pipe() -> Pipe:
    a, b = socket.socketpair()
    a.setblocking(False)
    b.setblocking(False)
    ra, wa = await asyncio.open_connection(sock=a)
    rb, wb = await asyncio.open_connection(sock=b)
    return Pipe(reader=ra, writer=wa, peer_reader=rb, peer_writer=wb)


def _link(pipe: Pipe, mac: str = "AA:BB:CC:DD:EE:01") -> Link:
    return Link(reader=pipe.reader, writer=pipe.writer, mac=mac)


@pytest.fixture()
async def pipe():
    p = await _pipe()
    yield p
    await p.close()


@pytest.fixture()
def hub(tmp_path: Path):
    cache = config.PeerCache(tmp_path / "p.db")
    h = Hub(Config(nick="me", channel=3), cache)
    h.dev = "11:11:11:11:11:11"
    yield h
    cache.close()


# --- helpers --------------------------------------------------------------


async def test_platform_problem_flags_termux(monkeypatch):
    from bluecode.transport import rfcomm

    monkeypatch.setenv("PREFIX", "/data/data/com.termux/files/usr")
    assert "Termux" in rfcomm.platform_problem()


async def test_platform_problem_allows_linux(monkeypatch):
    from bluecode.transport import rfcomm

    monkeypatch.delenv("PREFIX", raising=False)
    monkeypatch.delenv("ANDROID_ROOT", raising=False)
    monkeypatch.setattr(rfcomm.sys, "platform", "linux")
    assert rfcomm.platform_problem() is None


async def test_platform_problem_rejects_macos(monkeypatch):
    from bluecode.transport import rfcomm

    monkeypatch.delenv("PREFIX", raising=False)
    monkeypatch.delenv("ANDROID_ROOT", raising=False)
    monkeypatch.setattr(rfcomm.sys, "platform", "darwin")
    assert "not supported" in rfcomm.platform_problem()


# --- protocol -------------------------------------------------------------


async def test_frame_roundtrip(pipe):
    await P.write_frame(pipe.writer, P.text("привет\nмир"))
    got = await asyncio.wait_for(P.read_frame(pipe.peer_reader), 1)
    assert got["t"] == "msg" and got["text"] == "привет\nмир"


async def test_multibyte_frame_length_is_bytes_not_chars(pipe):
    text = "ж" * 1000  # 2 bytes per char
    await P.write_frame(pipe.writer, P.text(text))
    got = await asyncio.wait_for(P.read_frame(pipe.peer_reader), 1)
    assert got["text"] == text


async def test_rejects_oversized_frame(pipe):
    pipe.writer.write(b"\x00\x10\x00\x00" + b"x" * 10)
    await pipe.writer.drain()
    with pytest.raises(P.ProtocolError):
        await asyncio.wait_for(P.read_frame(pipe.peer_reader), 1)


async def test_rejects_non_object_json(pipe):
    body = json.dumps([1, 2]).encode()
    pipe.writer.write(len(body).to_bytes(4, "big") + body)
    await pipe.writer.drain()
    with pytest.raises(P.ProtocolError):
        await asyncio.wait_for(P.read_frame(pipe.peer_reader), 1)


async def test_rejects_missing_type(pipe):
    body = json.dumps({"text": "no type"}).encode()
    pipe.writer.write(len(body).to_bytes(4, "big") + body)
    await pipe.writer.drain()
    with pytest.raises(P.ProtocolError):
        await asyncio.wait_for(P.read_frame(pipe.peer_reader), 1)


# --- config / cache -------------------------------------------------------


def test_clean_nick_collapses_whitespace():
    assert config.clean_nick("  a   b \n c ") == "a b c"
    assert len(config.clean_nick("x" * 100)) == config.NICK_MAX


def test_nick_strips_control_chars_and_newlines():
    cleaned = config.clean_nick("bad\nnick\x1b[31mred")
    assert "\n" not in cleaned and "\x1b" not in cleaned


def test_peer_cache_remembers_and_updates(tmp_path: Path):
    cache = config.PeerCache(tmp_path / "p.db")
    cache.remember("aa:bb:cc:dd:ee:ff", "old")
    cache.remember("AA:BB:CC:DD:EE:FF", "new")
    assert cache.nick_of("aa:bb:cc:dd:ee:ff") == "new"
    assert cache.all() == {"AA:BB:CC:DD:EE:FF": "new"}
    cache.close()


def test_config_roundtrip(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    config.save_config(Config(nick="tester", channel=7))
    loaded = config.load_config()
    assert (loaded.nick, loaded.channel) == ("tester", 7)


def test_config_falls_back_on_bad_file(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    (tmp_path / "config.json").write_text("{not json", encoding="utf-8")
    loaded = config.load_config()
    assert loaded.nick and 1 <= loaded.channel <= 30


def test_config_rejects_out_of_range_channel(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    (tmp_path / "config.json").write_text('{"nick":"x","channel":99}', encoding="utf-8")
    assert config.load_config().channel == config.DEFAULT_CHANNEL


# --- session state machine ------------------------------------------------


async def test_incoming_invite_accept_starts_chat(hub, pipe):
    link = _link(pipe)
    task = asyncio.create_task(hub._on_invite(link, "peer"))
    await asyncio.sleep(0.05)
    assert len(hub.invites) == 1
    hub.resolve_invite(True)
    await asyncio.wait_for(task, 1)
    assert hub.chat is not None and hub.chat.nick == "peer"
    reply = await asyncio.wait_for(P.read_frame(pipe.peer_reader), 1)
    assert reply["t"] == "invite_reply" and reply["accept"] is True
    await hub.chat.close()


async def test_incoming_invite_decline_closes_link(hub, pipe):
    link = _link(pipe)
    task = asyncio.create_task(hub._on_invite(link, "peer"))
    await asyncio.sleep(0.05)
    hub.resolve_invite(False)
    await asyncio.wait_for(task, 1)
    assert hub.chat is None
    reply = await asyncio.wait_for(P.read_frame(pipe.peer_reader), 1)
    assert reply["accept"] is False


async def test_resolve_without_invites_returns_none(hub):
    assert hub.resolve_invite(True) is None


async def test_busy_hub_rejects_invite(hub, pipe):
    hub.chat = object()  # type: ignore[assignment]
    await asyncio.wait_for(hub._on_invite(_link(pipe), "peer"), 1)
    reply = await asyncio.wait_for(P.read_frame(pipe.peer_reader), 1)
    assert reply == {"t": "invite_reply", "accept": False, "reason": "busy"}


async def test_invite_timeout_declines(hub, pipe, monkeypatch):
    import bluecode.session as S

    monkeypatch.setattr(S, "INVITE_WAIT", 0.05)
    await asyncio.wait_for(hub._on_invite(_link(pipe), "peer"), 2)
    assert hub.chat is None
    reply = await asyncio.wait_for(P.read_frame(pipe.peer_reader), 1)
    assert reply["accept"] is False and reply["reason"] == "timeout"


async def test_timed_out_invite_does_not_block_later_invites(hub, pipe, monkeypatch):
    """Regression: a timed-out invite used to stay in the deque forever."""
    import bluecode.session as S

    monkeypatch.setattr(S, "INVITE_WAIT", 0.05)
    await asyncio.wait_for(hub._on_invite(_link(pipe), "peer"), 2)
    assert len(hub.invites) == 0

    # A second peer must still be able to invite us.
    pipe2 = await _pipe()
    task = asyncio.create_task(hub._on_invite(_link(pipe2), "peer2"))
    await asyncio.sleep(0.05)
    assert len(hub.invites) == 1, "hub rejected the second invite as busy"
    hub.resolve_invite(False)
    await asyncio.wait_for(task, 1)
    await pipe2.close()


async def test_simultaneous_dial_is_rejected_not_deadlocked(hub, pipe):
    hub._dialing_mac = "AA:BB:CC:DD:EE:01"
    await asyncio.wait_for(hub._on_invite(_link(pipe), "peer"), 1)
    reply = await asyncio.wait_for(P.read_frame(pipe.peer_reader), 1)
    assert reply["accept"] is False and reply["reason"] == "collision"
    assert not hub.invites and hub.chat is None


async def test_chat_delivers_messages_in_order(pipe):
    session = ChatSession(_link(pipe), "peer", "AA:BB:CC:DD:EE:01")
    await P.write_frame(pipe.peer_writer, P.text("one"))
    await P.write_frame(pipe.peer_writer, P.text("two"))
    first = await asyncio.wait_for(session.queue.get(), 1)
    second = await asyncio.wait_for(session.queue.get(), 1)
    assert (first.text, second.text) == ("one", "two")
    await session.close()


async def test_chat_reports_peer_leaving(pipe):
    session = ChatSession(_link(pipe), "peer", "AA:BB:CC:DD:EE:01")
    await P.write_frame(pipe.peer_writer, P.bye("bye"))
    item = await asyncio.wait_for(session.queue.get(), 1)
    assert item.kind == "closed" and item.text == "bye"
    assert session.closed.is_set()


async def test_chat_ignores_unknown_frames(pipe):
    session = ChatSession(_link(pipe), "peer", "AA:BB:CC:DD:EE:01")
    await P.write_frame(pipe.peer_writer, {"t": "future_thing", "x": 1})
    await P.write_frame(pipe.peer_writer, P.text("still works"))
    item = await asyncio.wait_for(session.queue.get(), 1)
    assert item.text == "still works"
    await session.close()


# --- multiline / code input ----------------------------------------------


def test_prepare_text_keeps_indentation_and_blank_lines():
    from bluecode.ui import prepare_text

    code = "def f():\n    return 1\n\n\n"
    assert prepare_text(code) == code


def test_prepare_text_drops_whitespace_only_drafts():
    from bluecode.ui import prepare_text

    for blank in ("", "   ", "\n\n", "\t\n "):
        assert prepare_text(blank) is None


def test_prepare_text_rejects_oversized_message():
    from bluecode.ui import prepare_text

    assert prepare_text("x" * (P.MAX_FRAME + 10)) is None


def test_shift_enter_sequences_are_distinguishable():
    """All terminal encodings of Shift+Enter must map to one bindable key."""
    from prompt_toolkit.input.vt100_parser import Vt100Parser

    import bluecode.ui as ui

    for seq in ("\x1b[13;2u", "\x1b[13;2;1u", "\x1b[27;2;13~"):
        keys = []
        parser = Vt100Parser(keys.append)
        parser.feed(seq)
        parser.flush()
        assert [k.key for k in keys] == [ui.Keys.F24], seq

    # Plain Enter must NOT be caught by the Shift+Enter binding.
    keys = []
    parser = Vt100Parser(keys.append)
    parser.feed("\r")
    parser.flush()
    assert [k.key for k in keys] == [ui.Keys.ControlM]


async def test_multiline_message_roundtrip(pipe):
    code = "for i in range(3):\n\tprint(i)\n\n# end"
    session = ChatSession(_link(pipe), "peer", "AA:BB:CC:DD:EE:01")
    await P.write_frame(pipe.peer_writer, P.text(code))
    item = await asyncio.wait_for(session.queue.get(), 1)
    assert item.text == code
    await session.close()


# --- end-to-end over a fake transport ------------------------------------


async def test_full_handshake_and_messaging(tmp_path: Path, monkeypatch):
    """A talks to B through the real Hub code paths, only the socket is fake."""
    import bluecode.session as S

    pa = await _pipe()  # a <-> b
    hub_a = Hub(Config(nick="alice", channel=3), config.PeerCache(tmp_path / "a.db"))
    hub_b = Hub(Config(nick="bob", channel=3), config.PeerCache(tmp_path / "b.db"))
    hub_a.dev = "AA:AA:AA:AA:AA:AA"
    hub_b.dev = "BB:BB:BB:BB:BB:BB"

    # B accepts every invitation, like pressing Enter on the prompt.
    async def auto_accept() -> None:
        while True:
            if hub_b.invites:
                hub_b.resolve_invite(True)
            await asyncio.sleep(0.01)

    accept_task = asyncio.create_task(auto_accept())
    # B's server side reads from the a->b direction.
    server_task = asyncio.create_task(
        hub_b._handle(Link(reader=pa.peer_reader, writer=pa.peer_writer, mac="AA:AA:AA:AA:AA:AA"))
    )

    async def fake_connect(mac, channel, timeout=5.0):
        return Link(reader=pa.reader, writer=pa.writer, mac=mac)

    monkeypatch.setattr(S.rfcomm, "connect", fake_connect)

    chat_a = await asyncio.wait_for(hub_a.open_chat("BB:BB:BB:BB:BB:BB", "bob"), 5)
    await asyncio.wait_for(_until(lambda: hub_b.chat is not None), 2)
    assert hub_a.chat is chat_a and hub_a.chat.nick == "bob"
    assert hub_b.chat is not None and hub_b.chat.nick == "alice"

    await chat_a.send_text("привет bob")
    got = await asyncio.wait_for(hub_b.chat.queue.get(), 2)
    assert got.kind == "msg" and got.text == "привет bob"

    await hub_b.chat.send_text("привет alice")
    got = await asyncio.wait_for(chat_a.queue.get(), 2)
    assert got.text == "привет alice"

    # Alice leaves; Bob must notice and the hub must free itself.
    await hub_a.close_chat()
    await asyncio.wait_for(_until(lambda: hub_b.chat is None), 2)

    accept_task.cancel()
    server_task.cancel()
    for t in (accept_task, server_task):
        try:
            await t
        except asyncio.CancelledError:
            pass
    await pa.close()
    hub_a.cache.close()
    hub_b.cache.close()


async def _until(pred, timeout=1.0) -> None:
    for _ in range(int(timeout / 0.02)):
        if pred():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met")


async def test_failed_connect_does_not_leak_dialing_flag(tmp_path: Path, monkeypatch):
    """Regression: connect() failing left _dialing=True forever, so this peer
    could never invite us again (we answered 'busy')."""
    import bluecode.session as S

    h = Hub(Config(nick="me", channel=3), config.PeerCache(tmp_path / "c.db"))
    h.dev = "11:11:11:11:11:11"

    async def boom(mac, channel, timeout=5.0):
        raise OSError(113, "No route to host")

    monkeypatch.setattr(S.rfcomm, "connect", boom)
    with pytest.raises(ChatError) as info:
        await h.open_chat("AA:BB:CC:DD:EE:01", "peer")
    assert "cannot reach peer" in str(info.value)
    assert h._dialing is False and h._dialing_mac == ""

    # And the peer can invite us afterwards.
    p = await _pipe()
    task = asyncio.create_task(h._on_invite(_link(p), "peer"))
    await asyncio.sleep(0.05)
    assert len(h.invites) == 1
    h.resolve_invite(False)
    await asyncio.wait_for(task, 1)
    await p.close()
    h.cache.close()


def test_error_texts_are_actionable():
    from bluecode.session import _connect_error_text, _refusal_text

    assert "pair" in _connect_error_text(OSError(13, "denied")).lower()
    assert "range" in _connect_error_text(asyncio.TimeoutError()).lower()
    assert "busy" in _refusal_text("busy")
    assert "try again" in _refusal_text("collision")
    assert "answer" in _refusal_text("timeout")
