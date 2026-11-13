from __future__ import annotations

import logging
from collections.abc import Callable

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from clipforge import __version__
from clipforge.config import Mode

from .support import BASE_URL, PORT, TOKEN


class Probe(BaseModel):  # module level: FastAPI resolves annotations from module globals
    count: int


UNAUTHORIZED = {
    "error": {
        "code": "UNAUTHORIZED",
        "message": "Потрібен дійсний токен сесії",
        "details": {},
        "retryable": False,
    }
}
INVALID_HOST = {
    "error": {
        "code": "INVALID_HOST",
        "message": "Недозволений заголовок Host",
        "details": {},
        "retryable": False,
    }
}


def test_health_with_token(client: TestClient) -> None:
    response = client.get("/v1/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "mode": "test", "version": __version__}


@pytest.mark.parametrize("mode", ["test", "production"])
def test_health_reports_mode(make_app: Callable[[Mode], FastAPI], mode: Mode) -> None:
    client = TestClient(
        make_app(mode), base_url=BASE_URL, headers={"Authorization": f"Bearer {TOKEN}"}
    )
    assert client.get("/v1/health").json()["mode"] == mode


def test_missing_token_is_401_with_error_shape(anonymous: TestClient) -> None:
    response = anonymous.get("/v1/health")
    assert response.status_code == 401
    assert response.json() == UNAUTHORIZED
    assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize(
    "authorization",
    [
        f"Bearer {TOKEN}x",
        f"Bearer {TOKEN[:-1]}",
        "Bearer wrong-token",
        "Bearer ",
        "Bearer",
        TOKEN,
        f"Basic {TOKEN}",
        f"Bearer  {TOKEN}",
        "",
    ],
)
def test_wrong_token_is_401(anonymous: TestClient, authorization: str) -> None:
    response = anonymous.get("/v1/health", headers={"Authorization": authorization})
    assert response.status_code == 401
    assert response.json() == UNAUTHORIZED


def test_bearer_scheme_is_case_insensitive(anonymous: TestClient) -> None:
    response = anonymous.get("/v1/health", headers={"Authorization": f"bearer {TOKEN}"})
    assert response.status_code == 200


def test_unknown_route_without_token_is_401_not_404(anonymous: TestClient) -> None:
    # T-041: GET /v1/projects without a token. Auth runs before routing.
    for method, path in [("GET", "/v1/projects"), ("POST", "/v1/sources"), ("GET", "/nope")]:
        response = anonymous.request(method, path)
        assert response.status_code == 401, path
        assert response.json() == UNAUTHORIZED


def test_unknown_route_with_token_is_404_error_shape(client: TestClient) -> None:
    response = client.get("/v1/does-not-exist")
    assert response.status_code == 404
    assert response.json() == {
        "error": {
            "code": "NOT_FOUND",
            "message": "Ресурс не знайдено",
            "details": {},
            "retryable": False,
        }
    }


def test_wrong_method_is_405_error_shape(client: TestClient) -> None:
    response = client.post("/v1/health")
    assert response.status_code == 405
    assert response.json()["error"]["code"] == "METHOD_NOT_ALLOWED"


def test_docs_and_openapi_are_not_served(client: TestClient) -> None:
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404


@pytest.mark.parametrize(
    "host",
    [
        "evil.example",
        "evil.example:18765",
        f"127.0.0.1:{PORT + 1}",
        "127.0.0.1",
        "localhost",
        f"localhost.evil.example:{PORT}",
        f"127.0.0.1.evil.example:{PORT}",
        f"[::1]:{PORT}",
        f"0.0.0.0:{PORT}",
        "",
    ],
)
def test_foreign_host_is_rejected_even_with_valid_token(client: TestClient, host: str) -> None:
    response = client.get("/v1/health", headers={"Host": host})
    assert response.status_code == 400
    assert response.json() == INVALID_HOST


def test_foreign_host_is_rejected_without_token(anonymous: TestClient) -> None:
    response = anonymous.get("/v1/health", headers={"Host": "evil.example"})
    assert response.status_code == 400
    assert response.json() == INVALID_HOST


@pytest.mark.parametrize("host", [f"localhost:{PORT}", f"LOCALHOST:{PORT}", f"127.0.0.1:{PORT}"])
def test_loopback_host_with_bound_port_is_accepted(client: TestClient, host: str) -> None:
    assert client.get("/v1/health", headers={"Host": host}).status_code == 200


def test_cors_is_not_opened_to_foreign_origin(anonymous: TestClient) -> None:
    preflight = anonymous.options(
        "/v1/health",
        headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"},
    )
    assert preflight.status_code == 401
    assert not any(name.lower().startswith("access-control-") for name in preflight.headers)

    with_token = TestClient(
        anonymous.app,
        base_url=BASE_URL,
        headers={"Authorization": f"Bearer {TOKEN}", "Origin": "https://evil.example"},
    )
    response = with_token.get("/v1/health")
    assert "access-control-allow-origin" not in response.headers


def test_unexpected_error_returns_500_without_exception_text(
    make_app: Callable[[Mode], FastAPI],
    caplog: pytest.LogCaptureFixture,
) -> None:
    app = make_app("test")

    @app.get("/v1/boom")
    async def boom() -> None:
        raise RuntimeError("secret-detail-sk-or-v1-abc")

    client = TestClient(
        app,
        base_url=BASE_URL,
        headers={"Authorization": f"Bearer {TOKEN}"},
        raise_server_exceptions=False,
    )
    with caplog.at_level(logging.DEBUG):
        response = client.get("/v1/boom")
    assert response.status_code == 500
    error_id = response.json()["error"]["details"]["error_id"]
    assert response.json() == {
        "error": {
            "code": "INTERNAL_ERROR",
            "message": "Внутрішня помилка сервера",
            "details": {"error_id": error_id},
            "retryable": False,
        }
    }
    assert "secret-detail" not in response.text
    # Logs carry the class name and the error id only: no message, no traceback.
    errors = [record for record in caplog.records if record.levelno >= logging.ERROR]
    assert [record.getMessage() for record in errors] == [
        f"Unhandled RuntimeError (error_id={error_id})"
    ]
    assert all(record.exc_info is None for record in caplog.records)
    assert "secret-detail" not in caplog.text
    assert "Traceback" not in caplog.text


def test_validation_error_is_422_and_does_not_echo_input(
    make_app: Callable[[Mode], FastAPI],
) -> None:
    app = make_app("test")

    @app.post("/v1/probe")
    async def probe(body: Probe) -> Probe:
        return body

    client = TestClient(app, base_url=BASE_URL, headers={"Authorization": f"Bearer {TOKEN}"})
    response = client.post("/v1/probe", json={"count": "sk-or-v1-not-a-number"})
    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert body["error"]["retryable"] is False
    assert body["error"]["details"]["fields"] == [{"loc": ["body", "count"], "type": "int_parsing"}]
    assert "sk-or-v1" not in response.text
