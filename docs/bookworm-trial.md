# OS trial on a spare TV (Bookworm / Trixie)

Goal: find out, on a spare TV, whether a current Raspberry Pi OS Lite is a clean home for Tubeless, *without* risking
the working TV. Nothing here changes the TV that runs Tubeless today.

> **Note (2026-10):** the Imager's current default is **Debian 13 "Trixie"** (kernel 6.18, Python 3.13, mpv 0.40,
> `pinctrl` instead of `raspi-gpio`, NetworkManager), not Bookworm. That is a bigger jump from the working TV
> (Bullseye, kernel 6.1) than planned, and also the OS a new builder would get today, so it is what we test.

> **sudo:** images made by recent Imager versions ask for a password on `sudo`. The trial needs passwordless sudo
> (`/etc/sudoers.d/010_<user>-nopasswd`, the long-time Raspberry Pi OS default). Claude cannot type passwords or change
> security settings, so you add that line yourself, once. The real installer will add a much narrower rule
> (just `systemctl poweroff`/`reboot`/`restart tubeless`).

## 1. Flash the SD card

Use **Raspberry Pi Imager**.

* **Device:** Raspberry Pi Zero 2 W
* **OS:** *Raspberry Pi OS (other)* -> **Raspberry Pi OS Lite (32-bit)**. (32-bit: the Zero 2 W has 512 MB of RAM.)
* **Edit settings** (the cog / "Next -> Edit settings") before writing:
  * Hostname: `tubeless-trial`
  * Username / password: whatever you like; remember the username (the examples below use `sam`)
  * Wi-Fi: your network, plus the **wireless LAN country** (GB)
  * Locale / time zone: yours
  * Services -> **Enable SSH** -> *Allow public-key authentication only*, and paste your public key
    (`cat ~/.ssh/id_ed25519.pub`)

## 2. First boot

Put the card in the spare TV and power it up. The screen will stay dark (the display driver isn't set up yet); that is
expected. Give it a couple of minutes, then from your Mac:

```
ssh <user>@tubeless-trial.local        # or its IP address from your router
```

Tell Claude the user name and address and it will take it from there. The first thing it will run is the read-only
audit below.

## 3. The audit (read-only)

```
scp tools/trial/audit.sh <user>@tubeless-trial.local:/tmp/ && ssh <user>@tubeless-trial.local sh /tmp/audit.sh
```

It reports the OS release, kernel, Python/Pillow/numpy/mpv versions available from apt, whether `pigpio` and
`raspi-gpio`/`pinctrl` exist, where `config.txt` lives (`/boot/firmware` on Bookworm), the network stack
(NetworkManager vs dhcpcd) and the Wi-Fi/Bluetooth state. It changes nothing.

## 4. What the trial then tries (Claude does this over SSH)

All of steps 1-4 are `sudo sh tools/trial/setup.sh <dir-with-overlays>`, which is built from tested pieces
(`tubeless.installer` shows a diff of the boot-file changes, keeps backups and is safe to repeat):

1. Install dependencies: `mpv python3-evdev python3-psutil python3-pil python3-numpy pigpio python3-pigpio`.
2. Waveshare overlays: for the trial we reuse the exact `.dtbo` files from the working TV (so only the OS differs).
   (On a Zero 2 W Waveshare also lists `vc4-kms-DPI-28inch.dtbo` for "grey shows as green"; try it if colours look wrong.)
3. `config.txt` (in `/boot/firmware/`): Waveshare's overlay lines plus `dtoverlay=audremap,pins_18_19`.
   `cmdline.txt`: `video=DPI-1:480x640@60,rotate=90`, `consoleblank=0 logo.nologo vt.global_cursor_default=0`, console to tty3.
4. Mask `getty@tty1`, enable `pigpiod`. Reboot.
5. Deploy Tubeless with `tools/deploy.sh <host>`, copy a few test videos, run the unit tests on the TV and the
   on-TV end-to-end test (`tools/e2e_pi.py`).

## 5. What we are looking for

| Question | Why it matters |
|---|---|
| Do the Waveshare overlays bring up the panel and touch on the newer kernel? | the biggest unknown |
| Does mpv (newer) give the same picture, rotation and `overlay-add` menus? | the player is the other big dependency |
| Do `pigpio` and a PWM backlight still work? | dimming |
| Are Pillow/numpy versions fine for the code? | it targets 3.9 / numpy 1.19 / Pillow 8.1 today |
| Is `raspi-gpio` still there, or only `pinctrl`? | the old guide's `rc.local` lines use it |
| Does NetworkManager (default on Bookworm) make the Wi-Fi hotspot setup easy? | the first-boot flow |

The result decides whether the installer targets Bullseye only, or both.

## Things that went wrong on the first attempt (and what they taught us)

* **A failing SD card.** `Unable to fetch` / "compressed data is corrupt" / "unexpected end of file" during `apt install`,
  plus `sdhost ... timeout waiting for hardware interrupt` in `dmesg`. A write-then-read-back test (120 MB, three times)
  returned different data every time, while the RAM and the power (`vcgencmd get_throttled` = `0x0`) were fine. The Imager's
  own verify step does not catch this. `setup.sh` now runs that check first and stops. Fix: re-seat the card, and if it
  persists use a genuine card from a reputable seller (`f3` can test a card for fake capacity).
* **An unreachable package mirror.** `raspbian.raspberrypi.com` redirects downloads to a university mirror; if that one is
  unreachable from your network you get `Unable to connect to mirror0.it.ox.ac.uk`. Pointing `URIs:` in
  `/etc/apt/sources.list.d/raspbian.sources` at another mirror (e.g. `http://archive.raspbian.org/raspbian/`) fixes it;
  signatures are still verified.
* **Installing `mpv` pulls in ~280 packages by default** (X11 utilities, speech models ...) and ran a 512 MB Pi out of
  memory. Use `--no-install-recommends` (about 183 packages, still large: mpv's video libraries).
* **`sudo` asks for a password** on images made by recent Imager versions (see the note at the top).

### Recovering after a flaky card (what worked)

Once the card reads back correctly (re-seating the card fixed ours), the damage already done still has to be undone:

1. Make the next boot run a full filesystem check (`fsck.mode=force` on the end of `/boot/firmware/cmdline.txt`; on this
   OS the check happens in the early-boot stage, which is the flickering activity light). Then **remove the flag**, or every
   boot repeats it. Our check removed one damaged entry ("Structure needs cleaning").
2. `sudo apt-get install debsums && sudo debsums -s`: lists every installed file whose checksum does not match its
   package, and packages whose checksum record was lost. In our case all of it was confined to the packages installed
   while the card was misbehaving; the original system was fine (only a config file the OS edits itself differed).
3. Reinstall exactly those packages: `sudo apt-get install --reinstall --no-install-recommends <packages>`. If apt refuses
   one that is half-configured (`Internal Error, No file name for ...`, which aborts the whole run), fetch it with
   `apt-get download <pkg>` and install it with `sudo dpkg -i`.
4. Re-run `sudo debsums -s` and `sudo dpkg --audit` until both are quiet.

If the original system is damaged too, re-flash instead. It is quicker than chasing it.

## Result (2026-10-11): Debian 13 "Trixie" works

Raspberry Pi OS Lite 32-bit, kernel 6.18, Python 3.13, mpv 0.40, Pillow 11, numpy 2.2, pigpio 1.78, NetworkManager.

| Question | Answer |
|---|---|
| Do the Waveshare overlays bring up the panel and touch on the newer kernel? | **Yes**, with the same `.dtbo` files as the Bullseye TV. `DPI-1` connected at 480x640, `fb0` 480x640 16 bpp, Goodix found (its name is now `11-005d Goodix Capacitive TouchScreen`; matching on "Goodix" still works). |
| Does mpv (newer) give the same picture, rotation and `overlay-add` menus? | **Yes.** mpv 0.40 picks the GPU renderer on the DRM display by itself (Bullseye's 0.32 fell back to SDL). `--video-rotate=270` and menu overlays work. |
| Do `pigpio` and a PWM backlight still work? | **Yes** (`pigpiod.service` ships with the package; duty 255 / 64 / 255 measured). |
| Are Pillow/numpy versions fine? | **Yes.** All tests pass on Python 3.9 / numpy 1.19 / Pillow 8.1 and on 3.13 / numpy 2 / Pillow 11-12. |
| Is `raspi-gpio` still there? | **No**, only `pinctrl`. The app now uses whichever exists. |
| Hardware test | `tools/e2e_pi.py`: 24/24; unit tests 97/97 on the TV. First-run calibration done by hand on the real touchscreen (error 10 px). |
| Boot | `tubeless-boot` ran (about 23 s on screen), `tubeless` autostarted. 47 s overall, but ~25 s of that is NetworkManager, which we don't wait for. |

Differences to remember for the installer: `/boot/firmware/`, `pinctrl`, NetworkManager (good for the Wi-Fi hotspot), cloud-init user-data from
the Imager, passwordless `sudo` is not the default, and `mpv` is ~180 packages even without recommends.

Found along the way: the first-run wizard overlay was lost if mpv's socket was not ready yet (fixed: it is retried).
