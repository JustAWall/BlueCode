"""RFCOMM (Bluetooth Classic, SPP-style) transport using the Linux kernel.

No PyBluez needed: Python ships AF_BLUETOOTH/BTPROTO_RFCOMM on Linux.
Requires BlueZ + a paired adapter; the peer must have paired/have trust set
up once (`bluetoothctl pair <MAC>`).
"""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import time
from typing import Awaitable, Callable

from .base import Link


def platform_problem() -> str | None:
    """Explain up front why this platform cannot work, instead of a traceback.

    Ubuntu/Debian with BlueZ: fine. Termux/Android: the kernel is Linux but
    apps are blocked from raw HCI/RFCOMM sockets by SELinux and there is no
    BlueZ/bluetoothctl, so nobody can connect. macOS/Windows have no
    AF_BLUETOOTH at all.
    """
    if os.environ.get("ANDROID_ROOT") or "com.termux" in os.environ.get("PREFIX", ""):
        return (
            "Termux/Android is not supported for RFCOMM: Android blocks raw\n"
            "Bluetooth sockets for apps and has no BlueZ. A Termux port would\n"
            "need a BLE backend via Android APIs (pyjnius), which is a different\n"
            "transport. Use a laptop with Linux instead."
        )
    if not sys.platform.startswith("linux"):
        return (
            f"this platform ({sys.platform}) is not supported: BlueCode needs\n"
            "Linux + BlueZ (AF_BLUETOOTH/RFCOMM sockets)."
        )
    if not hasattr(socket, "AF_BLUETOOTH"):
        return (
            "this Python build has no AF_BLUETOOTH support (usually because it\n"
            "was compiled without Linux Bluetooth headers)."
        )
    return None


def _rfcomm_socket() -> socket.socket:
    sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    return sock


def _peer_mac(sock: socket.socket) -> str:
    try:
        addr = sock.getpeername()
    except OSError:
        return ""
    if isinstance(addr, (tuple, list)) and addr:
        return str(addr[0]).upper()
    return str(addr).upper()


async def listen(
    channel: int,
    handler: Callable[[Link], Awaitable[None]],
) -> tuple[asyncio.AbstractServer, int]:
    """Bind an RFCOMM listener and call `handler(link)` per connection."""
    sock = _rfcomm_socket()
    sock.bind(("00:00:00:00:00:00", channel))
    sock.setblocking(False)

    async def _on_conn(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("socket")
        link = Link(reader=reader, writer=writer, mac=_peer_mac(peer) if peer else "")
        try:
            await handler(link)
        except (OSError, asyncio.CancelledError, ConnectionError):
            pass
        finally:
            if not link.extras.get("owned"):
                await link.close()

    # asyncio calls listen() itself; calling it twice makes the RFCOMM
    # socket fail with EBADFD on Linux, so do not pre-listen here.
    server = await asyncio.start_server(_on_conn, sock=sock)
    actual = sock.getsockname()[1] or channel
    return server, actual


async def connect(mac: str, channel: int, timeout: float = 5.0) -> Link:
    """Open an RFCOMM connection to `mac` on `channel`."""
    sock = _rfcomm_socket()
    sock.setblocking(False)
    loop = asyncio.get_running_loop()
    try:
        await asyncio.wait_for(loop.sock_connect(sock, (mac.upper(), channel)), timeout)
        reader, writer = await asyncio.open_connection(sock=sock)
    except BaseException:
        sock.close()
        raise
    return Link(reader=reader, writer=writer, mac=mac.upper())


def own_macs() -> list[str]:
    """Addresses of local Bluetooth controllers (from BlueZ)."""
    try:
        out = subprocess.run(
            ["bluetoothctl", "list"], capture_output=True, text=True, timeout=5
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    macs: list[str] = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].lower().startswith("controller"):
            macs.append(parts[1].upper())
    return macs


def _run(cmd: list[str], timeout: float = 5.0) -> bool:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def set_discoverable(on: bool) -> None:
    """Best-effort: make this adapter visible to other BlueCode users."""
    for cmd in (["power", "on"], ["pairable", "on"], ["discoverable", "on" if on else "off"]):
        _run(["bluetoothctl", *cmd])


def ensure_adapter(timeout: float = 6.0) -> str | None:
    """Bring Bluetooth up if it is not already, returning an error or None."""
    problem = platform_problem()
    if problem:
        return problem
    return _ensure_adapter_blocking(timeout)


def _ensure_adapter_blocking(timeout: float = 6.0) -> str | None:
    """Blocking implementation of `ensure_adapter`.

    `bluecode` should just work: unblock rfkill, start bluetoothd, power the
    controller on. Anything that needs interactive auth (polkit) cannot be
    fixed from here, so we report exactly what to run instead of hanging.
    """
    if own_macs():
        return None

    for cmd in (
        ["rfkill", "unblock", "bluetooth"],
        ["systemctl", "start", "bluetooth"],
    ):
        _run(cmd, timeout=timeout)

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        _run(["bluetoothctl", "power", "on"])
        macs = own_macs()
        if macs:
            return None
        time.sleep(0.4)

    return (
        "no Bluetooth adapter found.\n"
        "Try manually:\n"
        "  sudo systemctl start bluetooth\n"
        "  rfkill unblock bluetooth\n"
        "  bluetoothctl power on"
    )


async def ensure_adapter_async() -> str | None:
    """Non-blocking wrapper: subprocess calls must not freeze the UI loop."""
    problem = platform_problem()
    if problem:
        return problem
    return await asyncio.to_thread(_ensure_adapter_blocking)


async def own_macs_async() -> list[str]:
    return await asyncio.to_thread(own_macs)


async def set_discoverable_async(on: bool) -> None:
    await asyncio.to_thread(set_discoverable, on)
