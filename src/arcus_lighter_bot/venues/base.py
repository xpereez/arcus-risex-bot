from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import Market, Order, Position, Side


class Venue(ABC):
    name: str

    @abstractmethod
    async def markets(self) -> dict[str, Market]: ...

    @abstractmethod
    async def best_bid_ask(self, symbol: str) -> tuple[float, float]: ...

    @abstractmethod
    async def place_limit(
        self, symbol: str, side: Side, size: float, price: float, reduce_only: bool
    ) -> Order: ...

    @abstractmethod
    async def place_market(
        self, symbol: str, side: Side, size: float, reduce_only: bool, max_slippage_bps: int
    ) -> Order: ...

    @abstractmethod
    async def get_order(self, order_id: str, symbol: str) -> Order: ...

    @abstractmethod
    async def cancel_order(self, order_id: str, symbol: str) -> None: ...

    @abstractmethod
    async def positions(self) -> list[Position]: ...

    @abstractmethod
    async def account(self) -> dict[str, object]: ...

    async def close(self) -> None:
        return None

