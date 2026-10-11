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

echo "==> preflight: can this SD card be trusted?"
# A bad or counterfeit card silently returns different data than was written, which shows up later as "corrupt
# package" errors and a Pi that falls over at random. Write, flush, read back and compare before installing anything.
for round in 1 2; do
  head -c 64M /dev/urandom > /var/tmp/.tubeless-sdtest
  before=$(sha256sum /var/tmp/.tubeless-sdtest | cut -d' ' -f1)
  sync; echo 3 > /proc/sys/vm/drop_caches
  after=$(sha256sum /var/tmp/.tubeless-sdtest | cut -d' ' -f1)
  rm -f /var/tmp/.tubeless-sdtest
  if [ "$before" != "$after" ]; then
    echo "!! The SD card returned different data than was written (round $round)."
    echo "!! Do not continue on this card: re-seat it and try again, or replace it (a genuine SanDisk/Samsung card)."
    echo "!! Counterfeit cards can be checked on a computer with f3 (https://fight-flash-fraud.readthedocs.io/)."
    exit 1
  fi
done
echo "ok: written data reads back correctly"
if command -v vcgencmd >/dev/null 2>&1; then echo "power flags: $(vcgencmd get_throttled) (0x0 = fine)"; fi

echo "==> packages"
# --no-install-recommends matters: on a Lite image mpv's "recommended" extras pull in ~280 desktop packages
# (X11 utilities, speech models ...), which also runs a 512 MB Pi out of memory while apt resolves them.
apt-get -o Acquire::Retries=5 update
apt-get -o Acquire::Retries=5 install -y --no-install-recommends \
    mpv python3-evdev python3-psutil python3-pil python3-numpy pigpio python3-pigpio

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
