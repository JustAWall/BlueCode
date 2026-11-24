"""Config and state paths (XDG), plus the peer cache in SQLite."""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path


def _home() -> Path:
    return Path.home()


CONFIG_HOME = Path(os.environ.get("XDG_CONFIG_HOME") or _home() / ".config")
DATA_HOME = Path(os.environ.get("XDG_DATA_HOME") or _home() / ".local" / "share")
CONFIG_DIR = CONFIG_HOME / "bluecode"
DATA_DIR = DATA_HOME / "bluecode"

DEFAULT_CHANNEL = 3
NICK_MAX = 24


@dataclass
class Config:
    nick: str = ""
    channel: int = DEFAULT_CHANNEL


def default_nick() -> str:
    try:
        name = socket.gethostname().split(".")[0]
    except OSError:
        name = "bluecode"
    name = "".join(c for c in name if c.isalnum() or c in "-_") or "bluecode"
    return name[:NICK_MAX]


def clean_nick(nick: str) -> str:
    # Drop terminal escape sequences and other control chars: nicks are
    # printed by peers, so a raw "\x1b[31m" would repaint their terminal.
    nick = "".join(ch for ch in nick if ch.isprintable() or ch == " ")
    nick = " ".join(nick.split())
    return nick[:NICK_MAX]


def load_config() -> Config:
    path = CONFIG_DIR / "config.json"
    cfg = Config()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        cfg.nick = clean_nick(str(raw.get("nick", "")))
        cfg.channel = int(raw.get("channel", DEFAULT_CHANNEL))
    except (OSError, ValueError, TypeError):
        pass
    if not cfg.nick:
        cfg.nick = default_nick()
    if not 1 <= cfg.channel <= 30:
        cfg.channel = DEFAULT_CHANNEL
    return cfg


def save_config(cfg: Config) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_DIR / "config.json.tmp"
    tmp.write_text(json.dumps(asdict(cfg), indent=2), encoding="utf-8")
    tmp.replace(CONFIG_DIR / "config.json")


# --- peer cache -----------------------------------------------------------


class PeerCache:
    """Remembers MAC -> nickname so `showonline` can be fast."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or DATA_DIR / "peers.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS peers ("
            " mac TEXT PRIMARY KEY, nick TEXT NOT NULL, last_seen REAL NOT NULL)"
        )
        self.db.commit()

    def remember(self, mac: str, nick: str) -> None:
        self.db.execute(
            "INSERT INTO peers (mac, nick, last_seen) VALUES (?, ?, ?)"
            " ON CONFLICT(mac) DO UPDATE SET nick=excluded.nick, last_seen=excluded.last_seen",
            (mac.upper(), nick, time.time()),
        )
        self.db.commit()

    def nick_of(self, mac: str) -> str | None:
        row = self.db.execute(
            "SELECT nick FROM peers WHERE mac=?", (mac.upper(),)
        ).fetchone()
        return row[0] if row else None

    def all(self) -> dict[str, str]:
        rows = self.db.execute("SELECT mac, nick FROM peers").fetchall()
        return {mac: nick for mac, nick in rows}

    def close(self) -> None:
        self.db.close()


# --- bluetoothctl helpers -------------------------------------------------


def known_devices() -> list[tuple[str, str]]:
    """Return [(mac, name)] of devices BlueZ already knows about."""
    try:
        out = subprocess.run(
            ["bluetoothctl", "devices"],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    devices: list[tuple[str, str]] = []
    for line in out.splitlines():
        # "Device AA:BB:CC:DD:EE:FF Some Name"
        parts = line.strip().split(maxsplit=2)
        if len(parts) >= 2 and parts[0] == "Device" and _is_mac(parts[1]):
            name = parts[2] if len(parts) > 2 else parts[1]
            devices.append((parts[1].upper(), name))
    return devices


def discover(seconds: int = 8) -> set[str]:
    """Actively scan and return the MACs that actually answered (blocking).

    Parsing the scan output matters: `bluetoothctl devices` lists everything
    BlueZ has ever seen (headphones, old phones, ...). Probing that whole set
    is both slow and pointless, so we only keep devices seen in this window.

    We scan BR/EDR only: BlueCode speaks Bluetooth Classic, so BLE beacons
    (hundreds of phones and watches) would just add noise.
    """
    seen: set[str] = set()
    out = ""
    for args in (["scan", "bredr"], ["scan", "on"]):
        try:
            out = subprocess.run(
                ["bluetoothctl", "--timeout", str(seconds), *args],
                capture_output=True,
                text=True,
                timeout=seconds + 5,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        if "Device" in out:
            break
    for line in out.splitlines():
        parts = line.replace("\x1b", "").split()
        # Only devices present right now: [DEL] means it left the area.
        if parts and parts[0].startswith("[DEL]"):
            continue
        for i, token in enumerate(parts[:-1]):
            if token == "Device" and _is_mac(parts[i + 1]):
                seen.add(parts[i + 1].upper())
                break
    return seen


def is_paired(mac: str) -> bool:
    """True if BlueZ already has a link key for this device."""
    try:
        out = subprocess.run(
            ["bluetoothctl", "info", mac.upper()],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return any(
        line.strip() == "Paired: yes" for line in out.replace("\x1b", "").splitlines()
    )


def is_known(mac: str) -> bool:
    """True if BlueZ has any record of this device (seen at least once)."""
    try:
        out = subprocess.run(
            ["bluetoothctl", "info", mac.upper()],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    text = out.replace("\x1b", "")
    # Unknown devices produce "Device XX:... not available".
    return "Device " in text and "not available" not in text


def ensure_known(mac: str, seconds: int = 12) -> bool:
    """Scan until BlueZ knows `mac`, so `pair` does not fail with 'not available'.

    `bluetoothctl pair` refuses MACs it has never seen, which is the usual
    reason a first-time pairing fails even though the device is right there.
    """
    mac = mac.upper()
    if is_known(mac):
        return True
    return mac in discover(seconds)


def pair(mac: str, timeout: float = 45.0) -> str | None:
    """Pair + trust a device. Returns None on success, else an error text.

    Handles the three real-world failure modes we observed with BlueZ 5.87:
    the device being unknown to BlueZ, `pairable` having been reset, and no
    agent being registered in non-interactive mode.
    """
    mac = mac.upper()
    if is_paired(mac):
        _trust(mac)
        return None

    # Pairable can be reset by a previous failed attempt; set it every time.
    _run_bt(["pairable", "on"])

    if not ensure_known(mac):
        return (
            "device not found nearby. Turn Bluetooth on there, make it "
            "discoverable, and keep both machines within a few meters"
        )

    try:
        proc = subprocess.run(
            ["bluetoothctl", "--timeout", str(int(timeout)), "--agent",
             "NoInputNoOutput", "pair", mac],
            capture_output=True,
            text=True,
            timeout=timeout + 10,
            input="yes\n",
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"pairing failed: {exc}"

    text = (proc.stdout + proc.stderr).replace("\x1b", "")
    if "Pairing successful" in text or "AlreadyExists" in text or "already paired" in text.lower():
        _trust(mac)
        return None
    if "AuthenticationFailed" in text or "Rejected" in text or "rejected" in text:
        return "pairing rejected: confirm the request on the other device"
    if "ConnectionAttemptFailed" in text or "not available" in text:
        return "could not connect: device is off, out of range, or already busy"
    if "InProgress" in text:
        return "pairing already in progress, wait a few seconds and retry"
    return "pairing failed; try `bluetoothctl pair " + mac + "` manually"


def _trust(mac: str) -> None:
    _run_bt(["trust", mac])


def _run_bt(args: list[str], timeout: float = 10.0) -> bool:
    try:
        proc = subprocess.run(
            ["bluetoothctl", *args], capture_output=True, text=True, timeout=timeout
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def _is_mac(value: str) -> bool:
    parts = value.split(":")
    return len(parts) == 6 and all(
        len(p) == 2 and all(c in "0123456789abcdefABCDEF" for c in p) for p in parts
    )


is_mac = _is_mac
