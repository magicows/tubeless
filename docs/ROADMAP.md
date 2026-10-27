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

### Ads follow-ups

* **Try the ads on the TV.** An ad is joined to the static as one `edl://` entry (`ads.splice`), which mpv on the Pi must play across clips of
  different size and codec without a stall or a glitch. Only the simulator and tests have run it. If it doesn't join cleanly, change `splice`,
  or make the ad a third kind of entry in the playlist (a no-static layout was tried for streams and reverted).
* In Live TV mode an ad pushes the next video back by its length, so the channel runs late until you next tune in (the schedule is worked out
  from the clock without the ads, so tuning in is always true). Cutting the start of the next video by the ad's length would hold the clock.

### TV guide follow-ups

* What is on *next* on each channel in Live TV mode (the schedule gives it by arithmetic), and a clock in the guide's header.
* The guide has no way into Settings (toggle back to the menu first); a small gear row at the end of it would do.

### Jellyfin follow-ups

* Start offset: ask the server for `startTimeTicks`, so a Jellyfin video can resume mid-video and Live TV mode can join at the right minute
  (today a Jellyfin video starts from the beginning, because a transcoded stream can't be sought into).
* A "refresh" for a Jellyfin channel built from a series, so new episodes appear. Plex, later.
* Remote servers behind a proxy: the TV names itself so Cloudflare lets it through, but a long stream through a free proxy may still be limited;
  the fix is to use the server's home-network address. Worth a note in the README once it has been lived with.

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
