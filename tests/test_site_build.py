"""Tests for phase resolution + static-site generation (GitHub Pages path)."""
import json

from oracle import config, db
from oracle.run import GROUPS, _resolve


def test_resolve_phase_and_job_and_unknown():
    assert _resolve("morning") == GROUPS["morning"]
    assert _resolve("afternoon") == GROUPS["afternoon"]
    assert _resolve("fetch_us_close") == ["fetch_us_close"]
    assert _resolve("bogus") is None


def test_site_build_writes_snapshots_and_relative_index(monkeypatch, tmp_path):
    dbp = tmp_path / "state.db"
    monkeypatch.setattr(config, "DB_PATH", dbp)
    db.init_db(dbp)
    db.upsert_market_close("us_close", [
        {"trade_date": "2026-08-02", "symbol": "SOXX", "sector": "semis",
         "close": 100.0, "pct_change": 2.0, "fetched_at": "t"}], db_path=dbp)
    db.insert_news([{"trade_date": "2026-08-02", "source": "r", "category": "chip_export",
                     "headline": "chips rally", "summary": "", "sentiment": 0.6,
                     "fetched_at": "t"}])
    from oracle.analysis.pipeline import run_analysis
    run_analysis("2026-08-02")

    from oracle.site_build import build
    out = build(tmp_path / "site")

    # JSON snapshots for every endpoint
    for name in ("prediction", "report", "llm-usage", "heatmap", "accuracy",
                 "leaderboard", "weights", "reflections", "markets", "history",
                 "news-impact", "health"):
        assert (out / "api" / f"{name}.json").exists(), name
    pred = json.loads((out / "api" / "prediction.json").read_text())
    from oracle.analysis.pipeline import CHINA_SECTORS
    assert len(pred["predictions"]) == len(CHINA_SECTORS)

    # the daily action report snapshot is present and bucketed
    rep = json.loads((out / "api" / "report.json").read_text())
    assert rep["trade_date"] == "2026-08-02"
    assert all(k in rep for k in ("consider", "avoid", "watch"))
    # semis had a strong positive US spillover + chip sentiment -> constructive
    assert "semis" in {m["sector"] for m in rep["consider"]}

    # static-mode flag + relative asset paths (works under the Pages subpath)
    assert "window.CMO_STATIC = true" in (out / "static" / "config.js").read_text()
    html = (out / "index.html").read_text()
    assert 'src="static/app.js"' in html
    assert 'href="/static/' not in html and 'src="/static/' not in html
    assert (out / ".nojekyll").exists()
