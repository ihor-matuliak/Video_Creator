"""GET and PATCH /v1/settings; POST and DELETE /v1/runtime/credentials and POST
/v1/runtime/credentials/check (FR-048, docs/04 section 4).

The settings are the four non-secret ones of ``clipforge.services.settings``; PATCH rejects any
other key with 422. The API key is write-only: it is accepted by POST /v1/runtime/credentials,
kept in memory, and no response ever carries it (only ``has_api_key`` shows that one is set).
The check answers only whether OpenRouter accepts the key, the free-tier flag and the remaining
limit; it never returns the label or any other field of the provider's answer.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict

from clipforge.adapters.openrouter_key import KeyAccepted
from clipforge.services.runtime import Runtime
from clipforge.services.settings import Settings


class SettingsOut(BaseModel):
    stt_model: str
    stt_language: str | None
    analysis_model: str | None
    run_budget_usd: str | None
    has_api_key: bool


class SettingsPatch(BaseModel):
    """Only the fields that are present are changed; ``null`` resets a setting to its default.
    The values themselves are checked by the service (422 with a Ukrainian message)."""

    model_config = ConfigDict(extra="forbid", strict=True)

    stt_model: str | None = None
    stt_language: str | None = None
    analysis_model: str | None = None
    run_budget_usd: str | None = None


class CredentialsBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    openrouter_api_key: str


class KeyValidOut(BaseModel):
    state: Literal["valid"]
    is_free_tier: bool
    # A plain decimal string (USD), or null when the key has no spending limit.
    limit_remaining_usd: str | None


class KeyInvalidOut(BaseModel):
    state: Literal["invalid"]


def settings_out(settings: Settings) -> SettingsOut:
    return SettingsOut(
        stt_model=settings.stt_model,
        stt_language=settings.stt_language,
        analysis_model=settings.analysis_model,
        run_budget_usd=settings.run_budget_usd,
        has_api_key=settings.has_api_key,
    )


def build_router(runtime: Runtime) -> APIRouter:
    router = APIRouter()

    # Plain `def` routes run in FastAPI's thread pool: SQLite calls block.
    @router.get("/settings", response_model=SettingsOut)
    def get_settings() -> SettingsOut:
        return settings_out(runtime.settings.get())

    @router.patch("/settings", response_model=SettingsOut)
    def patch_settings(body: SettingsPatch) -> SettingsOut:
        changes = {name: getattr(body, name) for name in body.model_fields_set}
        return settings_out(runtime.settings.update(changes))

    @router.post("/runtime/credentials", status_code=204)
    def set_credentials(body: CredentialsBody) -> None:
        runtime.credentials.set_openrouter_key(body.openrouter_api_key)

    @router.delete("/runtime/credentials", status_code=204)
    def delete_credentials() -> None:
        runtime.credentials.clear_openrouter_key()

    # One blocking request of up to 15 s: a plain `def` runs in the thread pool, not on the loop.
    @router.post("/runtime/credentials/check", response_model=KeyValidOut | KeyInvalidOut)
    def check_credentials() -> KeyValidOut | KeyInvalidOut:
        result = runtime.key_checks.check()
        if isinstance(result, KeyAccepted):
            return KeyValidOut(
                state="valid",
                is_free_tier=result.is_free_tier,
                limit_remaining_usd=result.limit_remaining_usd,
            )
        return KeyInvalidOut(state="invalid")

    return router
