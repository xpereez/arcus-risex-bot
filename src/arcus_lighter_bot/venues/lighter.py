from __future__ import annotations

import asyncio
import json
import time
import uuid
from urllib.parse import urlsplit, urlunsplit

import lighter
import websockets

from ..config import Settings
from ..models import Market, Order, OrderStatus, Position, Side
from .base import Venue


class LighterVenue(Venue):
    name = "lighter"

    def __init__(self, settings: Settings) -> None:
        if settings.lighter_account_index is None:
            raise ValueError("LIGHTER_ACCOUNT_INDEX is required")
        self.settings = settings
        self._config = lighter.Configuration(host=settings.lighter_api_url)
        self._api_client = None
        self._order_api = None
        self._account_api = None
        self._signer = None
        self._markets: dict[str, Market] = {}
        self._lifetime_volume_usd: float | None = None
        self._volume_cached_at = 0.0
        self._volume_cache_seconds = 60.0

    async def markets(self) -> dict[str, Market]:
        self._ensure_api_client()
        response = await self._order_api.order_books(_request_timeout=10)
        payload = response.to_dict()
        result = {}
        for raw in payload.get("order_books", []):
            if raw.get("market_type") != "perp" or raw.get("status") != "active":
                continue
            symbol = raw["symbol"]
            result[symbol] = Market(
                symbol=symbol,
                market_id=int(raw["market_id"]),
                tick_size=10 ** -int(raw["supported_price_decimals"]),
                step_size=10 ** -int(raw["supported_size_decimals"]),
                min_notional=float(raw["min_quote_amount"]),
                mark_price=0,
            )
        self._markets = result
        return result

    async def best_bid_ask(self, symbol: str) -> tuple[float, float]:
        self._ensure_api_client()
        market = await self._market(symbol)
        response = await self._order_api.order_book_orders(market.market_id, 100, _request_timeout=10)
        payload = response.to_dict()
        if payload.get("bids") is not None or payload.get("asks") is not None:
            bids = [float(item["price"]) for item in payload.get("bids", [])]
            asks = [float(item["price"]) for item in payload.get("asks", [])]
        else:
            orders = payload.get("orders", payload.get("order_book_orders", []))
            bids = [float(item["price"]) for item in orders if not bool(item.get("is_ask"))]
            asks = [float(item["price"]) for item in orders if bool(item.get("is_ask"))]
        if not bids or not asks:
            raise RuntimeError(f"Lighter {symbol} book is one-sided")
        return max(bids), min(asks)

    async def place_limit(
        self, symbol: str, side: Side, size: float, price: float, reduce_only: bool
    ) -> Order:
        self._ensure_signer()
        market = await self._market(symbol)
        client_id = self._client_order_id()
        base_amount = round(size / market.step_size)
        price_int = round(price / market.tick_size)
        tx, response, error = await self._signer.create_order(
            market.market_id,
            client_id,
            base_amount,
            price_int,
            side is Side.SELL,
            self._signer.ORDER_TYPE_LIMIT,
            self._signer.ORDER_TIME_IN_FORCE_POST_ONLY,
            reduce_only,
            api_key_index=self.settings.lighter_api_key_index,
        )
        if error or response is None or getattr(response, "code", 200) != 200:
            raise RuntimeError(error or f"Lighter rejected order: {response}")
        return Order(str(client_id), self.name, symbol, side, size, price, reduce_only)

    async def place_market(
        self, symbol: str, side: Side, size: float, reduce_only: bool, max_slippage_bps: int
    ) -> Order:
        self._ensure_signer()
        market = await self._market(symbol)
        bid, ask = await self.best_bid_ask(symbol)
        reference = ask if side is Side.BUY else bid
        limit_price = reference * (
            1 + max_slippage_bps / 10_000 if side is Side.BUY else 1 - max_slippage_bps / 10_000
        )
        client_id = self._client_order_id()
        base_amount = round(size / market.step_size)
        price_int = round(limit_price / market.tick_size)
        tx, response, error = await self._signer.create_market_order(
            market.market_id,
            client_id,
            base_amount,
            price_int,
            side is Side.SELL,
            reduce_only,
            api_key_index=self.settings.lighter_api_key_index,
        )
        if error or response is None or getattr(response, "code", 200) != 200:
            raise RuntimeError(error or f"Lighter rejected market order: {response}")
        return Order(
            str(client_id),
            self.name,
            symbol,
            side,
            size,
            reference,
            reduce_only,
            status=OrderStatus.FILLED,
            filled_size=size,
            average_fill_price=reference,
        )

    async def get_order(self, order_id: str, symbol: str) -> Order:
        raise NotImplementedError("Lighter limit-order polling is not used by this strategy")

    async def cancel_order(self, order_id: str, symbol: str) -> None:
        self._ensure_signer()
        market = await self._market(symbol)
        _, response, error = await self._signer.cancel_order(
            market.market_id,
            int(order_id),
            api_key_index=self.settings.lighter_api_key_index,
        )
        if error or response is None or getattr(response, "code", 200) != 200:
            raise RuntimeError(error or f"Lighter cancel failed: {response}")

    async def positions(self) -> list[Position]:
        account = await self._raw_account()
        markets = account.get("positions", account.get("accounts", [{}])[0].get("positions", []))
        result = []
        for item in markets or []:
            size = self._signed_position_size(item)
            if abs(size) < 1e-12:
                continue
            result.append(
                Position(
                    venue=self.name,
                    symbol=item.get("symbol") or item.get("market_symbol", ""),
                    signed_size=size,
                    entry_price=float(item.get("avg_entry_price") or item.get("entry_price", 0)),
                    mark_price=float(item.get("mark_price", 0)),
                    liquidation_price=float(item["liquidation_price"]) if item.get("liquidation_price") else None,
                    unrealized_pnl=float(item.get("unrealized_pnl", 0)),
                )
            )
        return result

    async def account(self) -> dict[str, object]:
        raw = await self._raw_account()
        account = (raw.get("accounts") or [raw])[0]
        return {
            "venue": self.name,
            "equity": float(account.get("collateral") or account.get("equity", 0)),
            "free_collateral": float(account.get("available_balance") or account.get("free_collateral", 0)),
            "unrealized_pnl": float(account.get("unrealized_pnl", 0)),
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
            self._lifetime_volume_usd = await self._fetch_lifetime_volume_usd()
            self._volume_cached_at = now
        except (OSError, TimeoutError, ValueError, websockets.WebSocketException):
            pass
        return self._lifetime_volume_usd

    async def _fetch_lifetime_volume_usd(self) -> float:
        message = {
            "type": "subscribe",
            "channel": f"account_all_trades/{self.settings.lighter_account_index}",
        }
        async with websockets.connect(
            self._websocket_url(),
            open_timeout=5,
            close_timeout=1,
            ping_interval=None,
        ) as websocket:
            await websocket.send(json.dumps(message))
            async with asyncio.timeout(6):
                while True:
                    payload = json.loads(await websocket.recv())
                    if "total_volume" in payload:
                        return self._lifetime_volume_from_payload(payload)

    def _websocket_url(self) -> str:
        parsed = urlsplit(self.settings.lighter_api_url)
        scheme = "wss" if parsed.scheme == "https" else "ws"
        return urlunsplit((scheme, parsed.netloc, "/stream", "readonly=true", ""))

    @staticmethod
    def _lifetime_volume_from_payload(payload: dict) -> float:
        return float(payload["total_volume"])

    async def _raw_account(self) -> dict:
        self._ensure_api_client()
        response = await self._account_api.account(
            by="index", value=str(self.settings.lighter_account_index), _request_timeout=10
        )
        return response.to_dict()

    async def _market(self, symbol: str) -> Market:
        if not self._markets:
            await self.markets()
        return self._markets[symbol]

    @staticmethod
    def _client_order_id() -> int:
        # Lighter serializes client_order_index as an unsigned 48-bit integer.
        return int(time.time_ns() // 1_000) % ((1 << 48) - 1)

    @staticmethod
    def _signed_position_size(item: dict) -> float:
        size = float(item.get("position") or item.get("size") or item.get("position_size", 0))
        sign = item.get("sign")
        if sign is not None and size >= 0:
            size *= int(sign)
        return size

    async def close(self) -> None:
        if self._signer is not None:
            await self._signer.close()
        if self._api_client is not None:
            await self._api_client.close()

    def _ensure_api_client(self) -> None:
        if self._api_client is not None:
            return
        self._api_client = lighter.ApiClient(self._config)
        self._order_api = lighter.OrderApi(self._api_client)
        self._account_api = lighter.AccountApi(self._api_client)

    def _ensure_signer(self) -> None:
        if self._signer is not None:
            return
        self._signer = lighter.SignerClient(
            self.settings.lighter_api_url,
            self.settings.lighter_account_index,
            {self.settings.lighter_api_key_index: self.settings.lighter_api_private_key},
            chain_id=self.settings.lighter_chain_id,
        )
