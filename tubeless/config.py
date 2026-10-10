"""User-editable settings (config.json). Everything has a sensible default."""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import List, Optional

log = logging.getLogger(__name__)

PACKAGE_DIR = Path(__file__).resolve().parent
ASSETS_DIR = PACKAGE_DIR / "assets"


def expand(path) -> Path:
    return Path(os.path.expanduser(str(path)))


MAX_NAME_LENGTH = 16          # longer names don't fit next to the channel name in the menu title


def user_config_path() -> Path:
    return expand(os.environ.get("TUBELESS_CONFIG") or "~/.config/tubeless/config.json")


@dataclass
class Config:
    # What this TV is called (shown in the menu and on the boot screen).
    name: str = "Tubeless"

    # Where things live.
    videos_dir: str = "~/videos"
    data_dir: str = "~/.local/share/tubeless"
    log_file: str = "~/.local/share/tubeless/tubeless.log"
    mpv_log: str = "~/.local/share/tubeless/mpv.log"
    mpv_socket: str = "/tmp/tubeless-mpv.sock"
    # The static bursts between episodes. Empty = the ones that ship with Tubeless.
    static_short: str = ""
    static_long: str = ""

    # The line typed out on the boot screen.
    boot_greeting: str = "Wake up, couch potato..."

    # Hardware profile and per-field overrides (see hardware.py).
    hardware: str = "waveshare-2.8-dpi"
    hardware_overrides: dict = field(default_factory=dict)

    # Touch feel.
    menu_timeout: float = 5.0
    long_press: float = 1.0
    touch_move_px: float = 25.0
    swipe_min_px: float = 70.0
    scroll_px_per_row: float = 60.0

    # Power.
    shutdown_command: List[str] = field(default_factory=lambda: ["sudo", "systemctl", "poweroff"])

    # An older install's settings file, imported once on first start (brightness, shuffle, channel).
    legacy_state_file: str = "~/.simptv_state.json"

    # --- helpers -------------------------------------------------------------
    @property
    def videos(self) -> Path:
        return expand(self.videos_dir)

    @property
    def data(self) -> Path:
        return expand(self.data_dir)

    @property
    def short_static(self) -> str:
        return str(expand(self.static_short)) if self.static_short else str(ASSETS_DIR / "static_short.mp4")

    @property
    def long_static(self) -> str:
        return str(expand(self.static_long)) if self.static_long else str(ASSETS_DIR / "static_long.mp4")

    @classmethod
    def from_dict(cls, data: dict) -> "Config":
        known = {f.name for f in fields(cls)}
        unknown = sorted(set(data) - known)
        for key in unknown:
            log.warning("ignoring unknown config setting %r", key)
        return cls(**{k: v for k, v in data.items() if k in known})

    @classmethod
    def load(cls, path: Optional[str] = None) -> "Config":
        candidates = [path] if path else [
            os.environ.get("TUBELESS_CONFIG"),
            "~/.config/tubeless/config.json",
            "/etc/tubeless/config.json",
        ]
        for candidate in candidates:
            if candidate and expand(candidate).is_file():
                with open(expand(candidate)) as f:
                    cfg = cls.from_dict(json.load(f))
                log.info("config loaded from %s", expand(candidate))
                return cfg
        return cls()
