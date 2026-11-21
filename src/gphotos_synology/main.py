"""FastAPI application factory, lifespan manager, and CLI entrypoint."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from gphotos_synology import __version__
from gphotos_synology.config import ConfigManager, Settings
from gphotos_synology.database import StateDB
from gphotos_synology.web.routes import create_api_router
from gphotos_synology.worker.pipeline import MigrationPipeline
from gphotos_synology.worker.scheduler import BackgroundScheduler

STATIC_DIR = Path(__file__).resolve().parent / "web" / "static"


@dataclass
class AppContext:
    config_manager: ConfigManager
    db: StateDB
    pipeline: MigrationPipeline
    scheduler: BackgroundScheduler


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    )


def create_app(
    base_settings: Settings | None = None,
    *,
    start_worker: bool = True,
) -> FastAPI:
    """Create and wire the FastAPI app with SQLite state, pipeline, and scheduler."""
    configure_logging()
    config_manager = ConfigManager(base_settings=base_settings)
    db = StateDB(config_manager.settings.db_path)
    pipeline = MigrationPipeline(config_manager=config_manager, db=db)
    scheduler = BackgroundScheduler(config_manager=config_manager, db=db, pipeline=pipeline)
    ctx = AppContext(
        config_manager=config_manager,
        db=db,
        pipeline=pipeline,
        scheduler=scheduler,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if start_worker:
            ctx.scheduler.start()
        try:
            yield
        finally:
            if start_worker:
                ctx.scheduler.stop()

    fastapi_app = FastAPI(
        title="Google Photos -> Synology DSM Migrator",
        version=__version__,
        lifespan=lifespan,
    )
    fastapi_app.state.ctx = ctx
    fastapi_app.include_router(create_api_router())

    if STATIC_DIR.is_dir():
        fastapi_app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

        @fastapi_app.get("/", include_in_schema=False)
        def serve_dashboard() -> FileResponse:
            return FileResponse(STATIC_DIR / "index.html")

    return fastapi_app


def cli() -> None:
    settings = Settings()
    uvicorn.run(
        "gphotos_synology.main:create_app",
        host="0.0.0.0",
        port=settings.app_port,
        factory=True,
        reload=False,
    )


if __name__ == "__main__":
    cli()
