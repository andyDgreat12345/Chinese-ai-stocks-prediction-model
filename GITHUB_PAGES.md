# Zero-server deployment (GitHub Actions + Pages)

Run China Market Oracle **without any server** — GitHub runs the daily jobs on a
schedule, persists the growing database on a branch, and publishes the dashboard
as a static site on GitHub Pages. Everything below can be done from an iPad
browser.

## How it works

```
 GitHub Actions (cron)                         GitHub Pages
 ┌───────────────────────┐   commit state      ┌──────────────────┐
 │ 05:00 CST  morning    │ ───► oracle-state    │ static dashboard │
 │   fetch US + news      │      branch (SQLite  │ reads api/*.json │◄─ your iPad
 │   run analysis         │      + reflection    │ (Safari)         │   (Safari)
 │ 15:15 CST  afternoon  │      log)            └──────────────────┘
 │   fetch China close    │ ───► build site  ───────────▲
 │   score + reflect      │      (JSON snapshots)        │ deploy
 └───────────────────────┘ ─────────────────────────────┘
```

- **State** (the accumulating SQLite DB + reflection log) lives on the
  `oracle-state` branch, restored at the start of each run and pushed back at
  the end — so month 6 builds on month 1.
- **The dashboard** is the same terminal UI, built into static `api/*.json`
  snapshots (`oracle/site_build.py`) instead of a live backend.
- **Cost:** $0. Public repos get unlimited Actions minutes; this uses ~2 short
  runs/day.

## One-time setup (from the iPad browser)

1. **Merge the PR to your default branch.** Scheduled workflows only run from the
   default branch, so the schedule won't fire until this is on `main`.

2. **Enable Pages with the Actions source.**
   Repo → **Settings → Pages → Build and deployment → Source: GitHub Actions**.

3. **Give Actions write permission.**
   Repo → **Settings → Actions → General → Workflow permissions →
   "Read and write permissions"** → Save. (Lets the workflow push the
   `oracle-state` branch.)

4. **Kick off the first run manually.**
   Repo → **Actions → "Oracle" → Run workflow** → phase **all** → Run.
   - The first run creates the `oracle-state` branch and does the first Pages
     deploy. With no market data seeded yet, most panels start empty — that's
     expected; the scheduled runs fill them in.

5. **Open your dashboard.** After the first run finishes, the URL is
   **`https://<your-username>.github.io/<repo-name>/`** (also shown on the
   Actions run's "Deploy to Pages" step). Bookmark it on your iPad home screen.

That's it — from here the two daily runs (05:00 and 15:15 CST) keep it current.

## Operating it

- **Watch a run:** Actions tab → latest "Oracle" run → expand steps. The
  "Run jobs" step logs what each job wrote.
- **Run on demand:** Actions → Oracle → Run workflow (pick `morning`,
  `afternoon`, or `all`).
- **Edit the macro calendar:** the calendar is seeded from
  `examples/macro_events.sample.json` into the `oracle-state` branch on first
  run. To update it, edit `macro_events.json` on the **`oracle-state`** branch.
- **Reset everything:** delete the `oracle-state` branch; the next run starts
  fresh.

## Optional: enable the LLM reflection

By default the daily reflection is deterministic (rule-based) and needs no keys.
To upgrade it to an LLM-written reflection (spec §4b-iii):

1. Repo → **Settings → Secrets and variables → Actions**.
2. Add a **variable** `ORACLE_LLM_PROVIDER` = `claude` or `deepseek` (optionally
   `ORACLE_LLM_MODEL`, e.g. `claude-sonnet-5`).
3. Add the matching **secret**: `ANTHROPIC_API_KEY` or `DEEPSEEK_API_KEY`.

The workflow already passes these through. If the key is missing or the call
fails, it silently falls back to the rule-based reflection — nothing breaks.

## Trade-offs vs. a VPS

- **Timing** isn't second-precise — Actions cron can lag a few minutes under
  load. Fine for a daily batch.
- **`akshare`** pulls from Chinese endpoints, which may be slow or unreachable
  from GitHub's US-based runners. Jobs fail soft, so a missed China fetch just
  leaves that day's actuals unscored rather than breaking the run. If this
  proves flaky, the VPS option (see `DEPLOY.md`) runs from wherever you host it.
- **`pre_open_refresh`** (the 09:15 breaking-news confidence nudge) is omitted
  from the two-phase cadence to keep it simple; add a third `cron` if you want
  it.

## Privacy

A GitHub Pages site is **public**. This dashboard shows only your model's
predictions + accuracy (no credentials, no personal data), so that's usually
fine — but if you'd rather keep it private, use the VPS + Tailscale route in
`DEPLOY.md` instead.
