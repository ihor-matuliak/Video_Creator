"""Constants and helpers shared by the backend tests."""

from __future__ import annotations

from pathlib import Path

from clipforge.config import Config, Mode

TOKEN = "t" * 16 + "0123456789abcdef" + "T" * 16
PORT = 18765
BASE_URL = f"http://127.0.0.1:{PORT}"
BACKEND_DIR = Path(__file__).resolve().parents[1]


def make_config(data_dir: Path, mode: Mode = "test") -> Config:
    return Config(mode=mode, data_dir=data_dir, session_token=TOKEN)
