from __future__ import annotations

import json
import sqlite3
import time
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote


class MarketMakingDashboard:
    """Read-only projection of the shadow market maker recorder."""

    def __init__(self, database_path: str | Path, stale_after_seconds: float = 10.0) -> None:
        self.database_path = Path(database_path)
        self.stale_after_seconds = stale_after_seconds

    def snapshot(self) -> dict[str, Any]:
        if not self.database_path.exists():
            return self._unavailable("Recorder database not found")
        try:
            with closing(self._connect()) as database:
                counts = self._counts(database)
                event_range = database.execute(
                    "SELECT MIN(timestamp_ms) AS first_ms, MAX(timestamp_ms) AS last_ms FROM events"
                ).fetchone()
                decision = database.execute(
                    "SELECT timestamp_ms, state, reason, payload FROM decisions ORDER BY id DESC LIMIT 1"
                ).fetchone()
                quote_row = database.execute(
                    "SELECT payload FROM decisions WHERE payload IS NOT NULL ORDER BY id DESC LIMIT 1"
                ).fetchone()
                fill_summary = database.execute(
                    """
                    SELECT
                      COALESCE(SUM(price * size), 0) AS volume_usd,
                      COALESCE(SUM(CASE WHEN side = 'BUY' THEN size ELSE -size END), 0) AS inventory_base,
                      COALESCE(SUM(CASE WHEN side = 'BUY' THEN -price * size ELSE price * size END), 0) AS cash_usd,
                      COALESCE(AVG(CASE WHEN side = 'BUY'
                        THEN (fair_at_fill - price) / price * 10000
                        ELSE (price - fair_at_fill) / price * 10000 END), 0) AS spread_capture_bps
                    FROM fills
                    """
                ).fetchone()
                recent_fills = [dict(row) for row in database.execute(
                    """
                    SELECT fill_id, timestamp_ms, side, price, size, fair_at_fill
                    FROM fills ORDER BY timestamp_ms DESC LIMIT 20
                    """
                ).fetchall()]
                markouts = [dict(row) for row in database.execute(
                    """
                    SELECT horizon_ms, COUNT(*) AS samples,
                           AVG(value_bps) AS average_bps, SUM(value_usd) AS total_usd
                    FROM markouts GROUP BY horizon_ms ORDER BY horizon_ms
                    """
                ).fetchall()]
                gaps = int(database.execute(
                    "SELECT COUNT(*) FROM decisions WHERE reason LIKE '%continuity gap%'"
                ).fetchone()[0])
                runtime = (
                    database.execute("SELECT * FROM runtime WHERE singleton=1").fetchone()
                    if self._table_exists(database, "runtime") else None
                )
                quote_counts = {"NEW": 0, "REPLACE": 0, "CANCEL": 0}
                recent_quote_actions: list[dict[str, Any]] = []
                if self._table_exists(database, "quote_actions"):
                    quote_counts.update({
                        str(row["action"]): int(row["total"])
                        for row in database.execute(
                            "SELECT action,COUNT(*) AS total FROM quote_actions GROUP BY action"
                        ).fetchall()
                    })
                    recent_quote_actions = [dict(row) for row in database.execute(
                        """
                        SELECT timestamp_ms,action,side,price,size,queue_ahead,generation,reason
                        FROM quote_actions ORDER BY id DESC LIMIT 20
                        """
                    ).fetchall()]
                session_count = (
                    int(database.execute("SELECT COUNT(*) FROM sessions").fetchone()[0])
                    if self._table_exists(database, "sessions") else 0
                )
        except (sqlite3.Error, OSError, ValueError) as exc:
            return self._unavailable(str(exc))

        quote = json.loads(quote_row["payload"]) if quote_row else None
        last_seen_ms = int(runtime["updated_ms"] if runtime else (event_range["last_ms"] or 0))
        age_seconds = max(0.0, (time.time() * 1000 - last_seen_ms) / 1000) if last_seen_ms else None
        recorded_state = str(runtime["state"] if runtime else (decision["state"] if decision else "BOOT"))
        running = (
            age_seconds is not None
            and age_seconds <= self.stale_after_seconds
            and recorded_state != "STOPPED"
        )
        state = recorded_state if running or recorded_state == "HALTED" else "STOPPED"
        fair = float(runtime["fair_price"] or 0) if runtime else (float(quote["fair"]) if quote else 0.0)
        inventory = float(runtime["inventory_base"]) if runtime else float(fill_summary["inventory_base"])
        cash = float(runtime["cash_usd"]) if runtime else float(fill_summary["cash_usd"])
        equity = float(runtime["equity_usd"]) if runtime else cash + inventory * fair
        quote_attempts = quote_counts["NEW"] + quote_counts["REPLACE"]
        fill_rate = counts["fills"] / quote_attempts * 100 if quote_attempts else 0.0
        return {
            "id": "market-making-lighter",
            "name": "Market Making Lighter",
            "strategy": "Grid adaptativo",
            "mode": "shadow",
            "available": True,
            "running": running,
            "state": state,
            "recorded_state": recorded_state,
            "last_reason": runtime["reason"] if runtime else (decision["reason"] if decision else None),
            "last_seen_at": self._iso(last_seen_ms),
            "stale_for_seconds": age_seconds,
            "recorded_seconds": max(
                0.0, ((event_range["last_ms"] or 0) - (event_range["first_ms"] or 0)) / 1000
            ),
            "symbol": "BTC",
            "quote": quote,
            "records": counts,
            "shadow": {
                "volume_usd": float(fill_summary["volume_usd"]),
                "inventory_base": inventory,
                "cash_usd": cash,
                "equity_usd": equity,
                "spread_capture_bps": float(fill_summary["spread_capture_bps"]),
                "fill_rate_percent": fill_rate,
            },
            "runtime": None if runtime is None else {
                "rh_book_valid": bool(runtime["rh_book_valid"]),
                "reference_book_valid": bool(runtime["reference_book_valid"]),
                "best_bid": runtime["best_bid"],
                "best_ask": runtime["best_ask"],
                "reference_mid": runtime["reference_mid"],
                "fair_price": runtime["fair_price"],
                "basis_bps": runtime["basis_bps"],
                "volatility_bps": runtime["volatility_bps"],
                "toxicity_bps": runtime["toxicity_bps"],
                "open_shadow_quotes": runtime["open_shadow_quotes"],
            },
            "quote_actions": quote_counts,
            "recent_quote_actions": recent_quote_actions,
            "sessions": session_count,
            "continuity_gaps": gaps,
            "recent_fills": recent_fills,
            "markouts": markouts,
            "database_path": str(self.database_path),
            "sends_orders": False,
        }

    def _connect(self) -> sqlite3.Connection:
        uri = f"file:{quote(str(self.database_path.resolve()))}?mode=ro"
        database = sqlite3.connect(uri, uri=True, timeout=1)
        database.row_factory = sqlite3.Row
        return database

    @staticmethod
    def _counts(database: sqlite3.Connection) -> dict[str, int]:
        return {
            table: int(database.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in ("events", "decisions", "fills", "markouts")
        }

    @staticmethod
    def _table_exists(database: sqlite3.Connection, table: str) -> bool:
        return database.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone() is not None

    @staticmethod
    def _iso(timestamp_ms: int) -> str | None:
        if not timestamp_ms:
            return None
        return datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC).isoformat()

    def _unavailable(self, reason: str) -> dict[str, Any]:
        return {
            "id": "market-making-lighter",
            "name": "Market Making Lighter",
            "strategy": "Grid adaptativo",
            "mode": "shadow",
            "available": False,
            "running": False,
            "state": "UNAVAILABLE",
            "last_reason": reason,
            "last_seen_at": None,
            "records": {"events": 0, "decisions": 0, "fills": 0, "markouts": 0},
            "shadow": {
                "volume_usd": 0.0, "inventory_base": 0.0, "cash_usd": 0.0,
                "equity_usd": 0.0, "spread_capture_bps": 0.0, "fill_rate_percent": 0.0,
            },
            "quote": None,
            "runtime": None,
            "quote_actions": {"NEW": 0, "REPLACE": 0, "CANCEL": 0},
            "recent_quote_actions": [],
            "sessions": 0,
            "continuity_gaps": 0,
            "recent_fills": [],
            "markouts": [],
            "database_path": str(self.database_path),
            "sends_orders": False,
        }
