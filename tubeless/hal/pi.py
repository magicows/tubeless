"""The real Pi: evdev touch, pigpio backlight, /dev/fb0, systemctl power-off."""
from __future__ import annotations

import fcntl
import logging
import mmap
import select
import struct
import subprocess
import time
from typing import List, Optional

import numpy as np

from . import Backend, TouchSample

log = logging.getLogger(__name__)

FB_DEV = "/dev/fb0"
FB_SYS = "/sys/class/graphics/fb0"


class EvdevTouch:
    """
    Reads the touchscreen (multi-touch protocol B) and reports snapshots: how many fingers are down and where
    the first one is, normalised to 0..1 of the controller's raw range.
    """

    def __init__(self, hw):
        self.hw = hw
        self.device = None
        self.x_max = self.y_max = 1
        self.slot = 0
        self.tracking = {}
        self.pos_x = {}
        self.pos_y = {}

    def _find(self) -> str:
        import evdev
        for path in evdev.list_devices():
            try:
                dev = evdev.InputDevice(path)
                if self.hw.touch_name.lower() in dev.name.lower():
                    return path
            except OSError:
                continue
        return self.hw.touch_fallback

    def _open(self):
        import evdev
        from evdev import ecodes
        path = self._find()
        self.device = evdev.InputDevice(path)
        self.x_max = self.device.absinfo(ecodes.ABS_MT_POSITION_X).max or 1
        self.y_max = self.device.absinfo(ecodes.ABS_MT_POSITION_Y).max or 1
        self.slot, self.tracking, self.pos_x, self.pos_y = 0, {}, {}, {}
        self._sync_slots()
        log.info("listening on %s (%s) axes %sx%s", path, self.device.name, self.x_max, self.y_max)

    def _mt_values(self, code: int, nslots: int) -> List[int]:
        """EVIOCGMTSLOTS: the kernel's current value of one multi-touch axis for every slot."""
        size = 4 * (nslots + 1)
        request = (2 << 30) | (size << 16) | (ord("E") << 8) | 0x0A          # _IOC(_IOC_READ, 'E', 0x0a, size)
        buf = bytearray(size)
        struct.pack_into("I", buf, 0, code)
        fcntl.ioctl(self.device.fd, request, buf, True)
        return list(struct.unpack_from("%di" % nslots, buf, 4))

    def _sync_slots(self) -> None:
        """
        The kernel only reports a coordinate when it *changes*, so a reader that starts late would never learn the
        position of a touch that lands on the same X (or Y) as the previous one. Ask for what each slot holds now.
        """
        from evdev import ecodes
        try:
            nslots = self.device.absinfo(ecodes.ABS_MT_SLOT).max + 1
            xs = self._mt_values(ecodes.ABS_MT_POSITION_X, nslots)
            ys = self._mt_values(ecodes.ABS_MT_POSITION_Y, nslots)
            ids = self._mt_values(ecodes.ABS_MT_TRACKING_ID, nslots)
        except Exception as e:
            log.info("could not read the touch slots' current state (%s)", e)
            return
        for slot in range(nslots):
            self.pos_x[slot], self.pos_y[slot], self.tracking[slot] = xs[slot], ys[slot], ids[slot]

    def read(self, timeout: Optional[float]) -> List[TouchSample]:
        from evdev import ecodes
        if self.device is None:
            try:
                self._open()
            except Exception as e:
                log.warning("touch device not ready: %s", e)
                time.sleep(1.0)
                return []
        try:
            ready, _, _ = select.select([self.device.fd], [], [], timeout)
            if not ready:
                return []
            samples: List[TouchSample] = []
            for ev in self.device.read():
                if ev.type == ecodes.EV_ABS:
                    if ev.code == ecodes.ABS_MT_SLOT:
                        self.slot = ev.value
                    elif ev.code == ecodes.ABS_MT_TRACKING_ID:
                        self.tracking[self.slot] = ev.value
                    elif ev.code == ecodes.ABS_MT_POSITION_X:
                        self.pos_x[self.slot] = ev.value
                    elif ev.code == ecodes.ABS_MT_POSITION_Y:
                        self.pos_y[self.slot] = ev.value
                elif ev.type == ecodes.EV_KEY and ev.code == ecodes.BTN_TOUCH and ev.value == 0:
                    for s in self.tracking:
                        self.tracking[s] = -1                       # everything is up
                elif ev.type == ecodes.EV_SYN and ev.code == ecodes.SYN_REPORT:
                    down = [s for s, t in self.tracking.items() if t != -1]
                    if len(down) > 1:
                        samples.append(TouchSample(len(down)))
                    elif len(down) == 1 and down[0] in self.pos_x and down[0] in self.pos_y:
                        samples.append(TouchSample(1, self.pos_x[down[0]] / self.x_max, self.pos_y[down[0]] / self.y_max))
                    elif not down:
                        samples.append(TouchSample(0))
            return samples
        except Exception:
            log.exception("touch read failed; reopening the device")
            try:
                self.device.close()
            except Exception:
                pass
            self.device = None
            time.sleep(1.0)
            return []


class PigpioBacklight:
    """
    The backlight is on GPIO 18. Both hardware PWM channels belong to the audio, so it is dimmed
    with pigpio's DMA-timed software PWM instead.
    """

    def __init__(self, hw):
        self.hw = hw
        self.pi = None

    @property
    def ok(self) -> bool:
        return self.pi is not None

    def start(self, wait: float = 15.0) -> None:
        """Connect to pigpiod (it may still be starting when we launch at boot)."""
        try:
            import pigpio
        except ImportError:
            log.warning("pigpio not installed - backlight will be on/off only")
            return
        deadline = time.time() + wait
        while True:
            pi = pigpio.pi()
            if pi.connected:
                self.pi = pi
                pi.set_mode(self.hw.backlight_pin, pigpio.OUTPUT)
                pi.set_PWM_frequency(self.hw.backlight_pin, self.hw.backlight_pwm_hz)
                log.info("backlight PWM ready on GPIO %d", self.hw.backlight_pin)
                return
            if time.time() > deadline:
                log.warning("pigpiod not reachable - backlight will be on/off only")
                return
            time.sleep(0.5)

    def set(self, percent: int) -> None:
        percent = max(0, min(100, int(percent)))
        if self.pi is not None:
            self.pi.set_PWM_dutycycle(self.hw.backlight_pin, round(percent * 255 / 100))
        else:
            level = "dh" if percent > 0 else "dl"
            try:
                subprocess.run(["raspi-gpio", "set", str(self.hw.backlight_pin), "op", level], check=True)
            except Exception as e:
                log.warning("could not set the backlight pin: %s", e)
        log.info("backlight -> %d%%", percent)


class Framebuffer:
    """/dev/fb0 as 16-bit RGB565, fed landscape frames (the boot screen draws straight here)."""

    def __init__(self, hw):
        import os
        self.hw = hw
        w, h = (int(v) for v in open(FB_SYS + "/virtual_size").read().strip().split(","))
        self.w, self.h = w, h
        self.bpp = int(open(FB_SYS + "/bits_per_pixel").read())
        self.stride = int(open(FB_SYS + "/stride").read())
        self.f = open(FB_DEV, "r+b")
        self.mm = mmap.mmap(self.f.fileno(), self.stride * h)

    def show(self, px565_landscape) -> None:
        if self.bpp != 16:
            raise RuntimeError(f"unsupported framebuffer depth {self.bpp} (expected 16)")
        px = np.ascontiguousarray(np.rot90(px565_landscape, self.hw.rot_k)).astype("<u2")
        rows = px.view(np.uint8).reshape(self.h, -1)
        if rows.shape[1] != self.stride:
            padded = np.zeros((self.h, self.stride), np.uint8)
            padded[:, :rows.shape[1]] = rows
            rows = padded
        self.mm[:self.stride * self.h] = rows.tobytes()


class PiPower:
    def __init__(self, cfg):
        self.cfg = cfg

    def shutdown(self) -> None:
        log.info("shutting down: %s", self.cfg.shutdown_command)
        subprocess.Popen(self.cfg.shutdown_command)

    def reboot(self) -> None:
        subprocess.Popen(["sudo", "systemctl", "reboot"])


def make(cfg, hw) -> Backend:
    backend = Backend(touch=EvdevTouch(hw), backlight=PigpioBacklight(hw), power=PiPower(cfg))
    try:
        backend.display = Framebuffer(hw)
    except Exception as e:                      # the boot screen is optional
        log.info("no framebuffer: %s", e)
    return backend
