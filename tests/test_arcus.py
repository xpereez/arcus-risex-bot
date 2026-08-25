import httpx

from arcus_lighter_bot.venues.arcus import ArcusVenue


def test_arcus_positions_support_market_id_mapping():
    payload = {
        "positions": {
            "1": {
                "marketDisplayName": "BTC-USD",
                "size": "-0.00032",
                "averageEntryPrice": "77639.2",
                "markPx": "77531.6",
                "unrealizedPnl": "0.034432",
            }
        }
    }

    positions = ArcusVenue._positions_from_payload(payload)

    assert len(positions) == 1
    assert positions[0].symbol == "BTC"
    assert positions[0].signed_size == -0.00032
    assert positions[0].entry_price == 77639.2
    assert positions[0].mark_price == 77531.6


def test_arcus_retry_after_uses_server_hint():
    response = httpx.Response(429, headers={"Retry-After": "3"})
    assert ArcusVenue._retry_after_seconds(response) == 3.0


def test_arcus_retry_after_has_safe_fallback():
    response = httpx.Response(429, headers={"Retry-After": "invalid"})
    assert ArcusVenue._retry_after_seconds(response) == 1.0


def test_arcus_lifetime_volume_converts_nano_usd():
    payload = {"lifetimeVolume": 90_703_091_239_118}

    assert ArcusVenue._lifetime_volume_from_payload(payload) == 90_703.091239118
