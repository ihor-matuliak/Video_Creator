"""Sidecar entry point: ``python -m clipforge.server --port N [--config-stdin]`` (ADR 013).

* Binds 127.0.0.1 only. ``--port 0`` picks an ephemeral port; once the server is
  accepting requests, exactly one stdout line ``{"event":"ready","port":N}`` is printed.
* ``--config-stdin``: the first stdin line is the JSON configuration. After that the
  process watches stdin and exits on EOF, so a dead parent never leaves an orphan.
* Without ``--config-stdin`` the configuration comes from the environment (tests only).
* Before binding, the workspace database is opened and migrated and FFmpeg is looked up
  (ADR 017): a locked workspace or a failed migration exits with code 2 and no ready line.
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
from sqlalchemy.exc import SQLAlchemyError

from clipforge.adapters.media_tools import (
    MediaTools,
    MediaToolsUnavailableError,
    resolve_media_tools,
)
from clipforge.api.app import create_app
from clipforge.api.security import LOOPBACK_HOST
from clipforge.config import Config, ConfigError, load_config
from clipforge.services.analysis_jobs import openrouter_chat_factory
from clipforge.services.credentials import openrouter_key_checker
from clipforge.services.runtime import Runtime
from clipforge.services.transcription_jobs import openrouter_stt_factory
from clipforge.storage import StorageError

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

    runtime = _start_runtime(config)
    if runtime is None:
        return 2
    try:
        try:
            listener = _bind_loopback(args.port)
        except OSError:
            print("clipforge: не вдалося зайняти порт 127.0.0.1", file=sys.stderr)
            return 1

        return asyncio.run(_serve(config, runtime, listener, watch_stdin=args.config_stdin))
    finally:
        runtime.close()


def _start_runtime(config: Config) -> Runtime | None:
    """Open the workspace and look for FFmpeg once, before anything is served (ADR 017).

    ``None``: the workspace is locked by another sidecar or cannot be opened or migrated; the
    reason is on stderr and the exit code is 2. Missing FFmpeg is not fatal: media routes answer
    503 ``MEDIA_TOOLS_UNAVAILABLE`` while health and the rest keep working.
    """
    try:
        media_tools: MediaTools | None = resolve_media_tools(config.mode, os.environ)
    except MediaToolsUnavailableError as exc:
        print(f"clipforge: {exc.message}; маршрути з відео повертатимуть 503", file=sys.stderr)
        media_tools = None
    try:
        return Runtime.start(
            config.data_dir,
            media_tools=media_tools,
            stt_client_factory=openrouter_stt_factory(config),
            chat_client_factory=openrouter_chat_factory(config),
            key_checker=openrouter_key_checker(config),
            mode=config.mode,
        )
    except StorageError as exc:
        # Storage messages are Ukrainian and free of paths and stored values.
        print(f"clipforge: {exc}", file=sys.stderr)
    except (OSError, SQLAlchemyError):
        # Their text may carry the data directory or SQL, so only a fixed message is printed.
        print("clipforge: не вдалося відкрити робочу теку або базу даних", file=sys.stderr)
    return None


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


async def _serve(
    config: Config, runtime: Runtime, listener: socket.socket, *, watch_stdin: bool
) -> int:
    port: int = listener.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(config, port=port, runtime=runtime),
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
