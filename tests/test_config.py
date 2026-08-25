from __future__ import annotations

import pytest

from arcus_lighter_bot.config import Settings


def test_live_mode_is_locked_without_explicit_ack(monkeypatch):
    monkeypatch.setenv("BOT_MODE", "live")
    monkeypatch.setenv("ENABLE_LIVE_TRADING", "false")
    with pytest.raises(ValueError, match="Live mode is locked"):
        Settings.from_env()


def test_public_config_never_contains_secrets(monkeypatch):
    monkeypatch.setenv("ARCUS_API_KEY", "secret-api-key")
    monkeypatch.setenv("ARCUS_API_PRIVATE_KEY", "secret-private-key")
    public = Settings.from_env().public_dict()
    assert "arcus_api_key" not in public
    assert "arcus_api_private_key" not in public
    assert "secret" not in str(public)


def test_robinhood_lighter_uses_its_chain_id(monkeypatch):
    monkeypatch.delenv("LIGHTER_CHAIN_ID", raising=False)
    monkeypatch.setenv("LIGHTER_API_URL", "https://api.rh.lighter.xyz")
    assert Settings.from_env().lighter_chain_id == 466324
