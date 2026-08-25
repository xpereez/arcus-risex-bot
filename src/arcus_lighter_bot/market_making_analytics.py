from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict
from contextlib import closing
from datetime import UTC, datetime
import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote


HOUR_MS = 3_600_000


class MarketMakingAnalytics:
    """Read-only historical analysis of the shadow recorder."""

    def __init__(self, database_path: str | Path, cache_seconds: float = 60.0) -> None:
        self.database_path = Path(database_path)
        self.cache_seconds = cache_seconds
        self._cache: dict[int, tuple[float, dict[str, Any]]] = {}

    def report(self, hours: int = 72) -> dict[str, Any]:
        hours = min(168, max(1, int(hours)))
        cached = self._cache.get(hours)
        if cached and time.monotonic() - cached[0] < self.cache_seconds:
            return cached[1]
        if not self.database_path.exists():
            return self._unavailable("Recorder database not found", hours)

        now_ms = int(time.time() * 1000)
        first_hour_ms = (now_ms // HOUR_MS - hours + 1) * HOUR_MS
        try:
            with closing(self._connect()) as database:
                hourly = self._hourly(database, first_hour_ms, hours)
                fills = self._fills_with_context(database, first_hour_ms)
                quote_actions, quote_action_minutes = self._quote_action_stats(
                    database, first_hour_ms
                )
                continuity_gaps = int(
                    database.execute(
                        "SELECT COUNT(*) FROM decisions WHERE timestamp_ms>=? AND reason LIKE '%continuity gap%'",
                        (first_hour_ms,),
                    ).fetchone()[0]
                )
        except (sqlite3.Error, OSError, ValueError, json.JSONDecodeError) as exc:
            return self._unavailable(str(exc), hours)

        report = self._build_report(
            hours=hours,
            now_ms=now_ms,
            first_hour_ms=first_hour_ms,
            hourly=hourly,
            fills=fills,
            quote_actions=quote_actions,
            quote_action_minutes=quote_action_minutes,
            continuity_gaps=continuity_gaps,
        )
        self._cache[hours] = (time.monotonic(), report)
        return report

    def _hourly(
        self, database: sqlite3.Connection, first_hour_ms: int, hours: int
    ) -> list[dict[str, Any]]:
        buckets = {
            first_hour_ms + index * HOUR_MS: {
                "timestamp_ms": first_hour_ms + index * HOUR_MS,
                "events": 0,
                "decisions": 0,
                "quote_actions": 0,
                "fills": 0,
                "volume_usd": 0.0,
                "spread_capture_bps": None,
                "markout_5s_bps": None,
                "avg_volatility_bps": None,
                "avg_abs_basis_bps": None,
            }
            for index in range(hours)
        }

        self._merge_counts(database, buckets, "events", first_hour_ms, "events")
        self._merge_counts(database, buckets, "decisions", first_hour_ms, "decisions")
        if self._table_exists(database, "quote_actions"):
            self._merge_counts(database, buckets, "quote_actions", first_hour_ms, "quote_actions")

        for row in database.execute(
            """
            SELECT CAST(timestamp_ms / ? AS INTEGER) * ? AS hour_ms,
                   COUNT(*) AS fills, SUM(price * size) AS volume_usd,
                   AVG(CASE WHEN side='BUY' THEN (fair_at_fill-price)/price*10000
                            ELSE (price-fair_at_fill)/price*10000 END) AS spread_capture_bps
            FROM fills WHERE timestamp_ms>=? GROUP BY hour_ms
            """,
            (HOUR_MS, HOUR_MS, first_hour_ms),
        ):
            bucket = buckets.get(int(row["hour_ms"]))
            if bucket:
                bucket.update(
                    fills=int(row["fills"]),
                    volume_usd=float(row["volume_usd"] or 0),
                    spread_capture_bps=self._optional_float(row["spread_capture_bps"]),
                )

        for row in database.execute(
            """
            SELECT CAST(f.timestamp_ms / ? AS INTEGER) * ? AS hour_ms,
                   AVG(m.value_bps) AS markout_5s_bps
            FROM markouts m JOIN fills f ON f.fill_id=m.fill_id
            WHERE f.timestamp_ms>=? AND m.horizon_ms=5000 GROUP BY hour_ms
            """,
            (HOUR_MS, HOUR_MS, first_hour_ms),
        ):
            bucket = buckets.get(int(row["hour_ms"]))
            if bucket:
                bucket["markout_5s_bps"] = self._optional_float(row["markout_5s_bps"])

        decision_signals: dict[int, dict[str, list[float]]] = defaultdict(
            lambda: {"volatility": [], "basis": []}
        )
        for row in database.execute(
            "SELECT timestamp_ms,payload FROM decisions WHERE timestamp_ms>=? AND payload IS NOT NULL",
            (first_hour_ms,),
        ):
            payload = json.loads(row["payload"])
            hour_ms = int(row["timestamp_ms"]) // HOUR_MS * HOUR_MS
            decision_signals[hour_ms]["volatility"].append(float(payload.get("volatility_bps") or 0))
            decision_signals[hour_ms]["basis"].append(abs(float(payload.get("basis_bps") or 0)))
        for hour_ms, signals in decision_signals.items():
            bucket = buckets.get(hour_ms)
            if bucket:
                bucket["avg_volatility_bps"] = self._average(signals["volatility"])
                bucket["avg_abs_basis_bps"] = self._average(signals["basis"])

        result = list(buckets.values())
        for bucket in result:
            bucket["hour"] = self._iso(int(bucket["timestamp_ms"]))
        return result

    def _fills_with_context(
        self, database: sqlite3.Connection, first_hour_ms: int
    ) -> list[dict[str, Any]]:
        decisions: list[tuple[int, dict[str, Any]]] = []
        for row in database.execute(
            "SELECT timestamp_ms,payload FROM decisions WHERE timestamp_ms>=? AND payload IS NOT NULL ORDER BY timestamp_ms",
            (first_hour_ms - HOUR_MS,),
        ):
            decisions.append((int(row["timestamp_ms"]), json.loads(row["payload"])))
        decision_times = [item[0] for item in decisions]

        markouts = {
            str(row["fill_id"]): float(row["value_bps"])
            for row in database.execute(
                """
                SELECT m.fill_id,m.value_bps FROM markouts m
                JOIN fills f ON f.fill_id=m.fill_id
                WHERE f.timestamp_ms>=? AND m.horizon_ms=5000
                """,
                (first_hour_ms,),
            )
        }
        fills: list[dict[str, Any]] = []
        for row in database.execute(
            "SELECT fill_id,timestamp_ms,side,price,size,fair_at_fill FROM fills WHERE timestamp_ms>=? ORDER BY timestamp_ms",
            (first_hour_ms,),
        ):
            price = float(row["price"])
            fair = float(row["fair_at_fill"])
            spread_capture = (
                (fair - price) / price * 10_000
                if row["side"] == "BUY"
                else (price - fair) / price * 10_000
            )
            index = bisect_right(decision_times, int(row["timestamp_ms"])) - 1
            context = decisions[index][1] if index >= 0 else {}
            quoted_spread = self._quoted_spread(context)
            fills.append(
                {
                    "fill_id": str(row["fill_id"]),
                    "side": str(row["side"]),
                    "volume_usd": price * float(row["size"]),
                    "spread_capture_bps": spread_capture,
                    "markout_5s_bps": markouts.get(str(row["fill_id"])),
                    "volatility_bps": float(context.get("volatility_bps") or 0),
                    "abs_basis_bps": abs(float(context.get("basis_bps") or 0)),
                    "quoted_spread_bps": quoted_spread,
                }
            )
        return fills

    def _build_report(
        self,
        *,
        hours: int,
        now_ms: int,
        first_hour_ms: int,
        hourly: list[dict[str, Any]],
        fills: list[dict[str, Any]],
        quote_actions: int,
        quote_action_minutes: float,
        continuity_gaps: int,
    ) -> dict[str, Any]:
        fill_count = len(fills)
        volume = sum(float(fill["volume_usd"]) for fill in fills)
        spread_capture = self._average([float(fill["spread_capture_bps"]) for fill in fills])
        markouts = [float(fill["markout_5s_bps"]) for fill in fills if fill["markout_5s_bps"] is not None]
        markout_5s = self._average(markouts)
        summary = {
            "fills": fill_count,
            "volume_usd": volume,
            "quote_actions": quote_actions,
            "fill_rate_percent": fill_count / quote_actions * 100 if quote_actions else 0.0,
            "spread_capture_bps": spread_capture,
            "markout_5s_bps": markout_5s,
            "quote_actions_per_minute": quote_actions / quote_action_minutes,
            "active_hours": sum(1 for bucket in hourly if bucket["events"]),
        }
        alerts, recommendations = self._signals(summary, continuity_gaps)
        return {
            "available": True,
            "generated_at": self._iso(now_ms),
            "window": {
                "hours": hours,
                "from": self._iso(first_hour_ms),
                "to": self._iso(now_ms),
            },
            "summary": summary,
            "hourly": hourly,
            "regimes": {
                "side": self._group(fills, lambda fill: str(fill["side"]), {"BUY": "BUY", "SELL": "SELL"}),
                "volatility": self._group(
                    fills,
                    lambda fill: "low" if fill["volatility_bps"] < 0.5 else ("medium" if fill["volatility_bps"] < 2 else "high"),
                    {"low": "Baja <0,5 bps", "medium": "Media 0,5–2 bps", "high": "Alta ≥2 bps"},
                ),
                "basis": self._group(
                    fills,
                    lambda fill: "tight" if fill["abs_basis_bps"] < 1 else ("medium" if fill["abs_basis_bps"] < 3 else "wide"),
                    {"tight": "Estrecho <1 bps", "medium": "Medio 1–3 bps", "wide": "Amplio ≥3 bps"},
                ),
                "quoted_spread": self._group(
                    fills,
                    lambda fill: "tight" if fill["quoted_spread_bps"] < 5 else ("medium" if fill["quoted_spread_bps"] < 10 else "wide"),
                    {"tight": "Estrecho <5 bps", "medium": "Medio 5–10 bps", "wide": "Amplio ≥10 bps"},
                ),
            },
            "alerts": alerts,
            "recommendations": recommendations,
            "continuity_gaps": continuity_gaps,
        }

    @staticmethod
    def _signals(summary: dict[str, Any], continuity_gaps: int) -> tuple[list[dict[str, str]], list[str]]:
        alerts: list[dict[str, str]] = []
        recommendations: list[str] = []
        fills = int(summary["fills"])
        churn = float(summary["quote_actions_per_minute"])
        spread = summary["spread_capture_bps"]
        markout = summary["markout_5s_bps"]
        if fills < 30:
            alerts.append({
                "severity": "info",
                "code": "EARLY_SAMPLE",
                "title": "Muestra todavía temprana",
                "detail": f"{fills}/30 fills shadow mínimos para empezar a ajustar parámetros.",
            })
            recommendations.append("Mantener los parámetros estables hasta reunir al menos 30 fills shadow.")
        if churn > 20:
            alerts.append({
                "severity": "warning",
                "code": "HIGH_QUOTE_CHURN",
                "title": "Rotación de quotes elevada",
                "detail": f"{churn:.1f} acciones/min; conviene revisar tolerancia de refresh cuando la muestra madure.",
            })
            recommendations.append("Comparar una variante shadow con mayor refresh tolerance para reducir replaces.")
        if fills >= 3 and spread is not None and float(spread) < 0:
            alerts.append({
                "severity": "warning",
                "code": "NEGATIVE_CAPTURE",
                "title": "Spread capturado negativo",
                "detail": f"Media de {float(spread):.2f} bps en la ventana.",
            })
            recommendations.append("Abrir el spread o endurecer el filtro de toxicidad antes de testnet.")
        if fills >= 3 and markout is not None and float(markout) < 0:
            alerts.append({
                "severity": "warning",
                "code": "NEGATIVE_MARKOUT",
                "title": "Markout a 5 s negativo",
                "detail": f"Media de {float(markout):.2f} bps; posible selección adversa.",
            })
            recommendations.append("Cruzar los fills negativos con volatilidad y basis antes de cambiar el grid.")
        if continuity_gaps:
            alerts.append({
                "severity": "warning",
                "code": "CONTINUITY_GAPS",
                "title": "Gaps de continuidad detectados",
                "detail": f"{continuity_gaps} resyncs dentro de la ventana analizada.",
            })
        if len(alerts) == 0:
            alerts.append({
                "severity": "success",
                "code": "HEALTHY_SAMPLE",
                "title": "Sin alertas analíticas",
                "detail": "Las métricas de la ventana están dentro de los umbrales shadow.",
            })
        return alerts, list(dict.fromkeys(recommendations))

    @classmethod
    def _group(
        cls,
        fills: list[dict[str, Any]],
        classifier: Callable[[dict[str, Any]], str],
        labels: dict[str, str],
    ) -> list[dict[str, Any]]:
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for fill in fills:
            grouped[classifier(fill)].append(fill)
        result = []
        for key, label in labels.items():
            items = grouped.get(key, [])
            markouts = [float(item["markout_5s_bps"]) for item in items if item["markout_5s_bps"] is not None]
            result.append(
                {
                    "key": key,
                    "label": label,
                    "fills": len(items),
                    "volume_usd": sum(float(item["volume_usd"]) for item in items),
                    "spread_capture_bps": cls._average([float(item["spread_capture_bps"]) for item in items]),
                    "markout_5s_bps": cls._average(markouts),
                }
            )
        return result

    @staticmethod
    def _quoted_spread(payload: dict[str, Any]) -> float:
        bid = payload.get("bid") or {}
        ask = payload.get("ask") or {}
        fair = float(payload.get("fair") or 0)
        if not fair or bid.get("price") is None or ask.get("price") is None:
            return 0.0
        return (float(ask["price"]) - float(bid["price"])) / fair * 10_000

    @staticmethod
    def _merge_counts(
        database: sqlite3.Connection,
        buckets: dict[int, dict[str, Any]],
        table: str,
        first_hour_ms: int,
        key: str,
    ) -> None:
        for row in database.execute(
            f"""
            SELECT CAST(timestamp_ms / ? AS INTEGER) * ? AS hour_ms, COUNT(*) AS total
            FROM {table} WHERE timestamp_ms>=? GROUP BY hour_ms
            """,
            (HOUR_MS, HOUR_MS, first_hour_ms),
        ):
            bucket = buckets.get(int(row["hour_ms"]))
            if bucket:
                bucket[key] = int(row["total"])

    def _quote_action_stats(
        self, database: sqlite3.Connection, first_hour_ms: int
    ) -> tuple[int, float]:
        if not self._table_exists(database, "quote_actions"):
            return 0, 1.0
        row = database.execute(
            "SELECT COUNT(*) AS total,MIN(timestamp_ms) AS first_ms,MAX(timestamp_ms) AS last_ms FROM quote_actions WHERE timestamp_ms>=?",
            (first_hour_ms,),
        ).fetchone()
        count = int(row["total"])
        active_minutes = max(
            1.0,
            (int(row["last_ms"]) - int(row["first_ms"])) / 60_000
            if row["first_ms"] is not None and row["last_ms"] is not None
            else 1.0,
        )
        return count, active_minutes

    def _connect(self) -> sqlite3.Connection:
        uri = f"file:{quote(str(self.database_path.resolve()))}?mode=ro"
        database = sqlite3.connect(uri, uri=True, timeout=2)
        database.row_factory = sqlite3.Row
        return database

    @staticmethod
    def _table_exists(database: sqlite3.Connection, table: str) -> bool:
        return database.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone() is not None

    @staticmethod
    def _average(values: list[float]) -> float | None:
        return sum(values) / len(values) if values else None

    @staticmethod
    def _optional_float(value: Any) -> float | None:
        return None if value is None else float(value)

    @staticmethod
    def _iso(timestamp_ms: int) -> str:
        return datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC).isoformat()

    def _unavailable(self, reason: str, hours: int) -> dict[str, Any]:
        return {
            "available": False,
            "generated_at": self._iso(int(time.time() * 1000)),
            "window": {"hours": hours, "from": None, "to": None},
            "summary": {},
            "hourly": [],
            "regimes": {},
            "alerts": [{"severity": "warning", "code": "UNAVAILABLE", "title": "Análisis no disponible", "detail": reason}],
            "recommendations": [],
            "continuity_gaps": 0,
        }
