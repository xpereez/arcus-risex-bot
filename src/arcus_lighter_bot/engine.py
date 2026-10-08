from __future__ import annotations

import asyncio
import random
import time
import uuid
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal, ROUND_DOWN

from .config import Settings
from .models import Cycle, EnginePhase, OrderStatus, Side, utc_now
from .storage import Storage
from .venues.base import Venue


@dataclass(slots=True)
class LegResult:
    filled_size: float
    arcus_average_price: float | None
    risex_average_price: float | None


class BotEngine:
    def __init__(self, settings: Settings, arcus: Venue, risex: Venue, storage: Storage) -> None:
        self.settings = settings
        self.arcus = arcus
        self.risex = risex
        self.storage = storage
        self.phase = EnginePhase.STOPPED
        self.current_cycle: Cycle | None = None
        self.next_action_at = None
        self.last_error: str | None = None
        self.accepting_cycles = False
        self._task: asyncio.Task | None = None
        self._rng = random.Random()
        self._phase_started = utc_now()
        self._cycles_completed = 0
        self._session_volume = 0.0
        self._session_pnl = 0.0
        self._lock = asyncio.Lock()
        self._paper_timeout_floor = 3.0

    async def start(self) -> None:
        async with self._lock:
            self.accepting_cycles = True
            if self._task is None or self._task.done():
                self._task = asyncio.create_task(self._run(), name="delta-neutral-engine")
            if self.phase in {EnginePhase.STOPPED, EnginePhase.PAUSED, EnginePhase.ERROR}:
                self._set_phase(EnginePhase.IDLE)
            self.storage.event("control", "Motor iniciado", {"mode": self.settings.mode})

    async def pause(self) -> None:
        self.accepting_cycles = False
        self.storage.event("control", "Pausa solicitada; se completará el ciclo actual")
        if self.current_cycle is None:
            self._set_phase(EnginePhase.PAUSED)

    async def shutdown(self) -> None:
        await self.pause()
        if self._task and not self._task.done():
            await self._task
        await asyncio.gather(self.arcus.close(), self.risex.close())

    async def _run(self) -> None:
        try:
            arcus_markets, risex_markets = await asyncio.gather(
                self.arcus.markets(), self.risex.markets()
            )
            await self._preflight_flat_accounts()
            available = tuple(
                symbol
                for symbol in self.settings.markets
                if symbol in arcus_markets and symbol in risex_markets
            )
            if not available:
                raise RuntimeError("No configured market is available on both venues")
            while self.accepting_cycles:
                if self._session_pnl <= -self.settings.max_daily_loss_usd:
                    raise RuntimeError("Daily loss limit reached")
                await self._execute_cycle(available, arcus_markets, risex_markets)
                if not self.accepting_cycles:
                    break
                pause_minutes = self._rng.uniform(
                    self.settings.cycle_pause_min_minutes,
                    self.settings.cycle_pause_max_minutes,
                )
                await self._wait_phase(EnginePhase.COOLDOWN, pause_minutes * 60)
            self._set_phase(EnginePhase.PAUSED)
        except asyncio.CancelledError:
            self._set_phase(EnginePhase.STOPPED)
            raise
        except Exception as exc:
            self.last_error = str(exc)
            self._set_phase(EnginePhase.ERROR)
            self.accepting_cycles = False
            self.storage.event("engine_error", str(exc), level="error")

    async def _execute_cycle(self, symbols: tuple[str, ...], arcus_markets: dict, risex_markets: dict) -> None:
        symbol = self._rng.choice(symbols)
        side = self._rng.choice((Side.BUY, Side.SELL))
        notional = self._rng.uniform(self.settings.min_notional_usd, self.settings.max_notional_usd)
        bid, ask = await self.arcus.best_bid_ask(symbol)
        reference = (bid + ask) / 2
        step = max(arcus_markets[symbol].step_size, risex_markets[symbol].step_size)
        size = self._round_down(notional / reference, step)
        minimum = max(arcus_markets[symbol].min_notional, risex_markets[symbol].min_notional)
        if size <= 0 or size * reference < minimum:
            size = self._round_up(minimum / reference, step)
            notional = size * reference

        cycle = Cycle(
            id=f"cycle-{uuid.uuid4().hex[:12]}",
            symbol=symbol,
            arcus_side=side,
            target_size=size,
            notional_usd=notional,
        )
        self.current_cycle = cycle
        self.storage.save_cycle(cycle)
        self.storage.event(
            "cycle_started",
            f"Apertura {symbol}: Arcus {side.value}, RiseX {side.opposite.value}",
            {"cycle_id": cycle.id, "size": size, "notional_usd": notional},
        )

        opening = await self._arcus_then_risex(
            symbol=symbol,
            arcus_side=side,
            target_size=size,
            reduce_only=False,
            phase=EnginePhase.OPENING,
        )
        cycle.opened_size = opening.filled_size
        cycle.arcus_open_price = opening.arcus_average_price
        cycle.risex_open_price = opening.risex_average_price
        if opening.filled_size <= step / 2:
            cycle.status = "CANCELED_NO_FILL"
            cycle.ended_at = utc_now()
            self.storage.save_cycle(cycle)
            self.storage.event("cycle_skipped", f"{symbol}: orden Arcus sin fill")
            self.current_cycle = None
            return

        await self._assert_delta_neutral(symbol, step)

        hold_minutes = self._rng.uniform(self.settings.hold_min_minutes, self.settings.hold_max_minutes)
        cycle.status = "HOLDING"
        cycle.hold_until = utc_now() + timedelta(seconds=self.settings.scaled_seconds(hold_minutes * 60))
        self.storage.save_cycle(cycle)
        await self._wait_phase(EnginePhase.HOLDING, hold_minutes * 60)

        remaining = opening.filled_size
        arcus_close_value = 0.0
        risex_close_value = 0.0
        attempts = 0
        while remaining > step / 2:
            attempts += 1
            if attempts > 20:
                raise RuntimeError(f"Unable to close {symbol} on Arcus after 20 maker attempts")
            closing = await self._arcus_then_risex(
                symbol=symbol,
                arcus_side=side.opposite,
                target_size=remaining,
                reduce_only=True,
                phase=EnginePhase.CLOSING,
            )
            if closing.filled_size > 0:
                remaining = max(0.0, remaining - closing.filled_size)
                cycle.closed_size += closing.filled_size
                arcus_close_value += (closing.arcus_average_price or 0) * closing.filled_size
                risex_close_value += (closing.risex_average_price or 0) * closing.filled_size
                await self._assert_delta_neutral(symbol, step)
            if remaining > step / 2:
                await asyncio.sleep(self.settings.scaled_seconds(3))

        cycle.arcus_close_price = arcus_close_value / cycle.closed_size
        cycle.risex_close_price = risex_close_value / cycle.closed_size
        arcus_sign = 1 if side is Side.BUY else -1
        cycle.realized_pnl = arcus_sign * cycle.closed_size * (
            (cycle.arcus_close_price - (cycle.arcus_open_price or cycle.arcus_close_price))
            - (cycle.risex_close_price - (cycle.risex_open_price or cycle.risex_close_price))
        )
        cycle.status = "COMPLETED"
        cycle.ended_at = utc_now()
        self._cycles_completed += 1
        self._session_volume += cycle.closed_size * (
            (cycle.arcus_open_price or 0)
            + (cycle.risex_open_price or 0)
            + (cycle.arcus_close_price or 0)
            + (cycle.risex_close_price or 0)
        )
        self._session_pnl += cycle.realized_pnl - cycle.fees
        self.storage.save_cycle(cycle)
        self.storage.event(
            "cycle_completed",
            f"Ciclo {symbol} cerrado",
            {"cycle_id": cycle.id, "pnl": cycle.realized_pnl, "volume": self._session_volume},
        )
        self.current_cycle = None

    async def _arcus_then_risex(
        self,
        *,
        symbol: str,
        arcus_side: Side,
        target_size: float,
        reduce_only: bool,
        phase: EnginePhase,
    ) -> LegResult:
        self._set_phase(phase)
        bid, ask = await self.arcus.best_bid_ask(symbol)
        maker_price = bid if arcus_side is Side.BUY else ask
        order = await self.arcus.place_limit(
            symbol, arcus_side, target_size, maker_price, reduce_only
        )
        self.storage.event(
            "arcus_order",
            f"Limit {arcus_side.value} {symbol} enviada",
            {"order_id": order.id, "size": target_size, "price": maker_price, "reduce_only": reduce_only},
        )
        hedged = 0.0
        risex_value = 0.0
        timeout = (
            max(self._paper_timeout_floor, self.settings.scaled_seconds(self.settings.order_timeout_seconds))
            if self.settings.mode == "paper"
            else float(self.settings.order_timeout_seconds)
        )
        deadline = time.monotonic() + timeout
        latest = order
        while time.monotonic() < deadline:
            latest = await self.arcus.get_order(order.id, symbol)
            delta = latest.filled_size - hedged
            if delta > 1e-12:
                self._set_phase(
                    EnginePhase.HEDGING_CLOSE if reduce_only else EnginePhase.HEDGING_OPEN
                )
                hedge = await asyncio.wait_for(
                    self.risex.place_market(
                        symbol,
                        arcus_side.opposite,
                        delta,
                        reduce_only,
                        self.settings.max_slippage_bps,
                    ),
                    timeout=self.settings.max_unhedged_seconds,
                )
                if hedge.status is not OrderStatus.FILLED or abs(hedge.filled_size - delta) > 1e-9:
                    raise RuntimeError(f"Incomplete RiseX hedge for {symbol}: {hedge.filled_size}/{delta}")
                hedged += delta
                risex_value += (hedge.average_fill_price or hedge.price) * delta
                self.storage.event(
                    "partial_hedge",
                    f"Hedge RiseX {symbol}: {delta:.8f}",
                    {"arcus_order_id": order.id, "cumulative_hedged": hedged, "reduce_only": reduce_only},
                )
                self._set_phase(phase)
            if latest.status in {OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.REJECTED}:
                break
            await asyncio.sleep(
                self.settings.scaled_seconds(self.settings.order_poll_interval_seconds)
            )

        if latest.status not in {OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.REJECTED}:
            await self.arcus.cancel_order(order.id, symbol)
            latest = await self.arcus.get_order(order.id, symbol)
            final_delta = latest.filled_size - hedged
            if final_delta > 1e-12:
                hedge = await self.risex.place_market(
                    symbol,
                    arcus_side.opposite,
                    final_delta,
                    reduce_only,
                    self.settings.max_slippage_bps,
                )
                hedged += hedge.filled_size
                risex_value += (hedge.average_fill_price or hedge.price) * hedge.filled_size
            self.storage.event(
                "arcus_timeout",
                f"Orden Arcus cancelada tras timeout ({hedged:.8f}/{target_size:.8f})",
                {"order_id": order.id, "filled_size": hedged},
                level="warning",
            )

        if abs(hedged - latest.filled_size) > 1e-9:
            raise RuntimeError(f"Delta mismatch after Arcus order {order.id}")
        return LegResult(
            filled_size=hedged,
            arcus_average_price=latest.average_fill_price or (maker_price if hedged else None),
            risex_average_price=risex_value / hedged if hedged else None,
        )

    async def _wait_phase(self, phase: EnginePhase, real_seconds: float) -> None:
        duration = self.settings.scaled_seconds(real_seconds)
        self._set_phase(phase)
        self.next_action_at = utc_now() + timedelta(seconds=duration)
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline:
            if phase in {EnginePhase.HOLDING, EnginePhase.COOLDOWN} and not self.accepting_cycles:
                break
            await asyncio.sleep(min(1.0, deadline - time.monotonic()))
        self.next_action_at = None

    async def _preflight_flat_accounts(self) -> None:
        arcus_positions, risex_positions = await asyncio.gather(
            self.arcus.positions(), self.risex.positions()
        )
        existing = [
            position
            for position in [*arcus_positions, *risex_positions]
            if abs(position.signed_size) > 1e-10
        ]
        if existing:
            details = ", ".join(
                f"{position.venue}:{position.symbol}={position.signed_size}"
                for position in existing
            )
            raise RuntimeError(
                "Existing positions detected at startup; reconcile before starting new cycles: " + details
            )

    async def _assert_delta_neutral(self, symbol: str, step: float) -> None:
        deadline = time.monotonic() + self.settings.scaled_seconds(
            self.settings.max_unhedged_seconds
        )
        while True:
            arcus_positions, risex_positions = await asyncio.gather(
                self.arcus.positions(), self.risex.positions()
            )
            residual = sum(
                position.signed_size
                for position in [*arcus_positions, *risex_positions]
                if position.symbol == symbol
            )
            if abs(residual) <= step / 2 + 1e-12:
                return
            if time.monotonic() >= deadline:
                raise RuntimeError(f"Residual delta for {symbol}: {residual}")
            await asyncio.sleep(
                self.settings.scaled_seconds(self.settings.order_poll_interval_seconds)
            )

    def _set_phase(self, phase: EnginePhase) -> None:
        self.phase = phase
        self._phase_started = utc_now()

    async def snapshot(self) -> dict[str, object]:
        accounts, arcus_positions, risex_positions = await asyncio.gather(
            asyncio.gather(self.arcus.account(), self.risex.account()),
            self.arcus.positions(),
            self.risex.positions(),
        )
        tracked_accounts = [self._account_with_volume_tracking(account) for account in accounts]
        all_positions = [*arcus_positions, *risex_positions]
        net_delta: dict[str, float] = {}
        for position in all_positions:
            net_delta[position.symbol] = net_delta.get(position.symbol, 0.0) + position.signed_size
        return {
            "mode": self.settings.mode,
            "phase": self.phase.value,
            "accepting_cycles": self.accepting_cycles,
            "phase_started_at": self._phase_started.isoformat(),
            "next_action_at": self.next_action_at.isoformat() if self.next_action_at else None,
            "last_error": self.last_error,
            "current_cycle": self.current_cycle.to_dict() if self.current_cycle else None,
            "accounts": tracked_accounts,
            "positions": [position.to_dict() for position in all_positions],
            "net_delta": net_delta,
            "stats": {
                "cycles_completed": self._cycles_completed,
                "session_volume": self._session_volume,
                "session_pnl": self._session_pnl,
            },
            "events": self.storage.recent_events(),
            "cycles": self.storage.recent_cycles(),
            "config": self.settings.public_dict(),
        }

    def _account_with_volume_tracking(self, account: dict[str, object]) -> dict[str, object]:
        enriched = dict(account)
        lifetime_volume = account.get("lifetime_volume_usd")
        if lifetime_volume is None:
            enriched["volume_since_baseline_usd"] = None
            enriched["volume_baseline_started_at"] = None
            return enriched

        venue = str(account["venue"])
        baseline = self.storage.set_metadata_if_absent(
            f"volume_baseline:{venue}",
            {"volume_usd": float(lifetime_volume), "started_at": utc_now().isoformat()},
        )
        if not isinstance(baseline, dict):
            raise ValueError(f"Invalid volume baseline for {venue}")
        baseline_volume = float(baseline["volume_usd"])
        enriched["volume_since_baseline_usd"] = max(
            0.0, float(lifetime_volume) - baseline_volume
        )
        enriched["volume_baseline_started_at"] = baseline["started_at"]
        return enriched

    @staticmethod
    def _round_down(value: float, step: float) -> float:
        decimal_step = Decimal(str(step))
        return float((Decimal(str(value)) / decimal_step).to_integral_value(rounding=ROUND_DOWN) * decimal_step)

    @staticmethod
    def _round_up(value: float, step: float) -> float:
        down = BotEngine._round_down(value, step)
        return down if down >= value else down + step
