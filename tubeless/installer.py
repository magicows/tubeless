"""
The boot-file part of installing Tubeless: edits /boot/firmware/config.txt and cmdline.txt (and puts the
display overlays in place) so the Waveshare 2.8" DPI screen, its touch controller and the PWM speaker work.

It only ever *adds or fixes* what Tubeless needs, never removes unrelated lines, shows a diff before doing
anything, is safe to run repeatedly, and keeps a backup of every file it changes (once, the first time).

    python3 -m tubeless.installer boot                   show the diff of what would change (changes nothing)
    sudo python3 -m tubeless.installer boot --apply      make the changes
        --boot-dir /boot/firmware       (default: /boot/firmware if it exists, else /boot)
        --overlays-from DIR             copy the .dtbo files in DIR into <boot-dir>/overlays

Only the standard library is used, so it runs on a fresh Pi OS before anything else is installed.
"""
from __future__ import annotations

import argparse
import difflib
import shutil
import sys
import time
from pathlib import Path
from typing import List, Tuple

BEGIN = "# >>> tubeless (managed block: edit outside it) >>>"
END = "# <<< tubeless <<<"

# Waveshare's recommended set for this display (their wiki lists all five for Bullseye and Bookworm), plus the
# audio overlay that moves the PWM speaker to GPIO 18/19. `dtoverlay=vc4-kms-v3d` and `dtparam=audio=on` are
# Pi OS defaults and are added only if missing.
CONFIG_LINES = [
    "dtoverlay=vc4-kms-v3d",
    "dtoverlay=waveshare-28dpi-3b-4b",
    "dtoverlay=waveshare-28dpi-3b",
    "dtoverlay=waveshare-28dpi-4b",
    "dtoverlay=waveshare-touch-28dpi",
    "dtoverlay=vc4-kms-dpi-2inch8",
    "dtparam=audio=on",
    "dtoverlay=audremap,enable_jack,pins_18_19",
]

# Kernel command line: rotate the panel to landscape, keep the screen awake, hide the logo and the blinking cursor,
# and send the console to tty3 so the text console never shows on the panel.
VIDEO_TOKEN = "video=DPI-1:480x640@60,rotate=90"
CMDLINE_TOKENS = [VIDEO_TOKEN, "consoleblank=0", "logo.nologo", "vt.global_cursor_default=0"]


def _is_overlay_line(line: str) -> bool:
    return line.strip().startswith("dtoverlay=")


def apply_config_txt(text: str) -> str:
    """config.txt with everything Tubeless needs present exactly once (in a managed block at the end)."""
    lines = text.splitlines()
    if BEGIN in lines and END in lines:                              # drop a previous managed block; rebuilt below
        a, b = lines.index(BEGIN), lines.index(END)
        lines = lines[:a] + lines[b + 1:]
    while lines and not lines[-1].strip():
        lines.pop()
    present = {l.strip() for l in lines if l.strip() and not l.strip().startswith("#")}
    missing = [l for l in CONFIG_LINES if l not in present]
    if not missing:
        return "\n".join(lines) + "\n"
    # the block must be in the [all] section; a section header left open above would capture it
    sections = [l for l in lines if l.strip().startswith("[") and l.strip().endswith("]")]
    out = lines + [""]
    if sections and sections[-1].strip() != "[all]":
        out.append("[all]")
    out += [BEGIN] + missing + [END]
    return "\n".join(out) + "\n"


def apply_cmdline(text: str) -> str:
    """cmdline.txt (one line) with the Tubeless tokens in place and the text console moved off the panel."""
    out: List[str] = []
    have_video = False
    for t in text.split():
        if t.startswith("video=DPI-1:"):
            if not have_video:                                        # fix it where it is (never churn a correct line)
                out.append(VIDEO_TOKEN)
                have_video = True
            continue
        out.append("console=tty3" if t == "console=tty1" else t)
    for t in CMDLINE_TOKENS:
        if t not in out:
            out.append(t)
    return " ".join(out) + "\n"


def diff(name: str, before: str, after: str) -> str:
    return "".join(difflib.unified_diff(before.splitlines(True), after.splitlines(True), f"{name} (now)", f"{name} (after)"))


def default_boot_dir() -> Path:
    return Path("/boot/firmware") if Path("/boot/firmware/config.txt").exists() else Path("/boot")


def plan_boot(boot_dir: Path) -> List[Tuple[Path, str, str]]:
    """[(file, current text, new text)] for the files that would change."""
    changes = []
    for name, fn in (("config.txt", apply_config_txt), ("cmdline.txt", apply_cmdline)):
        path = boot_dir / name
        before = path.read_text()
        after = fn(before)
        if after != before:
            changes.append((path, before, after))
    return changes


def backup_once(path: Path) -> Path:
    backup = path.with_name(path.name + ".before-tubeless")
    if not backup.exists():
        shutil.copy2(path, backup)
    return backup


def copy_overlays(source: Path, boot_dir: Path, apply: bool) -> List[str]:
    dest = boot_dir / "overlays"
    done = []
    for f in sorted(source.glob("*.dtbo")):
        if f.name.startswith("._"):                                   # macOS resource-fork junk
            continue
        target = dest / f.name
        if target.exists() and target.read_bytes() == f.read_bytes():
            continue
        if apply:
            if target.exists():
                backup_once(target)
            shutil.copy2(f, target)
        done.append(f.name)
    return done


def cmd_boot(args) -> int:
    boot = Path(args.boot_dir) if args.boot_dir else default_boot_dir()
    if not (boot / "config.txt").exists():
        print(f"no config.txt in {boot}", file=sys.stderr)
        return 2
    print(f"boot files: {boot}")
    if args.overlays_from:
        names = copy_overlays(Path(args.overlays_from), boot, args.apply)
        print(("copied" if args.apply else "would copy") + f" overlays: {', '.join(names) or 'none needed (already in place)'}")
    changes = plan_boot(boot)
    if not changes:
        print("config.txt and cmdline.txt already have everything Tubeless needs.")
        return 0
    for path, before, after in changes:
        print()
        print(diff(path.name, before, after))
    if not args.apply:
        print("(dry run: nothing was changed. Add --apply, as root, to make these changes.)")
        return 0
    for path, before, after in changes:
        backup = backup_once(path)
        path.write_text(after)
        print(f"changed {path} (original kept as {backup.name})")
    print("a reboot is needed for the display, touch and audio changes to take effect.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="tubeless.installer")
    sub = ap.add_subparsers(dest="command", required=True)
    p = sub.add_parser("boot", help="edit config.txt / cmdline.txt and place the display overlays")
    p.add_argument("--boot-dir")
    p.add_argument("--overlays-from")
    p.add_argument("--apply", action="store_true")
    p.set_defaults(func=cmd_boot)
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
