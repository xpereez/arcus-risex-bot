from arcus_lighter_bot.venues.lighter import LighterVenue


def test_client_order_id_fits_lighter_uint48():
    client_id = LighterVenue._client_order_id()
    assert 0 <= client_id <= (1 << 48) - 1


def test_lighter_position_sign_is_applied_to_absolute_size():
    assert LighterVenue._signed_position_size({"position": "2.5", "sign": -1}) == -2.5
    assert LighterVenue._signed_position_size({"position": "2.5", "sign": 1}) == 2.5


def test_lighter_lifetime_volume_uses_protocol_total():
    assert LighterVenue._lifetime_volume_from_payload({"total_volume": 1484.113563}) == 1484.113563
