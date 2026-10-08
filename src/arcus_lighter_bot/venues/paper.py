from __future__ import annotations

import asyncio
import random
import time
import uuid
from dataclasses import replace

from ..models import Market, Order, OrderStatus, Position, Side
from .base import Venue


DEFAULT_PRICES = {
    "BTC": 77450.0,
    "LIT": 2.184,
    "HYPE": 78.45,
    "SOL": 94.67,
    "SPCX": 44.18,
    "NVDA": 181.22,
    "TSLA": 351.40,
}


class PaperVenue(Venue):
    def __init__(
        self,
        name: str,
        *,
        seed: int = 7,
        fill_plan: list[tuple[float, float]] | None = None,
    ) -> None:
        self.name = name
        self._rng = random.Random(seed + (1 if name == "risex" else 0))
        self._orders: dict[str, Order] = {}
        self._order_started: dict[str, float] = {}
        self._applied_fills: dict[str, float] = {}
        self._positions: dict[str, Position] = {}
        self._prices = dict(DEFAULT_PRICES)
        self._fill_plan = fill_plan or [(0.25, 0.30), (0.60, 0.68), (1.05, 1.0)]
        self._lock = asyncio.Lock()

    async def markets(self) -> dict[str, Market]:
        return {
            symbol: Market(
                symbol=symbol,
                market_id=index,
                tick_size=0.1 if symbol == "BTC" else (0.0001 if symbol == "LIT" else 0.01),
                step_size=0.00001 if symbol == "BTC" else (0.01 if symbol == "LIT" else 0.001),
                min_notional=10.0,
                mark_price=price,
            )
            for index, (symbol, price) in enumerate(self._prices.items(), start=1)
        }

    async def best_bid_ask(self, symbol: str) -> tuple[float, float]:
        mid = self._prices[symbol]
        drift = self._rng.uniform(-0.00025, 0.00025)
        mid *= 1 + drift
        self._prices[symbol] = mid
        spread = max(mid * 0.00012, 0.0001)
        return mid - spread / 2, mid + spread / 2

    async def place_limit(
        self, symbol: str, side: Side, size: float, price: float, reduce_only: bool
    ) -> Order:
        order = Order(
            id=f"paper-{self.name}-{uuid.uuid4().hex[:12]}",
            venue=self.name,
            symbol=symbol,
            side=side,
            size=size,
            price=price,
            reduce_only=reduce_only,
        )
        async with self._lock:
            self._orders[order.id] = order
            self._order_started[order.id] = time.monotonic()
            self._applied_fills[order.id] = 0.0
        return replace(order)

    async def place_market(
        self, symbol: str, side: Side, size: float, reduce_only: bool, max_slippage_bps: int
    ) -> Order:
        bid, ask = await self.best_bid_ask(symbol)
        touch = ask if side is Side.BUY else bid
        slippage = min(max_slippage_bps, 4) / 10_000
        price = touch * (1 + slippage if side is Side.BUY else 1 - slippage)
        order = Order(
            id=f"paper-{self.name}-{uuid.uuid4().hex[:12]}",
            venue=self.name,
            symbol=symbol,
            side=side,
            size=size,
            price=price,
            reduce_only=reduce_only,
            status=OrderStatus.FILLED,
            filled_size=size,
            average_fill_price=price,
        )
        async with self._lock:
            self._orders[order.id] = order
            self._apply_position(order, size)
        return replace(order)

    async def get_order(self, order_id: str, symbol: str) -> Order:
        async with self._lock:
            order = self._orders[order_id]
            if order.status in {OrderStatus.CANCELED, OrderStatus.FILLED, OrderStatus.REJECTED}:
                return replace(order)
            elapsed = time.monotonic() - self._order_started[order_id]
            fraction = max((value for after, value in self._fill_plan if elapsed >= after), default=0.0)
            new_filled = min(order.size, order.size * fraction)
            previous = self._applied_fills[order_id]
            if new_filled > previous:
                self._apply_position(order, new_filled - previous)
                self._applied_fills[order_id] = new_filled
                order.filled_size = new_filled
                order.average_fill_price = order.price
                order.status = (
                    OrderStatus.FILLED
                    if abs(order.size - new_filled) < 1e-12
                    else OrderStatus.PARTIALLY_FILLED
                )
            return replace(order)

    async def cancel_order(self, order_id: str, symbol: str) -> None:
        async with self._lock:
            order = self._orders[order_id]
            if order.status is not OrderStatus.FILLED:
                order.status = OrderStatus.CANCELED

    async def positions(self) -> list[Position]:
        async with self._lock:
            result = []
            for position in self._positions.values():
                mark = self._prices[position.symbol]
                pnl = position.signed_size * (mark - position.entry_price)
                result.append(replace(position, mark_price=mark, unrealized_pnl=pnl))
            return result

    async def account(self) -> dict[str, object]:
        positions = await self.positions()
        pnl = sum(position.unrealized_pnl for position in positions)
        return {
            "venue": self.name,
            "equity": 1000.0 + pnl,
            "free_collateral": 900.0 + pnl,
            "unrealized_pnl": pnl,
            "lifetime_volume_usd": None,
            "status": "connected",
        }

    def _apply_position(self, order: Order, delta_size: float) -> None:
        signed_delta = delta_size if order.side is Side.BUY else -delta_size
        current = self._positions.get(order.symbol)
        if current is None:
            new_size = signed_delta
            entry = order.price
        else:
            new_size = current.signed_size + signed_delta
            same_direction = current.signed_size * signed_delta > 0
            if same_direction:
                entry = (
                    abs(current.signed_size) * current.entry_price + abs(signed_delta) * order.price
                ) / abs(new_size)
            else:
                entry = current.entry_price if abs(new_size) > 1e-12 else order.price
        if abs(new_size) < 1e-10:
            self._positions.pop(order.symbol, None)
            return
        liquidation = entry * (0.5 if new_size > 0 else 1.5)
        self._positions[order.symbol] = Position(
            venue=self.name,
            symbol=order.symbol,
            signed_size=new_size,
            entry_price=entry,
            mark_price=self._prices[order.symbol],
            liquidation_price=liquidation,
            unrealized_pnl=0.0,
        )
