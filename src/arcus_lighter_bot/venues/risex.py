from __future__ import annotations

import inspect
import time
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from typing import Any

from ..config import Settings
from ..models import Market, Order, OrderStatus, Position, Side
from .base import Venue


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


class RiseXVenue(Venue):
    """RISEx venue used as the hedge leg for the Arcus delta-neutral engine."""

    name = "risex"

    def __init__(self, settings: Settings) -> None:
        if not settings.risex_account_address:
            raise ValueError("RISEX_ACCOUNT_ADDRESS is required")
        if not settings.risex_signer_private_key:
            raise ValueError("RISEX_SIGNER_PRIVATE_KEY is required")
        self.settings = settings
        self._markets: dict[str, Market] = {}
        self._client: Any | None = None
        self._info: Any | None = None
        self._account_address = settings.risex_account_address

    async def markets(self) -> dict[str, Market]:
        info = await self._ensure_info()
        response = await info.get_markets()
        result: dict[str, Market] = {}
        for raw in response or []:
            config = _field(raw, "config", {})
            name = str(_field(raw, "display_name", ""))
            symbol = self._base_symbol(name or str(_field(config, "name", "")))
            if not symbol or symbol not in self.settings.markets:
                continue
            mark_price = float(_field(raw, "mark_price", 0) or 0)
            step_size = float(_field(config, "step_size", 0) or 0)
            min_order_size = float(_field(config, "min_order_size", step_size) or step_size)
            result[symbol] = Market(
                symbol=symbol,
                market_id=int(_field(raw, "market_id")),
                tick_size=float(_field(config, "step_price", 0)),
                step_size=step_size,
                min_notional=min_order_size * mark_price,
                mark_price=mark_price,
            )
        if not result:
            raise RuntimeError("RiseX returned no configured markets")
        self._markets = result
        return result

    async def best_bid_ask(self, symbol: str) -> tuple[float, float]:
        book = await self._orderbook(symbol)
        bids = [float(_field(level, "price", 0)) for level in _field(book, "bids", [])]
        asks = [float(_field(level, "price", 0)) for level in _field(book, "asks", [])]
        if not bids or not asks:
            raise RuntimeError(f"RiseX {symbol} book is one-sided")
        return max(bids), min(asks)

    async def executable_depth_usd(
        self, symbol: str, side: Side, max_slippage_bps: int
    ) -> float:
        book = await self._orderbook(symbol)
        levels = _field(book, "asks" if side is Side.BUY else "bids", [])
        parsed = [
            (float(_field(level, "price", 0)), float(_field(level, "quantity", 0)))
            for level in levels
        ]
        parsed = [(price, size) for price, size in parsed if price > 0 and size > 0]
        if not parsed:
            return 0.0
        touch = min(price for price, _ in parsed) if side is Side.BUY else max(
            price for price, _ in parsed
        )
        factor = max_slippage_bps / 10_000
        limit = touch * (1 + factor if side is Side.BUY else 1 - factor)
        if side is Side.BUY:
            return sum(price * size for price, size in parsed if price <= limit)
        return sum(price * size for price, size in parsed if price >= limit)

    async def place_limit(
        self, symbol: str, side: Side, size: float, price: float, reduce_only: bool
    ) -> Order:
        return await self._place_order(
            symbol, side, size, price, reduce_only, market=False, post_only=True
        )

    async def place_market(
        self, symbol: str, side: Side, size: float, reduce_only: bool, max_slippage_bps: int
    ) -> Order:
        bid, ask = await self.best_bid_ask(symbol)
        reference = ask if side is Side.BUY else bid
        cap = reference * (
            1 + max_slippage_bps / 10_000
            if side is Side.BUY
            else 1 - max_slippage_bps / 10_000
        )
        before = await self._position_size(symbol)
        order = await self._place_order(
            symbol, side, size, cap, reduce_only, market=True, post_only=False
        )
        filled_size, average_price = await self._wait_for_market_fill(
            symbol, side, size, before
        )
        order.filled_size = filled_size
        order.average_fill_price = average_price or reference
        order.status = (
            OrderStatus.FILLED
            if abs(filled_size - size) <= self._market_step(symbol) / 2
            else OrderStatus.PARTIALLY_FILLED
            if filled_size > self._market_step(symbol) / 2
            else OrderStatus.REJECTED
        )
        return order

    async def get_order(self, order_id: str, symbol: str) -> Order:
        info = await self._ensure_info()
        raw = next(
            (
                candidate
                for candidate in await info.get_open_orders(self._account_address)
                if str(_field(candidate, "order_id", "")) == order_id
            ),
            None,
        )
        if raw is None:
            raise LookupError(f"RiseX order {order_id} is not open")
        status = str(_field(raw, "status", "OPEN")).upper()
        mapped = {
            "FILLED": OrderStatus.FILLED,
            "PARTIALLY_FILLED": OrderStatus.PARTIALLY_FILLED,
            "CANCELED": OrderStatus.CANCELED,
            "CANCELLED": OrderStatus.CANCELED,
            "REJECTED": OrderStatus.REJECTED,
        }.get(status, OrderStatus.OPEN)
        return Order(
            id=order_id,
            venue=self.name,
            symbol=symbol,
            side=self._side_from_value(_field(raw, "side", "BUY")),
            size=float(_field(raw, "size", _field(raw, "quantity", 0)) or 0),
            price=float(_field(raw, "price", 0) or 0),
            reduce_only=bool(_field(raw, "reduce_only", False)),
            status=mapped,
            filled_size=float(_field(raw, "filled_size", 0) or 0),
            average_fill_price=float(_field(raw, "avg_price", 0) or 0) or None,
        )

    async def cancel_order(self, order_id: str, symbol: str) -> None:
        client = await self._ensure_client()
        market = await self._market(symbol)
        await client.cancel_order(market_id=market.market_id, order_id=order_id)

    async def positions(self) -> list[Position]:
        info = await self._ensure_info()
        raw_positions = await info.get_all_positions(self._account_address)
        result: list[Position] = []
        markets = self._markets or await self.markets()
        for raw in raw_positions or []:
            market_id = int(_field(raw, "market_id", -1))
            market = next((item for item in markets.values() if item.market_id == market_id), None)
            if market is None:
                continue
            size = float(_field(raw, "size", 0) or 0)
            if size <= 0:
                continue
            signed_size = size if _field(raw, "side", 1) in (0, "0", "BUY", "LONG") else -size
            extras = _field(raw, "model_extra", {}) or {}
            entry = float(extras.get("avg_entry_price", extras.get("entry_price", 0)) or 0)
            result.append(
                Position(
                    venue=self.name,
                    symbol=market.symbol,
                    signed_size=signed_size,
                    entry_price=entry,
                    mark_price=market.mark_price,
                    liquidation_price=None,
                    unrealized_pnl=float(extras.get("unrealized_pnl", 0) or 0),
                )
            )
        return result

    async def account(self) -> dict[str, object]:
        info = await self._ensure_info()
        balance = float(await info.get_cross_margin_balance(self._account_address))
        positions = await self.positions()
        return {
            "venue": self.name,
            "equity": balance,
            "free_collateral": balance,
            "unrealized_pnl": sum(position.unrealized_pnl for position in positions),
            "lifetime_volume_usd": None,
            "status": "connected",
        }

    async def risk_account(self) -> dict[str, object]:
        return await self.account()

    async def configured_leverage(self, symbol: str) -> float | None:
        positions = await self.positions()
        for position in positions:
            if position.symbol == symbol:
                return None
        return None

    async def configure_leverage(self, symbol: str, leverage: float) -> float | None:
        client = await self._ensure_client()
        market = await self._market(symbol)
        requested = max(1, int(leverage))
        await client.update_leverage(market_id=market.market_id, leverage=requested)
        return float(requested)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
        if self._info is not None and self._info is not self._client:
            await self._info.close()

    async def _place_order(
        self,
        symbol: str,
        side: Side,
        size: float,
        price: float,
        reduce_only: bool,
        *,
        market: bool,
        post_only: bool,
    ) -> Order:
        client = await self._ensure_client()
        market_meta = await self._market(symbol)
        try:
            from risex.enums import OrderType, Side as RiseXSide, TimeInForce
        except ImportError as error:  # pragma: no cover
            raise RuntimeError("Install the risex package before enabling live mode") from error
        size_steps = self._steps(size, market_meta.step_size)
        price_ticks = self._steps(price, market_meta.tick_size, ROUND_HALF_UP)
        if size_steps <= 0:
            raise ValueError(f"RiseX size is below one market step for {symbol}")
        receipt = await client.place_order(
            market_id=market_meta.market_id,
            side=RiseXSide.LONG if side is Side.BUY else RiseXSide.SHORT,
            size_steps=size_steps,
            price_ticks=price_ticks,
            # RiseX market orders require price_ticks=0 and have no explicit
            # slippage bound. Use a marketable IOC limit for hedge orders so
            # the engine's max_slippage_bps cap remains enforceable.
            order_type=OrderType.LIMIT,
            time_in_force=TimeInForce.IOC if market else TimeInForce.GTC,
            post_only=post_only,
            reduce_only=reduce_only,
        )
        order_id = str(_field(receipt, "order_id", ""))
        if not order_id:
            raise RuntimeError("RiseX returned an empty order id")
        rounded_size = size_steps * market_meta.step_size
        rounded_price = price_ticks * market_meta.tick_size
        return Order(order_id, self.name, symbol, side, rounded_size, rounded_price, reduce_only)

    async def _wait_for_market_fill(
        self, symbol: str, side: Side, requested: float, before: float
    ) -> tuple[float, float | None]:
        step = self._market_step(symbol)
        deadline = time.monotonic() + max(2.0, float(self.settings.max_unhedged_seconds))
        last = 0.0
        while time.monotonic() < deadline:
            import asyncio

            await asyncio.sleep(0.2)
            after = await self._position_size(symbol)
            executed = max(0.0, after - before if side is Side.BUY else before - after)
            if abs(executed - requested) <= step / 2:
                return executed, None
            if executed > step / 2 and abs(executed - last) <= step / 2:
                return executed, None
            last = executed
        return last, None

    async def _position_size(self, symbol: str) -> float:
        positions = await self.positions()
        return next((item.signed_size for item in positions if item.symbol == symbol), 0.0)

    async def _orderbook(self, symbol: str) -> Any:
        info = await self._ensure_info()
        market = await self._market(symbol)
        return await info.get_orderbook(market.market_id, limit=100)

    async def _market(self, symbol: str) -> Market:
        if not self._markets:
            await self.markets()
        return self._markets[symbol]

    async def _ensure_info(self) -> Any:
        if self._info is not None:
            return self._info
        try:
            from risex import RiseXInfoClient
        except ImportError as error:  # pragma: no cover
            raise RuntimeError("Install risex==0.10.0 before enabling RiseX") from error
        self._info = await self._factory(RiseXInfoClient.create)
        return self._info

    async def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            from risex import RiseXClient
        except ImportError as error:  # pragma: no cover
            raise RuntimeError("Install risex==0.10.0 before enabling RiseX") from error
        self._client = await self._factory(
            RiseXClient.from_signer,
            self._account_address,
            self.settings.risex_signer_private_key,
        )
        return self._client

    async def _factory(self, factory: Any, *args: Any) -> Any:
        parameters = inspect.signature(factory).parameters
        kwargs: dict[str, Any] = {}
        if "mainnet" in parameters:
            kwargs["mainnet"] = self.settings.risex_network == "mainnet"
        if "base_url" in parameters:
            kwargs["base_url"] = self.settings.risex_api_url
        return await factory(*args, **kwargs)

    @staticmethod
    def _base_symbol(value: str) -> str:
        return value.split("-")[0].split("/")[0].upper().strip()

    @staticmethod
    def _side_from_value(value: Any) -> Side:
        return Side.BUY if str(value).upper() in {"BUY", "LONG", "0"} else Side.SELL

    @staticmethod
    def _steps(value: float, step: float, rounding: str = ROUND_DOWN) -> int:
        if step <= 0:
            raise ValueError("RiseX market returned a non-positive increment")
        decimal_value = Decimal(str(value))
        increment = Decimal(str(step))
        units = (decimal_value / increment).to_integral_value(rounding=rounding)
        return int(units)

    def _market_step(self, symbol: str) -> float:
        market = self._markets.get(symbol)
        if market is None:
            raise RuntimeError(f"RiseX market metadata unavailable for {symbol}")
        return market.step_size
