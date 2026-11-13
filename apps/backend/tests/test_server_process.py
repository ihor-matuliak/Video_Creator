"""The real sidecar as a subprocess: ADR 013 launch contract."""

from __future__ import annotations

import json
import os
import queue
import socket
import subprocess
import sys
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

import httpx2
import pytest

from clipforge import __version__

from .support import BACKEND_DIR, TOKEN

TIMEOUT = 30.0


def clean_env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("CLIPFORGE_")}
    env["PYTHONUNBUFFERED"] = "1"
    env.update(extra)
    return env


def config_line(data_dir: Path, mode: str = "test", token: str = TOKEN) -> bytes:
    document = {"mode": mode, "data_dir": str(data_dir), "session_token": token}
    return json.dumps(document).encode("utf-8") + b"\n"


@contextmanager
def sidecar(
    *args: str,
    env: dict[str, str] | None = None,
    entry: Sequence[str] = ("-m", "clipforge.server"),
) -> Iterator[subprocess.Popen[bytes]]:
    process = subprocess.Popen(
        [sys.executable, *entry, *args],
        cwd=BACKEND_DIR,
        env=env if env is not None else clean_env(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        yield process
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=TIMEOUT)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()


def first_stdout_line(process: subprocess.Popen[bytes]) -> bytes:
    assert process.stdout is not None
    lines: queue.Queue[bytes] = queue.Queue()
    threading.Thread(target=lambda: lines.put(process.stdout.readline()), daemon=True).start()  # type: ignore[union-attr]
    try:
        return lines.get(timeout=TIMEOUT)
    except queue.Empty:
        pytest.fail("sidecar did not print a line within the timeout")


def get(port: int, path: str, token: str | None = TOKEN) -> httpx2.Response:
    headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
    with httpx2.Client(trust_env=False, timeout=10.0) as http:
        return http.get(f"http://127.0.0.1:{port}{path}", headers=headers)


@pytest.mark.parametrize("mode", ["test", "production"])
def test_ready_line_health_and_exit_on_stdin_eof(tmp_path: Path, mode: str) -> None:
    with sidecar("--port", "0", "--config-stdin") as process:
        assert process.stdin is not None
        process.stdin.write(config_line(tmp_path, mode))
        process.stdin.flush()

        raw = first_stdout_line(process)
        ready = json.loads(raw)
        assert ready["event"] == "ready"
        port = ready["port"]
        assert isinstance(port, int)
        assert port > 0
        assert raw == f'{{"event":"ready","port":{port}}}\n'.encode("ascii")

        health = get(port, "/v1/health")
        assert health.status_code == 200
        assert health.json() == {"status": "ok", "mode": mode, "version": __version__}
        assert get(port, "/v1/health", token=None).status_code == 401
        assert get(port, "/v1/health", token="x" * 40).status_code == 401
        assert process.poll() is None  # keeps running while stdin is open

        process.stdin.close()  # EOF, as when the parent dies
        assert process.wait(timeout=TIMEOUT) == 0

        assert process.stdout is not None
        assert process.stderr is not None
        rest = process.stdout.read() + process.stderr.read()
        assert TOKEN.encode() not in rest

    with pytest.raises(httpx2.TransportError):
        get(port, "/v1/health")


def test_listens_on_loopback_only(tmp_path: Path) -> None:
    foreign = {
        info[4][0]
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
        if not str(info[4][0]).startswith("127.")
    }
    if not foreign:
        pytest.skip("no non-loopback IPv4 address on this machine")
    with sidecar("--port", "0", "--config-stdin") as process:
        assert process.stdin is not None
        process.stdin.write(config_line(tmp_path))
        process.stdin.flush()
        port = json.loads(first_stdout_line(process))["port"]
        assert get(port, "/v1/health").status_code == 200
        for address in sorted(foreign):
            try:
                socket.create_connection((str(address), int(port)), timeout=3).close()
            except OSError:
                continue  # refused or filtered: not reachable
            pytest.fail(f"sidecar is reachable on non-loopback address {address}")


def test_foreign_host_header_is_rejected_by_real_process(tmp_path: Path) -> None:
    with sidecar("--port", "0", "--config-stdin") as process:
        assert process.stdin is not None
        process.stdin.write(config_line(tmp_path))
        process.stdin.flush()
        port = json.loads(first_stdout_line(process))["port"]
        with httpx2.Client(trust_env=False, timeout=10.0) as http:
            response = http.get(
                f"http://127.0.0.1:{port}/v1/health",
                headers={"Authorization": f"Bearer {TOKEN}", "Host": "evil.example"},
            )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "INVALID_HOST"


@pytest.mark.parametrize(
    ("env", "line"),
    [
        pytest.param(
            {"CLIPFORGE_MODE": "test"}, config_line(BACKEND_DIR, "production"), id="env-mode-test"
        ),
        pytest.param(
            {"CLIPFORGE_TEST_PROVIDER_BASE_URL": "http://127.0.0.1:1/api/v1"},
            config_line(BACKEND_DIR, "production"),
            id="test-env-var",
        ),
        pytest.param({}, b"not json\n", id="invalid-json"),
        pytest.param({}, config_line(BACKEND_DIR, "production", token="short"), id="weak-token"),
        pytest.param({}, b"", id="stdin-closed-before-config"),
    ],
)
def test_invalid_configuration_exits_2_without_ready_line(env: dict[str, str], line: bytes) -> None:
    with sidecar("--port", "0", "--config-stdin", env=clean_env(**env)) as process:
        assert process.stdin is not None
        process.stdin.write(line)
        process.stdin.close()
        assert process.stdout is not None
        assert process.stderr is not None
        assert process.wait(timeout=TIMEOUT) == 2
        assert process.stdout.read() == b""
        message = process.stderr.read()
        assert message.startswith(b"clipforge:")
        assert TOKEN.encode() not in message


def test_environment_configuration_requires_test_mode(tmp_path: Path) -> None:
    env = clean_env(CLIPFORGE_DATA_DIR=str(tmp_path), CLIPFORGE_SESSION_TOKEN=TOKEN)
    with sidecar("--port", "0", env=env) as process:
        assert process.wait(timeout=TIMEOUT) == 2
        assert process.stdout is not None
        assert process.stdout.read() == b""


def test_port_argument_is_required_and_bounded() -> None:
    for args in ([], ["--port", "70000"], ["--port", "abc"]):
        with sidecar(*args) as process:
            assert process.wait(timeout=TIMEOUT) == 2
