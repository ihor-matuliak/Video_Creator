"""FR-040/T-005: safe export basenames and collision candidates."""

from __future__ import annotations

import unicodedata
from itertools import islice

import pytest
from hypothesis import given
from hypothesis import strategies as st

from clipforge.domain.naming import (
    FALLBACK_STEM,
    FORBIDDEN_CHARS,
    MAX_BASENAME_LENGTH,
    MAX_COLLISION_SUFFIX,
    NameCollisionError,
    collision_candidates,
    export_basename,
    first_free_name,
    is_reserved_name,
    sanitize_component,
    utf16_length,
)

CLIP_ID = "3f2a9c4e-1b7d-4e2f-9a0b-5c6d7e8f9a0b"


def name_for(stem: str, **overrides: object) -> str:
    arguments: dict[str, object] = {
        "source_stem": stem,
        "clip_id": CLIP_ID,
        "revision": 2,
        "start_ms": 62_345,
        "end_ms": 70_000,
    }
    arguments.update(overrides)
    return export_basename(**arguments)  # type: ignore[arg-type]


def assert_windows_safe(filename: str) -> None:
    assert not FORBIDDEN_CHARS.intersection(filename)
    assert all(not unicodedata.category(char).startswith("C") for char in filename)
    assert not is_reserved_name(filename)
    assert not filename.endswith((" ", "."))
    assert not filename.startswith((" ", "."))


def test_basename_contains_stem_clip_revision_and_times() -> None:
    assert name_for("Гра 1") == f"Гра 1_{CLIP_ID}_r2_00h01m02s345-00h01m10s000"


def test_hours_beyond_two_digits() -> None:
    name = name_for("x", start_ms=360_000_000, end_ms=360_000_001)
    assert name.endswith("_100h00m00s000-100h00m00s001")


@pytest.mark.parametrize(
    "stem",
    [
        "CON",
        "con",
        "PRN",
        "AUX",
        "NUL",
        "nul.txt",
        "NUL .txt",
        "CON ",
        "COM1",
        "com9",
        "LPT1",
        "lpt0",
        "COM¹",
        "LPT³",
        "CONIN$",
        "conout$",
        "nul.tar.gz",
    ],
)
def test_reserved_device_names_are_neutralized(stem: str) -> None:
    assert is_reserved_name(stem)
    safe = sanitize_component(stem)
    assert not is_reserved_name(safe)
    assert safe.startswith("_")
    filename = name_for(stem) + ".mp4"
    assert_windows_safe(filename)
    assert not is_reserved_name(filename)


@pytest.mark.parametrize("stem", ["CONSOLE", "COM10", "LPT", "nul_file", "aux2x", "con-1"])
def test_names_similar_to_reserved_are_kept(stem: str) -> None:
    assert not is_reserved_name(stem)
    assert sanitize_component(stem) == stem


@pytest.mark.parametrize(
    ("stem", "expected"),
    [
        ("video...", "video"),
        ("video. . .", "video"),
        ("  video  ", "video"),
        ("...hidden", "hidden"),
        ('a<b>c:d"e/f\\g|h?i*j', "a_b_c_d_e_f_g_h_i_j"),
        ("tab\there", "tab_here"),
        ("line\nbreak\r", "line_break_"),
        ("bidi‮gnp.exe", "bidi_gnp.exe"),
        ("zero​width", "zero_width"),
        ("nbsp and  many   spaces", "nbsp and many spaces"),
        ("é", "é"),
        ("lone\ud800surrogate", "lone_surrogate"),
        ("Матч #3 — фінал 😀", "Матч #3 — фінал 😀"),
    ],
)
def test_sanitize_component(stem: str, expected: str) -> None:
    assert sanitize_component(stem) == expected


@pytest.mark.parametrize("stem", ["", "   ", "...", ". .", " . . "])
def test_empty_stem_uses_fallback(stem: str) -> None:
    assert sanitize_component(stem) == FALLBACK_STEM
    assert name_for(stem).startswith(f"{FALLBACK_STEM}_{CLIP_ID}_r2_")


def test_control_only_stem_is_replaced_not_dropped() -> None:
    assert sanitize_component("\x00\x01") == "__"


def test_trailing_dots_and_spaces_never_end_the_filename() -> None:
    name = name_for("clip. . ")
    assert name.startswith(f"clip_{CLIP_ID}")
    for candidate in islice(collision_candidates(name), 5):
        assert not candidate.endswith((" ", "."))


def test_long_stem_is_truncated_to_length_cap() -> None:
    name = name_for("я" * 500)
    longest = f"{name} ({MAX_COLLISION_SUFFIX}).mp4"
    assert utf16_length(longest) <= MAX_BASENAME_LENGTH
    assert name.endswith(f"_{CLIP_ID}_r2_00h01m02s345-00h01m10s000")
    assert name.startswith("я" * 20)


def test_astral_characters_count_as_two_utf16_units() -> None:
    assert utf16_length("😀a") == 3
    name = name_for("😀" * 300, max_length=120)
    assert utf16_length(f"{name} ({MAX_COLLISION_SUFFIX}).mp4") <= 120


def test_truncation_does_not_leave_trailing_space_or_dot() -> None:
    # max_length=110 leaves 33 units for the stem: the cut lands right after ". ".
    name = name_for("x" * 31 + ". " + "y" * 50, max_length=110)
    assert name.startswith("x" * 31 + f"_{CLIP_ID}_")


def test_clip_id_is_sanitized_and_capped() -> None:
    name = name_for("x", clip_id="a/b:" + "z" * 200)
    assert name.startswith("x_a_b_" + "z" * 60 + "_r2_")
    assert not FORBIDDEN_CHARS.intersection(name)


def test_too_small_max_length_is_rejected() -> None:
    with pytest.raises(ValueError, match="max_length"):
        name_for("x", max_length=60)


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        ({"revision": 0}, ValueError),
        ({"revision": True}, TypeError),
        ({"start_ms": -1}, ValueError),
        ({"start_ms": 1.5}, TypeError),
        ({"end_ms": 62_345}, ValueError),
        ({"clip_id": ""}, ValueError),
        ({"extension": "mp4"}, ValueError),
        ({"extension": ".MP4/"}, ValueError),
    ],
)
def test_invalid_arguments_are_rejected(
    overrides: dict[str, object], error: type[Exception]
) -> None:
    with pytest.raises(error):
        name_for("x", **overrides)


def test_collision_candidates_sequence() -> None:
    assert list(islice(collision_candidates("name"), 4)) == [
        "name.mp4",
        "name (2).mp4",
        "name (3).mp4",
        "name (4).mp4",
    ]
    assert len(list(collision_candidates("name", limit=3))) == 3


def test_first_free_name_is_case_insensitive() -> None:
    assert first_free_name("name", []) == "name.mp4"
    assert first_free_name("name", ["NAME.MP4"]) == "name (2).mp4"
    assert first_free_name("name", ["name.mp4", "Name (2).mp4", "other.mp4"]) == "name (3).mp4"


def test_first_free_name_exhausted() -> None:
    taken = ["name.mp4", "name (2).mp4", "name (3).mp4"]
    with pytest.raises(NameCollisionError):
        first_free_name("name", taken, limit=3)


@given(
    stem=st.text(max_size=400),
    clip_id=st.text(min_size=1, max_size=80),
    revision=st.integers(min_value=1, max_value=10**6),
    start=st.integers(min_value=0, max_value=10**9),
    length=st.integers(min_value=1, max_value=10**8),
)
def test_any_input_gives_windows_safe_bounded_name(
    stem: str, clip_id: str, revision: int, start: int, length: int
) -> None:
    name = export_basename(
        source_stem=stem,
        clip_id=clip_id,
        revision=revision,
        start_ms=start,
        end_ms=start + length,
    )
    for candidate in (f"{name}.mp4", f"{name} ({MAX_COLLISION_SUFFIX}).mp4"):
        assert_windows_safe(candidate)
        assert utf16_length(candidate) <= MAX_BASENAME_LENGTH
    assert unicodedata.normalize("NFC", name) == name
