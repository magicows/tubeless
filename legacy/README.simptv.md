# SimpTV

A tiny touchscreen TV: a Pi Zero 2 W + Waveshare 2.8" DPI LCD (480x640, mounted sideways)
and a single speaker, playing folders of videos with a static burst between them.

## Files

| File | What it is |
|---|---|
| `touchscreen.py` | The whole app: starts mpv, reads touch, runs the menu, saves settings |
| `simptv_ui.py` | Draws the menu with Pillow and hit-tests taps on it |
| `simptv_boot.py` | The Matrix-style boot screen (see "Boot screen" below) |
| `restart-tv.sh` | Restarts the TV (`sudo systemctl restart simptv`) |
| `smallStatic.mp4`, `longerStatic.mp4` | Static between videos / on a channel change |
| `videos/` | One sub-folder per channel (`1.simpsons`, `2.nights`, ...; a leading number sets the order) |
| `.simptv_state.json` | Saved brightness, shuffle and channel |
| `playlist.m3u` | Generated each time; don't edit |
| `touchscreen.log` | App log (rotates at 3 MB) |
| `mpv.log` | mpv's own output (restarts itself at 512 KB) |
| `historic/` | Zipped old versions, old scripts, unit files and copies of the system files we changed. Nothing here is used |

## Controls

| | Menu closed | Menu open |
|---|---|---|
| Tap | play / pause | tap a row to use it; tap outside to close |
| Swipe left / right | next / previous video | |
| Swipe up / down | next / previous channel (empty channels are skipped) | scroll the list |
| Hold 1s | open menu | close menu |

The menu hides itself after 5 s. Rows: Pause/Play, Next, Previous, Shuffle, Brightness, then channels.

## How it fits together

* **Boot:** `simptv.service` (systemd) starts the app as soon as `pigpiod` and the display driver are up; it does not wait for the network.
  `Restart=always`: if the app dies, systemd restarts it and takes mpv down with it, so there is never a stray mpv.
* **Console:** `getty@tty1` is masked (no login prompt) and `vt.global_cursor_default=0` is on the kernel command line (no blinking cursor).
  Nothing from the text console is ever shown.
* **Playlist:** `static, video, static, video, ...` — every video has a static burst in front of it (even positions are statics, odd are videos).
  Skipping always lands on the static before the target video, so the channel-flick effect plays in every direction, even at the start of the playlist.
* **Video:** mpv runs `--fs` and `~/.config/mpv/mpv.conf` has `video-rotate=270`. The panel is natively portrait, so the picture is rotated in software.
* **Menu:** mpv doesn't rotate its own OSD, so the menu is drawn landscape with Pillow, turned 90 degrees and pushed to mpv with `overlay-add`.
  It is drawn at 85% size and centred for the 3D-printed bezel (`MENU_SCALE`, `MENU_CENTER` in `simptv_ui.py`).
* **Touch:** the controller's axes are swapped relative to how the TV is viewed (raw X = top to bottom, raw Y = right to left); `to_screen()` converts.
  Gestures are recognised in `GestureRecognizer`; one worker thread performs the actions.
* **Brightness:** the backlight is on GPIO 18 and is dimmed with pigpio software PWM, because both hardware PWM channels are taken by the audio overlay.
  Audio is mono from GPIO 19 (`dtoverlay=audremap,pins_18_19` in `/boot/config.txt`). There is no stereo.
* **Self-healing:** mpv is restarted (fresh playlist) if it exits or stops answering. If the saved channel has no videos, the app starts on the first channel that does.

## Boot screen

`simptv-boot.service` runs `simptv_boot.py`: green digital rain with a live status panel (display, touch, audio, backlight, video library,
Wi-Fi/IP, player). Each line is a real check. It draws straight to `/dev/fb0`, turns the backlight on at the saved brightness once its
first frame is drawn, and exits by itself when mpv has put a frame on screen (or after 2 minutes). It runs at low priority so it never slows mpv down.
The panel can't show anything until the display driver loads (about 9 s after power-on), so the first moments stay black.

```
python3 ~/simptv_boot.py --demo 30     # watch it on the panel for 30 s (run `sudo systemctl stop simptv` first, start it after)
python3 ~/simptv_boot.py --png out.png --at 9   # render a preview frame to a file, no screen needed
sudo systemctl disable simptv-boot     # turn it off (the screen is just black until the video starts)
```

Orientation: `ROT_K` at the top of `simptv_boot.py` (the panel is natively portrait; same turn as the menu). Layout: `PANEL`. Speed: `FPS`.

## Tuning

Constants near the top of `touchscreen.py`: `MENU_TIMEOUT`, `LONG_PRESS_THRESHOLD`, `TOUCH_MOVE_PX`, `SWIPE_MIN_PX`,
`MENU_SCROLL_PX_PER_ROW`, `BRIGHTNESS_LEVELS`.

## Day to day

```
sh ~/restart-tv.sh                 # restart
sudo systemctl stop simptv         # stop (the screen keeps its last brightness)
sudo systemctl start simptv
journalctl -u simptv -e            # crashes / startup output
tail -f ~/touchscreen.log          # what the app is doing
tail ~/mpv.log                     # what mpv is saying
```

## Rebuilding on a fresh Pi OS

```
sudo apt install mpv python3-evdev python3-psutil python3-pil python3-numpy pigpio python3-pigpio
sudo systemctl enable --now pigpiod
```

1. Add the Waveshare lines from the build guide to `/boot/config.txt`; put `video-rotate=270` in `~/.config/mpv/mpv.conf`; copy the files above.
2. Install the service (`/etc/systemd/system/simptv.service`), then `sudo systemctl daemon-reload && sudo systemctl enable --now simptv`:

   ```
   [Unit]
   Description=SimpTV touchscreen video player
   After=pigpiod.service local-fs.target
   Wants=pigpiod.service

   [Service]
   User=simpsons
   WorkingDirectory=/home/simpsons
   Environment=PYTHONUNBUFFERED=1
   ExecStartPre=/bin/sh -c 'for n in $$(seq 1 60); do [ -e /sys/class/drm/card0-DPI-1 ] && exit 0; sleep 0.5; done; exit 0'
   ExecStart=/usr/bin/python3 /home/simpsons/touchscreen.py
   Restart=always
   RestartSec=3
   KillMode=control-group
   TimeoutStopSec=10

   [Install]
   WantedBy=multi-user.target
   ```
   Also install `/etc/systemd/system/simptv-boot.service` (after `pigpiod.service`; `User=simpsons`; `ExecStart=/usr/bin/python3 /home/simpsons/simptv_boot.py`;
   `Nice=10`; `Restart=no`; `WantedBy=multi-user.target`) and `sudo systemctl enable simptv-boot`. It needs `python3-numpy` as well.
3. Console: `sudo systemctl mask getty@tty1.service`, add `vt.global_cursor_default=0` to the end of the single line in `/boot/cmdline.txt`.
4. In `/etc/rc.local`, leave out `raspi-gpio set 18 op dl` (the build guide adds it; the app owns that pin now and it would switch the backlight off again).

## Undo / old versions

`historic/` holds the originals of everything that was changed: `cmdline.txt-before-...`, `rc.local-before-...`,
`crontab-before-...` (the old `@reboot` entry), and zips of the old code. To go back to cron instead of the service:
`sudo systemctl disable --now simptv`, `sudo systemctl unmask getty@tty1`, and restore the crontab line.
