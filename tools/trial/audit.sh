#!/bin/sh
# Read-only report on a Raspberry Pi: is it a good home for Tubeless? Changes nothing.
#   ssh <pi> sh < tools/trial/audit.sh        (or copy it over and run it)
say() { printf '\n== %s ==\n' "$1"; }
have() { command -v "$1" >/dev/null 2>&1 && echo "yes ($(command -v "$1"))" || echo "no"; }

say "Board and OS"
tr -d '\0' < /proc/device-tree/model 2>/dev/null; echo
grep -E '^(PRETTY_NAME|VERSION_ID)=' /etc/os-release
uname -mr
getconf LONG_BIT | sed 's/^/userland bits: /'
cat /etc/rpi-issue 2>/dev/null | head -2
free -m | awk 'NR==2{print "RAM total/used MB: " $2 "/" $3}'
df -h / | awk 'NR==2{print "root: " $2 " total, " $4 " free"}'

say "Where the boot files live"
for d in /boot/firmware /boot; do [ -f "$d/config.txt" ] && echo "config.txt: $d/config.txt"; [ -f "$d/cmdline.txt" ] && echo "cmdline.txt: $d/cmdline.txt"; done
for d in /boot/firmware/overlays /boot/overlays; do [ -d "$d" ] && echo "overlays dir: $d ($(ls "$d" | wc -l) files)"; done
ls /boot/firmware/overlays /boot/overlays 2>/dev/null | grep -iE 'waveshare|2inch8|DPI-28|dpi24|vc4-kms-dpi|goodix|audremap' | sort -u | sed 's/^/  have overlay: /'

say "Versions apt would install (nothing is installed)"
for p in mpv python3 python3-pil python3-numpy python3-evdev python3-psutil pigpio python3-pigpio mediainfo ffmpeg bluealsa bluez network-manager; do
  v=$(apt-cache policy "$p" 2>/dev/null | awk '/Candidate:/{print $2}')
  printf '  %-18s %s\n' "$p" "${v:-(not in apt)}"
done

say "Tools the old guide / our scripts use"
echo "raspi-gpio: $(have raspi-gpio)"
echo "pinctrl:    $(have pinctrl)"
echo "vcgencmd:   $(have vcgencmd)"
echo "python3:    $(python3 --version 2>&1)"

say "Network and radios"
for u in NetworkManager dhcpcd wpa_supplicant systemd-networkd bluetooth hciuart avahi-daemon; do
  printf '  %-18s %s\n' "$u" "$(systemctl is-active $u 2>&1 | head -1) / $(systemctl is-enabled $u 2>&1 | head -1)"
done
command -v rfkill >/dev/null && rfkill list 2>/dev/null | sed 's/^/  /'
iw reg get 2>/dev/null | head -3 | sed 's/^/  /'
ip -br addr show wlan0 2>/dev/null | sed 's/^/  /'

say "Display driver state right now"
ls /sys/class/drm 2>/dev/null | tr '\n' ' '; echo
ls /sys/class/graphics 2>/dev/null | tr '\n' ' '; echo
dmesg 2>/dev/null | grep -iE 'vc4-drm|panel|goodix|dpi' | tail -5 | cut -c1-150
grep -hvE '^\s*(#|$)' /boot/firmware/config.txt /boot/config.txt 2>/dev/null | tail -20 | sed 's/^/  config.txt: /'

say "Done (nothing was changed)"
