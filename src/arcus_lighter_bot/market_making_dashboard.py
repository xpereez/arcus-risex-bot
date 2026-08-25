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
                      COALESCE(SUM(CASE WHEN side = 'BUY' THEN -price * size ELSE price * size END), 0) AS cash_usd
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
        except (sqlite3.Error, OSError, ValueError) as exc:
            return self._unavailable(str(exc))

        quote = json.loads(quote_row["payload"]) if quote_row else None
        last_seen_ms = int(event_range["last_ms"] or 0)
        age_seconds = max(0.0, (time.time() * 1000 - last_seen_ms) / 1000) if last_seen_ms else None
        running = age_seconds is not None and age_seconds <= self.stale_after_seconds
        recorded_state = str(decision["state"]) if decision else "BOOT"
        state = recorded_state if running or recorded_state == "HALTED" else "STOPPED"
        fair = float(quote["fair"]) if quote else 0.0
        inventory = float(fill_summary["inventory_base"])
        cash = float(fill_summary["cash_usd"])
        equity = cash + inventory * fair
        return {
            "id": "market-making-lighter",
            "name": "Market Making Lighter",
            "strategy": "Grid adaptativo",
            "mode": "shadow",
            "available": True,
            "running": running,
            "state": state,
            "recorded_state": recorded_state,
            "last_reason": decision["reason"] if decision else None,
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
            },
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
            "shadow": {"volume_usd": 0.0, "inventory_base": 0.0, "cash_usd": 0.0, "equity_usd": 0.0},
            "quote": None,
            "continuity_gaps": 0,
            "recent_fills": [],
            "markouts": [],
            "database_path": str(self.database_path),
            "sends_orders": False,
        }
