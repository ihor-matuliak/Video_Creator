"""Constants and helpers shared by the backend tests."""

from __future__ import annotations

import contextlib
import json
import socket
import socketserver
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory
from fastapi.testclient import TestClient

from clipforge.api.app import create_app
from clipforge.config import Config, Mode
from clipforge.services.runtime import Runtime
from clipforge.storage.migrate import MIGRATIONS_DIR

TOKEN = "t" * 16 + "0123456789abcdef" + "T" * 16
PORT = 18765
RuntimeFactory = Callable[..., Runtime]
BASE_URL = f"http://127.0.0.1:{PORT}"
BACKEND_DIR = Path(__file__).resolve().parents[1]


def head_revision() -> str:
    """The newest Alembic revision: what a database that was just upgraded stores."""
    config = AlembicConfig()
    config.set_main_option("script_location", str(MIGRATIONS_DIR).replace("%", "%%"))
    head = ScriptDirectory.from_config(config).get_current_head()
    assert head is not None
    return head


def make_config(data_dir: Path, mode: Mode = "test", provider_url: str | None = None) -> Config:
    """``provider_url`` is ``FakeProvider.base_url``: the only provider a test config may use."""
    return Config(
        mode=mode, data_dir=data_dir, session_token=TOKEN, test_provider_base_url=provider_url
    )


FIXTURES = BACKEND_DIR.parents[1] / "test-results" / "fixtures"


def fixture_file(name: str) -> Path:
    """A generated media fixture (``npm run fixtures``). Tests only read it or copy it."""
    path = FIXTURES / name
    if not path.is_file():
        pytest.fail(f"Fixture {name} is missing: run `npm run fixtures` first")
    return path


def client_for(runtime: Runtime) -> TestClient:
    """An authenticated client for a second app over ``runtime`` (the ``client`` fixture
    covers the usual case)."""
    app = create_app(make_config(runtime.database.data_dir, "test"), port=PORT, runtime=runtime)
    return TestClient(
        app,
        base_url=BASE_URL,
        headers={"Authorization": f"Bearer {TOKEN}"},
        raise_server_exceptions=False,
    )


# --- a fake OpenRouter on loopback ---------------------------------------------------------------

# A field of the key info that the API must never return: it stands for the real ``label``, which
# may hold a part of the key.
LABEL = "label-Jv83Xq-must-not-be-returned"


def key_info(**data: object) -> bytes:
    """The body of ``GET /key`` as OpenRouter sends it (shape from its docs); ``data`` overrides."""
    fields: dict[str, object] = {
        "label": LABEL,
        "limit": 100,
        "limit_reset": None,
        "limit_remaining": 87.5,
        "usage": 12.5,
        "usage_daily": 1.25,
        "is_free_tier": False,
        "is_management_key": False,
        "rate_limit": {"requests": -1, "interval": "10s"},
        **data,
    }
    return json.dumps({"data": fields}).encode("utf-8")


@dataclass(frozen=True, slots=True)
class RecordedRequest:
    method: str
    path: str
    headers: Mapping[str, str]  # lower-case names
    body: bytes


@dataclass(slots=True)
class Script:
    """What the fake provider does with the next requests."""

    status: int = 200
    body: bytes = field(default_factory=key_info)
    headers: Mapping[str, str] = field(default_factory=dict)
    # Wait this long before answering (cut short when the provider is closed).
    delay_s: float = 0.0
    # Not None: send the headers, then one space every ``drip_s`` seconds, until the client leaves.
    drip_s: float | None = None
    # Not None: announce this many bytes in Content-Length but close after ``body`` (a cut answer).
    claimed_length: int | None = None
    # Not None: send the status line and the headers one byte every ``header_drip_s`` seconds,
    # then the body at once. No single read waits long, but the headers take long as a whole.
    header_drip_s: float | None = None


def refuse_env_proxies(monkeypatch: pytest.MonkeyPatch) -> str:
    """Point every proxy variable of the environment at a loopback port nobody listens on and
    return that URL. A client that trusts the environment sends its request there and fails."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        url = f"http://127.0.0.1:{probe.getsockname()[1]}"
    for name in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        monkeypatch.setenv(name, url)
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    return url


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        handler: type[BaseHTTPRequestHandler],
        provider: FakeProvider,
    ) -> None:
        self.provider = provider
        super().__init__(address, handler)

    def process_request(
        self, request: socket.socket | tuple[bytes, socket.socket], client_address: Any
    ) -> None:
        self.provider.connection_opened()
        super().process_request(request, client_address)

    def process_request_thread(
        self, request: socket.socket | tuple[bytes, socket.socket], client_address: Any
    ) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.provider.connection_closed()

    def server_bind(self) -> None:
        # HTTPServer.server_bind asks DNS for the name of the host: slow on Windows, not needed.
        socketserver.TCPServer.server_bind(self)
        self.server_name = "127.0.0.1"
        self.server_port = self.server_address[1]


class _Handler(BaseHTTPRequestHandler):
    server: _Server

    def do_GET(self) -> None:
        provider = self.server.provider
        # No Nagle delay: a byte sent on purpose one by one must reach the client one by one.
        self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        provider.requests.append(
            RecordedRequest(
                method=self.command,
                path=self.path,
                headers={name.lower(): value for name, value in self.headers.items()},
                body=body,
            )
        )
        script = provider.script
        if script.delay_s and provider.stopped.wait(script.delay_s):
            return
        # The client may have given up (a timeout, a refused redirect): then there is nobody to
        # answer, which is not a failure of the fake.
        with contextlib.suppress(OSError):
            if script.header_drip_s is not None:
                self._answer_slowly(script, provider)
                return
            self.send_response(script.status)
            for name, value in script.headers.items():
                self.send_header(name, value)
            self.send_header("Content-Type", "application/json")
            if script.drip_s is None:
                length = script.claimed_length or len(script.body)
                self.send_header("Content-Length", str(length))
                self.end_headers()
                self.wfile.write(script.body)
                return
            self.end_headers()
            while not provider.stopped.wait(script.drip_s):
                self.wfile.write(b" ")

    # The fake answers a POST (the STT endpoint) like a GET: same recording, same script.
    do_POST = do_GET

    def _answer_slowly(self, script: Script, provider: FakeProvider) -> None:
        """The head of the answer one byte at a time; stops when the client leaves or on close."""
        head = (
            f"HTTP/1.0 {script.status} Scripted\r\n"
            f"Content-Type: application/json\r\nContent-Length: {len(script.body)}\r\n\r\n"
        ).encode("ascii")
        for index in range(len(head)):
            self.wfile.write(head[index : index + 1])
            if provider.stopped.wait(script.header_drip_s):
                return
        self.wfile.write(script.body)

    def log_message(self, format: str, *args: object) -> None:
        """Nothing is printed: a test run has no use for request lines."""


class FakeProvider:
    """A loopback HTTP server that stands in for OpenRouter (no network, no paid call).

    Starts at construction on a free port of 127.0.0.1. ``requests`` holds every request that
    reached it, in order. ``script`` says how it answers; ``reply`` replaces it. ``close`` stops
    the server (a later connect is refused) and cuts every answer still waiting.
    """

    def __init__(self) -> None:
        self.requests: list[RecordedRequest] = []
        self.script = Script()
        self.stopped = threading.Event()
        # Connections the fake accepted (a request may never follow one) and how many are open.
        self.connections = 0
        self._open = 0
        self._connections_changed = threading.Condition()
        self._server = _Server(("127.0.0.1", 0), _Handler, self)
        # A short poll interval: ``shutdown`` waits for one, and every test closes a provider.
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            args=(0.005,),
            name="fake-provider",
            daemon=True,
        )
        self._thread.start()

    @property
    def base_url(self) -> str:
        """Like ``https://openrouter.ai/api/v1``: the path is the one the adapter appends to."""
        return f"http://127.0.0.1:{self._server.server_address[1]}/api/v1"

    def reply(
        self,
        status: int = 200,
        body: bytes | None = None,
        *,
        headers: Mapping[str, str] | None = None,
        delay_s: float = 0.0,
        drip_s: float | None = None,
        claimed_length: int | None = None,
        header_drip_s: float | None = None,
    ) -> None:
        """Answer every following request like this; ``body`` defaults to a valid key info."""
        self.script = Script(
            status=status,
            body=key_info() if body is None else body,
            headers={} if headers is None else headers,
            delay_s=delay_s,
            drip_s=drip_s,
            claimed_length=claimed_length,
            header_drip_s=header_drip_s,
        )

    def connection_opened(self) -> None:
        with self._connections_changed:
            self.connections += 1
            self._open += 1

    def connection_closed(self) -> None:
        with self._connections_changed:
            self._open -= 1
            self._connections_changed.notify_all()

    def wait_idle(self, timeout_s: float) -> bool:
        """True when the fake has finished with every connection, i.e. every client has left
        (or the answer is complete): nothing is still being sent to anyone."""
        with self._connections_changed:
            return self._connections_changed.wait_for(lambda: self._open == 0, timeout_s)

    def close(self) -> None:
        self.stopped.set()
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
