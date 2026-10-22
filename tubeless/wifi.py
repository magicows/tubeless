"""
Wi-Fi setup: what the TV does when it has no network (or you want to change it).

The TV makes its own WPA2 hotspot ("Tubeless-XXXX"); you join it from a phone or laptop, pick your home network on a
page that opens by itself (a "captive portal"), type its password, and the TV joins it. Uses NetworkManager, which is
what Raspberry Pi OS Bookworm and Trixie use; older releases (Bullseye) don't have it, and then the feature is
simply not offered.

Everything that needs root goes through one tiny root-owned helper (packaging/tubeless-wifi-helper) via sudo.
This module is the app's side of that, plus a fake for tests and the simulator.
"""
from __future__ import annotations

import json
import socket
import struct
import logging
import os
import secrets
import shutil
import subprocess
import time
from dataclasses import dataclass
from typing import List, Optional, Tuple

log = logging.getLogger(__name__)

HELPER_PATH = "/usr/local/lib/tubeless/wifi-helper"
HOTSPOT_IP = "10.42.0.1"
_PASSWORD_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"          # no look-alikes (l/1/i, 0/o)


@dataclass
class Network:
    ssid: str
    signal: int = 0
    secured: bool = True

    def as_dict(self) -> dict:
        return {"ssid": self.ssid, "signal": self.signal, "secured": self.secured}


def new_hotspot_credentials() -> Tuple[str, str]:
    """A fresh (SSID, password) for this setup session. The password is shown on the TV, which is the 'proof' of
    being in the room."""
    ssid = "Tubeless-" + "".join(secrets.choice("ABCDEFGHJKMNPQRSTUVWXYZ23456789") for _ in range(4))
    password = "".join(secrets.choice(_PASSWORD_ALPHABET) for _ in range(8))
    return ssid, password


def wifi_qr_text(ssid: str, password: str) -> str:
    """The text a phone camera understands as 'join this Wi-Fi'."""
    def esc(s: str) -> str:
        return "".join("\\" + c if c in '\\;,:"' else c for c in s)
    return f"WIFI:T:WPA;S:{esc(ssid)};P:{esc(password)};;"


def parse_scan(text: str) -> List[Network]:
    try:
        raw = json.loads(text or "[]")
    except ValueError:
        return []
    out = []
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, dict) and isinstance(item.get("ssid"), str) and item["ssid"]:
            out.append(Network(item["ssid"], int(item.get("signal") or 0), bool(item.get("secured", True))))
    return out


def icmp_packet(sequence: int = 1, ident: int = 0, payload: bytes = b"tubeless") -> bytes:
    """An ICMP echo request (type 8) with its checksum."""
    header = struct.pack("!BBHHH", 8, 0, 0, ident, sequence)
    data = header + payload
    total = sum((data[i] << 8) + (data[i + 1] if i + 1 < len(data) else 0) for i in range(0, len(data), 2))
    while total >> 16:
        total = (total & 0xFFFF) + (total >> 16)
    return struct.pack("!BBHHH", 8, 0, (~total) & 0xFFFF, ident, sequence) + payload


def ping(host: str, timeout: float = 2.0, tries: int = 2) -> Optional[bool]:
    """Does `host` answer a ping? True/False, or None if this system won't let an ordinary user ping (then nobody should act on it).
    Uses Linux's unprivileged ICMP sockets, so no root and no extra program."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_ICMP)
    except (PermissionError, OSError, AttributeError):
        return None
    try:
        sock.settimeout(timeout)
        for n in range(tries):
            try:
                sock.sendto(icmp_packet(n + 1), (host, 0))
                reply, _ = sock.recvfrom(1024)
                if reply and len(reply) >= 1:
                    return True
            except socket.timeout:
                continue
            except OSError:
                return False                      # no route / network unreachable
        return False
    finally:
        sock.close()


def default_gateway(route_output: str, interface: str = "wlan0") -> Optional[str]:
    """The router's address from `ip -4 route show default`."""
    for line in (route_output or "").splitlines():
        parts = line.split()
        if parts[:1] == ["default"] and "via" in parts and ("dev" not in parts or parts[parts.index("dev") + 1] == interface):
            return parts[parts.index("via") + 1]
    return None


class Wifi:
    """What the app needs. connected_ip() and has_saved() are cheap; the rest take seconds."""

    available = False

    def connected_ip(self) -> Optional[str]: raise NotImplementedError
    def reachable(self) -> Optional[bool]:
        """Can we actually reach the router? (Having an address proves nothing: a crashed Wi-Fi chip keeps its address.)
        None if we can't tell."""
        return None
    def has_saved(self) -> bool: raise NotImplementedError
    def scan(self) -> List[Network]: raise NotImplementedError
    def hotspot_up(self, ssid: str, password: str) -> Tuple[bool, str]: raise NotImplementedError
    def hotspot_down(self) -> None: raise NotImplementedError
    def connect(self, ssid: str, password: str) -> Tuple[bool, str]: raise NotImplementedError
    def recover(self) -> Tuple[bool, str]: raise NotImplementedError


class NetworkManagerWifi(Wifi):
    def __init__(self, helper: str = HELPER_PATH, interface: str = "wlan0"):
        self.helper, self.interface = helper, interface
        self.available = bool(shutil.which("nmcli")) and os.path.exists(helper)

    def _helper(self, *args: str, stdin: str = "", timeout: int = 90) -> subprocess.CompletedProcess:
        return subprocess.run(["sudo", "-n", self.helper, *args], input=stdin + "\n", capture_output=True, text=True,
                              timeout=timeout)

    def connected_ip(self) -> Optional[str]:
        try:
            out = subprocess.run(["ip", "-4", "-o", "addr", "show", "dev", self.interface], capture_output=True,
                                 text=True, timeout=5).stdout
            state = subprocess.run(["nmcli", "-t", "-f", "DEVICE,STATE,CONNECTION", "dev"], capture_output=True,
                                   text=True, timeout=5).stdout
        except (OSError, subprocess.SubprocessError):
            return None
        for line in state.splitlines():                                  # our own hotspot doesn't count as 'online'
            if line.startswith(self.interface + ":") and "tubeless-hotspot" in line:
                return None
        for part in out.split():
            if "/" in part and part.count(".") == 3:
                return part.split("/")[0]
        return None

    def reachable(self) -> Optional[bool]:
        try:
            routes = subprocess.run(["ip", "-4", "route", "show", "default"], capture_output=True, text=True, timeout=5).stdout
        except (OSError, subprocess.SubprocessError):
            return None
        gateway = default_gateway(routes, self.interface)
        return ping(gateway) if gateway else None

    def has_saved(self) -> bool:
        try:
            res = self._helper("has-saved", timeout=20)
        except (OSError, subprocess.SubprocessError):
            return True                                                  # can't tell: don't hijack the screen
        return res.returncode != 0 or res.stdout.strip() != "no"

    def scan(self) -> List[Network]:
        try:
            res = self._helper("scan", timeout=30)
        except (OSError, subprocess.SubprocessError) as e:
            log.warning("wifi scan failed: %s", e)
            return []
        return parse_scan(res.stdout) if res.returncode == 0 else []

    def hotspot_up(self, ssid: str, password: str) -> Tuple[bool, str]:
        try:
            res = self._helper("hotspot-up", ssid, stdin=password, timeout=60)
        except (OSError, subprocess.SubprocessError) as e:
            return False, str(e)
        return res.returncode == 0, res.stderr.strip()

    def hotspot_down(self) -> None:
        try:
            self._helper("hotspot-down", timeout=30)
        except (OSError, subprocess.SubprocessError):
            pass

    def recover(self) -> Tuple[bool, str]:
        """The Wi-Fi chip crashed and the network is gone: reload its driver (root helper). Takes up to half a minute."""
        try:
            res = self._helper("recover", timeout=120)
        except (OSError, subprocess.SubprocessError) as e:
            return False, str(e)
        return res.returncode == 0, res.stderr.strip()

    def connect(self, ssid: str, password: str) -> Tuple[bool, str]:
        try:
            res = self._helper("connect", ssid, stdin=password, timeout=90)
        except (OSError, subprocess.SubprocessError) as e:
            return False, str(e)
        return res.returncode == 0, res.stderr.strip()


class FakeWifi(Wifi):
    """For tests and the desktop simulator: pretends to be a TV with some neighbours' networks."""
    available = True

    def __init__(self, ip: Optional[str] = None, saved: bool = False, networks=None, accepts=None, delay: float = 0.0):
        self.ip, self.saved = ip, saved
        self.networks = networks if networks is not None else [Network("HomeWiFi", 80), Network("Neighbour", 45), Network("CafeOpen", 30, False)]
        self.accepts = accepts if accepts is not None else {"HomeWiFi": "correct horse"}      # ssid -> password
        self.delay = delay
        self.recovers, self.ip_after_recovery = True, "192.168.1.50"          # does a recover() bring the network back?
        self.zombie = False                                                     # keeps its address but reaches nothing
        self.hotspot: Optional[Tuple[str, str]] = None
        self.calls: List[tuple] = []

    def connected_ip(self): return None if self.hotspot else self.ip

    def reachable(self):
        if self.zombie:                                       # the chip is dead but the interface still has its address
            return False
        return None if self.ip is None else True

    def has_saved(self): return self.saved

    def scan(self):
        self.calls.append(("scan",))
        return list(self.networks)

    def hotspot_up(self, ssid, password):
        self.calls.append(("hotspot_up", ssid))
        self.hotspot = (ssid, password)
        return True, ""

    def hotspot_down(self):
        self.calls.append(("hotspot_down",))
        self.hotspot = None

    def recover(self):
        self.calls.append(("recover",))
        if self.recovers:
            self.ip, self.zombie = self.ip_after_recovery, False
        return self.recovers, "" if self.recovers else "the Wi-Fi chip did not come back"

    def connect(self, ssid, password):
        self.calls.append(("connect", ssid))
        self.hotspot = None
        time.sleep(self.delay)
        if ssid in self.accepts and self.accepts[ssid] == password:
            self.ip, self.saved = "192.168.1.50", True
            return True, ""
        return False, "Secrets were required, but not provided." if ssid in self.accepts else "No network with that name."


def make_wifi() -> Wifi:
    return NetworkManagerWifi()
