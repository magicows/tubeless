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

## Getting videos onto the TV

With no videos, the TV says so and offers **Upload page**. (It is also in Settings > Upload videos.) The screen then
shows an address, a QR code and a 4-digit PIN; open it on your phone or laptop (same Wi-Fi), enter the PIN, pick a
channel and send files. The same page renames the TV. Hold the TV's screen to leave. Copying over SFTP/scp into
`~/videos/<channel>/` works too.

## YouTube channels (optional)

A channel can be made of YouTube playlists and single videos. Install the extra pieces once:

```bash
sudo sh install.sh --with-youtube        # ffmpeg, and yt-dlp from PyPI into ~/.local/share/tubeless/ytdlp
```

Then open the phone page (Settings > Upload videos), choose **+ YouTube channel**, paste a playlist or video link, tick the
videos you want, and add more playlists or single videos to the same channel whenever you like.

* **Download to the TV** (the default): the TV fetches the videos at 480p while you wait, then plays them like any other
  video, with no Wi-Fi needed. Downloads run only while the phone page is open.
* **Untick it to stream** instead: nothing is stored; each video loads from YouTube when its turn comes (about 10 seconds on a
  Zero 2 W) and needs Wi-Fi. If a video can't be played the TV says so and moves on; fix or delete the channel on the phone page.
* Public and unlisted playlists only; there is no YouTube sign-in. YouTube keeps changing, so update yt-dlp now and then:
  `python3 -m tubeless youtube update` (`youtube status` shows the version).

This uses [yt-dlp](https://github.com/yt-dlp/yt-dlp), which Tubeless does not include. Downloading from YouTube is against
YouTube's terms of service and this route doesn't show YouTube's ads, so it is an opt-in extra for your own use; you are
responsible for how you use it.

## Make it yours

Every TV has its own name, shown in the menu title and on the boot screen (up to 16 characters):

```
python -m tubeless config set name "Living Room" --restart
python -m tubeless config show
```

The boot-screen greeting, folders and static clips are settings too (`config show` lists them). A web page for this,
for people who'd rather not use SSH, is on the [roadmap](docs/ROADMAP.md).

## Install on a fresh Pi

You need the build from the guide (see Credits): a Pi Zero 2 W with the Waveshare 2.8" DPI LCD, the speaker and the wiring.

1. Flash **Raspberry Pi OS Lite (32-bit)** with the Raspberry Pi Imager. In its settings give it a hostname, a user,
   your Wi-Fi (and Wi-Fi country), and **enable SSH**, ideally with your public key.
2. Put the Tubeless folder on the Pi (`git clone`, or copy it) and run:

   ```
   sudo sh install.sh --name "Living Room"
   ```

   (`--dry-run` first if you want to see what it will do.) It checks the SD card by writing and reading back data, installs
   the packages, fetches Waveshare's display overlays and **verifies each file against a known checksum**, edits
   `config.txt` and `cmdline.txt` (showing a diff and keeping backups), installs the services, and adds a narrow `sudo` rule
   that lets the TV power itself off and restart itself and nothing else. It is safe to run again.
3. Put videos in `~/videos`, one folder per channel (`1.cartoons`, `2.movies`, ...), then `sudo reboot`. The first start
   walks you through calibrating the touch screen.

To update a TV from your computer later: `tools/deploy.sh <ssh-host>`. More in [docs/](docs/).

## Credits

Tubeless exists because of the original **Simpsons TV** project by **buba447**:

* the build guide, by the author at **<https://withrow.io/simpsons-tv-build-guide>** (and its Waveshare-screen
  variant, <https://withrow.io/simpsons-tv-build-guide-waveshare>), which is where the shell, the wiring and the whole idea
  come from, and
* the original player scripts, at **<https://github.com/buba447/simpsonstv>** (GitHub user *buba447*).

Tubeless is a separate, from-scratch implementation for a different stack (Pi Zero 2 W, mpv, a touchscreen). It contains
no code from that repository, which carries no licence of its own. If you build the TV, please go and read the guide.

Also: **Waveshare**, whose wiki supplies the display and touch overlays this build needs (the installer fetches them
from Waveshare; they are not redistributed here), and the **DejaVu** and **Droid** font authors
([`tubeless/assets/fonts/LICENSES.txt`](tubeless/assets/fonts/LICENSES.txt)).

**No videos are included, and none ever will be.** Bring your own.

## Licence

[MIT](LICENSE). The shell design, the original guide and the original scripts belong to their own authors.
