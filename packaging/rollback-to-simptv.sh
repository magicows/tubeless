#!/bin/sh
# Go back to the old single-file SimpTV (touchscreen.py) if you ever need to.
set -e
sudo systemctl disable --now tubeless tubeless-boot 2>/dev/null || true
sudo systemctl enable simptv simptv-boot
sudo systemctl start simptv
echo "back on the old SimpTV: $(systemctl is-active simptv)"
