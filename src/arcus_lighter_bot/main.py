from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
import os
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .config import Settings, load_env_file
from .engine import BotEngine
from .market_making_dashboard import MarketMakingDashboard
from .market_making_process import MarketMakingProcessManager
from .storage import Storage
from .venues.arcus import ArcusVenue
from .venues.lighter import LighterVenue
from .venues.paper import PaperVenue


ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "static"
load_env_file(str(ROOT / ".env"))
settings = Settings.from_env()
storage = Storage(str(ROOT / settings.database_path) if not Path(settings.database_path).is_absolute() else settings.database_path)

if settings.mode == "paper":
    arcus = PaperVenue("arcus", seed=11)
    lighter_venue = PaperVenue("lighter", seed=17)
else:
    arcus = ArcusVenue(settings)
    lighter_venue = LighterVenue(settings)

engine = BotEngine(settings, arcus, lighter_venue, storage)
market_making_database = Path(
    os.getenv(
        "MARKET_MAKING_LIGHTER_DATABASE_PATH",
        str(ROOT.parent / "marketMakingLighter" / "data" / "market_maker.sqlite3"),
    )
)
market_making_dashboard = MarketMakingDashboard(market_making_database)
market_making_project = Path(
    os.getenv("MARKET_MAKING_LIGHTER_PROJECT_PATH", str(ROOT.parent / "marketMakingLighter"))
)
market_making_auto_start = os.getenv("MARKET_MAKING_AUTO_START", "true").lower() in {
    "1", "true", "yes", "on"
}
market_making_process = MarketMakingProcessManager(
    market_making_project, auto_start=market_making_auto_start
)


def market_making_snapshot() -> dict[str, object]:
    state = market_making_dashboard.snapshot()
    process = market_making_process.status()
    state["process"] = process
    if process["running"] and not state["running"]:
        state["running"] = True
        state["state"] = "STARTING"
    if process["last_error"]:
        state["last_reason"] = process["last_error"]
    return state


@asynccontextmanager
async def lifespan(_: FastAPI):
    if settings.auto_start:
        await engine.start()
    if market_making_process.auto_start:
        try:
            await market_making_process.start()
        except (FileNotFoundError, RuntimeError):
            pass
    try:
        yield
    finally:
        await market_making_process.shutdown()
        await engine.shutdown()


app = FastAPI(title="Arcus / Lighter Control", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/api/state")
async def state() -> dict[str, object]:
    try:
        return await engine.snapshot()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/api/bots")
async def bots() -> dict[str, object]:
    try:
        arcus_state: dict[str, object] = await engine.snapshot()
    except Exception as exc:
        arcus_state = {
            "mode": settings.mode,
            "phase": "UNAVAILABLE",
            "accepting_cycles": False,
            "last_error": str(exc),
            "accounts": [],
            "positions": [],
            "net_delta": {},
            "stats": {"cycles_completed": 0, "session_volume": 0, "session_pnl": 0},
            "events": [],
            "config": settings.public_dict(),
        }
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "bots": {
            "arcus_lighter": arcus_state,
            "market_making_lighter": market_making_snapshot(),
        },
    }


@app.get("/api/bots/market-making-lighter")
async def market_making_state() -> dict[str, object]:
    return market_making_snapshot()


@app.post("/api/bots/market-making-lighter/start")
async def start_market_making() -> dict[str, object]:
    try:
        await market_making_process.start()
    except (FileNotFoundError, RuntimeError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return market_making_snapshot()


@app.post("/api/bots/market-making-lighter/stop")
async def stop_market_making() -> dict[str, object]:
    await market_making_process.stop()
    return market_making_snapshot()


@app.post("/api/start")
async def start() -> dict[str, bool]:
    await engine.start()
    return {"ok": True}


@app.post("/api/pause")
async def pause() -> dict[str, bool]:
    await engine.pause()
    return {"ok": True}


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "mode": settings.mode}


def run() -> None:
    uvicorn.run("arcus_lighter_bot.main:app", host="127.0.0.1", port=8787, reload=False)


if __name__ == "__main__":
    run()
