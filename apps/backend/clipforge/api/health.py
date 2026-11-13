"""GET /v1/health."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

from clipforge import __version__
from clipforge.config import Config, Mode


class HealthResponse(BaseModel):
    status: Literal["ok"]
    mode: Mode
    version: str


def build_router(config: Config) -> APIRouter:
    router = APIRouter()

    @router.get("/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        return HealthResponse(status="ok", mode=config.mode, version=__version__)

    return router
