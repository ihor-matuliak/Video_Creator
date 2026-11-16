"""STT chunk planning: core windows, upload context, adaptive split, payload size (FR-015, T-016).

A source of ``D`` ms is covered by non-overlapping *core* windows
``[k*core_ms, min((k+1)*core_ms, D))``. Each core is uploaded together with
``context_ms`` of audio on both available sides (``upload`` window, clipped to
``[0, D]``); context is never counted in coverage. A core that cannot be sent
(for example HTTP 413) is split in half and the context of both halves is
recomputed; the parent stays in the plan marked ``superseded``.

Pure functions and immutable values only: no I/O, no FFmpeg, no database.
Every failure has a stable ``code`` and a Ukrainian ``message``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

CORE_WINDOW_MS: Final = 300_000
CONTEXT_MS: Final = 5_000
MAX_UPLOAD_MS: Final = 310_000
# A core is split only while both halves stay at least this long.
MIN_CORE_MS: Final = 5_000

# Starting STT profile: mono 16 kHz. The estimate assumes 16-bit PCM WAV, the
# largest of the allowed formats (FLAC is smaller), so it is an upper bound.
SAMPLE_RATE_HZ: Final = 16_000
CHANNELS: Final = 1
BYTES_PER_SAMPLE: Final = 2
WAV_HEADER_BYTES: Final = 44
# 20 MiB of binary audio. The request body (base64 + JSON) is allowed to exceed
# it by the base64 inflation plus a fixed margin for the JSON envelope.
PAYLOAD_BINARY_CEILING_BYTES: Final = 20 * 1024 * 1024
JSON_OVERHEAD_BYTES: Final = 16 * 1024
JSON_MARGIN_BYTES: Final = 64 * 1024

CHUNK_NOT_INTEGER: Final = "CHUNK_NOT_INTEGER"
CHUNK_INVALID_PARAMETER: Final = "CHUNK_INVALID_PARAMETER"
CHUNK_UPLOAD_TOO_LONG: Final = "CHUNK_UPLOAD_TOO_LONG"
CHUNK_BELOW_MIN_CORE: Final = "CHUNK_BELOW_MIN_CORE"
CHUNK_NOT_IN_PLAN: Final = "CHUNK_NOT_IN_PLAN"
CHUNK_SUPERSEDED: Final = "CHUNK_SUPERSEDED"


class ChunkingError(ValueError):
    """Chunk planning failed. ``code`` is stable, ``message`` is Ukrainian."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(code)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class ChunkWindow:
    """One STT request: half-open core ``[core_start, core_end)`` plus upload context."""

    core_start_ms: int
    core_end_ms: int
    upload_start_ms: int
    upload_end_ms: int

    @property
    def core_duration_ms(self) -> int:
        return self.core_end_ms - self.core_start_ms

    @property
    def upload_duration_ms(self) -> int:
        return self.upload_end_ms - self.upload_start_ms


@dataclass(frozen=True, slots=True)
class PlannedChunk:
    """A window in the plan. A superseded chunk was split and must not be sent."""

    window: ChunkWindow
    superseded: bool = False


@dataclass(frozen=True, slots=True)
class ChunkPlan:
    """Ordered chunks of one source. A parent is followed by its two children."""

    duration_ms: int
    context_ms: int
    entries: tuple[PlannedChunk, ...]

    @property
    def active(self) -> tuple[ChunkWindow, ...]:
        """Windows to transcribe, ordered by core start; their cores cover ``[0, D)`` exactly."""
        return tuple(entry.window for entry in self.entries if not entry.superseded)

    def split(self, window: ChunkWindow, *, min_core_ms: int = MIN_CORE_MS) -> ChunkPlan:
        """Return a plan where ``window`` is superseded by two halves of its core."""
        index = next(
            (i for i, entry in enumerate(self.entries) if entry.window == window),
            None,
        )
        if index is None:
            raise ChunkingError(CHUNK_NOT_IN_PLAN, "Цієї частини немає в плані розбиття")
        if self.entries[index].superseded:
            raise ChunkingError(CHUNK_SUPERSEDED, "Цю частину вже розділено на менші")
        first, second = split_chunk(
            window, self.duration_ms, context_ms=self.context_ms, min_core_ms=min_core_ms
        )
        entries = (
            *self.entries[:index],
            PlannedChunk(window, superseded=True),
            PlannedChunk(first),
            PlannedChunk(second),
            *self.entries[index + 1 :],
        )
        return ChunkPlan(self.duration_ms, self.context_ms, entries)


@dataclass(frozen=True, slots=True)
class PayloadEstimate:
    """Upper-bound size of one STT request for a given upload window."""

    upload_ms: int
    audio_bytes: int
    request_bytes: int
    binary_ceiling_bytes: int
    request_ceiling_bytes: int

    @property
    def fits(self) -> bool:
        return (
            self.audio_bytes <= self.binary_ceiling_bytes
            and self.request_bytes <= self.request_ceiling_bytes
        )


def _require_int(value: object, label: str, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ChunkingError(CHUNK_NOT_INTEGER, f"{label}: значення має бути цілим числом")
    if value < minimum:
        raise ChunkingError(
            CHUNK_INVALID_PARAMETER, f"{label}: значення має бути не менше {minimum}"
        )
    return value


def _base64_size(binary_bytes: int) -> int:
    return 4 * ((binary_bytes + 2) // 3)


def _window(core_start_ms: int, core_end_ms: int, duration_ms: int, context_ms: int) -> ChunkWindow:
    return ChunkWindow(
        core_start_ms=core_start_ms,
        core_end_ms=core_end_ms,
        upload_start_ms=max(0, core_start_ms - context_ms),
        upload_end_ms=min(duration_ms, core_end_ms + context_ms),
    )


def plan_chunks(
    duration_ms: object,
    *,
    core_ms: object = CORE_WINDOW_MS,
    context_ms: object = CONTEXT_MS,
) -> ChunkPlan:
    """Cover ``[0, duration_ms)`` with consecutive cores of ``core_ms`` plus context.

    The last core is shorter when the duration is not a multiple of ``core_ms``.
    The upload window of every chunk is at most ``MAX_UPLOAD_MS``.
    """
    duration = _require_int(duration_ms, "Тривалість джерела", 1)
    core = _require_int(core_ms, "Розмір ядра", 1)
    context = _require_int(context_ms, "Контекст", 0)
    if core + 2 * context > MAX_UPLOAD_MS:
        raise ChunkingError(
            CHUNK_UPLOAD_TOO_LONG,
            f"Ядро з контекстом перевищує {MAX_UPLOAD_MS // 1000} с на запит",
        )
    entries = tuple(
        PlannedChunk(_window(start, min(start + core, duration), duration, context))
        for start in range(0, duration, core)
    )
    return ChunkPlan(duration, context, entries)


def split_chunk(
    window: ChunkWindow,
    duration_ms: object,
    *,
    context_ms: object = CONTEXT_MS,
    min_core_ms: object = MIN_CORE_MS,
) -> tuple[ChunkWindow, ChunkWindow]:
    """Split the core of ``window`` in half and recompute the context of both halves.

    The first half takes ``core // 2`` ms, the second the rest, so the halves cover
    the parent core exactly. Refused when either half would be shorter than
    ``min_core_ms``.
    """
    duration = _require_int(duration_ms, "Тривалість джерела", 1)
    context = _require_int(context_ms, "Контекст", 0)
    minimum = _require_int(min_core_ms, "Мінімальне ядро", 1)
    core_ms = window.core_duration_ms
    if core_ms // 2 < minimum:
        raise ChunkingError(
            CHUNK_BELOW_MIN_CORE,
            f"Ядро {core_ms} мс замале для поділу: половина має бути не менше {minimum} мс",
        )
    middle = window.core_start_ms + core_ms // 2
    return (
        _window(window.core_start_ms, middle, duration, context),
        _window(middle, window.core_end_ms, duration, context),
    )


def estimate_payload(
    upload_ms: object,
    *,
    binary_ceiling_bytes: object = PAYLOAD_BINARY_CEILING_BYTES,
    sample_rate_hz: object = SAMPLE_RATE_HZ,
    channels: object = CHANNELS,
    bytes_per_sample: object = BYTES_PER_SAMPLE,
    json_overhead_bytes: object = JSON_OVERHEAD_BYTES,
) -> PayloadEstimate:
    """Estimate the request size for ``upload_ms`` of PCM audio (default mono 16 kHz, 16-bit).

    ``audio_bytes`` is a WAV file and must fit ``binary_ceiling_bytes``;
    ``request_bytes`` is its base64 size plus the JSON envelope and must fit the
    base64 size of the ceiling plus ``JSON_MARGIN_BYTES``.
    """
    upload = _require_int(upload_ms, "Тривалість запиту", 1)
    ceiling = _require_int(binary_ceiling_bytes, "Ліміт аудіо", 1)
    rate = _require_int(sample_rate_hz, "Частота дискретизації", 1)
    channel_count = _require_int(channels, "Кількість каналів", 1)
    sample_bytes = _require_int(bytes_per_sample, "Байтів на семпл", 1)
    overhead = _require_int(json_overhead_bytes, "Службові байти JSON", 0)
    pcm_bytes = (upload * rate * channel_count * sample_bytes + 999) // 1000
    audio_bytes = WAV_HEADER_BYTES + pcm_bytes
    return PayloadEstimate(
        upload_ms=upload,
        audio_bytes=audio_bytes,
        request_bytes=_base64_size(audio_bytes) + overhead,
        binary_ceiling_bytes=ceiling,
        request_ceiling_bytes=_base64_size(ceiling) + JSON_MARGIN_BYTES,
    )
