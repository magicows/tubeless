# Roadmap

## Decisions so far

* **Scope:** the supported build is exactly one TV (Pi Zero 2 W + Waveshare 2.8" DPI LCD, mono PWM speaker). Hardware
  lives in one profile; other hardware is welcome as pull requests, not a promise. No profile system until someone needs it.
* **Headless, Raspberry Pi OS *Lite*.** No desktop, no X.
* **Typing happens on a phone or laptop, not on the TV.** Anything needing a keyboard (Wi-Fi password, TV name,
  file names) goes through a small web page; the touchscreen never shows a keyboard.
* **Opinionated modes** instead of doing everything at once:
  * *Playback*: Wi-Fi off (saves battery), Bluetooth speaker optional.
  * *Transfer*: Wi-Fi on, playback stopped, the screen becomes an upload progress display. No background uploads.
  * *Setup*: no known Wi-Fi, so the TV makes a hotspot with a QR code on screen and a captive portal.
* **Upload advice, not conversion.** The upload page checks resolution/codec/bitrate and recommends; the user can override.
* **Updates** come from GitHub releases, from the web page (built: check, install with a checksum and import test, roll back).
  A touchscreen menu item is not planned: the TV only has Wi-Fi in upload mode.
* **Distribution: a one-line installer, no image.** Flash Raspberry Pi OS Lite with Imager, SSH in, run one `curl ... | sudo sh`
  (`get.sh`: downloads a pinned, checksummed release and runs `install.sh`). Running it again updates. An image built by CI is
  optional and not planned. Repo: github.com/magicows/tubeless. First release (v0.1.0) is tagged when the author's own TV is upgraded.
* **OS:** the trial on a spare TV showed that current Raspberry Pi OS Lite (Debian 13 "Trixie", kernel 6.18) works
  ([docs/bookworm-trial.md](bookworm-trial.md)): 24/24 on-TV checks. Bullseye stays supported while the installer is built;
  Trixie becomes the default for new builds and for the image once the installer has been run end to end on it.
* **Name:** *Tubeless* (a TV with no tube). Each TV has its own display name in `config.json` (the author's is "SimpTV").
  The Simpsons theming stays a user-supplied theme: no media is ever shipped.

## Done

- [x] Refactor into a package with a hardware abstraction (`hal/`), config file and per-build hardware profile
- [x] Desktop simulator in the browser; whole-app tests; an on-TV end-to-end test (24 checks on real hardware)
- [x] Touch: gesture recognizer, affine calibration wizard (first run + menu), reads kernel slot state on open
- [x] Menu pages (main + settings), direct touch on rows, swipe to scroll
- [x] Resume (episode + position) and non-repeating shuffle that survives power loss
- [x] Safe shutdown with confirm, sleep timer, auto-dim and standby (a touch wakes without acting)
- [x] Boot screen packaged, with name and greeting from config
- [x] Own static clips, bundled fonts, atomic state files
- [x] Systemd unit templates, `tools/deploy.sh`, rollback script
- [x] **Installer** (`sudo sh install.sh`): SD-card read-back test, lean package install, Waveshare overlays fetched at install time
  and checked against pinned SHA-256s, `config.txt`/`cmdline.txt` edits with diff + backups, console + pigpiod, services, a narrow
  sudoers rule; idempotent, `--dry-run`. Run end to end on the Trixie trial TV (a from-blank-card run is still to do)
- [x] Trial of Debian 13 (Trixie): 24/24 on-TV checks
- [x] Web companion settings, status and log, YouTube quality (360p default), yt-dlp updater, new-video detection, update from a release
- [x] "Technical difficulties" picture, one static clip with two lengths, per-channel play order
- [x] Wi-Fi guard (router ping, chip reload, one self-restart), health notes in the log, stream timeouts
- [x] Faster YouTube changeovers: the next video's address is worked out in the background (on by default, v0.1.6)

## Next

1. ~~**Friendly empty states**~~ done: with no videos the TV shows "No videos yet" with *Upload page* and *Check again* buttons.
2. ~~**Library index**~~ done: durations read with mediainfo into `index.json` (only new or changed files), while in upload mode and after uploads; the page shows each channel's length. Still to do: use the durations for "Live TV mode" below.
3. ~~**Web companion**~~ done: TV name, channels, uploads, YouTube, settings, status and recent log, standby picture, updates.
4. ~~**Transfer mode**~~ done (Settings > Upload videos, or the empty screen): menu toggle, fixed info screen with URL and QR code, resumable chunked uploads written to a `.part`
   file and renamed when done, disk-space check, file advice.
5. ~~**Setup mode**~~ done and verified on the trial TV (Settings > Wi-Fi setup; automatic on first boot; Trixie/Bookworm only): first-boot hotspot with a QR code, captive portal, Wi-Fi country and the phone's clock/timezone.
6. ~~**`get.sh` one-line installer**~~ written (release workflow publishes a tarball + SHA-256 on a `v*` tag; `get.sh` downloads, checks and runs
   `install.sh`). Not yet run on a blank card (item 7). The web page's updater and rollback are built too.
7. ~~**Fresh-card test**~~ done (2026-10): SimpTV rebuilt from a blank Trixie card with `get.sh --with-youtube`, videos and settings copied across.
   It took about 40 minutes, mostly the package step, and found: first-run touch calibration skipped on a card with no videos (fixed),
   Upload offering hotspot setup while a saved Wi-Fi was still connecting after boot (fixed), `dpkg` wedging once on `libllvm19`
   (unexplained; Ctrl-C then `dpkg --configure -a` and run it again cleared it). The migration guide should record these.
8. **Install-time page** (idea, from the SimpTV rebuild): the installer starts a tiny web page, standard library only, before the long package
   download. It shows live progress (the TV screen stays dark until the display is set up, so today nothing says what is happening) and lets
   the builder set the TV name, boot greeting, YouTube yes/no and the standby picture while the install runs. It writes `config.json`, which the
   later steps read. Same PIN idea as the upload page, printed in the terminal. Smaller step first: `get.sh` asking for
   name and YouTube before it starts (the wake-up message is no longer an install option: it has a placeholder and is set on the upload page).
9. **Streaming after a reboot** (known gap): a streaming channel can run through several videos before Wi-Fi is up (three were skipped with
   "No Wi-Fi" in one test). Wait for the network, or retry the same video instead of moving on.
10. **Channel sources** (see below), after the library index.
11. **Themes** (see below). Could also be picked on the install-time page.

## Channel sources (idea; needs the library index and the web companion)

A channel is a folder today. The change is to let a channel be a *source*: a local folder, a YouTube playlist, or a Jellyfin
selection. Everything after the source (static between episodes, shuffle, resume, sleep timer) stays the same. A source gives
the player a list of things to play and their lengths. Channels are created and edited on the web companion, never on the TV.

* **YouTube playlist** (opt in). *Built:* download or stream, per-video ticking, merging playlists and single videos, error messages
  on the TV, `--with-youtube` installer step, daily yt-dlp watch workflow (opens an issue if a new yt-dlp breaks it). The page's TV tab has a yt-dlp card (version, check, update). New videos in a channel's playlists are found each time upload mode starts and marked on the page, unticked
  (downloads only ever happen on the upload page, by your choice). Original notes: (Wi-Fi needed for streaming). Public or unlisted playlists only, no sign-in. The user pastes a playlist URL on the
  companion page, and picks per channel:
  * **Download for offline** (ticked): the TV fetches 480p H.264 files into the channel's folder while on Wi-Fi, then plays them like
    any local file. Most robust: no buffering, works offline.
  * **Stream** (unticked): played straight from YouTube at 480p or lower, H.264 only (the Zero 2 W can't hardware-decode VP9).
  * On a failure (video removed, YouTube refusing, no network) the TV shows a message instead of looping or going blank; the user can
    switch channel, or fix or delete the channel on the companion page. A channel that fails repeatedly is skipped.
  * Uses `yt-dlp`, installed by the user as an extra and never bundled. It needs frequent updates because YouTube changes things.
    It may also need a JavaScript runtime (to be checked on 32-bit Pi OS). Downloading from YouTube is against YouTube's terms, and
    this route does not show YouTube's ads; hence opt-in, personal use, and clear wording.
* **Jellyfin** (Plex later). The companion page connects to the user's Jellyfin server (address plus sign-in once; only the access
  token is kept on the TV; Quick Connect as a no-typing option). A channel is a saved selection, not copied files:
  * a whole TV series (in order or shuffled), several series, a Jellyfin collection or playlist, or hand-picked movies;
  * the TV asks the server for a 480p H.264 stream, so the server does the transcoding and the Pi only plays;
  * the item list is fetched when the channel is tuned, with a clear message if the server can't be reached.
  * Developed against a fake server in the tests; the author's own server is used only for the final check, with credentials typed by
    the author on the companion page.

## Themes

**Built (unreleased):** a theme is a few colours plus shape options (`tubeless/themes.py`), chosen on the upload page. It restyles the menu,
the information screens (upload, Wi-Fi setup, calibration, no videos, shutting down) and the web pages. Built in: Classic (the original
yellow and green), Neobrutalist, Nord, Dracula, Gruvbox, Solarized light. A pasted palette (hex codes or a coolors.co link) becomes a theme:
darkest or lightest colour for the background, the best-contrast colour for the text, the most colourful of the rest for the accent, with
a readability check; "light" and "neobrutalist shapes" are options. The boot screen keeps its Matrix rain.

**Decided:** themes are chosen on the web page only, not from the TV's menu (the page can show better previews).

**Still to do:** better previews of a theme and of a pasted palette on the page; a theme's own boot greeting, menu title and static clips; Matrix rain colours following
the theme (on request); sharing a theme as a file.

## Later / experiments

* **Bluetooth speaker** (experimental): BlueALSA, pair from the touchscreen, software volume, audio-delay setting, fall
  back to the wired speaker. Wi-Fi and Bluetooth share a radio, hence the modes above.
* **Live TV mode** (a per-channel setting): channels behave as if broadcasting. A deterministic schedule per channel
  (seed + epoch + cached durations) gives "what is on now" by arithmetic only, costs no CPU for channels you aren't
  watching, and needs one seek when you tune in. Needs the library index (2).
* **Battery/UPS awareness**: a single low-battery signal wire to a spare GPIO would buy a graceful shutdown; a fuel
  gauge would need software I2C (the standard I2C pins are used by the display; pins 10, 11, 26, 27 look free).
* Hardware decoding (`v4l2m2m`) to allow higher-resolution video; a read-only root with a data partition to survive
  unplugging; resume across channel flips.

## Open questions

* ~~Licence~~ decided: MIT, with the original author credited.
* Which web stack for the companion (must stay small on a 512 MB Pi).
* Whether the Waveshare overlays can be redistributed in an image or must be fetched at install time (assume fetch).
