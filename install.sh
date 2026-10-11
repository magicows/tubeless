#!/bin/sh
# Install Tubeless on a fresh Raspberry Pi OS Lite (Pi Zero 2 W + the Waveshare 2.8" DPI screen).
#
#     sudo sh install.sh --name "Living Room"          install everything
#     sudo sh install.sh --dry-run                     show what it would do, change nothing
#
# See `python3 -m tubeless.installer all --help` for the options. Safe to run again.
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
if [ "$(id -u)" != 0 ]; then
  echo "Installing needs root (packages, boot files, services): running again with sudo..."
  exec sudo sh "$0" "$@"
fi
command -v python3 >/dev/null 2>&1 || { echo "python3 is required (it is on Raspberry Pi OS by default)"; exit 1; }
cd "$HERE"
exec python3 -m tubeless.installer all --source "$HERE" "$@"
