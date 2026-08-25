from __future__ import annotations

import asyncio
import pytest

from arcus_lighter_bot.market_making_process import MarketMakingProcessManager


@pytest.mark.asyncio
async def test_manager_starts_and_stops_shadow_process(tmp_path) -> None:
    executable = tmp_path / ".venv" / "bin" / "market-making-lighter"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\ntrap 'exit 0' INT TERM\nwhile :; do sleep 1; done\n")
    executable.chmod(0o755)
    manager = MarketMakingProcessManager(tmp_path)

    started = await manager.start()
    assert started["running"] is True
    assert int((tmp_path / "data" / "market_maker.pid").read_text()) == started["pid"]

    stopped = await manager.stop(timeout_seconds=2)
    assert stopped["running"] is False
    assert not (tmp_path / "data" / "market_maker.pid").exists()


@pytest.mark.asyncio
async def test_manager_forces_shadow_mode(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    executable = tmp_path / ".venv" / "bin" / "market-making-lighter"
    executable.parent.mkdir(parents=True)
    output = tmp_path / "mode.txt"
    executable.write_text(
        f"#!/bin/sh\nprintf '%s' \"$MM_MODE\" > '{output}'\ntrap 'exit 0' INT TERM\nsleep 5\n"
    )
    executable.chmod(0o755)
    manager = MarketMakingProcessManager(tmp_path)
    monkeypatch.setenv("MM_MODE", "live")

    await manager.start()
    for _ in range(20):
        if output.exists():
            break
        await asyncio.sleep(0.01)
    assert output.read_text() == "shadow"
    await manager.stop(timeout_seconds=2)
