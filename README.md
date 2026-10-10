# Tubeless

A tiny touchscreen TV. A Raspberry Pi Zero 2 W, a 2.8" screen in a 3D-printed retro shell, a speaker, and a
folder of videos: swipe to flip channels, with a burst of TV static between episodes.

> **Status: early.** It runs on the author's TV every day, but this is the first cut of the shareable version.
> It currently supports exactly one build (see [Hardware](#hardware)). Names, settings and file layouts may still change.

## What it does

* **Channels** are just folders in `~/videos` (`1.cartoons`, `2.movies`, ...). Swipe up/down to change channel.
* **Swipe left/right** for the next/previous episode, **tap** to pause, **hold** for the menu.
* A **short static burst** plays in front of every video, and a longer one when you change channel.
* **Shuffle that doesn't repeat**: each channel's shuffled order is remembered and worked through; it only reshuffles
  once you've been through everything. New videos are slotted into what you haven't seen yet.
* **Resumes where you left off**, in the right episode and the right place in it, even after unplugging it.
* **Menu** (hold): play/pause, next/previous, brightness, channels, and *Settings*: shuffle, reshuffle, sleep timer,
  auto-dim, touch calibration, shut down.
* **First-run touch calibration** (and a menu item to redo it when the screen gets dirty).
* **Boot screen**: green digital rain with a live status of what's starting up.
* Software PWM **backlight dimming**, because the audio takes both hardware PWM channels.

## Try it without a Pi

```
uv venv --python 3.9 .venv && uv pip install --python .venv/bin/python numpy pillow pytest
.venv/bin/python -m tubeless sim
```

Open <http://127.0.0.1:8765/>. The real app runs against a pretend Pi and a pretend mpv; the mouse is a finger
(or use the buttons / arrow keys). `--fresh` starts from scratch and shows the first-run calibration;
`--screen boot` previews the boot screen.

```
.venv/bin/python -m pytest          # 78 tests, including whole-app tests driven by synthetic touches
```

## Hardware

| | |
|---|---|
| Board | Raspberry Pi Zero 2 W |
| Screen | Waveshare 2.8" DPI LCD (480x640 portrait, mounted sideways), Goodix touch controller |
| Backlight | GPIO 18 |
| Audio | mono PWM speaker/amp on GPIO 19 (`dtoverlay=audremap,pins_18_19`) |
| OS (tested) | Raspberry Pi OS Lite 32-bit, Bullseye (kernel 6.1) |

Everything specific to this build lives in [`tubeless/hardware.py`](tubeless/hardware.py). Support for another
screen means adding a profile there. Pull requests welcome; the project itself only promises the build above.

## Layout

```
tubeless/          the app (see docs/ARCHITECTURE.md)
  app.py           gestures, menu, calibration, standby, resume
  library.py       channels, shuffle order, resume
  hal/             hardware: pi.py (real) and sim.py (pretend)
  player.py        mpv driver, and a fake mpv for the simulator and tests
  bootscreen.py    the boot screen
tests/             unit tests and whole-app tests
tools/             deploy.sh, on-TV end-to-end test, Bookworm trial kit
packaging/         systemd unit templates
docs/              architecture, roadmap, trial instructions
```

## Running on a TV

For now: `tools/deploy.sh <ssh-host>` copies the code to a Pi that already has the display working,
installs the services and starts it. A proper installer (and later a flashable image) is on the
[roadmap](docs/ROADMAP.md).

## Credits and licence

The shell and wiring follow the build guide at <https://withrow.io/simpsons-tv-build-guide-waveshare>
(whose original player code is at <https://github.com/buba447/simpsonstv>). The display overlays come from
Waveshare's wiki for the 2.8" DPI LCD. Fonts are bundled under their own licences
([`tubeless/assets/fonts/LICENSES.txt`](tubeless/assets/fonts/LICENSES.txt)).

**No videos are included, and none ever will be.** Bring your own.

Licence: *to be decided before the first public release.*
