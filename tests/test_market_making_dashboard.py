from __future__ import annotations

import json
import sqlite3
import time

from arcus_lighter_bot.market_making_dashboard import MarketMakingDashboard


def create_database(path, timestamp_ms: int) -> None:
    database = sqlite3.connect(path)
    database.executescript(
        """
        CREATE TABLE events (id INTEGER PRIMARY KEY, timestamp_ms INTEGER, venue TEXT, message_type TEXT, payload TEXT);
        CREATE TABLE decisions (id INTEGER PRIMARY KEY, timestamp_ms INTEGER, state TEXT, reason TEXT, payload TEXT);
        CREATE TABLE fills (fill_id TEXT PRIMARY KEY, timestamp_ms INTEGER, side TEXT, price REAL, size REAL, fair_at_fill REAL);
        CREATE TABLE markouts (id INTEGER PRIMARY KEY, fill_id TEXT, horizon_ms INTEGER, value_usd REAL, value_bps REAL);
        """
    )
    quote = json.dumps(
        {
            "fair": 100.0,
            "reservation": 100.0,
            "basis_bps": 1.0,
            "volatility_bps": 2.0,
            "toxicity_bps": 0.0,
            "bid": {"price": 99.0, "size": 1.0},
            "ask": {"price": 101.0, "size": 1.0},
        }
    )
    database.execute("INSERT INTO events VALUES(1, ?, 'robinhood', 'update/order_book', '{}')", (timestamp_ms,))
    database.execute("INSERT INTO decisions VALUES(1, ?, 'SHADOW', NULL, ?)", (timestamp_ms, quote))
    database.execute("INSERT INTO fills VALUES('f1', ?, 'BUY', 99, 0.5, 100)", (timestamp_ms,))
    database.execute("INSERT INTO markouts VALUES(1, 'f1', 1000, 0.5, 10)")
    database.commit()
    database.close()


def test_snapshot_projects_shadow_recorder(tmp_path) -> None:
    path = tmp_path / "market.sqlite3"
    create_database(path, int(time.time() * 1000))

    state = MarketMakingDashboard(path).snapshot()

    assert state["running"] is True
    assert state["state"] == "SHADOW"
    assert state["records"] == {"events": 1, "decisions": 1, "fills": 1, "markouts": 1}
    assert state["shadow"]["inventory_base"] == 0.5
    assert state["shadow"]["volume_usd"] == 49.5
    assert state["quote"]["bid"]["price"] == 99.0


def test_stale_recorder_is_reported_as_stopped(tmp_path) -> None:
    path = tmp_path / "market.sqlite3"
    create_database(path, 1)

    state = MarketMakingDashboard(path).snapshot()

    assert state["available"] is True
    assert state["running"] is False
    assert state["state"] == "STOPPED"


def test_missing_database_is_non_fatal(tmp_path) -> None:
    state = MarketMakingDashboard(tmp_path / "missing.sqlite3").snapshot()

    assert state["available"] is False
    assert state["state"] == "UNAVAILABLE"
