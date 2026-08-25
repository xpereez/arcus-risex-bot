from __future__ import annotations

import json
import sqlite3
import time

from arcus_lighter_bot.market_making_analytics import MarketMakingAnalytics


def create_database(path, now_ms: int) -> None:
    database = sqlite3.connect(path)
    database.executescript(
        """
        CREATE TABLE events (id INTEGER PRIMARY KEY, timestamp_ms INTEGER, venue TEXT, message_type TEXT, payload TEXT);
        CREATE TABLE decisions (id INTEGER PRIMARY KEY, timestamp_ms INTEGER, state TEXT, reason TEXT, payload TEXT);
        CREATE TABLE fills (fill_id TEXT PRIMARY KEY, timestamp_ms INTEGER, side TEXT, price REAL, size REAL, fair_at_fill REAL);
        CREATE TABLE markouts (id INTEGER PRIMARY KEY, fill_id TEXT, horizon_ms INTEGER, value_usd REAL, value_bps REAL);
        CREATE TABLE quote_actions (id INTEGER PRIMARY KEY, timestamp_ms INTEGER, session_id TEXT, action TEXT, side TEXT, price REAL, size REAL, queue_ahead REAL, generation INTEGER, reason TEXT);
        """
    )
    quote = json.dumps(
        {
            "fair": 100.0,
            "basis_bps": 0.4,
            "volatility_bps": 0.3,
            "bid": {"price": 99.96, "size": 1.0},
            "ask": {"price": 100.04, "size": 1.0},
        }
    )
    database.execute("INSERT INTO events VALUES(1, ?, 'robinhood', 'update', '{}')", (now_ms,))
    database.execute("INSERT INTO decisions VALUES(1, ?, 'SHADOW', NULL, ?)", (now_ms - 10, quote))
    database.execute("INSERT INTO fills VALUES('f1', ?, 'BUY', 99, 0.5, 100)", (now_ms,))
    database.execute("INSERT INTO markouts VALUES(1, 'f1', 5000, 0.25, 5)")
    database.execute("INSERT INTO quote_actions VALUES(1, ?, 's1', 'NEW', 'BUY', 99, 0.5, 0, 1, 'new')", (now_ms,))
    database.commit()
    database.close()


def test_report_builds_hourly_summary_and_regimes(tmp_path) -> None:
    now_ms = int(time.time() * 1000)
    path = tmp_path / "market.sqlite3"
    create_database(path, now_ms)

    report = MarketMakingAnalytics(path, cache_seconds=0).report(24)

    assert report["available"] is True
    assert len(report["hourly"]) == 24
    assert report["summary"]["fills"] == 1
    assert report["summary"]["volume_usd"] == 49.5
    assert report["summary"]["markout_5s_bps"] == 5
    assert report["regimes"]["side"][0]["fills"] == 1
    assert report["regimes"]["volatility"][0]["fills"] == 1
    assert report["regimes"]["basis"][0]["fills"] == 1
    assert report["regimes"]["quoted_spread"][1]["fills"] == 1
    assert report["alerts"][0]["code"] == "EARLY_SAMPLE"


def test_report_supports_recorder_without_quote_lifecycle(tmp_path) -> None:
    now_ms = int(time.time() * 1000)
    path = tmp_path / "market.sqlite3"
    create_database(path, now_ms)
    database = sqlite3.connect(path)
    database.execute("DROP TABLE quote_actions")
    database.commit()
    database.close()

    report = MarketMakingAnalytics(path, cache_seconds=0).report(2)

    assert report["available"] is True
    assert report["summary"]["quote_actions"] == 0


def test_missing_database_returns_unavailable_report(tmp_path) -> None:
    report = MarketMakingAnalytics(tmp_path / "missing.sqlite3").report()

    assert report["available"] is False
    assert report["alerts"][0]["code"] == "UNAVAILABLE"


def test_negative_markout_never_reports_healthy_sample() -> None:
    alerts, _ = MarketMakingAnalytics._signals(
        {
            "fills": 30,
            "quote_actions_per_minute": 1.0,
            "spread_capture_bps": 2.0,
            "markout_5s_bps": -1.0,
        },
        continuity_gaps=0,
    )

    assert [alert["code"] for alert in alerts] == ["NEGATIVE_MARKOUT"]
