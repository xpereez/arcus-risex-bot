from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from arcus_lighter_bot.config import Settings
from arcus_lighter_bot.engine import BotEngine
from arcus_lighter_bot.models import EnginePhase, Position, Side
from arcus_lighter_bot.storage import Storage
from arcus_lighter_bot.venues.paper import PaperVenue


class TrackingPaperVenue(PaperVenue):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.market_orders = []

    async def place_market(self, symbol, side, size, reduce_only, max_slippage_bps):
        order = await super().place_market(symbol, side, size, reduce_only, max_slippage_bps)
        self.market_orders.append(order)
        return order


def settings(**changes) -> Settings:
    base = Settings.from_env()
    return replace(
        base,
        markets=("BTC",),
        min_notional_usd=25,
        max_notional_usd=25,
        hold_min_minutes=0,
        hold_max_minutes=0,
        cycle_pause_min_minutes=0,
        cycle_pause_max_minutes=0,
        paper_time_scale=100,
        **changes,
    )


@pytest.mark.asyncio
async def test_each_partial_arcus_fill_is_hedged_incrementally(tmp_path):
    arcus = PaperVenue("arcus", fill_plan=[(0.01, 0.25), (0.03, 0.60), (0.05, 1.0)])
    risex = TrackingPaperVenue("risex")
    engine = BotEngine(settings(), arcus, risex, Storage(str(tmp_path / "bot.sqlite3")))
    engine._paper_timeout_floor = 0.2

    result = await engine._arcus_then_risex(
        symbol="BTC",
        arcus_side=Side.BUY,
        target_size=0.001,
        reduce_only=False,
        phase=EnginePhase.OPENING,
    )

    assert result.filled_size == pytest.approx(0.001)
    assert sum(order.filled_size for order in risex.market_orders) == pytest.approx(0.001)
    assert all(order.side is Side.SELL for order in risex.market_orders)


@pytest.mark.asyncio
async def test_timeout_cancels_arcus_and_keeps_partial_fill_hedged(tmp_path):
    arcus = PaperVenue("arcus", fill_plan=[(0.01, 0.40)])
    risex = TrackingPaperVenue("risex")
    storage = Storage(str(tmp_path / "bot.sqlite3"))
    engine = BotEngine(settings(), arcus, risex, storage)
    engine._paper_timeout_floor = 0.12

    result = await engine._arcus_then_risex(
        symbol="BTC",
        arcus_side=Side.SELL,
        target_size=0.001,
        reduce_only=False,
        phase=EnginePhase.OPENING,
    )

    assert result.filled_size == pytest.approx(0.0004)
    assert sum(order.filled_size for order in risex.market_orders) == pytest.approx(0.0004)
    assert storage.recent_events()[0]["kind"] == "arcus_timeout"


@pytest.mark.asyncio
async def test_complete_cycle_opens_and_closes_with_inverse_hedges(tmp_path):
    arcus = PaperVenue("arcus", fill_plan=[(0.0, 1.0)])
    risex = TrackingPaperVenue("risex")
    engine = BotEngine(settings(), arcus, risex, Storage(str(tmp_path / "bot.sqlite3")))
    engine._paper_timeout_floor = 0.1
    arcus_markets, risex_markets = await arcus.markets(), await risex.markets()

    await engine._execute_cycle(("BTC",), arcus_markets, risex_markets)

    assert engine.current_cycle is None
    assert engine._cycles_completed == 1
    assert len(risex.market_orders) == 2
    assert risex.market_orders[0].side is risex.market_orders[1].side.opposite
    assert await arcus.positions() == []
    assert await risex.positions() == []
    cycle = engine.storage.recent_cycles(1)[0]
    expected_volume = cycle["closed_size"] * sum(
        cycle[key]
        for key in (
            "arcus_open_price",
            "risex_open_price",
            "arcus_close_price",
            "risex_close_price",
        )
    )
    assert engine._session_volume == pytest.approx(expected_volume)


@pytest.mark.asyncio
async def test_delta_check_retries_until_risex_position_is_visible(tmp_path):
    long = Position("arcus", "BTC", 0.001, 100, 100, None, 0)
    short = Position("risex", "BTC", -0.001, 100, 100, None, 0)
    arcus = SimpleNamespace(positions=AsyncMock(return_value=[long]))
    risex = SimpleNamespace(positions=AsyncMock(side_effect=[[], [short]]))
    engine = BotEngine(settings(), arcus, risex, Storage(str(tmp_path / "bot.sqlite3")))

    await engine._assert_delta_neutral("BTC", 0.00001)

    assert risex.positions.await_count == 2


def test_wallet_volume_is_measured_from_persisted_baseline(tmp_path):
    storage = Storage(str(tmp_path / "bot.sqlite3"))
    storage.set_metadata_if_absent(
        "volume_baseline:arcus",
        {"volume_usd": 1000.0, "started_at": "2026-08-25T07:30:19Z"},
    )
    engine = BotEngine(
        settings(), PaperVenue("arcus"), PaperVenue("risex"), storage
    )

    account = engine._account_with_volume_tracking(
        {"venue": "arcus", "lifetime_volume_usd": 1125.5}
    )

    assert account["volume_since_baseline_usd"] == 125.5
    assert account["volume_baseline_started_at"] == "2026-08-25T07:30:19Z"
