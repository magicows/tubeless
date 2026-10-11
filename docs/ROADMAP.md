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
* **Updates** come from GitHub releases (a menu item, later).
* **Distribution: both.** An idempotent installer script is the source of truth; a flashable image is built from it by CI.
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

## Next

1. ~~**Friendly empty states**~~ done: with no videos the TV shows "No videos yet" with *Upload page* and *Check again* buttons.
2. **Library index**: scan new files, record durations (mediainfo), keep it fresh when files appear.
3. **Web companion** (small, streaming, low-memory): TV name (today: `python -m tubeless config set name ...`), channel management, settings, status/logs.
4. ~~**Transfer mode**~~ done (Settings > Upload videos, or the empty screen): menu toggle, fixed info screen with URL and QR code, resumable chunked uploads written to a `.part`
   file and renamed when done, disk-space check, file advice.
5. **Setup mode**: first-boot hotspot with a QR code, captive portal, Wi-Fi country and the phone's clock/timezone.
6. **Updater** from GitHub releases, with rollback.
7. **Image build** in CI from the installer.

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
