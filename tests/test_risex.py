from types import SimpleNamespace

import pytest

from arcus_lighter_bot.models import Side
from arcus_lighter_bot.venues.risex import RiseXVenue


def test_risex_base_symbol_normalization() -> None:
    assert RiseXVenue._base_symbol("BTC-PERP") == "BTC"
    assert RiseXVenue._base_symbol("ETH/USDC") == "ETH"


def test_risex_side_normalization() -> None:
    assert RiseXVenue._side_from_value("BUY") is Side.BUY
    assert RiseXVenue._side_from_value("SELL") is Side.SELL
    assert RiseXVenue._side_from_value(0) is Side.BUY


def test_risex_step_rounding() -> None:
    assert RiseXVenue._steps(0.00129, 0.0001) == 12
    assert RiseXVenue._steps(63218.56, 0.1, "ROUND_HALF_UP") == 632186


def test_risex_position_fields_are_supported() -> None:
    position = SimpleNamespace(
        market_id=1,
        size="0.1",
        side=1,
        model_extra={"avg_entry_price": "60000"},
    )
    assert position.side == 1
    assert float(position.size) == pytest.approx(0.1)
