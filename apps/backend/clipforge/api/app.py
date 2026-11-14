"""FastAPI application factory."""

from __future__ import annotations

from fastapi import FastAPI

from clipforge import __version__
from clipforge.api import clips, exports, health, jobs, projects, sources
from clipforge.api.errors import UnexpectedErrorMiddleware, install_error_handlers
from clipforge.api.security import LocalAccessMiddleware, allowed_hosts
from clipforge.config import Config
from clipforge.services.runtime import Runtime


def create_app(config: Config, *, port: int, runtime: Runtime) -> FastAPI:
    """Build the app; ``port`` is the actual bound port, used for the Host check.

    ``runtime`` is owned by the caller, which closes it after the server has stopped.
    """
    # No docs/openapi routes: nothing is served that is not part of the API.
    app = FastAPI(
        title="ClipForge Local",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    install_error_handlers(app)
    app.include_router(health.build_router(config), prefix="/v1")
    for module in (projects, sources, clips, exports, jobs):
        app.include_router(module.build_router(runtime), prefix="/v1")
    app.add_middleware(
        LocalAccessMiddleware,
        session_token=config.session_token,
        allowed_hosts=allowed_hosts(port),
    )
    # Added last = outermost, so it also covers LocalAccessMiddleware.
    app.add_middleware(UnexpectedErrorMiddleware)
    return app
