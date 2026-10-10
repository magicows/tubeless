# Bookworm trial (spare TV)

Goal: find out, on a spare TV, whether a current Raspberry Pi OS Lite is a clean home for Tubeless, *without* risking
the working TV. Nothing here changes the TV that runs Tubeless today.

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

1. Install dependencies: `mpv python3-evdev python3-psutil python3-pil python3-numpy pigpio python3-pigpio`.
2. Waveshare overlays: download `28DPI-DTBO.zip` from Waveshare's wiki and copy the `.dtbo` files to
   `/boot/firmware/overlays/`. (On a Zero 2 W Waveshare also lists `vc4-kms-DPI-28inch.dtbo` for "grey shows as green".)
3. `config.txt` (in `/boot/firmware/`): the lines from Waveshare's page, plus `dtoverlay=audremap,pins_18_19`.
   `cmdline.txt`: `video=DPI-1:480x640M@60,rotate=90`, `consoleblank=0 logo.nologo quiet vt.global_cursor_default=0`.
4. Mask `getty@tty1`, enable `pigpiod`, deploy Tubeless with `tools/deploy.sh`, copy a few test videos.
5. Run the on-TV end-to-end test (`tools/e2e_pi.py`).

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
