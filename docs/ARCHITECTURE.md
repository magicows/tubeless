# Architecture

## The idea

Everything that touches the real world sits behind a small interface, so the same app code runs on the TV, in the desktop simulator and in tests.

```
                    +----------------------- App (app.py) -----------------------+
 touch  --samples-->| touch loop -> GestureRecognizer -> events queue             |
                    |                                       |                     |
                    |                         worker thread: menu / playback /    |
                    |                         calibration / standby / shutdown    |
                    |   monitor thread: restart the player   tracker thread:      |
                    |   health / Wi-Fi guard threads         progress + resume    |
                    +-----+-----------------+-------------------+-----------------+
                          |                 |                   |
                      Player            Library/Playlists     Settings/Store
              (mpv  |  FakePlayer)      (channels, order)     (atomic JSON files)
                          |
                  backend.touch / .backlight / .power
                  (hal/pi.py  |  hal/sim.py)

   web page (web.py) <-- phone/laptop       Jellyfin server <-- jellyfin.py        yt-dlp <-- youtube.py, lookahead.py
```

## Modules

| Module | Job | Pure? |
|---|---|---|
| `hardware.py` | the one place that knows the supported build; touch-calibration maths | yes |
| `gestures.py`, `skip.py` | taps, swipes and long presses from touch samples; which playlist entry a skip lands on | yes |
| `menu.py`, `render.py` | the menu as data; drawing it, the info screens and hit-testing (colours and shapes come from the active theme) | yes |
| `themes.py` | themes: built-in palettes, pasted palettes turned into readable themes | yes |
| `library.py` | channels (and their numbers), the saved shuffle order, resume, the playlist file, streamed-video addresses | disk only |
| `live.py` | Live TV mode: a channel's fixed running order and what is on air at a given time | yes |
| `daynight.py` | dim at night: sunrise and sunset, the fade, the system clock check | yes |
| `state.py` | settings and progress as atomic JSON | disk only |
| `app.py` | ties it together (threads, gesture -> action, the hooks the web page uses) | |
| `player.py` | `MpvPlayer` (real) and `FakePlayer` (mpv-alike for the simulator and tests) | |
| `lookahead.py` | works out the next YouTube video's stream address while one plays | mostly |
| `hal/pi.py`, `hal/sim.py` | touch, backlight, framebuffer, power: real and pretend | |
| `web.py`, `setupweb.py` | the upload/companion page, and the Wi-Fi setup page (standard library HTTP servers) | |
| `youtube.py`, `jellyfin.py`, `jfchannel.py` | YouTube channels (yt-dlp); the Jellyfin client and a Jellyfin channel's file | |
| `libindex.py`, `advice.py` | video lengths (mediainfo), upload advice | |
| `statics.py` | the TV static clip: presets, lengths, converting an uploaded clip | |
| `wifi.py` | NetworkManager access, the hotspot, the router ping used by the Wi-Fi guard | |
| `health.py` | power flags, temperature, load, memory and signal, noted in the log each minute | |
| `updater.py` | checking for, installing and rolling back a release | |
| `installer.py`, `installweb.py` | the installer's steps; the page that shows its progress | |
| `bootscreen.py` | the boot screen (rain, status checks, compositor) | mostly |
| `simserver.py` | the desktop simulator (browser) | |

## Playback model

* A channel's playlist is `static, video, static, video, ...`: every video has a static in front of it, so skipping always lands on a static
  and the channel-flick effect plays in every direction. With the static between videos off (its length set to Off on the page) it is `video, video, ...`,
  and an ad (if due) is the only entry between two videos. So nothing may assume odd positions are videos: `Playlists.layout` (written with the
  playlist) says what each position holds, and `Playlists.episode_at`, `next_video_pos` and `skip.layout_target` all read it.
* A "video" is a file, a YouTube address (`yt-<id>`, played through mpv's yt-dlp hook) or a Jellyfin stream (`jf-<id>`, an http address the
  server transcodes to H.264). `Library.is_stream` covers the last two; only YouTube gets look-ahead.
* The shuffled order is saved per channel (`channels/*.json`); progress (episode + seconds) is saved separately (`progress.json`, small, written
  every 30 s and on pause). mpv is given the order **rotated to start at the current episode**, so a restart resumes there, "previous" from the
  first entry wraps to the one before it, and no `--playlist-start` is needed.
* **A live channel** (set per channel, `live_channels` in the settings) replaces that when its lengths are all known: `Playlists.plan()` asks the app for the video on air now and its
  position (`live.slot`), writes it as the channel's progress, and the normal resume step seeks to it. The running order is fixed (a stable
  shuffle keyed by file name), so a finished loop returns to what is on air rather than reshuffling.
* Resume is a seek after the episode loads (`App._track`), because mpv applies `--start` to every file in a playlist. A Jellyfin video is
  never sought into (a transcoded stream can't be), so it starts from the beginning.
* **Look-ahead (YouTube streams):** mpv's hook only runs yt-dlp when an entry starts (6 to 12 s on the Pi), so while a video plays a
  background thread resolves the next one's real addresses and swaps them into the playlist (`loadfile ... insert-at`, mpv 0.38+), with
  checks that the swap landed where it should and a fall-back that rebuilds the playlist if not.
* Streams wait for the network after a start (`_wait_for_network`), and a video that doesn't begin is given up on (45 s) and the player
  restarted past it (120 s), with a message on screen saying why.

## Touch

The Goodix controller's axes are swapped relative to how the TV is viewed. Raw positions are normalised to 0..1 and mapped to the landscape
view by an **affine map** (`Hardware.touch_affine`). The calibration wizard replaces it with a least-squares fit of 5 taps
(`hardware.solve_affine`), rejecting fits worse than 45 px.

`EvdevTouch` reads the kernel's per-slot state on open (`EVIOCGMTSLOTS`): the kernel only reports a coordinate when it changes, so a reader
starting late would otherwise never learn where a touch that repeats the last X/Y landed.

## Display

The panel is natively portrait (480x640) and mpv rotates the *video* (`--video-rotate=270`) but not its OSD. Menus are drawn as ordinary
landscape pictures, turned a quarter turn, and shown as bitmap overlays (`overlay-add`). mpv runs `--fs --no-config` so its coordinate space is
exactly the panel and nothing outside the command line affects it. The boot screen and the information screens (upload, Wi-Fi setup,
calibration) draw straight to `/dev/fb0` (RGB565) when no player is running.

## Threads and who owns what

* touch loop: reads samples, makes gestures, posts timer events (menu auto-hide, ticks). Never blocks on the player.
* worker: the only thread that changes menu/playback state. Everything the user does is an event on its queue.
* monitor: restarts the player if it exits (playlist finished -> new cycle; otherwise resume) or stops answering.
* tracker: saves progress, performs the resume seek, watches streams, drives look-ahead.
* health and Wi-Fi guard: a note in the log each minute; a router ping every 20 s, reloading the Wi-Fi chip and, once per boot, restarting the TV
  if it stays down.
* the web server (while in upload mode), and short-lived workers for downloads, yt-dlp updates, library scans and look-ahead lookups.

## Testing

* `tests/` runs on any machine (`pytest`, about 540 tests). `tests/rig.py` runs the whole app against the pretend Pi and mpv and drives it with
  synthetic touches (taps, swipes, holds, menu rows, calibration, standby, shutdown, resume). `tests/jf_fake.py` is a pretend Jellyfin server (it
  even refuses Python's default user agent, as Cloudflare does); yt-dlp has a `FakeYtDlp`.
* `tools/e2e_pi.py` runs the same kind of scenario **on the TV** with real mpv, real display and backlight PWM, and a virtual touchscreen
  (uinput). It uses a scratch data folder and a harmless stand-in for the shutdown command. [manual-test-plan.md](manual-test-plan.md) is the
  by-hand checklist.
* The code targets the Pi's Python 3.9 / numpy 1.19 / Pillow 8.1 as well as current versions (CI runs 3.9 and 3.13).

## Files on a TV

| Path | What |
|---|---|
| `~/.config/tubeless/config.json` | name, wake-up message, videos folder, static clips, hardware overrides (all optional) |
| `~/.local/share/tubeless/settings.json` | brightness, shuffle, channel, channel numbers, play orders, themes, night dimming, which channels are live, calibration ... |
| `~/.local/share/tubeless/jellyfin.json` | the Jellyfin server and sign-in token (readable by the TV's user only; never the password) |
| `~/.local/share/tubeless/progress.json` | where you are in each channel |
| `~/.local/share/tubeless/channels/*.json` | each channel's shuffled order |
| `~/.local/share/tubeless/index.json` | video lengths read by mediainfo |
| `~/.local/share/tubeless/playlist.m3u` | what mpv is playing (mode 0600: Jellyfin addresses carry the token) |
| `~/.local/share/tubeless/tubeless.log`, `mpv.log` | logs (the app log rotates at 3 MB) |
| `~/videos/<channel>/` | a channel: video files, or `channel.json` (YouTube) or `jellyfin-channel.json` (Jellyfin) |
