"""Terminal UI: menu, presence list, settings and the chat screen.

Scrollback style (not full-screen): the transcript stays in the terminal
scrollback, and prompt_toolkit keeps the draft input line alive at the
bottom. An incoming message is printed *above* the draft, so whatever you
were typing is never lost. That answers "how does the peer see my draft" --
they don't: the draft is local, and messages arriving mid-typing go up.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import shutil
import time
from contextlib import suppress

from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.input.ansi_escape_sequences import ANSI_SEQUENCES
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.patch_stdout import patch_stdout

from . import art
from . import config
from . import presence
from . import protocol as P
from .session import ChatError, ChatSession, Hub
from .transport import rfcomm

def _supports_color() -> bool:
    """Colors only when stdout is a real TTY and NO_COLOR is not set."""
    import os
    import sys

    if os.environ.get("NO_COLOR"):
        return False
    try:
        return sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


_COLOR = _supports_color()
RESET = "\x1b[0m" if _COLOR else ""
SCAN_SECONDS = 8
DIM = "\x1b[2m" if _COLOR else ""
BOLD = "\x1b[1m" if _COLOR else ""
CYAN = "\x1b[36m" if _COLOR else ""
GREEN = "\x1b[32m" if _COLOR else ""
YELLOW = "\x1b[33m" if _COLOR else ""
RED = "\x1b[31m" if _COLOR else ""
MAGENTA = "\x1b[35m" if _COLOR else ""


def clear_screen() -> None:
    import sys

    if sys.stdout.isatty():
        print("\x1b[2J\x1b[H", end="")


# Shift+Enter is not a single key in most terminals: they send either CSI-u
# (ESC[13;2u) or xterm modifyOtherKeys (ESC[27;2;13~). prompt_toolkit does not
# know either sequence (the latter is mapped to plain Enter!), so register them
# explicitly. We map both to F24, a key no terminal sends on its own.
_SHIFT_ENTER_SEQS = ("\x1b[13;2u", "\x1b[13;2;1u", "\x1b[27;2;13~")
for _seq in _SHIFT_ENTER_SEQS:
    ANSI_SEQUENCES[_seq] = Keys.F24
SHIFT_ENTER = "f24"


def clock(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts or time.time()).strftime("%H:%M")


def prepare_text(raw: str) -> str | None:
    """Return the exact text to send, or None if it must not be sent.

    Whitespace-only drafts are ignored, but otherwise the text is preserved
    byte for byte: leading indentation and trailing blank lines are part of
    pasted code. Oversized messages are rejected here instead of raising deep
    inside the protocol encoder.
    """
    if not raw.strip():
        return None
    try:
        P.encode(P.text(raw))
    except P.ProtocolError:
        return None
    return raw


def wrap(text: str, indent: str) -> str:
    return text.replace("\n", "\n" + indent)


class App:
    def __init__(self) -> None:
        self.cfg = config.load_config()
        self.cache = config.PeerCache()
        self.hub = Hub(self.cfg, self.cache)
        self.dev = ""
        self.online: list[presence.Presence] = []
        self.session: PromptSession | None = None
        self._announced: set[int] = set()
        self._watcher: asyncio.Task | None = None

    # --- lifecycle ---------------------------------------------------

    async def run(self) -> None:
        self.session = PromptSession(history=_history())
        print(f"{DIM}checking bluetooth...{RESET}", flush=True)
        problem = await rfcomm.ensure_adapter_async()
        if problem:
            print(f"{RED}{problem}{RESET}")
            return
        self.dev = (await rfcomm.own_macs_async() or [""])[0]

        # First run on a fresh machine: choose the nick before we advertise it.
        if not (config.CONFIG_DIR / "config.json").exists():
            first = await self._ask(
                f"{DIM}choose your nickname (Enter = {self.cfg.nick}): {RESET}"
            )
            if first:
                self.cfg.nick = config.clean_nick(first) or self.cfg.nick
            config.save_config(self.cfg)
            print(f"{GREEN}nick = {self.cfg.nick}{RESET}")

        try:
            channel = await self.hub.start()
        except OSError as exc:
            print(f"{RED}cannot open RFCOMM channel {self.cfg.channel}: {exc}{RESET}")
            print(
                f"{DIM}another app may use it; pick another in mysettings "
                f"(currently {self.cfg.channel}){RESET}"
            )
            return
        # Auto-advertise: without this peers cannot discover us at all.
        await rfcomm.set_discoverable_async(True)
        self._watcher = asyncio.create_task(self._watch_invites())
        # raw=True: otherwise StdoutProxy escapes our ANSI codes and "?[1m"
        # garbage ends up on screen instead of bold text.
        with patch_stdout(raw=True):
            self._print_banner()
            print(
                f"{BOLD}{CYAN}BlueCode{RESET} {DIM}v0.1{RESET}  "
                f"nick={BOLD}{self.cfg.nick}{RESET}  "
                f"mac={self.dev or '?'}  channel={channel}  {GREEN}listening{RESET}"
            )
            try:
                await self._menu()
            finally:
                self._watcher.cancel()
                with suppress(asyncio.CancelledError):
                    await self._watcher
                await self.hub.stop()
                await rfcomm.set_discoverable_async(False)
                self.cache.close()
                print(f"{DIM}bye{RESET}")

    def _print_banner(self) -> None:
        """Random BlueCode ASCII art, sized to the terminal."""
        size = shutil.get_terminal_size((80, 24))
        # Leave room for the status line and the menu below the banner.
        banner = art.pick(width=size.columns, height=max(4, size.lines - 10))
        if banner:
            print(f"{CYAN}{banner}{RESET}", flush=True)

    # --- menu ---------------------------------------------------------

    def _banner(self) -> None:
        print()
        print(f"  {BOLD}1{RESET} - showonline")
        print(f"  {BOLD}2{RESET} - mysettings")
        print(f"  {BOLD}3{RESET} - quit_n_stop_be_scanned")

    async def _ask(self, prompt: str) -> str | None:
        """Read a line; None means EOF/Ctrl+C (caller decides what that means)."""
        assert self.session is not None
        try:
            return (await self.session.prompt_async(ANSI(prompt))).strip()
        except (EOFError, KeyboardInterrupt):
            return None

    async def _menu(self) -> None:
        while True:
            self._banner()
            ans = await self._ask(f"{DIM}me = {self.cfg.nick}{RESET}\n> ")
            if ans is None:
                return
            cmd = ans
            low = cmd.lower()

            # A pending invitation owns the prompt: Enter means yes.
            if self.hub.invites and low in ("", "y", "n", "yes", "no"):
                accept = not low.startswith("n")
                self._resolve_invite(accept)
                chat = await self._await_chat(accept)
                if chat is not None:
                    await self._enter_chat(chat)
                continue

            if not cmd:
                continue
            if low in ("1", "showonline", "show online"):
                await self._show_online()
            elif low in ("2", "mysettings", "settings"):
                await self._settings()
            elif low in ("3", "q", "quit", "exit"):
                return
            elif low.startswith("chat"):
                await self._chat_command(cmd)
            elif low in ("y", "n", "yes", "no"):
                print(f"{YELLOW}no pending invitation{RESET}")
            else:
                print(f"{DIM}unknown command: {cmd}{RESET}")

    async def _await_chat(self, accept: bool) -> ChatSession | None:
        """The invite handler runs in a server task; give it a moment."""
        if not accept:
            return None
        for _ in range(40):
            if self.hub.chat is not None:
                return self.hub.chat
            await asyncio.sleep(0.05)
        print(f"{RED}could not open chat (peer gone?){RESET}")
        return None

    # --- presence -----------------------------------------------------

    async def _show_online(self) -> None:
        print(f"{DIM}probing known peers...{RESET}")
        self.online = await presence.find_online(
            nick=self.cfg.nick,
            dev=self.dev,
            channel=self.hub.channel,
            cache=self.cache,
        )
        if self.online:
            self._print_online()
            return

        # Nobody known yet (or all offline): scan for nearby BlueCode users.
        # This is the normal path for a first meeting -- no MAC exchange.
        print(f"{DIM}nobody known; scanning {SCAN_SECONDS}s for nearby devices...{RESET}")
        self.online = await presence.find_online(
            nick=self.cfg.nick,
            dev=self.dev,
            channel=self.hub.channel,
            cache=self.cache,
            scan=True,
            scan_seconds=SCAN_SECONDS,
        )
        self._print_online()
        if not self.online:
            print(
                f"{DIM}hint: the other side must run bluecode and be paired "
                f"once (bluetoothctl pair <MAC>){RESET}"
            )

    def _print_online(self) -> None:
        print()
        print(f"{DIM}me = {self.cfg.nick}{RESET}")
        print(f"  {BOLD}0{RESET} - B4ck at menu")
        if not self.online:
            print(f"  {DIM}nobody online{RESET}")
            return
        for i, peer in enumerate(self.online, start=1):
            print(f"  {BOLD}{i}{RESET} - {peer.label} {DIM}({peer.mac}){RESET}")

    async def _chat_command(self, cmd: str) -> None:
        parts = cmd.split()
        if len(parts) != 2:
            print(f"{DIM}usage: chat <number>   or   chat <AA:BB:CC:DD:EE:FF>{RESET}")
            return
        target = parts[1]

        # Direct-by-MAC: the reliable path for a first meeting, when nobody
        # is in the cached/scan list yet.
        if config.is_mac(target):
            mac = target.upper()
            label = self.cache.nick_of(mac) or mac
            print(f"{DIM}inviting {label}...{RESET}")
            await self._open(mac, label)
            return

        if not self.online:
            await self._show_online()
        try:
            idx = int(target)
        except ValueError:
            print(f"{DIM}chat needs a number or MAC, e.g. chat 4 / chat AA:BB:CC:DD:EE:FF{RESET}")
            return
        if idx == 0:
            return
        if not 1 <= idx <= len(self.online):
            print(f"{YELLOW}no such peer. run showonline again{RESET}")
            return
        peer = self.online[idx - 1]
        print(f"{DIM}inviting {peer.label}...{RESET}")
        await self._open(peer.mac, peer.label)

    async def _open(self, mac: str, label: str) -> None:
        # RFCOMM needs a link key. If the device is not paired yet, do it
        # here so the user never has to touch bluetoothctl.
        if not await asyncio.to_thread(config.is_paired, mac):
            print(f"{DIM}pairing with {label}...{RESET}")
            print(
                f"{DIM}if a code or prompt appears there, confirm it on both "
                f"devices{RESET}"
            )
            problem = await asyncio.to_thread(config.pair, mac)
            if problem:
                print(f"{RED}{label}: {problem}{RESET}")
                return
            print(f"{GREEN}paired with {label}{RESET}")
        try:
            chat = await self.hub.open_chat(mac, label)
        except ChatError as exc:
            print(f"{RED}{label}: {exc}{RESET}")
            return
        await self._enter_chat(chat)

    # --- settings -----------------------------------------------------

    async def _settings(self) -> None:
        while True:
            print()
            print(f"  current nick: {BOLD}{self.cfg.nick}{RESET}")
            print(f"  {DIM}nick <new name> | channel <1-30> | back{RESET}")
            cmd = await self._ask("settings> ")
            if cmd is None:
                return
            low = cmd.lower()
            # Same muscle memory as the main menu: 0/3/q all go back.
            if low in ("", "back", "b", "0", "3", "q", "quit", "exit"):
                return
            if low.startswith("nick"):
                value = config.clean_nick(cmd[4:])
                if not value:
                    print(f"{YELLOW}nick cannot be empty{RESET}")
                    continue
                self.cfg.nick = value
                config.save_config(self.cfg)
                print(f"{GREEN}nick set to {value}{RESET} {DIM}(applies to next session){RESET}")
            elif low.startswith("channel"):
                value = cmd[7:].strip()
                try:
                    channel = int(value)
                except ValueError:
                    print(f"{YELLOW}channel must be 1..30{RESET}")
                    continue
                if not 1 <= channel <= 30:
                    print(f"{YELLOW}channel must be 1..30{RESET}")
                    continue
                self.cfg.channel = channel
                config.save_config(self.cfg)
                print(f"{DIM}channel saved; restart bluecode to rebind{RESET}")
            else:
                print(f"{DIM}unknown setting{RESET}")

    # --- invitations --------------------------------------------------

    def _resolve_invite(self, accept: bool) -> None:
        inv = self.hub.resolve_invite(accept)
        if inv is None:
            return
        self._announced.discard(id(inv))
        if accept:
            print(f"{GREEN}accepted {inv.nick}{RESET}")
        else:
            print(f"{YELLOW}declined {inv.nick}{RESET}")

    async def _watch_invites(self) -> None:
        """Announce incoming invitations even while the menu prompt is open."""
        while True:
            for inv in list(self.hub.invites):
                if id(inv) not in self._announced:
                    self._announced.add(id(inv))
                    print(
                        f"\n{CYAN}{inv.nick}{RESET} want to talk with you. "
                        f"Accept? [y/n] ({BOLD}y{RESET}):"
                    )
            await asyncio.sleep(0.2)

    # --- chat ---------------------------------------------------------

    async def _enter_chat(self, chat: ChatSession | None) -> None:
        if chat is None:
            return
        clear_screen()
        print(f"{BOLD}{CYAN}chat with {chat.nick}{RESET} {DIM}({chat.mac}){RESET}")
        print(f"{DIM}Enter send · Shift+Enter / Alt+Enter newline · Ctrl+Q leave{RESET}")
        print(f"{DIM}{'-' * 60}{RESET}")
        printer = asyncio.create_task(self._print_incoming(chat))
        try:
            await self._chat_loop(chat)
        finally:
            printer.cancel()
            with suppress(asyncio.CancelledError):
                await printer
            # Flush anything that arrived right as we were leaving, e.g. the
            # "peer left" event, so the user actually sees why the chat ended.
            self._drain_queue(chat)
            await self.hub.close_chat()
            print(f"{DIM}chat closed{RESET}")

    def _drain_queue(self, chat: ChatSession) -> None:
        while True:
            try:
                self._render(chat, chat.queue.get_nowait())
            except asyncio.QueueEmpty:
                break

    def _render(self, chat: ChatSession, item) -> None:  # type: ignore[no-untyped-def]
        if item.kind == "msg":
            prefix = f"{DIM}[{clock(item.ts)}]{RESET} {MAGENTA}{chat.nick}{RESET}: "
            print(prefix + wrap(item.text, " " * 9), flush=True)
        elif item.kind == "typing":
            print(f"{DIM}* {chat.nick} is typing...{RESET}", flush=True)
        elif item.kind == "closed":
            print(f"{YELLOW}* {chat.nick} {item.text}{RESET}", flush=True)

    async def _print_incoming(self, chat: ChatSession) -> None:
        while True:
            item = await chat.queue.get()
            self._render(chat, item)

    async def _chat_loop(self, chat: ChatSession) -> None:
        kb = KeyBindings()

        @kb.add("enter")
        def _send(event) -> None:  # type: ignore[no-untyped-def]
            if not event.current_buffer.text.strip():
                return
            event.current_buffer.validate_and_handle()

        @kb.add(SHIFT_ENTER)
        def _newline_shift(event) -> None:  # type: ignore[no-untyped-def]
            event.current_buffer.insert_text("\n")

        # Fallback for terminals that do not send a distinguishable Shift+Enter.
        @kb.add("escape", "enter")
        def _newline_alt(event) -> None:  # type: ignore[no-untyped-def]
            event.current_buffer.insert_text("\n")

        @kb.add("c-q")
        def _leave(event) -> None:  # type: ignore[no-untyped-def]
            event.app.exit(exception=EOFError)

        @kb.add("c-c")
        def _cancel(event) -> None:  # type: ignore[no-untyped-def]
            event.current_buffer.reset()

        chat_session: PromptSession = PromptSession(
            history=_history("chat"),
            key_bindings=kb,
            multiline=True,
        )
        last_typing = 0.0

        def on_text_changed(_buf) -> None:  # type: ignore[no-untyped-def]
            nonlocal last_typing
            now = time.monotonic()
            if now - last_typing > 2.0 and _buf.text.strip():
                last_typing = now
                asyncio.create_task(chat.send_typing())

        chat_session.default_buffer.on_text_changed += on_text_changed

        while not chat.closed.is_set():
            prompt_task = asyncio.create_task(
                chat_session.prompt_async(ANSI(f"{GREEN}>{RESET} "), pre_run=None)
            )
            closed_task = asyncio.create_task(chat.closed.wait())
            done, _ = await asyncio.wait(
                {prompt_task, closed_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if closed_task in done and not prompt_task.done():
                prompt_task.cancel()
                with suppress(asyncio.CancelledError, EOFError):
                    await prompt_task
                break
            closed_task.cancel()
            with suppress(asyncio.CancelledError):
                await closed_task
            try:
                raw = await prompt_task
            except (EOFError, KeyboardInterrupt):
                break
            if not raw.strip():
                continue
            # Keep the text exactly as typed/pasted: leading indentation and
            # trailing blank lines are meaningful when sending code.
            text = prepare_text(raw)
            if text is None:
                print(
                    f"{YELLOW}message too long to send in one frame, split it{RESET}"
                )
                continue
            try:
                await chat.send_text(text)
            except (OSError, ConnectionError):
                print(f"{RED}send failed: peer is gone{RESET}")
                break
            # prompt_toolkit already echoed the line we typed, so no reprint.


def _history(name: str = "menu"):  # type: ignore[no-untyped-def]
    from prompt_toolkit.history import FileHistory

    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    return FileHistory(config.DATA_DIR / f"history-{name}.txt")
