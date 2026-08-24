"""Tests for the regime robustness study.

The subtle failure this module is prone to is statistical rather than
computational: pooling overlapping bucket families into one sign test counts
every trade once per family and manufactures significance. There is a test
below pinning that specifically, because the pooled version reported p<0.0001
where the honest figure was p=0.055.
"""
from oracle.research import regimes as rg


def _trades(n, net_by_sector=None, sectors=("a", "b")):
    """Setup rows that all qualify, spread across sectors and dates."""
    rows = []
    for i in range(n):
        sec = sectors[i % len(sectors)]
        body = (net_by_sector or {}).get(sec, 1.0)
        rows.append({"sector": sec,
                     "date": f"202{4 + (i // 120)}-{(i % 12) + 1:02d}-{(i % 28) + 1:02d}",
                     "prior_body": -2.0, "gap": -1.0,
                     "volatility": 1.0 + (i % 9) * 0.3,
                     "close_d0": body})
    return rows


def test_sign_test_is_a_binomial_tail():
    assert rg._sign_test(3, 3) == 0.125
    assert rg._sign_test(0, 3) == 1.0
    assert abs(rg._sign_test(8, 10) - 0.0546875) < 1e-9
    assert rg._sign_test(0, 0) == 1.0


def test_sign_test_is_per_family_and_never_pooled():
    """Pooling overlapping families is the bug this module was fixed for.

    Every trade appears once in each family, so a pooled count treats one
    observation as six. The result must expose per-family agreement, and any
    pooled count must be carried as description without a p-value.
    """
    res = rg.analyse(_trades(240), min_n=5)
    assert set(res["agreement"]) == set(rg.bucket_families(_trades(240)))
    for a in res["agreement"].values():
        assert a["measured"] >= 0
        if a["measured"]:
            assert a["sign_p"] == rg._sign_test(a["positive"], a["measured"])
    # the pooled figure exists for the reader but carries no p-value
    assert "pooled_positive" in res and "pooled_measured" in res
    assert "sign_p" not in res


def test_buckets_within_a_family_partition_the_trades():
    """Disjointness is what makes the per-family sign test legitimate."""
    rows = rg.add_trend(_trades(120))
    for fam, buckets in rg.bucket_families(rows).items():
        seen = []
        for _label, chunk in buckets:
            seen.extend(id(r) for r in chunk)
        assert len(seen) == len(set(seen)), f"{fam} double-counts trades"


def test_trend_label_never_reads_its_own_session():
    """A row's trailing trend must come only from strictly earlier sessions."""
    rows = [{"sector": "a", "date": f"2024-01-{i:02d}", "gap": -1.0,
             "close_d0": 5.0, "prior_body": -2.0} for i in range(1, 6)]
    out = sorted(rg.add_trend(rows), key=lambda r: r["date"])
    assert out[0]["trend"] is None or out[0]["trend"] == 0
    # each later row accumulates only the rows before it
    assert out[1]["trend"] == 4.0          # one prior session of (-1 + 5)
    assert out[2]["trend"] == 8.0


def test_thin_buckets_are_not_counted_as_disagreement():
    """A 3-trade bucket cannot disagree; it can only be unreadable."""
    rows = _trades(60) + [{"sector": "rare", "date": "2024-07-01",
                           "prior_body": -2.0, "gap": -1.0,
                           "volatility": 1.0, "close_d0": -50.0}]
    res = rg.analyse(rows, min_n=25)
    sector_rows = {r["label"]: r for r in res["families"]["sector"]}
    assert sector_rows["sector:rare"]["status"] == "too thin"
    assert res["agreement"]["sector"]["measured"] == 2   # a and b only


def test_narrow_edge_is_reported_as_narrow():
    """A rule that only works in one sector must not read as broad."""
    rows = _trades(240, net_by_sector={"a": 4.0, "b": -1.0})
    res = rg.analyse(rows, min_n=5)
    assert res["agreement"]["sector"]["positive"] == 1
    assert res["worst_net"] < 0
    assert "NARROW" in rg.format_report(res)


def test_broad_edge_is_reported_as_broad():
    res = rg.analyse(_trades(600), min_n=5)
    text = rg.format_report(res)
    assert "BROAD" in text
    assert res["pooled_positive"] == res["pooled_measured"]


def test_report_refuses_to_name_a_best_bucket():
    """Trading the winning slice is the error this table could invite."""
    text = rg.format_report(rg.analyse(_trades(240), min_n=5))
    assert "Do NOT trade the best bucket" in text
    assert "Not investment advice" in text


def test_no_trades_does_not_raise():
    assert rg.analyse([])["status"] == "no_trades"
    assert "no_trades" in rg.format_report({"status": "no_trades"})
