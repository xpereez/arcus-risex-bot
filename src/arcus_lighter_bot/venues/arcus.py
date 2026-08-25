from __future__ import annotations

import asyncio
import json
import time
import uuid
from decimal import Decimal

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ..config import Settings
from ..models import Market, Order, OrderStatus, Position, Side
from .base import Venue


class ArcusVenue(Venue):
    name = "arcus"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.http = httpx.AsyncClient(base_url=settings.arcus_api_url, timeout=10)
        key_bytes = bytes.fromhex(settings.arcus_api_private_key.removeprefix("0x"))
        self._signer = Ed25519PrivateKey.from_private_bytes(key_bytes[:32])
        self._markets: dict[str, Market] = {}
        self._orders: dict[str, Order] = {}
        self._lifetime_volume_usd: float | None = None
        self._volume_cached_at = 0.0
        self._volume_cache_seconds = 60.0

    async def markets(self) -> dict[str, Market]:
        response = await self._get("/v1/markets")
        response.raise_for_status()
        result = {}
        for raw in response.json()["markets"]:
            if raw.get("type") != "PERPETUAL" or raw.get("status") != "ONLINE":
                continue
            symbol = raw["baseAsset"]
            result[symbol] = Market(
                symbol=symbol,
                market_id=int(raw["marketId"]),
                tick_size=float(raw["tickSize"]),
                step_size=float(raw["stepSize"]),
                min_notional=float(raw["minOrderNotional"]),
                mark_price=float(raw["markPrice"]),
            )
        self._markets = result
        return result

    async def best_bid_ask(self, symbol: str) -> tuple[float, float]:
        await self._market(symbol)
        market_name = f"{symbol}-USD"
        response = await self._get(f"/v1/bbo/{market_name}")
        response.raise_for_status()
        data = response.json()
        bid = data.get("bidPrice") or data.get("bestBid") or data.get("bid")
        ask = data.get("askPrice") or data.get("bestAsk") or data.get("ask")
        if isinstance(bid, dict):
            bid = bid.get("price")
        if isinstance(ask, dict):
            ask = ask.get("price")
        if bid is None or ask is None:
            book = await self._get(f"/v1/l2OrderBook/{market_name}", params={"nLevels": 1})
            book.raise_for_status()
            payload = book.json()
            bid = payload["bids"][0][0]
            ask = payload["asks"][0][0]
        return float(bid), float(ask)

    async def place_limit(
        self, symbol: str, side: Side, size: float, price: float, reduce_only: bool
    ) -> Order:
        return await self._place(symbol, side, size, price, reduce_only, "LIMIT", "ALO")

    async def place_market(
        self, symbol: str, side: Side, size: float, reduce_only: bool, max_slippage_bps: int
    ) -> Order:
        bid, ask = await self.best_bid_ask(symbol)
        reference = ask if side is Side.BUY else bid
        cap = reference * (1 + max_slippage_bps / 10_000 if side is Side.BUY else 1 - max_slippage_bps / 10_000)
        return await self._place(symbol, side, size, cap, reduce_only, "MARKET", "IOC")

    async def _place(
        self,
        symbol: str,
        side: Side,
        size: float,
        price: float,
        reduce_only: bool,
        order_type: str,
        tif: str,
    ) -> Order:
        market = await self._market(symbol)
        client_id = f"al-{uuid.uuid4().hex[:18]}"
        body = {
            "accountIndex": self.settings.arcus_account_index,
            "clientId": client_id,
            "marketId": market.market_id,
            "orderSide": side.value,
            "orderType": order_type,
            "price": self._decimal(price, market.tick_size),
            "quantity": self._decimal(size, market.step_size),
            "reduceOnly": reduce_only,
            "timeInForce": tif,
            "goodTilTime": str((time.time_ns() + 35 * 24 * 3600 * 1_000_000_000) // 1_000),
            "isPositionTPSL": None,
        }
        headers = self._order_headers(body, market)
        response = await self.http.post(
            "/v1/placeOrder",
            params={"address": self.settings.arcus_address},
            headers=headers,
            content=json.dumps(body, separators=(",", ":")),
        )
        response.raise_for_status()
        raw = response.json()
        order_id = str(raw.get("orderId") or raw.get("id") or raw.get("order", {}).get("orderId"))
        if not order_id or order_id == "None":
            raise RuntimeError(f"Arcus did not return an order id: {raw}")
        order = Order(order_id, self.name, symbol, side, size, price, reduce_only)
        self._orders[order_id] = order
        return order

    async def get_order(self, order_id: str, symbol: str) -> Order:
        response = await self._get(
            f"/v1/order/{order_id}",
            params={"address": self.settings.arcus_address, "accountIndex": self.settings.arcus_account_index},
        )
        response.raise_for_status()
        raw = response.json().get("order", response.json())
        cached = self._orders[order_id]
        status_raw = str(raw.get("status", "OPEN")).upper()
        status = {
            "FILLED": OrderStatus.FILLED,
            "PARTIALLY_FILLED": OrderStatus.PARTIALLY_FILLED,
            "CANCELED": OrderStatus.CANCELED,
            "CANCELLED": OrderStatus.CANCELED,
            "REJECTED": OrderStatus.REJECTED,
        }.get(status_raw, OrderStatus.OPEN)
        return Order(
            id=order_id,
            venue=self.name,
            symbol=symbol,
            side=cached.side,
            size=cached.size,
            price=cached.price,
            reduce_only=cached.reduce_only,
            status=status,
            filled_size=float(raw.get("filledSize", 0)),
            average_fill_price=float(raw["avgFillPrice"]) if raw.get("avgFillPrice") else None,
            created_at=cached.created_at,
        )

    async def cancel_order(self, order_id: str, symbol: str) -> None:
        market = await self._market(symbol)
        body = {
            "accountIndex": self.settings.arcus_account_index,
            "kind": "orderId",
            "marketId": market.market_id,
            "orderId": order_id,
        }
        headers = self._cancel_headers(body)
        response = await self.http.post(
            "/v1/cancelOrder",
            params={"address": self.settings.arcus_address},
            headers=headers,
            content=json.dumps(body, separators=(",", ":")),
        )
        response.raise_for_status()

    async def positions(self) -> list[Position]:
        response = await self._get(
            "/v1/positions",
            params={"address": self.settings.arcus_address, "accountIndex": self.settings.arcus_account_index},
        )
        response.raise_for_status()
        return self._positions_from_payload(response.json())

    @staticmethod
    def _positions_from_payload(payload: dict) -> list[Position]:
        raw_positions = payload.get("positions", [])
        if isinstance(raw_positions, dict):
            raw_positions = raw_positions.values()
        return [
            Position(
                venue=ArcusVenue.name,
                symbol=item.get("baseAsset") or item.get("marketDisplayName", "").split("-")[0],
                signed_size=float(item.get("signedSize") or item.get("size", 0)),
                entry_price=float(item.get("entryPrice") or item.get("averageEntryPrice", 0)),
                mark_price=float(item.get("markPrice") or item.get("markPx", 0)),
                liquidation_price=float(item["liquidationPrice"]) if item.get("liquidationPrice") else None,
                unrealized_pnl=float(item.get("unrealizedPnl", 0)),
            )
            for item in raw_positions
            if float(item.get("signedSize") or item.get("size", 0)) != 0
        ]

    async def account(self) -> dict[str, object]:
        response = await self._get(
            "/v1/account",
            params={"address": self.settings.arcus_address, "accountIndex": self.settings.arcus_account_index},
        )
        response.raise_for_status()
        raw = response.json().get("account", response.json())
        return {
            "venue": self.name,
            "equity": float(raw.get("equity", 0)),
            "free_collateral": float(raw.get("freeCollateral", 0)),
            "unrealized_pnl": float(raw.get("unrealizedPnl", 0)),
            "lifetime_volume_usd": await self._cached_lifetime_volume_usd(),
            "status": "connected",
        }

    async def _cached_lifetime_volume_usd(self) -> float | None:
        now = time.monotonic()
        if (
            self._lifetime_volume_usd is not None
            and now - self._volume_cached_at < self._volume_cache_seconds
        ):
            return self._lifetime_volume_usd
        try:
            response = await self._get(
                "/v1/account/stats",
                params={"address": self.settings.arcus_address},
            )
            response.raise_for_status()
            self._lifetime_volume_usd = self._lifetime_volume_from_payload(response.json())
            self._volume_cached_at = now
        except (httpx.HTTPError, KeyError, TypeError, ValueError):
            pass
        return self._lifetime_volume_usd

    @staticmethod
    def _lifetime_volume_from_payload(payload: dict) -> float:
        # Arcus returns USD values in nano-units, matching its public web app.
        return float(payload["lifetimeVolume"]) / 1_000_000_000

    async def close(self) -> None:
        await self.http.aclose()

    async def _get(self, path: str, *, params: dict | None = None) -> httpx.Response:
        for attempt in range(4):
            response = await self.http.get(path, params=params)
            if response.status_code != 429 or attempt == 3:
                return response
            await asyncio.sleep(self._retry_after_seconds(response))
        raise AssertionError("unreachable")

    @staticmethod
    def _retry_after_seconds(response: httpx.Response) -> float:
        try:
            return max(1.0, float(response.headers.get("Retry-After", "1")))
        except ValueError:
            return 1.0

    async def _market(self, symbol: str) -> Market:
        if not self._markets:
            await self.markets()
        return self._markets[symbol]

    def _order_headers(self, body: dict, market: Market) -> dict[str, str]:
        timestamp = time.time_ns()
        price_ticks = self._ratio(body["price"], market.tick_size)
        quantity_quantums = self._ratio(body["quantity"], market.step_size)
        payload = {
            "ad": self.settings.arcus_address.lower(),
            "ai": self.settings.arcus_account_index,
            "c": body["clientId"],
            "ct": timestamp,
            "g": int(body["goodTilTime"]) * 1000,
            "m": market.market_id,
            "op": 1,
            "p": price_ticks,
            "q": quantity_quantums,
            "r": 1 if body["reduceOnly"] else 0,
            "s": 1 if body["orderSide"] == "SELL" else 0,
            "t": {"GTT": 0, "FOK": 1, "IOC": 2, "ALO": 3}[body["timeInForce"]],
            "v": 1,
        }
        return self._signed_headers(timestamp, self._canonical(payload))

    def _cancel_headers(self, body: dict) -> dict[str, str]:
        timestamp = time.time_ns()
        payload = {
            "ad": self.settings.arcus_address.lower(),
            "ai": self.settings.arcus_account_index,
            "ct": timestamp,
            "id": body["orderId"],
            "m": body["marketId"],
            "op": 2,
            "v": 1,
        }
        return self._signed_headers(timestamp, self._canonical(payload))

    def _signed_headers(self, timestamp: int, payload: str) -> dict[str, str]:
        signature = self._signer.sign(payload.encode()).hex()
        return {
            "Content-Type": "application/json",
            "X-API-Key": self.settings.arcus_api_key,
            "X-Timestamp": str(timestamp),
            "X-Signature": signature,
        }

    @staticmethod
    def _canonical(payload: dict[str, object]) -> str:
        return json.dumps(payload, separators=(",", ":"), ensure_ascii=True)

    @staticmethod
    def _ratio(value: str, increment: float) -> int:
        return int(Decimal(value) / Decimal(str(increment)))

    @staticmethod
    def _decimal(value: float, increment: float) -> str:
        quantum = Decimal(str(increment))
        return format((Decimal(str(value)) / quantum).to_integral_value() * quantum, "f")
