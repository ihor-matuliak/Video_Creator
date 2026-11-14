"""Unhandled exceptions in the real sidecar: ADR 013 "500 without exception text".

Starlette re-raises after its 500 handler and uvicorn logs the traceback on
``uvicorn.error``, so only a real uvicorn server can prove that no exception text
reaches stderr/stdout.
"""

from __future__ import annotations

import json
import re
import secrets
from pathlib import Path

from .support import TOKEN
from .test_server_process import TIMEOUT, clean_env, config_line, first_stdout_line, get, sidecar

# The production app has no failing route and must not get one. This launcher wraps
# create_app inside the real clipforge.server, so the real uvicorn setup and the real
# stdin/stdout/stderr contract are exercised. It runs in the child process only.
LAUNCHER = """
import os
import sys

import clipforge.server as server
from clipforge.api.app import create_app


def create_app_with_failing_route(config, **kwargs):
    app = create_app(config, **kwargs)
    sentinel = os.environ["BOOM_SENTINEL"]

    @app.get("/v1/boom")
    async def boom():
        raise RuntimeError(sentinel)

    return app


server.create_app = create_app_with_failing_route
sys.exit(server.main(sys.argv[1:]))
"""


def test_unhandled_exception_is_500_with_error_id_and_leaves_no_text_in_output(
    tmp_path: Path,
) -> None:
    sentinel = f"sentinel-{secrets.token_hex(16)}"
    env = clean_env(BOOM_SENTINEL=sentinel)
    with sidecar("--port", "0", "--config-stdin", env=env, entry=("-c", LAUNCHER)) as process:
        assert process.stdin is not None
        assert process.stdout is not None
        assert process.stderr is not None
        process.stdin.write(config_line(tmp_path))
        process.stdin.flush()
        port = json.loads(first_stdout_line(process))["port"]

        response = get(port, "/v1/boom")
        assert response.status_code == 500
        body = response.json()
        error_id = body["error"]["details"]["error_id"]
        assert isinstance(error_id, str)
        assert re.fullmatch(r"[0-9a-f]{16}", error_id)
        assert body == {
            "error": {
                "code": "INTERNAL_ERROR",
                "message": "Внутрішня помилка сервера",
                "details": {"error_id": error_id},
                "retryable": False,
            }
        }
        assert sentinel not in response.text
        assert "Traceback" not in response.text

        assert get(port, "/v1/health").status_code == 200  # the server survives the failure

        process.stdin.close()
        assert process.wait(timeout=TIMEOUT) == 0
        stdout = process.stdout.read()
        stderr = process.stderr.read()

    # Only the class name and the error id are logged: that is enough to find the failure.
    assert b"RuntimeError" in stderr
    assert error_id.encode("ascii") in stderr
    for name, output in (("stdout", stdout), ("stderr", stderr)):
        assert sentinel.encode() not in output, name
        assert b"Traceback" not in output, name
        assert TOKEN.encode() not in output, name
