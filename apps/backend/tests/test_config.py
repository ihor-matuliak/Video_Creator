from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from clipforge import __version__
from clipforge.config import (
    ENV_DATA_DIR,
    ENV_MODE,
    ENV_SESSION_TOKEN,
    ENV_TEST_PROVIDER_BASE_URL,
    Config,
    ConfigError,
    load_config,
    validate_test_provider_url,
)

from .support import BACKEND_DIR, TOKEN

DATA_DIR = str(Path(__file__).resolve().parent)


def stdin_doc(**overrides: object) -> str:
    document: dict[str, object] = {
        "mode": "production",
        "data_dir": DATA_DIR,
        "session_token": TOKEN,
    }
    document.update(overrides)
    return json.dumps(document)


def test_version_matches_pyproject() -> None:
    project = tomllib.loads((BACKEND_DIR / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["version"] == __version__


def test_env_config_in_test_mode() -> None:
    env = {
        ENV_MODE: "test",
        ENV_DATA_DIR: DATA_DIR,
        ENV_SESSION_TOKEN: TOKEN,
        ENV_TEST_PROVIDER_BASE_URL: "http://127.0.0.1:4567/api/v1",
        "CLIPFORGE_ALLOW_LIVE_AI": "false",
    }
    config = load_config(stdin_line=None, env=env)
    assert config == Config(
        mode="test",
        data_dir=Path(DATA_DIR),
        session_token=TOKEN,
        test_provider_base_url="http://127.0.0.1:4567/api/v1",
    )


@pytest.mark.parametrize("env_mode", [None, "production", "", "TEST", "dev"])
def test_env_config_requires_test_mode(env_mode: str | None) -> None:
    env = {ENV_DATA_DIR: DATA_DIR, ENV_SESSION_TOKEN: TOKEN}
    if env_mode is not None:
        env[ENV_MODE] = env_mode
    with pytest.raises(ConfigError):
        load_config(stdin_line=None, env=env)


@pytest.mark.parametrize("missing", [ENV_DATA_DIR, ENV_SESSION_TOKEN])
def test_env_config_requires_data_dir_and_token(missing: str) -> None:
    env = {ENV_MODE: "test", ENV_DATA_DIR: DATA_DIR, ENV_SESSION_TOKEN: TOKEN}
    del env[missing]
    with pytest.raises(ConfigError):
        load_config(stdin_line=None, env=env)


def test_stdin_production_config() -> None:
    config = load_config(stdin_line=stdin_doc(), env={})
    assert config.mode == "production"
    assert config.data_dir == Path(DATA_DIR)
    assert config.session_token == TOKEN
    assert config.test_provider_base_url is None


def test_stdin_test_config_may_use_test_provider_from_env() -> None:
    env = {ENV_MODE: "test", ENV_TEST_PROVIDER_BASE_URL: "http://localhost:9/api/v1"}
    config = load_config(stdin_line=stdin_doc(mode="test"), env=env)
    assert config.mode == "test"
    assert config.test_provider_base_url == "http://localhost:9/api/v1"


def test_production_rejects_env_mode_test() -> None:
    with pytest.raises(ConfigError):
        load_config(stdin_line=stdin_doc(), env={ENV_MODE: "test"})


def test_production_accepts_explicit_env_mode_production() -> None:
    config = load_config(stdin_line=stdin_doc(), env={ENV_MODE: "production"})
    assert config.mode == "production"


@pytest.mark.parametrize(
    "name",
    [
        ENV_TEST_PROVIDER_BASE_URL,
        "CLIPFORGE_TEST_ANYTHING",
        "CLIPFORGE_TEST_",
        "clipforge_test_provider_base_url",
    ],
)
@pytest.mark.parametrize("value", ["http://127.0.0.1:1/api/v1", "https://openrouter.ai/api/v1", ""])
def test_production_rejects_any_test_env(name: str, value: str) -> None:
    with pytest.raises(ConfigError):
        load_config(stdin_line=stdin_doc(), env={name: value})


def test_production_rejects_provider_url_in_stdin() -> None:
    with pytest.raises(ConfigError):
        load_config(stdin_line=stdin_doc(provider_base_url="https://openrouter.ai/api/v1"), env={})


def test_test_mode_rejects_conflicting_env_mode() -> None:
    with pytest.raises(ConfigError):
        load_config(stdin_line=stdin_doc(mode="test"), env={ENV_MODE: "production"})


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:4567/api/v1",
        "http://localhost:4567/api/v1",
        "http://LOCALHOST/x",
        "http://127.0.0.1",
    ],
)
def test_loopback_provider_url_is_accepted(url: str) -> None:
    assert validate_test_provider_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "",
        "127.0.0.1:8080",
        "https://127.0.0.1:4567/api/v1",
        "https://openrouter.ai/api/v1",
        "http://openrouter.ai/api/v1",
        "http://example.com/api/v1",
        "http://10.0.0.5:4567/",
        "http://192.168.1.10/",
        "http://0.0.0.0:4567/",
        "http://[::1]:4567/",
        "http://127.0.0.2:4567/",
        "http://127.0.0.1.evil.example/",
        "http://localhost.evil.example/",
        "http://127.0.0.1@evil.example/",
        "http://127.0.0.1:80@evil.example/",
        "http://user:pass@127.0.0.1/",
        "http://127.0.0.1:99999/",
        "http://127.0.0.1:abc/",
        "http://127.0.0.1\n.evil.example/",
        "http://127.0.0.1 .evil.example/",
        "ftp://127.0.0.1/",
        "file:///c:/x",
    ],
)
def test_non_loopback_test_provider_url_is_rejected(url: str) -> None:
    with pytest.raises(ConfigError):
        validate_test_provider_url(url)


def test_non_loopback_provider_url_is_rejected_by_load_config() -> None:
    env = {
        ENV_MODE: "test",
        ENV_DATA_DIR: DATA_DIR,
        ENV_SESSION_TOKEN: TOKEN,
        ENV_TEST_PROVIDER_BASE_URL: "https://openrouter.ai/api/v1",
    }
    with pytest.raises(ConfigError):
        load_config(stdin_line=None, env=env)


@given(
    host=st.from_regex(r"[a-z][a-z0-9-]{0,20}(\.[a-z][a-z0-9-]{0,10}){1,3}", fullmatch=True),
    port=st.integers(min_value=1, max_value=65535),
)
def test_any_non_loopback_dotted_host_is_rejected(host: str, port: int) -> None:
    with pytest.raises(ConfigError):
        validate_test_provider_url(f"http://{host}:{port}/api/v1")


@pytest.mark.parametrize(
    "line",
    [
        "",
        "not json",
        "[]",
        '"text"',
        "{}",
        json.dumps({"mode": "test", "data_dir": DATA_DIR}),
        stdin_doc(mode="staging"),
        stdin_doc(mode=None),
        stdin_doc(extra=1),
        stdin_doc(data_dir="relative/dir"),
        stdin_doc(data_dir=""),
        stdin_doc(data_dir=7),
        stdin_doc(session_token=""),
        stdin_doc(session_token="short"),
        stdin_doc(session_token="a" * 31),
        stdin_doc(session_token="a" * 31 + " "),
        stdin_doc(session_token="а" * 40),  # Cyrillic letters: not ASCII
        stdin_doc(session_token=123),
    ],
)
def test_invalid_stdin_config_is_rejected(line: str) -> None:
    with pytest.raises(ConfigError):
        load_config(stdin_line=line, env={})


def test_config_errors_and_repr_never_contain_the_token() -> None:
    bad = stdin_doc(mode="staging")
    with pytest.raises(ConfigError) as info:
        load_config(stdin_line=bad, env={})
    assert TOKEN not in str(info.value)

    with pytest.raises(ConfigError) as info:
        load_config(stdin_line=stdin_doc(data_dir="relative"), env={})
    assert TOKEN not in str(info.value)

    config = load_config(stdin_line=stdin_doc(), env={})
    assert TOKEN not in repr(config)
    assert TOKEN not in str(config)
