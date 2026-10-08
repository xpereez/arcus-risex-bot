from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .config import Settings, load_env_file
from .engine import BotEngine
from .storage import Storage
from .venues.arcus import ArcusVenue
from .venues.risex import RiseXVenue
from .venues.paper import PaperVenue


ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "static"
load_env_file(str(ROOT / ".env"))
settings = Settings.from_env()
storage = Storage(str(ROOT / settings.database_path) if not Path(settings.database_path).is_absolute() else settings.database_path)

if settings.mode == "paper":
    arcus = PaperVenue("arcus", seed=11)
    risex_venue = PaperVenue("risex", seed=17)
else:
    arcus = ArcusVenue(settings)
    risex_venue = RiseXVenue(settings)

engine = BotEngine(settings, arcus, risex_venue, storage)


@asynccontextmanager
async def lifespan(_: FastAPI):
    if settings.auto_start:
        await engine.start()
    try:
        yield
    finally:
        await engine.shutdown()


app = FastAPI(title="Arcus / RiseX Control", lifespan=lifespan)
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
    return {"generated_at": datetime.now(UTC).isoformat(), "bots": {"arcus_risex": arcus_state}}


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
