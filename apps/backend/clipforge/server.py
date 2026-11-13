"""Sidecar entry point: ``python -m clipforge.server --port N [--config-stdin]`` (ADR 013).

* Binds 127.0.0.1 only. ``--port 0`` picks an ephemeral port; once the server is
  accepting requests, exactly one stdout line ``{"event":"ready","port":N}`` is printed.
* ``--config-stdin``: the first stdin line is the JSON configuration. After that the
  process watches stdin and exits on EOF, so a dead parent never leaves an orphan.
* Without ``--config-stdin`` the configuration comes from the environment (tests only).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import json
import os
import socket
import sys
import threading
from collections.abc import Sequence

import uvicorn

from clipforge.api.app import create_app
from clipforge.api.security import LOOPBACK_HOST
from clipforge.config import Config, ConfigError, load_config

_MAX_CONFIG_LINE_BYTES = 16 * 1024


def main(argv: Sequence[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8")

    args = _parse_args(argv)
    try:
        stdin_line = _read_config_line() if args.config_stdin else None
        config = load_config(stdin_line=stdin_line, env=os.environ)
    except ConfigError as exc:
        print(f"clipforge: {exc}", file=sys.stderr)
        return 2

    try:
        listener = _bind_loopback(args.port)
    except OSError:
        print("clipforge: не вдалося зайняти порт 127.0.0.1", file=sys.stderr)
        return 1

    return asyncio.run(_serve(config, listener, watch_stdin=args.config_stdin))


def _port(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError("port must be an integer") from None
    if not 0 <= value <= 65535:
        raise argparse.ArgumentTypeError("port must be between 0 and 65535")
    return value


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m clipforge.server")
    parser.add_argument(
        "--port",
        type=_port,
        required=True,
        metavar="N",
        help="port on 127.0.0.1; 0 selects a free port",
    )
    parser.add_argument(
        "--config-stdin",
        action="store_true",
        help="read the JSON configuration from the first stdin line and exit on stdin EOF",
    )
    return parser.parse_args(argv)


def _read_config_line() -> str:
    raw = sys.stdin.buffer.readline(_MAX_CONFIG_LINE_BYTES + 1)
    if not raw:
        raise ConfigError("stdin закрито до отримання конфігурації")
    if len(raw) > _MAX_CONFIG_LINE_BYTES:
        raise ConfigError("Конфігурація в stdin завелика")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ConfigError("Конфігурація в stdin не є коректним UTF-8") from None


def _bind_loopback(port: int) -> socket.socket:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.bind((LOOPBACK_HOST, port))
    except OSError:
        listener.close()
        raise
    return listener


async def _serve(config: Config, listener: socket.socket, *, watch_stdin: bool) -> int:
    port: int = listener.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(config, port=port),
            log_level="warning",
            access_log=False,  # request lines must never reach logs
            server_header=False,
            proxy_headers=False,
            http="h11",
            ws="none",
            loop="asyncio",
        )
    )
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    # uvicorn has no 'accepting connections' hook, so poll its started flag.
    while not server.started and not serving.done():  # noqa: ASYNC110
        await asyncio.sleep(0.01)
    if not server.started:
        await serving
        return 1

    _announce_ready(port)
    if watch_stdin:
        _exit_on_stdin_eof(asyncio.get_running_loop(), server)
    await serving
    return 0


def _announce_ready(port: int) -> None:
    line = json.dumps({"event": "ready", "port": port}, separators=(",", ":"))
    sys.stdout.buffer.write(line.encode("ascii") + b"\n")  # "\n" on every platform
    sys.stdout.buffer.flush()


def _exit_on_stdin_eof(loop: asyncio.AbstractEventLoop, server: uvicorn.Server) -> None:
    fd = sys.stdin.fileno()

    def watch() -> None:
        # os.read, not sys.stdin.buffer: a daemon thread blocked inside the buffered
        # reader can abort the interpreter at shutdown.
        with contextlib.suppress(OSError):
            while os.read(fd, 4096):
                pass
        # RuntimeError: the loop is already closed, so the server is gone anyway.
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(_request_exit, server)

    threading.Thread(target=watch, name="stdin-watcher", daemon=True).start()


def _request_exit(server: uvicorn.Server) -> None:
    server.should_exit = True


if __name__ == "__main__":
    sys.exit(main())
