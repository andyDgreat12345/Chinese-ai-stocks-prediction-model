#!/usr/bin/env bash
# One-command setup on a fresh Ubuntu/Debian VPS. Run from the repo root:
#
#     bash deploy/bootstrap.sh [days]        # days of history to backfill (default 365)
#
# Creates a virtualenv, installs deps, initializes the DB, seeds the macro
# calendar, backfills real US+China history, and runs the first REAL backtest.
# Afterwards, install the always-on services with:  sudo bash deploy/install.sh
set -euo pipefail

DAYS="${1:-365}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

echo "== [1/5] Python virtualenv =="
python3 -m venv .venv
.venv/bin/pip install --upgrade pip >/dev/null
.venv/bin/pip install -r requirements.txt

echo "== [2/5] Initialize database =="
.venv/bin/python -m oracle.db

echo "== [3/5] Seed macro calendar (if absent) =="
mkdir -p data
if [ ! -f data/macro_events.json ] && [ -f examples/macro_events.sample.json ]; then
  cp examples/macro_events.sample.json data/macro_events.json
  echo "seeded data/macro_events.json — edit it to add upcoming Fed/CPI/PMI dates"
fi

echo "== [4/5] Backfill ~$DAYS days of real US + China history =="
echo "   (fails soft per symbol — a blocked feed just leaves gaps, doesn't abort)"
.venv/bin/python -m oracle.backfill "$DAYS" || echo "   backfill reported issues — inspect the log above"

echo "== [5/5] First REAL backtest — does the model beat the baselines? =="
.venv/bin/python -m oracle.backtest || true

cat <<'NEXT'

────────────────────────────────────────────────────────────────────
Setup done. Read the backtest above HONESTLY:
  * the model earns trust only if its bet-accuracy AND Sharpe beat the
    baselines, with a small p-value. If it doesn't, that's a real finding —
    not a reason to trust it with money.

To run it automatically every day, install the two services:
    sudo bash deploy/install.sh
Then check them with:
    systemctl status oracle-scheduler oracle-dashboard
    journalctl -u oracle-scheduler -f
────────────────────────────────────────────────────────────────────
NEXT
