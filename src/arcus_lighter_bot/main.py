from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .config import Settings, load_env_file
from .engine import BotEngine
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


@asynccontextmanager
async def lifespan(_: FastAPI):
    if settings.auto_start:
        await engine.start()
    yield
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

