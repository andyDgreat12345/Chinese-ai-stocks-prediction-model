# Deploying China Market Oracle

Stand the system up on an always-on host (a small VPS) so the overnight jobs run
even when your laptop is off, then supervise the first live daily cycle.

> The US close is ~04:00 CST, so the machine must be awake overnight — that's the
> whole reason for a VPS over a laptop (spec §7).

---

## 0. Prerequisites

- A Linux VPS (Ubuntu/Debian shown here), 1 vCPU / 1 GB RAM is plenty.
- Python 3.11+ and `git`.
- SSH access.

```bash
sudo apt update && sudo apt install -y python3 python3-venv git
```

---

## 1. Get the code

```bash
cd ~
git clone <your-repo-url> oracle && cd oracle
# use the merged main branch (or the feature branch until it's merged):
git checkout main
```

## 2. Python environment

```bash
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m oracle.db      # create data/oracle.db
```

## 3. Configure

**Macro calendar (optional but recommended).** Seed it, then keep it current:

```bash
mkdir -p data
cp examples/macro_events.sample.json data/macro_events.json
# edit data/macro_events.json — add upcoming Fed/CPI/PMI dates
```

**Verify the data codes on first run** (see step 4). In particular the China
sector-ETF codes in `oracle/config.py` (`CHINA_SECTOR_ETFS`) — ingestion fails
soft, so a wrong code simply leaves that sector unscored until you fix it.

**Timezone:** you do *not* need to set the host timezone — job timing uses
APScheduler's `Asia/Shanghai` clock internally.

---

## 4. Supervised first run (do this before installing services)

Run the daily jobs by hand and watch the output. This is where you confirm the
live feeds actually work — everything up to now was verified on fixtures.

```bash
# individual jobs:
.venv/bin/python -m oracle.run fetch_us_close
.venv/bin/python -m oracle.run fetch_world_news
.venv/bin/python -m oracle.run run_analysis

# or the whole sequence in order:
.venv/bin/python -m oracle.run all
```

What to check:
- `fetch_us_close` wrote rows (yfinance reachable).
- `fetch_china_close` wrote both indices **and** sector ETFs — if a sector ETF
  is missing, fix its code in `config.CHINA_SECTOR_ETFS`.
- `run_analysis` wrote 5 predictions.
- `reflect_and_update` scored predictions and wrote a reflection (needs a China
  close to compare against).

Then start the dashboard locally and eyeball it:

```bash
.venv/bin/python app.py        # http://localhost:8000  (Ctrl-C to stop)
```

---

## 5. Install as services

The scheduler and dashboard run as two `systemd` services so they auto-start on
boot and restart on failure.

```bash
sudo bash deploy/install.sh
```

This fills the unit templates in `deploy/` with your checkout path, user, and
venv, installs them to `/etc/systemd/system/`, and enables + starts both.

Check them:

```bash
systemctl status oracle-scheduler oracle-dashboard
journalctl -u oracle-scheduler -f      # follow the scheduler log
```

---

## 6. Access the dashboard securely

The dashboard has **no authentication**, so the service binds to `127.0.0.1`
only. Two safe ways to reach it:

**A) SSH tunnel (simplest).** From your laptop:

```bash
ssh -N -L 8000:localhost:8000 you@your-vps
# then open http://localhost:8000 on your laptop
```

**B) Reverse proxy with auth.** Put nginx in front with HTTP basic auth + TLS if
you want a persistent URL. Do **not** bind the service to `0.0.0.0` without
adding auth first.

---

## 7. Operating it

```bash
# logs
journalctl -u oracle-scheduler -n 100 --no-pager
journalctl -u oracle-dashboard -n 100 --no-pager

# restart after a config change
sudo systemctl restart oracle-scheduler

# update to new code
cd ~/oracle && git pull && .venv/bin/pip install -r requirements.txt
sudo systemctl restart oracle-scheduler oracle-dashboard
```

Back up `data/oracle.db` and `data/reflection_log.jsonl` periodically — the
reflection log is the project's most valuable artifact (spec §4b-iii).

---

## 8. Day-one checklist

Over the first full cycle (CST), confirm each job fired in the scheduler log:

| Time | Job | Expect |
|---|---|---|
| 04:15 | `fetch_us_close` | US rows written |
| 04:30 | `fetch_world_news` | headlines + macro calendar loaded |
| 05:00 | `run_analysis` | 5 predictions |
| 09:15 | `pre_open_refresh` | confidence adjusted iff breaking news |
| 15:05 | `fetch_china_close` | indices + sector ETFs |
| 15:15 | `reflect_and_update` | predictions scored, reflection written |

After 15:15 the dashboard's Accuracy and Reflection panels should populate. The
US→China leaderboard stays empty until ~30 trading days accumulate — that's the
minimum-sample guard doing its job, not a bug.

---

## Troubleshooting

- **A sector always unscored** → its ETF code in `config.CHINA_SECTOR_ETFS` is
  wrong/renamed; check `fetch_china_close` logs and fix the code.
- **No news** → some RSS feeds geo-block or rate-limit; check the feed URLs in
  `config.NEWS_FEEDS`. Jobs fail soft, so this won't crash anything.
- **Jobs not firing** → `systemctl status oracle-scheduler`; the SQLite jobstore
  persists across restarts, so a restart won't lose the schedule.
