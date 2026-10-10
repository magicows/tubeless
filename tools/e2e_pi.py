#!/usr/bin/env python3
"""
End-to-end test ON the TV: the real app, real mpv, real display and backlight, driven by a virtual
touchscreen (uinput). Uses a scratch data folder and a harmless stand-in for the shutdown command, so
your real settings, progress and TV are left alone. It does stop the TV service while it runs and
starts it again afterwards.

    sudo python3 tools/e2e_pi.py          (from the repo folder on the Pi; needs root for uinput)
"""
import json
import os
import pwd
import subprocess
import sys
import time
from pathlib import Path

from evdev import AbsInfo, UInput, ecodes as e

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tubeless import render                                    # noqa: E402
from tubeless.hardware import Hardware, apply_affine, invert_affine   # noqa: E402
from tubeless.player import MpvIPC                             # noqa: E402

USER = os.environ.get("TUBELESS_USER", "simpsons")
HOME = Path(pwd.getpwnam(USER).pw_dir)
HW = Hardware()
WORK = Path("/tmp/tubeless-e2e")
SERVICES = [s for s in ("tubeless", "tubeless-boot", "simptv", "simptv-boot")]
XM, YM = 639, 479

results = []


def check(name, cond, extra=""):
    results.append(bool(cond))
    print(("PASS " if cond else "FAIL ") + name, extra, flush=True)


def wait_for(cond, timeout=20.0, step=0.1):
    end = time.time() + timeout
    while time.time() < end:
        try:
            if cond():
                return True
        except Exception:
            pass
        time.sleep(step)
    return False


def sh(*args, **kw):
    return subprocess.run(args, capture_output=True, text=True, **kw)


# ─────────────────────────────────────────────
WORK.mkdir(exist_ok=True)
os.chmod(WORK, 0o777)
data = WORK / "data"
cfg = {
    "name": "E2E", "videos_dir": str(HOME / "videos"), "data_dir": str(data), "log_file": str(WORK / "app.log"),
    "mpv_log": str(WORK / "mpv.log"), "mpv_socket": str(WORK / "mpv.sock"),
    "static_short": str(HOME / "smallStatic.mp4"), "static_long": str(HOME / "longerStatic.mp4"),
    "legacy_state_file": str(WORK / "none.json"), "menu_timeout": 10.0, "long_press": 0.5,
    "hardware_overrides": {"touch_name": "Tubeless E2E Touch"},
    "shutdown_command": ["sh", "-c", f"touch {WORK}/shutdown-called"],
}
cfg_path = WORK / "config.json"
cfg_path.write_text(json.dumps(cfg))
(WORK / "shutdown-called").unlink(missing_ok=True)
sh("rm", "-rf", str(data))
data.mkdir()
(data / "settings.json").write_text(json.dumps({"shuffle": True, "brightness": 100}))   # an existing install: no first-run wizard
sh("chown", "-R", f"{USER}:{USER}", str(WORK))

was_active = [s for s in SERVICES if sh("systemctl", "is-active", s).stdout.strip() == "active"]
for s in was_active:
    sh("systemctl", "stop", s)
time.sleep(2)

cap = {e.EV_KEY: [e.BTN_TOUCH],
       e.EV_ABS: [(e.ABS_MT_SLOT, AbsInfo(0, 0, 4, 0, 0, 0)), (e.ABS_MT_TRACKING_ID, AbsInfo(0, 0, 65535, 0, 0, 0)),
                  (e.ABS_MT_POSITION_X, AbsInfo(0, 0, XM, 0, 0, 0)), (e.ABS_MT_POSITION_Y, AbsInfo(0, 0, YM, 0, 0, 0))]}
ui = UInput(cap, name="Tubeless E2E Touch")
time.sleep(1)
to_raw = invert_affine(HW.touch_affine)
tid = [100]


def feed(vx, vy):
    # The kernel drops a slot's X/Y if they equal its previous value, which a real finger never does but a
    # script does (and a freshly restarted app would then never see where the touch was). Nudge each touch.
    vx += (tid[0] % 4) * 3.0                                           # 3 view px is always >= 2 raw units
    rx, ry = apply_affine(to_raw, vx, vy)
    ui.write(e.EV_ABS, e.ABS_MT_POSITION_X, int(max(0, min(1, rx)) * XM))
    ui.write(e.EV_ABS, e.ABS_MT_POSITION_Y, int(max(0, min(1, ry)) * YM))


def down(vx, vy):
    tid[0] += 1
    ui.write(e.EV_ABS, e.ABS_MT_SLOT, 0)
    ui.write(e.EV_ABS, e.ABS_MT_TRACKING_ID, tid[0])
    feed(vx, vy)
    ui.write(e.EV_KEY, e.BTN_TOUCH, 1)
    ui.syn()


def up():
    ui.write(e.EV_ABS, e.ABS_MT_TRACKING_ID, -1)
    ui.write(e.EV_KEY, e.BTN_TOUCH, 0)
    ui.syn()


def tap(vx=320, vy=240, settle=0.5):
    down(vx, vy); time.sleep(0.08); up(); time.sleep(settle)


def swipe(x0, y0, x1, y1, dur=0.35, steps=12, settle=1.0):
    down(x0, y0)
    for i in range(1, steps + 1):
        time.sleep(dur / steps)
        feed(x0 + (x1 - x0) * i / steps, y0 + (y1 - y0) * i / steps)
        ui.syn()
    up(); time.sleep(settle)


def hold(secs=0.9, settle=1.5):
    down(320, 240); time.sleep(secs); up(); time.sleep(settle)


def row_xy(n):
    cx, cy = HW.bezel_center
    return cx + (300 - 320) * HW.bezel_scale, cy + (render.ROWS_Y0 + n * (render.ROW_H + render.ROW_GAP) + render.ROW_H / 2 - 240) * HW.bezel_scale


def tap_row(n, settle=1.2):
    tap(*row_xy(n), settle=settle)


# ─────────────────────────────────────────────
app = None


def start_app():
    global app, ipc
    env = ["env", "-i", f"HOME={HOME}", f"USER={USER}", "PATH=/usr/bin:/bin", f"PYTHONPATH={ROOT}", "PYTHONUNBUFFERED=1"]
    app = subprocess.Popen(["sudo", "-u", USER] + env + ["python3", "-m", "tubeless", "run", "--config", str(cfg_path)],
                           cwd=str(ROOT), stdout=open(WORK / "stdout.log", "w"), stderr=subprocess.STDOUT)
    ipc = MpvIPC(cfg["mpv_socket"])


def stop_app():
    if app and app.poll() is None:
        sh("pkill", "-TERM", "-u", USER, "-f", "tubeless run --config " + str(cfg_path))
        try:
            app.wait(timeout=15)
        except subprocess.TimeoutExpired:
            app.kill()
    sh("pkill", "-x", "mpv")
    time.sleep(1.5)


def get(prop):
    try:
        return ipc.command("get_property", prop)
    except Exception:
        return None


def settings():
    return json.loads((data / "settings.json").read_text())


def progress():
    p = data / "progress.json"
    return json.loads(p.read_text()) if p.exists() else {}


def playing():
    return get("time-pos") is not None and get("core-idle") is False


try:
    start_app()
    check("the app starts and mpv plays", wait_for(playing, 40), f"(pid {app.pid})")
    check("the playlist starts with a static, then a video", (data / "playlist.m3u").read_text().splitlines()[0].endswith("smallStatic.mp4"))
    import pigpio
    pig = pigpio.pi()
    pwm = lambda: pig.get_PWM_dutycycle(18)
    check("backlight lit at 100% (PWM duty 255)", pwm() == 255, f"(duty {pwm()})")

    # --- playback gestures
    wait_for(lambda: get("playlist-pos") is not None and get("playlist-pos") % 2 == 1, 10)
    tap()
    check("tap pauses", wait_for(lambda: get("pause") is True, 5))
    tap()
    check("tap resumes", wait_for(lambda: get("pause") is False, 5))

    p0 = get("playlist-pos")
    swipe(520, 240, 160, 240)
    check("swipe left = next video (via a static)", wait_for(lambda: get("playlist-pos") not in (None, p0), 6), f"({p0} -> {get('playlist-pos')})")
    wait_for(lambda: get("playlist-pos") % 2 == 1 and get("time-pos") is not None, 8)
    p1 = get("playlist-pos")
    swipe(160, 240, 520, 240)
    check("swipe right = previous video", wait_for(lambda: get("playlist-pos") not in (None, p1), 6), f"({p1} -> {get('playlist-pos')})")

    # --- the menu
    wait_for(lambda: get("playlist-pos") % 2 == 1 and get("time-pos") is not None, 8)
    hold()
    shot_menu, shot_plain = str(WORK / "menu.png"), str(WORK / "plain.png")
    ipc.command("screenshot-to-file", shot_menu, "window")
    time.sleep(1.0)
    from PIL import Image
    im = Image.open(shot_menu).convert("RGB")
    yellow = sum(1 for x in range(0, im.width, 3) for y in range(0, im.height, 3)
                 if abs(im.getpixel((x, y))[0] - 254) < 12 and abs(im.getpixel((x, y))[1] - 217) < 14 and im.getpixel((x, y))[2] < 60)
    check("the menu is drawn over the video (yellow outline and highlight)", yellow > 150, f"({yellow} yellow samples, screenshot {im.size})")

    tap_row(3)                                                         # Brightness: 100 -> 25
    check("Brightness row dims the backlight and saves it", wait_for(lambda: settings().get("brightness") == 25, 6) and wait_for(lambda: pwm() == 64, 4), f"(duty {pwm()})")
    tap_row(3); tap_row(3); tap_row(3)                                 # 50, 75, 100
    check("Brightness cycles back to 100%", wait_for(lambda: settings().get("brightness") == 100 and pwm() == 255, 8))

    tap_row(4)                                                         # Settings >
    tap_row(1)                                                         # Shuffle: off
    check("Shuffle row toggles and saves", wait_for(lambda: settings().get("shuffle") is False, 6))
    tap_row(1)                                                         # back on
    check("...and back on", wait_for(lambda: settings().get("shuffle") is True, 6))

    # --- touch calibration wizard, by touch
    tap_row(5)                                                         # Calibrate touch
    time.sleep(1.5)
    for tx, ty in render.calibration_targets(HW):
        tap(tx, ty, settle=0.9)
    check("calibration wizard completes and saves", wait_for(lambda: settings().get("touch_calibrated") is True, 8))
    aff = settings().get("touch_affine")
    # judge the fitted map by where it sends real touch points (the harness nudges each touch by a few px on purpose)
    errs = []
    for vx, vy in [(100, 100), (540, 380), (320, 240), (150, 400), (500, 90)]:
        rx, ry = apply_affine(to_raw, vx, vy)
        gx, gy = apply_affine(aff, rx, ry)
        errs.append(max(abs(gx - vx), abs(gy - vy)))
    check("the fitted touch map sends touches to the right place", aff and max(errs) < 12, f"(worst error {max(errs):.1f} px)")
    wait_for(lambda: get("pause") is not None, 5)
    time.sleep(2.0)

    # --- channels
    from tubeless.library import Library
    chan_before = settings().get("channel") or Library(cfg["videos_dir"]).channels()[0].name
    swipe(320, 400, 320, 110)
    check("swipe up = next channel (saved)", wait_for(lambda: settings().get("channel") not in (None, chan_before), 6), f"({chan_before} -> {settings().get('channel')})")
    check("channel change plays static, then the new channel", wait_for(playing, 15))
    swipe(320, 110, 320, 400)
    check("swipe down = previous channel", wait_for(lambda: settings().get("channel") == chan_before, 6))
    wait_for(playing, 15)

    # --- resume across a restart
    wait_for(lambda: get("playlist-pos") is not None and get("playlist-pos") % 2 == 1 and get("time-pos") is not None, 15)
    time.sleep(5)                                                      # let the channel's own resume-seek finish first
    ipc.command("seek", 100, "absolute")
    time.sleep(1.5)
    tap()                                                              # pausing records where you are
    time.sleep(1.0)
    prog = progress().get(settings().get("channel"), {})
    check("pausing saves the episode and position", prog.get("pos", 0) >= 99, f"({prog})")
    episode = (prog.get("current") or "")
    stop_app()
    start_app()
    check("restart: playing again", wait_for(playing, 40))
    check("restart: resumes the same episode", wait_for(lambda: episode and (get("path") or "").endswith(episode), 15), f"({episode})")
    check("restart: resumes near where you were (rewound 3 s)", wait_for(lambda: 94 <= (get("time-pos") or 0) <= 112, 20), f"(time-pos {get('time-pos')})")

    # --- safe shutdown (the command is a harmless stand-in)
    hold()
    tap_row(4)                                                         # Settings
    swipe(320, 380, 320, 200, settle=1.2)                              # scroll down to "Shut down"
    tap_row(5)
    check("shutdown asks for a second tap first", not (WORK / "shutdown-called").exists())
    tap_row(5)
    check("second tap runs the shutdown command", wait_for(lambda: (WORK / "shutdown-called").exists(), 8))

    check("no errors in the app log", "Traceback" not in (WORK / "app.log").read_text() and "Traceback" not in (WORK / "stdout.log").read_text(),
          "" if "Traceback" not in (WORK / "app.log").read_text() else "(see /tmp/tubeless-e2e/app.log)")
finally:
    print("\n%d/%d passed" % (sum(results), len(results)), flush=True)
    stop_app()
    ui.close()
    for s in was_active:
        sh("systemctl", "start", s)
    print("TV services restored:", ", ".join(was_active) or "(none were running)")
