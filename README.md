# Tubeless

A pocket TV with no tube. A Raspberry Pi Zero 2 W, a 2.8" touchscreen in a 3D-printed retro shell, a small speaker and a
folder of videos. Swipe to change channel. A burst of static plays between episodes.

<p align="center">
  <img src="docs/images/tv-menu.png" alt="The menu over a playing video" width="400">
  <img src="docs/images/tv-boot.png" alt="The boot screen" width="400">
</p>

> **Status: early.** It runs on the author's TV every day, and this is the first public cut. It supports one build only (see
> [Hardware](#hardware)). Settings and file layouts may still change.

## What it does

* **Channels are folders.** Put videos in `~/videos/1.cartoons`, `~/videos/2.movies` and so on. Swipe up or down to change channel.
* **Touch controls.** Swipe left or right for the next or previous episode, tap to pause, hold for the menu.
* **Static between episodes.** A short burst before every video and a longer one when you change channel.
* **Shuffle that doesn't repeat.** Each channel remembers its shuffled order and works through it. New videos are slotted in
  among the ones you haven't seen.
* **Resume.** It returns to the right episode and the right place in it, even after you pull the plug.
* **No keyboard on the TV.** Everything you would type (Wi-Fi password, TV name, YouTube links) goes through a web page on your phone.
* **YouTube channels** (optional): playlists and single videos, downloaded to the TV or streamed.
* **Menu:** pause, next and previous, brightness, channels, and under Settings: shuffle, sleep timer, auto-dim,
  touch calibration, upload, Wi-Fi setup, shut down.
* **Boot screen** with a live checklist of what is starting up, and a name and greeting you choose.

## Get videos onto the TV

With no videos the TV says so and offers an upload page. The same page is under Settings > Upload videos.

<p align="center">
  <img src="docs/images/tv-no-videos.png" alt="The no videos screen" width="320">
  <img src="docs/images/tv-upload.png" alt="The upload page screen: address, QR code and PIN" width="320">
</p>

The TV shows an address, a QR code and a 4-digit PIN. Open the address on a phone or laptop on the same Wi-Fi and enter the PIN.
Only someone who can see the TV can get in. From there you can:

* create channels and send video files (large files upload in pieces, and a dropped connection resumes);
* get advice on files that may not play well (resolution, codec, bitrate). Nothing is converted for you;
* rename the TV;
* build YouTube channels (below).

Hold the TV's screen to leave the upload page. Copying files over SFTP or `scp` into `~/videos/<channel>/` works too.

## YouTube channels

A channel can be made of YouTube playlists and single videos. Paste a playlist link and every video in it is ticked. Untick any you
don't want. Later you can paste another playlist, or one video, and it merges into the same channel without touching your earlier choices.

<p align="center">
  <img src="docs/images/phone-channels.png" alt="The channel list on a phone" width="230">
  <img src="docs/images/phone-youtube-add.png" alt="Looking up a playlist and choosing videos" width="230">
  <img src="docs/images/phone-youtube-channel.png" alt="A YouTube channel with downloaded videos and one problem" width="230">
</p>

* **Download to the TV** (the default). The TV fetches each video at 480p or lower, then plays it like any other file. It works
  with no Wi-Fi. Downloads run one at a time while the phone page is open, and the TV shows the progress.
* **Stream** (untick the box). Nothing is stored. Each video loads from YouTube when its turn comes, which takes about 10 seconds on a
  Zero 2 W, and the TV says "Loading from YouTube". If a video can't be played the TV says why and moves on. Fix or delete the channel on
  the phone page.
* Public and unlisted playlists only. There is no YouTube sign-in.

This needs two extras, which the installer adds when you ask (`--with-youtube`, see [Install](#install-on-a-fresh-pi)):
[yt-dlp](https://github.com/yt-dlp/yt-dlp) and ffmpeg. Tubeless does not include either. YouTube changes often, so keep yt-dlp current:

```
python3 -m tubeless youtube status
python3 -m tubeless youtube update
```

Downloading from YouTube is against YouTube's terms of service, and this route doesn't show YouTube's ads. It is an opt-in extra for
your own use, and how you use it is up to you.

## Wi-Fi setup

A new TV, or one moved to a new router, has no network. Tubeless handles that without a keyboard.

<p align="center">
  <img src="docs/images/tv-wifi-setup.png" alt="The Wi-Fi setup screen" width="320">
  <img src="docs/images/phone-wifi-setup.png" alt="Choosing a network on a phone" width="200">
</p>

1. The TV makes its own Wi-Fi network and shows its name and password, with a QR code that joins it.
2. Your phone joins it and the setup page opens by itself (or go to `10.42.0.1`).
3. Pick your network, type its password, and the TV joins it and goes back to playing.

If the password is wrong the TV says so and brings its network back so you can try again. Your old Wi-Fi settings are kept until the
new ones work. A TV that has never had Wi-Fi starts setup by itself. A TV that has Wi-Fi saved but can't reach it keeps playing, since
it works fine offline. Start setup any time from Settings > Wi-Fi setup. Hold the screen to cancel.

This uses NetworkManager, so it needs Raspberry Pi OS Bookworm or Trixie. It isn't available on Bullseye.

## Install on a fresh Pi

You need the build from the original guide (see [Credits](#credits)): a Pi Zero 2 W, the Waveshare 2.8" DPI LCD, a speaker and the wiring.

1. Flash **Raspberry Pi OS Lite (32-bit, Trixie)** with the Raspberry Pi Imager. In its settings set a hostname and user, your Wi-Fi
   and Wi-Fi country, and turn on SSH, ideally with your public key.
2. Boot the Pi and SSH in. The screen may stay dark until the installer has set up the display. Get the code and run the installer:

   ```
   sudo apt install -y git
   git clone https://github.com/magicows/tubeless.git
   cd tubeless
   sudo sh install.sh --name "Living Room"
   ```

   Add `--with-youtube` for YouTube channels. Add `--dry-run` first to see what it would do without changing anything.

   The installer:
   * tests the SD card by writing data and reading it back;
   * installs the packages it needs, without the recommended extras (a 512 MB Pi runs out of memory otherwise);
   * downloads Waveshare's display overlays and checks each file against a known checksum;
   * edits `config.txt` and `cmdline.txt`, showing the changes and keeping backups;
   * installs the services and a narrow `sudo` rule: power off, reboot, restart Tubeless, and one root-owned Wi-Fi helper.

   It is safe to run again.
3. Reboot. The first start walks you through calibrating the touch screen. With no videos yet the TV offers the upload page.

To update a TV from your computer later: `tools/deploy.sh <ssh-host>`.

## Make it yours

Every TV has its own name, shown in the menu title and on the boot screen (up to 16 characters). Change it on the upload page or:

```
python3 -m tubeless config set name "Living Room" --restart
python3 -m tubeless config show
```

The boot greeting, folders and static clips are settings too. Themes (colours, boot style, your own static) are on the
[roadmap](docs/ROADMAP.md).

## Hardware

| | |
|---|---|
| Board | Raspberry Pi Zero 2 W |
| Screen | Waveshare 2.8" DPI LCD (480x640 portrait, mounted sideways), Goodix touch controller |
| Backlight | GPIO 18, software PWM through pigpio |
| Audio | mono PWM speaker and amp on GPIO 19 (`dtoverlay=audremap,pins_18_19`) |
| OS | Raspberry Pi OS Lite 32-bit. Trixie is tested end to end. Bullseye (the original build's OS) plays videos, but uploads, Wi-Fi setup and YouTube are only tested on Trixie. |

Everything specific to this build lives in [`tubeless/hardware.py`](tubeless/hardware.py). Support for another screen means adding a
profile there. Pull requests are welcome, but the project only promises the build above.

## Try it without a Pi

The simulator runs the real app against a pretend Pi and a pretend mpv, in your browser. The mouse is a finger.

```
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m tubeless sim
```

Open <http://127.0.0.1:8765/>. Useful flags: `--fresh` (first-run calibration), `--screen boot`, `--no-network` (Wi-Fi setup),
`--fake-youtube` (a pretend yt-dlp).

## Development

```
.venv/bin/python -m pytest        # about 280 tests, including whole-app tests driven by synthetic touches
python3 tools/screenshots.py      # regenerates the pictures in docs/images (needs playwright and Chrome)
```

```
tubeless/        the app: app.py, library.py, web.py, youtube.py, wifi.py, hal/ (real and pretend hardware)
tests/           unit tests and whole-app tests
tools/           deploy.sh, an on-TV end-to-end test, screenshots
packaging/       systemd unit templates and the Wi-Fi helper
docs/            architecture, roadmap, manual test plan, the Trixie trial
```

Continuous integration runs the tests on Python 3.9 and 3.13. A daily job checks the newest yt-dlp against what Tubeless relies on
and opens an issue if something breaks. Where this is going: [docs/ROADMAP.md](docs/ROADMAP.md).

## Credits

Tubeless exists because of the original **Simpsons TV** project by **buba447**:

* the build guide, at **<https://withrow.io/simpsons-tv-build-guide>** (and the Waveshare-screen version,
  <https://withrow.io/simpsons-tv-build-guide-waveshare>). The shell, the wiring and the idea come from there;
* the original player scripts, at **<https://github.com/buba447/simpsonstv>**.

Tubeless is a separate, from-scratch implementation for a different stack (Pi Zero 2 W, mpv, a touchscreen). It contains no code from
that repository, which has no licence of its own. If you build the TV, read the guide.

Also: **Waveshare**, whose wiki supplies the display and touch overlays this build needs (the installer fetches them from Waveshare;
they are not redistributed here), and the **DejaVu** and **Droid** font authors
([`tubeless/assets/fonts/LICENSES.txt`](tubeless/assets/fonts/LICENSES.txt)).

**No videos are included, and none ever will be.** Bring your own.

## Licence

[MIT](LICENSE). The shell design, the original guide and the original scripts belong to their own authors.
