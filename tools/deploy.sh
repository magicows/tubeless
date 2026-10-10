#!/bin/sh
# Copy this checkout to a TV and (re)start it.     tools/deploy.sh simpsons
# First deploy to a TV installs the systemd units and switches off an older SimpTV install; after that it just
# updates the code and restarts. Your videos and settings are never touched.
set -e
HOST="${1:?usage: tools/deploy.sh <ssh-host>}"
cd "$(dirname "$0")/.."
APPDIR='$HOME/tubeless'
echo "==> copying code to $HOST"
rsync -a --delete --exclude .venv --exclude snapshots --exclude .git --exclude legacy --exclude sim_data \
      --exclude __pycache__ --exclude .pytest_cache --exclude tests --exclude '*.pyc' ./ "$HOST:tubeless/"
echo "==> installing / refreshing the services"
ssh -o BatchMode=yes "$HOST" 'set -e
  APPDIR="$HOME/tubeless"; NAME=$(python3 -c "import json,os; print(json.load(open(os.path.expanduser(\"~/.config/tubeless/config.json\"))).get(\"name\",\"Tubeless\"))" 2>/dev/null || echo Tubeless)
  for unit in tubeless tubeless-boot; do
    sed -e "s|@USER@|$USER|g" -e "s|@APPDIR@|$APPDIR|g" -e "s|@NAME@|$NAME|g" "$APPDIR/packaging/$unit.service.in" | sudo tee /etc/systemd/system/$unit.service >/dev/null
  done
  sudo systemctl daemon-reload
  # an older SimpTV install (touchscreen.py) is switched off but left in place, so rolling back is easy
  for old in simptv simptv-boot; do
    if systemctl list-unit-files | grep -q "^$old.service"; then sudo systemctl disable --now $old 2>/dev/null || true; fi
  done
  sudo systemctl enable tubeless tubeless-boot 2>&1 | tail -1
  sudo systemctl restart tubeless
  sleep 12; systemctl is-active tubeless'
