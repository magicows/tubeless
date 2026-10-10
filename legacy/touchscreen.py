import os
import re
import json
import math
import queue
import random
import select
import socket
import subprocess
import sys
import threading
import evdev
from evdev import InputDevice, ecodes
import time
import logging
from logging.handlers import RotatingFileHandler
import psutil

import simptv_ui

# ─────────────────────────────────────────────
#  Playback state
# ─────────────────────────────────────────────
playProcess = None

# ─────────────────────────────────────────────
#  Menu state
#  All menu/playback decisions are made on the single worker thread
#  (see handle_gesture), so this state is only ever mutated from there.
#  The touch loop only reads the open flag and the hide deadline.
# ─────────────────────────────────────────────
menu_index    = 0            # highlighted row
menu_top      = 0            # first visible row (only changes when the user scrolls)
shuffle_on    = True
MENU_TIMEOUT  = 5.0
menu_deadline = 0.0          # time.time() after which the menu auto-hides

_menu_lock  = threading.Lock()
_menu_open  = False          # protected by _menu_lock

def is_menu_open():
    with _menu_lock:
        return _menu_open

def set_menu_open(state: bool):
    global _menu_open
    with _menu_lock:
        _menu_open = state

# ─────────────────────────────────────────────
#  Persistent settings
# ─────────────────────────────────────────────
STATE_FILE = os.path.expanduser("~/.simptv_state.json")

def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}

def save_state(**updates):
    state = load_state()
    state.update(updates)
    try:
        # Write to a temp file then rename, so a power cut can't leave a half-written file.
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        logging.warning(f"Could not save state: {e}")

# ─────────────────────────────────────────────
#  Audio pin + backlight
#
#  Wiring: the speaker amp is fed from GPIO 19 (PWM audio) and the panel
#  backlight is on GPIO 18. The Pi Zero's two hardware PWM channels are
#  both claimed by the audio overlay, so the backlight is dimmed with
#  pigpio's DMA-timed software PWM instead.
# ─────────────────────────────────────────────
BACKLIGHT_PIN     = 18
BACKLIGHT_PWM_HZ  = 800
BRIGHTNESS_LEVELS = [25, 50, 75, 100]
brightness        = 100

def init_audio_pin():
    try:
        subprocess.run(["raspi-gpio", "set", "19", "op", "a5"], check=True)
        logging.info("Audio PWM initialised on GPIO 19")
    except Exception as e:
        logging.warning(f"Could not init audio pin: {e}")

class Backlight:
    def __init__(self):
        self.pi = None

    def start(self, wait=15.0):
        """Connect to pigpiod (it may still be starting when we launch at boot)."""
        try:
            import pigpio
        except ImportError:
            logging.warning("pigpio not installed — backlight will be on/off only")
            return
        deadline = time.time() + wait
        while True:
            pi = pigpio.pi()
            if pi.connected:
                self.pi = pi
                pi.set_mode(BACKLIGHT_PIN, pigpio.OUTPUT)
                pi.set_PWM_frequency(BACKLIGHT_PIN, BACKLIGHT_PWM_HZ)
                logging.info("Backlight PWM ready on GPIO %d", BACKLIGHT_PIN)
                return
            if time.time() > deadline:
                logging.warning("pigpiod not reachable — backlight will be on/off only")
                return
            time.sleep(0.5)

    def set(self, percent):
        percent = max(0, min(100, percent))
        if self.pi is not None:
            self.pi.set_PWM_dutycycle(BACKLIGHT_PIN, round(percent * 255 / 100))
        else:
            level = "dh" if percent > 0 else "dl"
            try:
                subprocess.run(["raspi-gpio", "set", str(BACKLIGHT_PIN), "op", level], check=True)
            except Exception as e:
                logging.warning(f"Could not set backlight pin: {e}")
        logging.info(f"Backlight -> {percent}%")

backlight = Backlight()

def cycle_brightness():
    global brightness
    bigger = [b for b in BRIGHTNESS_LEVELS if b > brightness]
    brightness = bigger[0] if bigger else BRIGHTNESS_LEVELS[0]
    backlight.set(brightness)
    save_state(brightness=brightness)

# ─────────────────────────────────────────────
#  Directory / playlist helpers
# ─────────────────────────────────────────────
def build_directory_list(base_path="~/videos"):
    base_path = os.path.expanduser(base_path)
    if not os.path.isdir(base_path):
        raise ValueError(f"Base path {base_path} does not exist.")
    subdirs = []
    for entry in os.scandir(base_path):
        if entry.is_dir():
            subdirs.append(entry.name)

    def parse_dir_name(name):
        match = re.match(r"^(\d+)\.(.*)$", name)
        return (int(match.group(1)), match.group(2)) if match else (999, name)

    subdirs.sort(key=parse_dir_name)
    return [os.path.join(base_path, d) for d in subdirs]

def directory_label(directory):
    name = os.path.basename(directory)
    match = re.match(r"^\d+\.(.*)$", name)
    if match:
        name = match.group(1)
    return name.strip().replace("_", " ")


directories             = build_directory_list("~/videos")
current_directory_index = 0

# Restore saved settings (channel is stored by folder name so renaming or
# adding folders can't point at the wrong one).
_saved = load_state()
shuffle_on = bool(_saved.get("shuffle", True))
brightness = _saved.get("brightness", 100)
if brightness not in BRIGHTNESS_LEVELS:
    brightness = 100
_saved_channel = _saved.get("channel")
_channel_names = [os.path.basename(d) for d in directories]
if _saved_channel in _channel_names:
    current_directory_index = _channel_names.index(_saved_channel)

TOUCH_DEVICE_NAME   = os.environ.get("SIMPTV_TOUCH_NAME", "Goodix")   # env override is for testing
fallback_device     = "/dev/input/event1"
static_video_path   = os.path.expanduser("~/smallStatic.mp4")
longer_static_path  = os.path.expanduser("~/longerStatic.mp4")
fixed_playlist_file = os.path.join(
    os.path.dirname(os.path.realpath(__file__)), "playlist.m3u"
)
MPV_SOCKET          = "/tmp/mpvsocket"
MENU_OVERLAY_FILE   = "/dev/shm/simptv_menu.raw"   # tmpfs, so no SD card writes
MENU_OVERLAY_ID     = 0

LONG_PRESS_THRESHOLD   = 1.0    # seconds held without moving
TOUCH_MOVE_PX          = 25     # movement (in screen px) beyond which a touch is a swipe, not a tap
SWIPE_MIN_PX           = 70     # minimum travel for a swipe to count
MENU_SCROLL_PX_PER_ROW = 60     # menu swipe distance per row scrolled

MPV_LOG      = os.path.expanduser("~/mpv.log")        # mpv's own stdout/stderr (kept out of the app log)
LOG_FILE     = os.path.expanduser("~/touchscreen.log")
MAX_LOG_SIZE = 3 * 1024 * 1024

logger = logging.getLogger()
logger.setLevel(logging.INFO)
handler = RotatingFileHandler(LOG_FILE, maxBytes=MAX_LOG_SIZE, backupCount=3)
handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
logger.addHandler(handler)

logging.info("SimpTV starting up.")
logging.info(f"Restored settings: channel={directories[current_directory_index]} shuffle={shuffle_on} brightness={brightness}")
for d in directories:
    logging.info(d)

def find_touch_device():
    """Find the touchscreen by name so a renumbered /dev/input/eventN doesn't break us."""
    for path in evdev.list_devices():
        try:
            dev = InputDevice(path)
            if TOUCH_DEVICE_NAME.lower() in dev.name.lower():
                return path
        except OSError:
            continue
    return fallback_device

# ─────────────────────────────────────────────
#  Retry decorator
# ─────────────────────────────────────────────
def retry_on_error(max_retries=3):
    def decorator(func):
        def wrapper(*args, **kwargs):
            attempts = 0
            while attempts < max_retries:
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    attempts += 1
                    logging.info(f"Error in {func.__name__}: {e}. Attempt {attempts}/{max_retries}.")
                    time.sleep(2)
            raise RuntimeError(f"{func.__name__} failed after {max_retries} retries")
        return wrapper
    return decorator

# ─────────────────────────────────────────────
#  Playlist generation
# ─────────────────────────────────────────────
def _write_playlist(directory):
    """Build playlist.m3u for one channel folder. Raises ValueError if it has no videos."""
    videos = []
    for root, _, files in os.walk(directory):
        for file in files:
            if file.lower().endswith(".mp4"):
                videos.append(os.path.join(root, file))
    if not videos:
        raise ValueError(f"No videos found in {directory}.")
    if shuffle_on:
        random.shuffle(videos)
    else:
        videos.sort()
    # Every video is preceded by a static burst: static, video, static, video, ...
    # (so there is always a static to land on, even in front of the first video).
    with open(fixed_playlist_file, "w") as f:
        for video in videos:
            f.write(f"{static_video_path}\n")
            f.write(f"{video}\n")
    logging.info(f"Playlist generated ({'shuffled' if shuffle_on else 'ordered'}, {len(videos)} videos)")

@retry_on_error()
def generate_playlist():
    _write_playlist(directories[current_directory_index])

def channel_has_videos(index):
    for _, _, files in os.walk(directories[index]):
        if any(f.lower().endswith(".mp4") for f in files):
            return True
    return False

def next_playable_channel(start, step):
    """First channel at or after 'start' (moving by 'step', wrapping) that has videos, else None."""
    n = len(directories)
    for i in range(n):
        index = (start + i * step) % n
        if channel_has_videos(index):
            return index
    return None

def pick_startup_channel():
    """If the saved channel is empty or gone, fall back to the first one that has videos."""
    global current_directory_index
    index = next_playable_channel(current_directory_index, +1)
    if index is None:
        raise RuntimeError("No channel has any .mp4 videos")
    if index != current_directory_index:
        logging.warning(f"Channel {directories[current_directory_index]} has no videos; "
                        f"starting on {directories[index]} instead")
        current_directory_index = index

# ─────────────────────────────────────────────
#  mpv process + IPC
# ─────────────────────────────────────────────
def open_mpv_log():
    """Append to ~/mpv.log, starting over once it passes 512 KB."""
    try:
        big = os.path.exists(MPV_LOG) and os.path.getsize(MPV_LOG) > 512 * 1024
        f = open(MPV_LOG, "w" if big else "a")
        f.write(f"\n--- mpv started {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
        f.flush()
        return f
    except OSError:
        return subprocess.DEVNULL

@retry_on_error()
def play_playlist():
    global playProcess
    if playProcess and playProcess.poll() is None:
        playProcess.terminate()
        playProcess.wait()
    # --fs makes mpv's OSD space exactly the 480x640 panel, which the menu
    # overlay is drawn for. video-rotate=270 comes from ~/.config/mpv/mpv.conf.
    mpv_out = open_mpv_log()
    try:
        playProcess = subprocess.Popen([
            "mpv", "--hwdec=no", "--fs",
            "--playlist=" + fixed_playlist_file,
            "--input-ipc-server=" + MPV_SOCKET,
            "--loop-playlist=no",
            "--msg-level=all=warn",
        ], stdout=mpv_out, stderr=subprocess.STDOUT)
    finally:
        if mpv_out is not subprocess.DEVNULL:
            mpv_out.close()
    logging.info(f"Started mpv PID: {playProcess.pid}")

class MpvIPC:
    """
    One persistent connection to mpv's JSON IPC socket (replaces spawning
    `socat` for every command). Thread-safe; reconnects after an mpv restart.
    """
    def __init__(self, path):
        self.path  = path
        self._sock = None
        self._file = None
        self._lock = threading.Lock()
        self._req  = 0

    def _connect(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(3)
        s.connect(self.path)
        self._sock = s
        self._file = s.makefile("rw", encoding="utf-8")

    def _close(self):
        for obj in (self._file, self._sock):
            try:
                if obj:
                    obj.close()
            except Exception:
                pass
        self._file = self._sock = None

    def command(self, *args):
        """Send one command. Returns the reply data (None if mpv reported an error).
        Raises OSError if mpv can't be reached."""
        with self._lock:
            for attempt in (1, 2):
                try:
                    if self._sock is None:
                        self._connect()
                    self._req += 1
                    rid = self._req
                    self._file.write(json.dumps({"command": list(args), "request_id": rid}) + "\n")
                    self._file.flush()
                    while True:
                        line = self._file.readline()
                        if not line:
                            raise ConnectionError("mpv closed the IPC socket")
                        reply = json.loads(line)
                        if reply.get("request_id") == rid:
                            return reply.get("data") if reply.get("error") == "success" else None
                except (OSError, ValueError):
                    self._close()
                    if attempt == 2:
                        raise

    def get(self, prop):
        try:
            return self.command("get_property", prop)
        except Exception:
            return None

mpv = MpvIPC(MPV_SOCKET)

def send_mpv_json_command(args):
    """Never raises (so worker/timer threads can't die on a flaky mpv)."""
    try:
        mpv.command(*args)
    except Exception as e:
        logging.warning(f"mpv command {args[:1]} failed: {e}")

# ─────────────────────────────────────────────
#  Menu rendering (bitmap overlay, see simptv_ui.py)
# ─────────────────────────────────────────────
def build_menu_items():
    paused = bool(mpv.get("pause"))
    items = [
        {"icon": "play" if paused else "pause", "label": "Play" if paused else "Pause", "action": ("playpause",)},
        {"icon": "next",    "label": "Next video",     "action": ("next",)},
        {"icon": "prev",    "label": "Previous video", "action": ("prev",)},
        {"icon": "shuffle", "label": "Shuffle", "value": "ON" if shuffle_on else "OFF", "action": ("shuffle",)},
        {"icon": "sun",     "label": "Brightness", "value": f"{brightness}%", "action": ("brightness",)},
    ]
    for i, directory in enumerate(directories):
        items.append({
            "icon": "tv", "label": directory_label(directory),
            "current": i == current_directory_index, "action": ("channel", i),
        })
    return items

def render_menu():
    """Draw the menu and push it to mpv."""
    global menu_index, menu_top
    if not is_menu_open():
        return
    items = build_menu_items()
    menu_index = max(0, min(menu_index, len(items) - 1))
    menu_top = max(0, min(menu_top, max(0, len(items) - simptv_ui.VISIBLE_ROWS)))
    subtitle = directory_label(directories[current_directory_index])
    image = simptv_ui.render_menu_landscape(items, menu_index, subtitle=subtitle, top=menu_top)
    raw, w, h = simptv_ui.to_panel_bgra(image)
    with open(MENU_OVERLAY_FILE, "wb") as f:
        f.write(raw)
    send_mpv_json_command(["overlay-add", MENU_OVERLAY_ID, 0, 0,
                           MENU_OVERLAY_FILE, 0, "bgra", w, h, w * 4])

def clear_osd():
    send_mpv_json_command(["overlay-remove", MENU_OVERLAY_ID])

# ─────────────────────────────────────────────
#  Menu open / close
# ─────────────────────────────────────────────
def schedule_menu_hide():
    """Push the auto-hide deadline out; the touch loop notices when it passes."""
    global menu_deadline
    menu_deadline = time.time() + MENU_TIMEOUT

def open_menu():
    global menu_index, menu_top
    set_menu_open(True)
    menu_index = 0
    menu_top = 0
    schedule_menu_hide()
    logging.info("Menu opened")
    render_menu()

def close_menu():
    set_menu_open(False)
    clear_osd()
    logging.info("Menu closed")

# ─────────────────────────────────────────────
#  Menu item actions
# ─────────────────────────────────────────────
def menu_select():
    global shuffle_on
    items = build_menu_items()
    if not 0 <= menu_index < len(items):
        return
    item = items[menu_index]
    action = item["action"]
    logging.info(f"Menu select index={menu_index} label={item['label']}")

    if action[0] == "playpause":
        toggle_play_pause()
        schedule_menu_hide()
        render_menu()

    elif action[0] == "next":
        close_menu()
        skip_video(+1)

    elif action[0] == "prev":
        close_menu()
        skip_video(-1)

    elif action[0] == "shuffle":
        shuffle_on = not shuffle_on
        save_state(shuffle=shuffle_on)
        generate_playlist()
        send_mpv_json_command(["loadfile", fixed_playlist_file, "replace"])
        schedule_menu_hide()
        render_menu()

    elif action[0] == "brightness":
        cycle_brightness()
        schedule_menu_hide()
        render_menu()

    elif action[0] == "channel":
        close_menu()
        switch_to_directory(action[1])

# ─────────────────────────────────────────────
#  Playback actions
# ─────────────────────────────────────────────
def skip_video(direction):
    """
    Playlist layout is: static, video, static, video, ...  (even = static, odd = video)
    Each static belongs to the video that follows it. Skipping always lands on the
    static in front of the target video, so the channel-flick effect plays in every
    case, including at the very start of the playlist and when you skip again while
    a static is showing.

        on a video (pos odd):    forward -> pos+1     back -> pos-3
        on a static (pos even):  forward -> pos+2     back -> pos-2
    Targets are always even (a static). Going back past the start wraps round to the last
    video, so "previous" always changes the episode instead of replaying the first one.
    """
    pos   = mpv.get("playlist-pos")
    count = mpv.get("playlist-count")
    if pos is None or count is None or count < 2:
        return                 # nothing to skip (e.g. the lone channel-change static is playing)
    on_video = (pos % 2 == 1)
    if direction > 0:
        target = pos + 1 if on_video else pos + 2
    else:
        target = pos - 3 if on_video else pos - 2
    if target < 0:
        target = count - 2     # wrap: the static in front of the last video
    if target >= count:
        restart_mpv()          # ran off the end of the playlist: reshuffle and go again
        return
    send_mpv_json_command(["set_property", "playlist-pos", target])
    send_mpv_json_command(["set_property", "pause", False])

def toggle_play_pause():
    send_mpv_json_command(["cycle", "pause"])

_switch_timer = None

def switch_to_directory(directory_index, step=None):
    """
    Change channel. With 'step' (+1/-1, from a swipe) empty channels are skipped in that
    direction; without it (a menu tap) the exact channel is used, or nothing happens if empty.
    """
    global current_directory_index, _switch_timer
    if step is not None:
        index = next_playable_channel(directory_index, step)
    else:
        index = directory_index % len(directories)
        if not channel_has_videos(index):
            index = None
    if index is None:
        logging.warning("Channel change ignored: no videos in that channel")
        return
    current_directory_index = index
    save_state(channel=os.path.basename(directories[current_directory_index]))
    logging.info(f"Switching to directory: {directories[current_directory_index]}")
    send_mpv_json_command(["loadfile", longer_static_path, "replace"])
    generate_playlist()
    # A newer channel change replaces any pending one, so rapid swipes don't pile up.
    if _switch_timer is not None:
        _switch_timer.cancel()
    _switch_timer = threading.Timer(
        3.0,
        lambda: send_mpv_json_command(["loadfile", fixed_playlist_file, "replace"])
    )
    _switch_timer.start()

# ─────────────────────────────────────────────
#  Gesture recognition
#
#  Pure logic, no I/O: fed (finger down?, x, y, time) samples in screen
#  coordinates and returns gestures. One finger only; a second finger
#  cancels the gesture.
# ─────────────────────────────────────────────
class GestureRecognizer:
    def __init__(self):
        self.tracking   = False
        self.suppressed = False     # set when a 2nd finger appears, until all lift
        self.moved      = False
        self.long_fired = False
        self.t0 = self.x0 = self.y0 = self.x = self.y = 0.0

    def cancel(self):
        self.tracking   = False
        self.suppressed = True

    def update(self, active, x, y, now):
        """Returns a list of gesture tuples (usually empty)."""
        out = []
        if not active:
            self.suppressed = False
            if self.tracking:
                self.tracking = False
                dx, dy = self.x - self.x0, self.y - self.y0
                if self.long_fired:
                    pass
                elif self.moved:
                    if max(abs(dx), abs(dy)) >= SWIPE_MIN_PX:
                        if abs(dx) >= abs(dy):
                            direction = "right" if dx > 0 else "left"
                        else:
                            direction = "down" if dy > 0 else "up"
                        out.append(("swipe", direction, self.x0, self.y0, dx, dy))
                else:
                    out.append(("tap", self.x0, self.y0))
            return out

        if self.suppressed:
            return out
        if not self.tracking:
            self.tracking   = True
            self.moved      = False
            self.long_fired = False
            self.t0, self.x0, self.y0 = now, x, y
        self.x, self.y = x, y
        if not self.moved and math.hypot(x - self.x0, y - self.y0) > TOUCH_MOVE_PX:
            self.moved = True
        return out

    def poll(self, now):
        """Time-based gestures: a long press fires while the finger is still down."""
        if self.tracking and not self.moved and not self.long_fired \
                and now - self.t0 >= LONG_PRESS_THRESHOLD:
            self.long_fired = True
            return [("long_press",)]
        return []

    def next_timeout(self, now):
        """Seconds until poll() could produce something, or None."""
        if self.tracking and not self.moved and not self.long_fired:
            return max(0.0, self.t0 + LONG_PRESS_THRESHOLD - now)
        return None

# ─────────────────────────────────────────────
#  Gesture -> action (runs on the worker thread only)
#
#    Menu closed                    Menu open
#    tap          play / pause      tap row      activate it
#    swipe left   next video        tap outside  close menu
#    swipe right  previous video    swipe up     scroll down the list
#    swipe up     next channel      swipe down   scroll up the list (a tap never scrolls)
#    swipe down   previous channel
#    hold         open menu         hold         close menu
# ─────────────────────────────────────────────
_events = queue.Queue()

def handle_menu_gesture(g):
    global menu_index, menu_top
    kind = g[0]
    if kind == "long_press":
        close_menu()
    elif kind == "tap":
        hit = simptv_ui.hit_test(g[1], g[2], menu_top, len(build_menu_items()))
        if hit == simptv_ui.OUTSIDE:
            close_menu()
        elif hit is None:
            schedule_menu_hide()
        else:
            menu_index = hit
            menu_select()
    elif kind == "swipe":
        direction, dy = g[1], g[5]
        if direction in ("up", "down"):
            rows = max(1, int(round(abs(dy) / MENU_SCROLL_PX_PER_ROW)))
            step = rows if direction == "up" else -rows
            total = len(build_menu_items())
            menu_top = max(0, min(max(0, total - simptv_ui.VISIBLE_ROWS), menu_top + step))
            # keep the highlight on a visible row
            menu_index = max(menu_top, min(menu_top + simptv_ui.VISIBLE_ROWS - 1, menu_index))
            schedule_menu_hide()
            render_menu()
        else:
            schedule_menu_hide()

def handle_playback_gesture(g):
    kind = g[0]
    if kind == "long_press":
        open_menu()
    elif kind == "tap":
        toggle_play_pause()
    elif kind == "swipe":
        direction = g[1]
        if direction == "left":
            skip_video(+1)
        elif direction == "right":
            skip_video(-1)
        elif direction == "up":
            switch_to_directory(current_directory_index + 1, step=+1)
        elif direction == "down":
            switch_to_directory(current_directory_index - 1, step=-1)

def handle_gesture(g):
    logging.info(f"gesture {g}")
    if g[0] == "menu_timeout":
        if is_menu_open() and time.time() >= menu_deadline:
            close_menu()
    elif is_menu_open():
        handle_menu_gesture(g)
    else:
        handle_playback_gesture(g)

def worker_loop():
    while True:
        g = _events.get()
        try:
            handle_gesture(g)
        except Exception:
            logging.exception("Error handling gesture")

# ─────────────────────────────────────────────
#  Touch input loop (single thread, no timers)
#
#  The controller reports axes swapped relative to how the TV is viewed
#  (measured by tapping the four corners): raw X runs top->bottom and raw Y
#  runs right->left. to_screen() converts to 640x480 landscape coordinates.
# ─────────────────────────────────────────────
def to_screen(raw_x, raw_y, x_max, y_max):
    sx = (y_max - raw_y) / y_max * simptv_ui.VIEW_W
    sy = raw_x / x_max * simptv_ui.VIEW_H
    return sx, sy

def touch_control_loop():
    rec = GestureRecognizer()
    while True:
        device = None
        try:
            device_path = find_touch_device()
            device = InputDevice(device_path)
            x_max = device.absinfo(ecodes.ABS_MT_POSITION_X).max or 1
            y_max = device.absinfo(ecodes.ABS_MT_POSITION_Y).max or 1
            logging.info(f"Listening on {device_path} ({device.name}) axes {x_max}x{y_max}")

            slot, tracking_id, pos_x, pos_y = 0, {}, {}, {}
            posted_deadline = None      # menu auto-hide is posted once per deadline

            while True:
                now = time.time()
                timeout = rec.next_timeout(now)
                if is_menu_open() and posted_deadline != menu_deadline:
                    until_hide = max(0.0, menu_deadline - now)
                    timeout = until_hide if timeout is None else min(timeout, until_hide)
                if timeout is None or timeout > 1.0:
                    timeout = 1.0       # defensive wake-up; events normally wake us sooner
                ready, _, _ = select.select([device.fd], [], [], timeout)

                if ready:
                    for ev in device.read():
                        if ev.type == ecodes.EV_ABS:
                            if ev.code == ecodes.ABS_MT_SLOT:
                                slot = ev.value
                            elif ev.code == ecodes.ABS_MT_TRACKING_ID:
                                tracking_id[slot] = ev.value
                            elif ev.code == ecodes.ABS_MT_POSITION_X:
                                pos_x[slot] = ev.value
                            elif ev.code == ecodes.ABS_MT_POSITION_Y:
                                pos_y[slot] = ev.value
                        elif ev.type == ecodes.EV_KEY and ev.code == ecodes.BTN_TOUCH and ev.value == 0:
                            for s in tracking_id:
                                tracking_id[s] = -1          # everything is up
                        elif ev.type == ecodes.EV_SYN and ev.code == ecodes.SYN_REPORT:
                            down = [s for s, t in tracking_id.items() if t != -1]
                            if len(down) > 1:
                                rec.cancel()
                            elif len(down) == 1 and down[0] in pos_x and down[0] in pos_y:
                                sx, sy = to_screen(pos_x[down[0]], pos_y[down[0]], x_max, y_max)
                                for g in rec.update(True, sx, sy, time.time()):
                                    _events.put(g)
                            elif not down:
                                for g in rec.update(False, 0, 0, time.time()):
                                    _events.put(g)

                now = time.time()
                for g in rec.poll(now):
                    _events.put(g)
                if is_menu_open() and now >= menu_deadline and posted_deadline != menu_deadline:
                    posted_deadline = menu_deadline
                    _events.put(("menu_timeout",))

        except Exception:
            # Device unplugged/not ready yet, or an unexpected bug: log it and re-open, never die.
            logging.exception("Touch loop error; reopening the device")
            time.sleep(1)
        finally:
            try:
                if device is not None:
                    device.close()
            except Exception:
                pass

# ─────────────────────────────────────────────
#  mpv health monitor
#  - restarts mpv if it exits (e.g. the playlist finished)
#  - restarts mpv if it stops answering on its IPC socket
# ─────────────────────────────────────────────
HEARTBEAT_EVERY   = 60     # seconds
EXIT_CHECK_EVERY  = 2      # seconds
MIN_RESTART_GAP   = 5      # seconds, so a crash-looping mpv can't spin the CPU

def monitor_mpv():
    global playProcess
    last_heartbeat = time.time()
    last_restart   = 0
    while True:
        time.sleep(EXIT_CHECK_EVERY)
        try:
            if playProcess is None or playProcess.poll() is not None:
                if time.time() - last_restart < MIN_RESTART_GAP:
                    continue
                logging.info("mpv is not running (playlist finished or crashed) — restarting")
                cleanup_zombies()
                restart_mpv(regenerate=True)
                last_restart = last_heartbeat = time.time()
                continue

            if time.time() - last_heartbeat >= HEARTBEAT_EVERY:
                last_heartbeat = time.time()
                try:
                    mpv.command("get_property", "pause")
                    logging.debug("mpv heartbeat OK")
                except Exception as e:
                    logging.warning(f"mpv heartbeat failed ({e}) — restarting")
                    cleanup_zombies()
                    restart_mpv()
                    last_restart = time.time()
                else:
                    cleanup_zombies()
        except Exception as e:
            logging.error(f"Monitor error: {e}")

def restart_mpv(regenerate=True):
    global playProcess
    logging.info("Restarting mpv...")
    try:
        if playProcess and playProcess.poll() is None:
            playProcess.terminate()
            playProcess.wait(timeout=10)
    except Exception as e:
        logging.error(f"Error terminating mpv: {e}")
    finally:
        playProcess = None
    if os.path.exists(MPV_SOCKET):
        try:
            os.remove(MPV_SOCKET)
        except Exception as e:
            logging.error(f"Failed to remove socket: {e}")
    try:
        if regenerate:
            generate_playlist()
        play_playlist()
    except Exception as e:
        logging.error(f"Error restarting mpv: {e}")

def cleanup_zombies():
    parent = psutil.Process(os.getpid())
    for child in parent.children(recursive=True):
        try:
            if child.status() == psutil.STATUS_ZOMBIE:
                logging.info(f"Cleaning zombie PID {child.pid}")
                child.kill()
        except Exception as e:
            logging.error(f"Error cleaning zombie {child.pid}: {e}")

# ─────────────────────────────────────────────
#  Entry point
# ─────────────────────────────────────────────
if __name__ == "__main__":
    try:
        # Clear a stale mpv socket from a previous run (only when actually starting up,
        # never on import, so tests can load this module without disturbing a running TV).
        if os.path.exists(MPV_SOCKET):
            try:
                os.remove(MPV_SOCKET)
            except OSError as e:
                logging.info(f"Failed to remove socket: {e}")
        init_audio_pin()
        backlight.start()
        backlight.set(brightness)       # (simptv-boot.service shows the boot screen until mpv's first frame)
        pick_startup_channel()
        generate_playlist()
        play_playlist()
        threading.Thread(target=monitor_mpv, daemon=True).start()
        threading.Thread(target=worker_loop, daemon=True).start()
        touch_control_loop()
    except Exception:
        # Exit non-zero so systemd (Restart=always) brings the whole app back.
        logging.critical("Critical error", exc_info=True)
        sys.exit(1)
