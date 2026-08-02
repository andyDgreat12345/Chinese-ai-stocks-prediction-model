#!/usr/bin/env bash
# Install China Market Oracle as two systemd services (scheduler + dashboard).
# Run from the repo root on the target host:  sudo bash deploy/install.sh
#
# Fills the unit templates with this checkout's path, the invoking user, and the
# virtualenv, then enables + starts both services. Idempotent — re-run to update.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_USER="${SUDO_USER:-$(id -un)}"
VENV="${VENV:-$APP_DIR/.venv}"

if [[ ! -x "$VENV/bin/python" ]]; then
  echo "ERROR: no virtualenv at $VENV" >&2
  echo "Create it first:  python3 -m venv $VENV && $VENV/bin/pip install -r requirements.txt" >&2
  exit 1
fi

echo "App dir : $APP_DIR"
echo "User    : $RUN_USER"
echo "Venv    : $VENV"

for unit in oracle-scheduler oracle-dashboard; do
  src="$APP_DIR/deploy/$unit.service"
  dst="/etc/systemd/system/$unit.service"
  sed -e "s#__APP_DIR__#$APP_DIR#g" \
      -e "s#__USER__#$RUN_USER#g" \
      -e "s#__VENV__#$VENV#g" \
      "$src" | sudo tee "$dst" >/dev/null
  echo "installed $dst"
done

sudo systemctl daemon-reload
sudo systemctl enable --now oracle-scheduler.service oracle-dashboard.service

echo
echo "Done. Check status with:"
echo "  systemctl status oracle-scheduler oracle-dashboard"
echo "  journalctl -u oracle-scheduler -f"
