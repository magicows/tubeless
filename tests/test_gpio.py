import subprocess

from tubeless.hal import pi
from tubeless.hardware import Hardware


def fake_which(available):
    return lambda tool: f"/usr/bin/{tool}" if tool in available else None


def record(monkeypatch, available):
    calls = []
    monkeypatch.setattr(pi.shutil, "which", fake_which(available))
    monkeypatch.setattr(pi.subprocess, "run", lambda cmd, check=False: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0))
    return calls


def test_prefers_pinctrl(monkeypatch):
    calls = record(monkeypatch, {"pinctrl", "raspi-gpio"})
    assert pi.gpio_set(19, "a5") is True
    assert calls == [["pinctrl", "set", "19", "a5"]]


def test_falls_back_to_raspi_gpio(monkeypatch):
    calls = record(monkeypatch, {"raspi-gpio"})
    assert pi.gpio_set(18, "op", "dh") is True
    assert calls == [["raspi-gpio", "set", "18", "op", "dh"]]


def test_neither_tool_is_not_fatal(monkeypatch):
    calls = record(monkeypatch, set())
    assert pi.gpio_set(19, "a5") is False and calls == []


def test_audio_start_sets_the_amp_pin(monkeypatch):
    calls = record(monkeypatch, {"pinctrl"})
    pi.PiAudio(Hardware()).start()
    assert calls == [["pinctrl", "set", "19", "a5"]]


def test_backlight_without_pigpio_uses_the_gpio_tool(monkeypatch):
    calls = record(monkeypatch, {"pinctrl"})
    bl = pi.PigpioBacklight(Hardware())
    bl.set(100)
    bl.set(0)
    assert calls == [["pinctrl", "set", "18", "op", "dh"], ["pinctrl", "set", "18", "op", "dl"]]
