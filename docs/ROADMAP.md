# Roadmap

What Tubeless is (see the [README](../README.md)) is already built: playback, the web companion, YouTube and Jellyfin channels, themes, Live TV
mode, dimming, the installer and updater. This file is only what is **still to do**, and the decisions that shape it.

## Decisions

* **Scope:** the supported build is exactly one TV (Pi Zero 2 W + Waveshare 2.8" DPI LCD, mono PWM speaker). Hardware lives in one profile
  (`hardware.py`); other hardware is welcome as pull requests, not a promise. No profile system until someone needs it.
* **Headless, Raspberry Pi OS *Lite*, Debian 13 "Trixie".** No desktop, no X. Trixie is the tested OS; Bullseye plays videos but uploads, Wi-Fi
  setup and YouTube are only tested on Trixie.
* **Typing happens on a phone or laptop, not on the TV.** Anything needing a keyboard (Wi-Fi password, names, YouTube and Jellyfin links and
  sign-ins) goes through a web page; the touchscreen never shows a keyboard. Choices that suit a bigger screen with previews (themes) are made on
  the page, not the TV.
* **Opinionated modes** instead of doing everything at once: *playback* (the player runs), *upload* (Wi-Fi on, playback stopped, the screen shows an
  address and PIN; no background uploads) and *setup* (no known Wi-Fi, so the TV makes a hotspot with a QR code and a captive portal).
* **Upload advice, not conversion.** The page checks resolution, codec and bitrate and recommends; the user can override.
* **Updates** come from GitHub releases, installed from the web page (checksum, import test, roll back). No touchscreen item: the TV only has
  Wi-Fi in upload mode. Releases are cut for meaningful batches of work, not for each feature.
* **Distribution: a one-line installer, no image.** Flash Raspberry Pi OS Lite, SSH in, run `get.sh` (downloads a checksummed release and runs the
  installer). An image built by CI is not planned.
* **Optional extras stay optional and unbundled:** yt-dlp and ffmpeg (YouTube), a Jellyfin server of your own. No media is ever shipped; the
  Simpsons theming stays the author's own, user-supplied.
* **Name:** *Tubeless* (a TV with no tube). Each TV has its own display name in `config.json`.

## Planned

### Ads between videos

Opt-in stingers: between videos on any channel, play a random clip from an ads folder, every 1, 2, 3 ... videos (the user chooses how many).

* A switch and a frequency in Settings on the web page, off by default. The folder is `~/ads`.
* Switched on with an empty folder, the page says plainly that it does nothing until the folder is filled.
* Filling it: upload clips to it from the upload page (an "Ads" target), or download them from YouTube. A YouTube clip can't be streamed as an
  ad, so the stream option isn't offered there.
* Not played when you change channel (that is the static's job), nor on a swipe to skip.
* How: put the ad in the *static slot* of the playlist, as one `edl://` entry made of the static clip then the ad, so the static/video alternation
  that skipping and episode detection rely on doesn't change. To try on the TV: mpv must join clips of different size and codec cleanly. The
  alternative is a third entry kind in the playlist with the layout logic taught about it (a no-static layout was tried for streams and reverted).
* With Live TV mode on, ads are not part of the schedule (they fill the gap between videos), so the clock stays true.

### TV guide view

A third icon in the menu's header, next to upload and power, toggles the menu between the settings view and a TV guide, and remembers the choice.
The guide is a mini listing of every channel: number, name, and what is on it now (title and time left); tapping a row tunes to it. With Live TV
mode on it is what is on air now (the schedule gives it by arithmetic); otherwise what each channel would play next (where it left off). Needs a
per-channel "now on" lookup (the live slot, or saved progress), a list view in `render.py` like the menu's with a number column, and a stored setting.

### Jellyfin follow-ups

* Start offset: ask the server for `startTimeTicks`, so a Jellyfin video can resume mid-video and Live TV mode can join at the right minute
  (today a Jellyfin video starts from the beginning, because a transcoded stream can't be sought into).
* A "refresh" for a Jellyfin channel built from a series, so new episodes appear. Plex, later.
* Remote servers behind a proxy: the TV names itself so Cloudflare lets it through, but a long stream through a free proxy may still be limited;
  the fix is to use the server's home-network address. Worth a note in the README once it has been lived with.

### Live TV mode follow-ups

* A per-channel switch (it is one global switch today), and the guide above as its "now on" listing.

### Themes follow-ups

* A theme's own boot greeting, menu title and static clips; the boot screen's Matrix rain following the theme (on request); sharing a theme as a file.

### Dim at night follow-ups

* A "preview the night level for ten seconds" button, so it can be tried without waiting for dusk.

### Installer

* `get.sh` asking for the name and YouTube before it starts (the page does this while it runs).
* Try the install page on a real blank card (it is tested with a fake installer, not a real run).
* Decide whether `dtparam=sdio_overclock=25` becomes an installer default (see the README's Wi-Fi chip section; today it is manual).

## Ideas

* **Bluetooth speaker** (experimental): BlueALSA, pair from the touchscreen, software volume, audio-delay setting, fall back to the wired speaker.
  Wi-Fi and Bluetooth share a radio, hence the modes above.
* **Battery/UPS awareness:** a single low-battery signal wire to a spare GPIO would buy a graceful shutdown; a fuel gauge would need software I2C
  (the standard I2C pins are used by the display; pins 10, 11, 26, 27 look free).
* Hardware decoding (`v4l2m2m`) to allow higher-resolution video; a read-only root with a data partition to survive unplugging; resume across channel flips.

## Open questions

* Does the wired-power and battery comparison (the same card in a mains-only TV) change the Wi-Fi chip story? (Results pending.)
