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


def discover(seconds: int = 8) -> None:
    """Actively scan for nearby devices (blocking)."""
    try:
        subprocess.run(
            ["bluetoothctl", "--timeout", str(seconds), "scan", "on"],
            capture_output=True,
            text=True,
            timeout=seconds + 5,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def _is_mac(value: str) -> bool:
    parts = value.split(":")
    return len(parts) == 6 and all(
        len(p) == 2 and all(c in "0123456789abcdefABCDEF" for c in p) for p in parts
    )


is_mac = _is_mac
