from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum


def utc_now() -> datetime:
    return datetime.now(UTC)


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"

    @property
    def opposite(self) -> "Side":
        return Side.SELL if self is Side.BUY else Side.BUY


class OrderStatus(StrEnum):
    OPEN = "OPEN"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"


class EnginePhase(StrEnum):
    STOPPED = "STOPPED"
    IDLE = "IDLE"
    OPENING = "OPENING_ARCUS"
    HEDGING_OPEN = "HEDGING_RISEX"
    HOLDING = "HOLDING"
    CLOSING = "CLOSING_ARCUS"
    HEDGING_CLOSE = "CLOSING_RISEX"
    COOLDOWN = "COOLDOWN"
    PAUSED = "PAUSED"
    ERROR = "ERROR"


@dataclass(slots=True)
class Market:
    symbol: str
    market_id: int
    tick_size: float
    step_size: float
    min_notional: float
    mark_price: float


@dataclass(slots=True)
class Order:
    id: str
    venue: str
    symbol: str
    side: Side
    size: float
    price: float
    reduce_only: bool
    status: OrderStatus = OrderStatus.OPEN
    filled_size: float = 0.0
    average_fill_price: float | None = None
    created_at: datetime = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, object]:
        result = asdict(self)
        result["side"] = self.side.value
        result["status"] = self.status.value
        result["created_at"] = self.created_at.isoformat()
        return result


@dataclass(slots=True)
class Position:
    venue: str
    symbol: str
    signed_size: float
    entry_price: float
    mark_price: float
    liquidation_price: float | None
    unrealized_pnl: float

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(slots=True)
class Cycle:
    id: str
    symbol: str
    arcus_side: Side
    target_size: float
    notional_usd: float
    opened_size: float = 0.0
    closed_size: float = 0.0
    arcus_open_price: float | None = None
    risex_open_price: float | None = None
    arcus_close_price: float | None = None
    risex_close_price: float | None = None
    realized_pnl: float = 0.0
    fees: float = 0.0
    started_at: datetime = field(default_factory=utc_now)
    hold_until: datetime | None = None
    ended_at: datetime | None = None
    status: str = "OPENING"

    def to_dict(self) -> dict[str, object]:
        result = asdict(self)
        result["arcus_side"] = self.arcus_side.value
        for key in ("started_at", "hold_until", "ended_at"):
            value = result[key]
            result[key] = value.isoformat() if value else None
        return result
