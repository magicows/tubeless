#!/bin/sh
# Trial setup for a fresh Raspberry Pi OS Lite on a Pi Zero 2 W with the Waveshare 2.8" DPI screen. Run as root:
#
#     sudo sh tools/trial/setup.sh [dir-with-the-waveshare-.dtbo-files]
#
# It installs the packages Tubeless needs, edits config.txt / cmdline.txt (showing a diff, keeping backups, safe to
# run again), masks the login prompt on the panel and enables pigpiod. It does NOT reboot: do that yourself after.
set -e
[ "$(id -u)" = 0 ] || { echo "run this with sudo"; exit 1; }
HERE="$(cd "$(dirname "$0")/../.." && pwd)"
OVERLAYS="$1"

echo "==> packages"
apt-get update
apt-get install -y mpv python3-evdev python3-psutil python3-pil python3-numpy pigpio python3-pigpio

echo "==> boot files and display overlays"
if [ -n "$OVERLAYS" ]; then
  PYTHONPATH="$HERE" python3 -m tubeless.installer boot --overlays-from "$OVERLAYS" --apply
else
  PYTHONPATH="$HERE" python3 -m tubeless.installer boot --apply
fi

echo "==> console and services"
systemctl mask getty@tty1.service
if systemctl list-unit-files | grep -q '^pigpiod.service'; then
  systemctl enable pigpiod
else
  echo "!! no pigpiod.service was installed by the pigpio package on this OS: the backlight will be on/off only"
fi

echo
echo "Done. Reboot now (sudo reboot) so the display, touch and audio changes take effect."
