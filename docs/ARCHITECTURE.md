# Architecture

## The idea

Everything that touches the real world sits behind a small interface, so the same app code runs on the TV, in the
desktop simulator and in tests.

```
                    +----------------------- App (app.py) -----------------------+
 touch  --samples-->| touch loop -> GestureRecognizer -> events queue             |
                    |                                       |                     |
                    |                         worker thread: menu / playback /    |
                    |                         calibration / standby / shutdown    |
                    |   monitor thread: restart the player   tracker thread:      |
                    |                                        progress + resume    |
                    +-----+-----------------+-------------------+-----------------+
                          |                 |                   |
                      Player            Library/Playlists     Settings/Store
              (mpv  |  FakePlayer)      (channels, order)     (atomic JSON files)
                          |
                  backend.touch / .backlight / .power
                  (hal/pi.py  |  hal/sim.py)
```

## Modules

| Module | Job | Pure? |
|---|---|---|
| `hardware.py` | the one place that knows the supported build; touch-calibration maths | yes |
| `gestures.py` | taps, swipes, long press from touch samples | yes |
| `skip.py` | which playlist entry a skip lands on | yes |
| `menu.py` | the menu as data (pages, rows, actions) | yes |
| `library.py` | channels, the saved shuffle order, resume, m3u | disk only |
| `state.py` | settings / progress as atomic JSON | disk only |
| `render.py` | draws the menu, calibration and message screens; hit-testing | yes |
| `app.py` | ties it together (threads, gesture -> action) | |
| `player.py` | `MpvPlayer` (real) and `FakePlayer` (mpv-alike for sim/tests) | |
| `hal/pi.py`, `hal/sim.py` | touch, backlight, framebuffer, power: real and pretend | |
| `bootscreen.py` | the boot screen (rain, status checks, compositor) | mostly |
| `simserver.py` | the desktop simulator (browser) | |

## Playback model

* A channel's playlist is `static, video, static, video, ...`: every video has a static in front of it, so
  skipping always lands on a static and the channel-flick effect plays in every direction. Skip targets are in `skip.py`.
* The shuffled order is saved per channel (`channels/*.json`); progress (episode + seconds) is saved separately
  (`progress.json`, small, written every 30 s and on pause). mpv is given the order **rotated to start at the
  current episode**, so a restart resumes there, "previous" from the first entry wraps to the one before it, and no
  `--playlist-start` is needed.
* Resume is a seek after the episode loads (`App._track`), because mpv applies `--start` to every file in a playlist.

## Touch

The Goodix controller's axes are swapped relative to how the TV is viewed. Raw positions are normalised to 0..1 and
mapped to the landscape view by an **affine map** (`Hardware.touch_affine`). The calibration wizard replaces it with a
least-squares fit of 5 taps (`hardware.solve_affine`), rejecting fits worse than 45 px.

`EvdevTouch` reads the kernel's per-slot state on open (`EVIOCGMTSLOTS`): the kernel only reports a coordinate when it
changes, so a reader starting late would otherwise never learn where a touch that repeats the last X/Y landed.

## Display

The panel is natively portrait (480x640) and mpv rotates the *video* (`--video-rotate=270`) but not its OSD. Menus are
drawn as ordinary landscape pictures, turned a quarter turn, and shown as bitmap overlays (`overlay-add`). mpv runs
`--fs --no-config` so its coordinate space is exactly the panel and nothing outside the command line affects it.
The boot screen draws straight to `/dev/fb0` (RGB565) before mpv exists.

## Threads and who owns what

* touch loop: reads samples, makes gestures, posts timer events (menu auto-hide, ticks). Never blocks on the player.
* worker: the only thread that changes menu/playback state. Everything the user does is an event on its queue.
* monitor: restarts the player if it exits (playlist finished -> new cycle; otherwise resume) or stops answering.
* tracker: saves progress, performs the resume seek.

## Testing

* `tests/` runs on any machine (`pytest`). `tests/rig.py` runs the whole app against the pretend Pi and mpv and
  drives it with synthetic touches (taps, swipes, holds, menu rows, calibration, standby, shutdown, resume).
* `tools/e2e_pi.py` runs the same kind of scenario **on the TV** with real mpv, real display and backlight PWM, and a
  virtual touchscreen (uinput). It uses a scratch data folder and a harmless stand-in for the shutdown command.
* The code targets the Pi's Python 3.9 / numpy 1.19 / Pillow 8.1, so run the unit tests there too when in doubt.

## Files on a TV

| Path | What |
|---|---|
| `~/.config/tubeless/config.json` | name, videos folder, static clips, hardware overrides (all optional) |
| `~/.local/share/tubeless/settings.json` | brightness, shuffle, channel, calibration, auto-dim |
| `~/.local/share/tubeless/progress.json` | where you are in each channel |
| `~/.local/share/tubeless/channels/*.json` | each channel's shuffled order |
| `~/.local/share/tubeless/tubeless.log`, `mpv.log` | logs (the app log rotates at 3 MB) |
