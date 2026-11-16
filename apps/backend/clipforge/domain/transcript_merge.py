"""Merge per-chunk STT results into one source-time transcript (FR-015, T-017..T-022).

Input is provider-agnostic: for every chunk window an adapter supplies words and
segments in seconds *local to the uploaded audio*, the chunk state and whether the
provider gave word timestamps. Output is a transcript in integer source
milliseconds that follows ``contracts/transcript.schema.json``.

Rules (docs/05 section 4):

* ``ms = upload_start_ms + round(seconds * 1000)``, applied exactly once.
* The result must lie in the chunk's upload window (which is inside ``[0, D]``).
  Overflow of up to ``TIMING_TOLERANCE_MS`` is clamped with a diagnostic; more is
  invalid provider timing. A word is never shifted by minutes.
* A word or segment belongs to the chunk whose *core* contains its midpoint.
  Context-only items are dropped.
* The same normalised text with close intervals, reported by two chunks inside
  the part of audio both of them heard, is one occurrence. Equal text elsewhere
  is never deduplicated.
* A provider item whose start and end are the same millisecond *after rounding* is a
  point, not an error: it gets one millisecond (``TIMING_ZERO_LENGTH`` diagnostic).
  ``start > end`` in the provider's own seconds stays invalid.
* Ids are ``w1..`` / ``s1..`` by ordinal in start order; speaker labels stay
  chunk-local (``SpeakerKey``); coverage lists the core of every chunk.
* ``timing_level`` is ``word`` only when every chunk that returned speech has word
  timing. Otherwise it is ``segment`` with a diagnostic and no words: word times
  are never synthesised from segments.
* At ``word`` level provider segments are ignored (a provider may return one segment
  for the whole audio): segments are built from the merged words, see
  ``_segments_from_words``. At ``segment`` level a chunk that has word timing still
  gives segments built from its own words by the same rules, and its provider
  segments are ignored; only a chunk without word timing contributes provider
  segments. Such a segment longer than ``MAX_TEXT_CHARS`` cannot be split here (no
  word times): ``MERGE_SEGMENT_TOO_LONG`` names the chunk so the caller can split it.
* ``check_chunk`` runs the validation of one chunk on its own, with the same rules and
  error codes as ``merge_transcript`` (except for the checks that need the other
  chunks or the source duration).

Pure functions and immutable values only: no I/O, no database, no HTTP.
Every failure has a stable ``code`` and a Ukrainian ``message``.
"""

from __future__ import annotations

import itertools
import math
import unicodedata
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Final, Literal, Protocol

from clipforge.domain.chunking import ChunkWindow

ChunkState = Literal["succeeded", "failed", "pending", "unknown_outcome"]
TimingLevel = Literal["word", "segment"]

SCHEMA_VERSION: Final = 1
# Provider time outside the uploaded audio by more than this is invalid, not rounding.
TIMING_TOLERANCE_MS: Final = 100
# Two reports of one spoken word may differ by this much (gap between intervals).
DUPLICATE_GAP_MS: Final = 200
MAX_TEXT_CHARS: Final = 2_000
# Segments built from words: a pause this long or longer ends a segment, and a
# segment never spans more than MAX_SEGMENT_MS (except one word longer than that).
PAUSE_BREAK_MS: Final = 1_500
MAX_SEGMENT_MS: Final = 20_000
LANGUAGE_MIN_CHARS: Final = 2
LANGUAGE_MAX_CHARS: Final = 32
UNDETERMINED_LANGUAGE: Final = "und"

MERGE_INVALID_DURATION: Final = "MERGE_INVALID_DURATION"
MERGE_WINDOW_INVALID: Final = "MERGE_WINDOW_INVALID"
MERGE_CORES_NOT_TILING: Final = "MERGE_CORES_NOT_TILING"
MERGE_RESULT_INCONSISTENT: Final = "MERGE_RESULT_INCONSISTENT"
MERGE_INVALID_PROVIDER_TIMING: Final = "MERGE_INVALID_PROVIDER_TIMING"
MERGE_INVALID_PROVIDER_TEXT: Final = "MERGE_INVALID_PROVIDER_TEXT"
MERGE_SEGMENT_TOO_LONG: Final = "MERGE_SEGMENT_TOO_LONG"

DIAGNOSTIC_TIMING_CLAMPED: Final = "TIMING_CLAMPED"
DIAGNOSTIC_TIMING_ZERO_LENGTH: Final = "TIMING_ZERO_LENGTH"
DIAGNOSTIC_SEGMENT_FALLBACK: Final = "SEGMENT_TIMING_FALLBACK"

_STATES: Final = frozenset({"succeeded", "failed", "pending", "unknown_outcome"})
# A segment ends after a word that ends with one of these, optionally followed by closers.
_SENTENCE_END: Final = ".!?…"
_CLOSERS: Final = "\"'»”“’)]}"
_WORD_LABEL: Final = "Слово"
_SEGMENT_LABEL: Final = "Сегмент"


class MergeError(ValueError):
    """Merging failed. ``code`` is stable, ``message`` is Ukrainian.

    ``chunk_core_start_ms`` names the offending chunk when the fault is in one chunk.
    """

    def __init__(self, code: str, message: str, *, chunk_core_start_ms: int | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.message = message
        self.chunk_core_start_ms = chunk_core_start_ms


@dataclass(frozen=True, slots=True)
class ChunkItem:
    """A word or segment as reported by the provider.

    ``start_s`` / ``end_s`` are seconds from the start of the *uploaded* audio.
    ``speaker`` is a label that is meaningful only inside its own chunk.
    """

    start_s: float
    end_s: float
    text: str
    speaker: str | None = None
    confidence: float | None = None


@dataclass(frozen=True, slots=True)
class ChunkResult:
    """Outcome of one chunk.

    ``words`` and ``segments`` must be empty unless ``state`` is ``succeeded``; an
    empty response of a failed chunk is not silence. ``has_word_timing`` states that
    the provider returned word timestamps for this chunk (a silent chunk may leave it
    ``False``). ``language`` is whatever the provider reported, if anything.
    """

    window: ChunkWindow
    state: ChunkState
    words: tuple[ChunkItem, ...] = ()
    segments: tuple[ChunkItem, ...] = ()
    has_word_timing: bool = False
    language: str | None = None


@dataclass(frozen=True, slots=True)
class SpeakerKey:
    """Provider speaker label qualified by the chunk it came from.

    The same label in two chunks gives two different keys: the provider does not
    promise that labels mean the same person across requests.
    """

    chunk_core_start_ms: int
    label: str


@dataclass(frozen=True, slots=True)
class TimedText:
    """A merged word or segment in source milliseconds, interval ``[start_ms, end_ms)``."""

    id: str
    ordinal: int
    start_ms: int
    end_ms: int
    text: str
    speaker: SpeakerKey | None = None
    confidence: float | None = None


@dataclass(frozen=True, slots=True)
class CoverageEntry:
    """Core window of one chunk and its state. Context is never listed."""

    start_ms: int
    end_ms: int
    state: ChunkState


@dataclass(frozen=True, slots=True)
class MergeDiagnostic:
    """Something the user or caller should know about; not part of the stored schema."""

    code: str
    message: str
    chunk_core_start_ms: int | None = None


@dataclass(frozen=True, slots=True)
class MergedTranscript:
    duration_ms: int
    timing_level: TimingLevel
    language: str
    words: tuple[TimedText, ...]
    segments: tuple[TimedText, ...]
    coverage: tuple[CoverageEntry, ...]
    diagnostics: tuple[MergeDiagnostic, ...]

    def to_schema_dict(self) -> dict[str, object]:
        """The ``NormalizedTranscript`` document (no speakers, confidence or diagnostics)."""
        return {
            "schema_version": SCHEMA_VERSION,
            "timing_level": self.timing_level,
            "language": self.language,
            "duration_ms": self.duration_ms,
            "words": [_item_dict(item) for item in self.words],
            "segments": [_item_dict(item) for item in self.segments],
            "coverage": [
                {"start_ms": entry.start_ms, "end_ms": entry.end_ms, "state": entry.state}
                for entry in self.coverage
            ],
        }


def _item_dict(item: TimedText) -> dict[str, object]:
    return {
        "id": item.id,
        "ordinal": item.ordinal,
        "start_ms": item.start_ms,
        "end_ms": item.end_ms,
        "text": item.text,
    }


@dataclass(frozen=True, slots=True)
class _Unit:
    """A normalised word or segment of one chunk, before ownership and deduplication."""

    uid: int
    chunk: int
    seq: int
    start_ms: int
    end_ms: int
    text: str
    key: str
    speaker: str | None
    confidence: float | None


def merge_transcript(duration_ms: object, results: Sequence[ChunkResult]) -> MergedTranscript:
    """Build the source-time transcript of a source of ``duration_ms`` from chunk results.

    ``results`` must hold one entry per *active* chunk of the plan, in any order:
    their cores have to tile ``[0, duration_ms)`` exactly (a superseded parent must
    not be passed together with its children).
    """
    duration = _require_duration(duration_ms)
    ordered = sorted(results, key=lambda result: result.window.core_start_ms)
    _check_windows(ordered, duration)
    for result in ordered:
        _check_result(result)

    speech = [
        result
        for result in ordered
        if result.state == "succeeded" and (result.words or result.segments)
    ]
    without_word_timing = [result for result in speech if not result.has_word_timing]
    timing_level: TimingLevel = "segment" if without_word_timing else "word"

    diagnostics: list[MergeDiagnostic] = []
    uids = itertools.count()
    word_units: list[list[_Unit]] = []
    segment_units: list[list[_Unit]] = []
    for index, result in enumerate(ordered):
        words_of_chunk, provider_segments = _normalize_chunk(result, index, uids, diagnostics)
        word_units.append(words_of_chunk)
        if timing_level == "segment":
            segment_units.append(
                _segment_units_from_words(words_of_chunk, uids)
                if result.has_word_timing
                else provider_segments
            )

    windows = [result.window for result in ordered]
    for result in without_word_timing:
        diagnostics.append(
            MergeDiagnostic(
                DIAGNOSTIC_SEGMENT_FALLBACK,
                f"Частина {result.window.core_start_ms}–{result.window.core_end_ms} мс "
                "без часових міток слів: транскрипт лише на рівні сегментів",
                result.window.core_start_ms,
            )
        )

    words: tuple[TimedText, ...]
    if timing_level == "word":
        words = _assemble(_reconcile(windows, word_units), "w", windows)
        segments = _segments_from_words(words)
    else:
        words = ()
        segments = _assemble(_reconcile(windows, segment_units), "s", windows)
    coverage = tuple(
        CoverageEntry(result.window.core_start_ms, result.window.core_end_ms, result.state)
        for result in ordered
    )
    return MergedTranscript(
        duration_ms=duration,
        timing_level=timing_level,
        language=_pick_language(ordered),
        words=words,
        segments=segments,
        coverage=coverage,
        diagnostics=tuple(diagnostics),
    )


def check_chunk(result: ChunkResult) -> None:
    """Validate one chunk on its own; raise ``MergeError`` with the code merge would give.

    Covers the window shape, the consistency of state/words/segments/word timing, the
    text and timing of every word, and, for a chunk without word timing, of every
    provider segment (``MERGE_SEGMENT_TOO_LONG`` included). Provider segments of a
    chunk with word timing are ignored, as they are in ``merge_transcript``. Not
    covered, because they need more than this chunk: that the cores tile the source
    and that the upload window lies inside it.
    """
    _check_window(result.window, None)
    _check_result(result)
    _normalize_chunk(result, 0, itertools.count(), [])


def _require_duration(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise MergeError(
            MERGE_INVALID_DURATION, "Тривалість джерела має бути цілим числом мс не менше 1"
        )
    return value


def _check_window(window: ChunkWindow, duration: int | None) -> None:
    """Window shape; ``duration`` (when known) also bounds the upload window from above."""
    if not (
        0
        <= window.upload_start_ms
        <= window.core_start_ms
        < window.core_end_ms
        <= window.upload_end_ms
        and (duration is None or window.upload_end_ms <= duration)
    ):
        inside = "" if duration is None else f", джерело {duration} мс"
        raise MergeError(
            MERGE_WINDOW_INVALID,
            f"Частина {window.core_start_ms}–{window.core_end_ms} мс має некоректні межі "
            f"(контекст {window.upload_start_ms}–{window.upload_end_ms} мс{inside})",
            chunk_core_start_ms=window.core_start_ms,
        )


def _check_windows(ordered: Sequence[ChunkResult], duration: int) -> None:
    for result in ordered:
        _check_window(result.window, duration)
    expected = 0
    for result in ordered:
        if result.window.core_start_ms != expected:
            raise MergeError(
                MERGE_CORES_NOT_TILING,
                f"Ядра частин мають точно покривати 0–{duration} мс без пропусків і накладань: "
                f"очікувався початок {expected} мс, отримано {result.window.core_start_ms} мс",
                chunk_core_start_ms=result.window.core_start_ms,
            )
        expected = result.window.core_end_ms
    if expected != duration:
        raise MergeError(
            MERGE_CORES_NOT_TILING,
            f"Ядра частин закінчуються на {expected} мс, а джерело триває {duration} мс",
        )


def _check_result(result: ChunkResult) -> None:
    start = result.window.core_start_ms
    if result.state not in _STATES:
        raise MergeError(
            MERGE_RESULT_INCONSISTENT,
            f"Частина {start} мс: невідомий стан {result.state!r}",
            chunk_core_start_ms=start,
        )
    if result.state != "succeeded" and (result.words or result.segments):
        raise MergeError(
            MERGE_RESULT_INCONSISTENT,
            f"Частина {start} мс у стані {result.state} не може містити слів чи сегментів",
            chunk_core_start_ms=start,
        )
    if result.words and not result.has_word_timing:
        raise MergeError(
            MERGE_RESULT_INCONSISTENT,
            f"Частина {start} мс містить слова, але не позначена як така, що має часові мітки слів",
            chunk_core_start_ms=start,
        )
    if result.has_word_timing and result.segments and not result.words:
        raise MergeError(
            MERGE_RESULT_INCONSISTENT,
            f"Частина {start} мс заявляє часові мітки слів, але слів немає",
            chunk_core_start_ms=start,
        )


def _normalize_chunk(
    result: ChunkResult, chunk: int, uids: Iterator[int], diagnostics: list[MergeDiagnostic]
) -> tuple[list[_Unit], list[_Unit]]:
    """Validated ``(words, provider segments)`` of one chunk in source ms.

    Provider segments are returned only for a chunk without word timing: otherwise they
    are ignored (and not validated), because segments are built from the words.
    """
    words = _normalize_items(result.words, result.window, chunk, _WORD_LABEL, uids, diagnostics)
    if result.has_word_timing:
        return words, []
    segments = _normalize_items(
        result.segments, result.window, chunk, _SEGMENT_LABEL, uids, diagnostics
    )
    return words, segments


def _normalize_items(
    items: Sequence[ChunkItem],
    window: ChunkWindow,
    chunk: int,
    label: str,
    uids: Iterator[int],
    diagnostics: list[MergeDiagnostic],
) -> list[_Unit]:
    """Convert one chunk's items to source ms, validating bounds and text."""
    low = window.upload_start_ms
    high = window.upload_end_ms
    units: list[_Unit] = []
    for seq, item in enumerate(items):
        number = seq + 1
        text = item.text.strip()
        if label == _SEGMENT_LABEL and len(text) > MAX_TEXT_CHARS:
            # No word times to split by: only a smaller chunk can give shorter segments.
            raise MergeError(
                MERGE_SEGMENT_TOO_LONG,
                f"{label} №{number} частини {window.core_start_ms} мс довший за "
                f"{MAX_TEXT_CHARS} символів, а часових міток слів немає: частину слід розділити",
                chunk_core_start_ms=window.core_start_ms,
            )
        if not text or len(text) > MAX_TEXT_CHARS:
            raise MergeError(
                MERGE_INVALID_PROVIDER_TEXT,
                f"{label} №{number} частини {window.core_start_ms} мс: "
                f"текст порожній або довший за {MAX_TEXT_CHARS} символів",
                chunk_core_start_ms=window.core_start_ms,
            )
        start = low + _round_ms(item.start_s, window, label, number)
        end = low + _round_ms(item.end_s, window, label, number)
        if item.start_s > item.end_s:
            raise _timing_error(window, label, number, "початок пізніше за кінець")
        if not (
            low - TIMING_TOLERANCE_MS <= start <= high + TIMING_TOLERANCE_MS
            and low - TIMING_TOLERANCE_MS <= end <= high + TIMING_TOLERANCE_MS
        ):
            raise _timing_error(
                window, label, number, f"{start}–{end} мс поза аудіо {low}–{high} мс"
            )
        clamped_start = min(max(start, low), high)
        clamped_end = min(max(end, low), high)
        if (clamped_start, clamped_end) != (start, end):
            overflow = max(low - start, start - high, low - end, end - high, 0)
            diagnostics.append(
                MergeDiagnostic(
                    DIAGNOSTIC_TIMING_CLAMPED,
                    f"{label} №{number} частини {window.core_start_ms} мс вийшло за межі "
                    f"аудіо на {overflow} мс і обрізане",
                    window.core_start_ms,
                )
            )
        if start == end:
            # A point in time after rounding to ms (providers emit these, e.g. at 0.1 s
            # granularity): give it the minimal duration inside the audio.
            clamped_start = min(clamped_start, high - 1)
            clamped_end = clamped_start + 1
            diagnostics.append(
                MergeDiagnostic(
                    DIAGNOSTIC_TIMING_ZERO_LENGTH,
                    f"{label} №{number} частини {window.core_start_ms} мс має нульову "
                    "тривалість (після округлення до мс): кінець зсунуто на 1 мс",
                    window.core_start_ms,
                )
            )
        elif clamped_start >= clamped_end:
            raise _timing_error(
                window, label, number, f"інтервал {start}–{end} мс не має додатної тривалості"
            )
        units.append(
            _Unit(
                uid=next(uids),
                chunk=chunk,
                seq=seq,
                start_ms=clamped_start,
                end_ms=clamped_end,
                text=text,
                key=_phrase_key(text),
                speaker=item.speaker or None,
                confidence=item.confidence,
            )
        )
    return units


def _round_ms(seconds: float, window: ChunkWindow, label: str, number: int) -> int:
    """``round(seconds * 1000)``; the caller adds the upload offset."""
    if isinstance(seconds, bool) or not isinstance(seconds, int | float):
        raise _timing_error(window, label, number, "час не є числом")
    scaled = seconds * 1000
    if isinstance(scaled, float) and not math.isfinite(scaled):
        raise _timing_error(window, label, number, "час не є скінченним числом")
    return round(scaled)


def _timing_error(window: ChunkWindow, label: str, number: int, detail: str) -> MergeError:
    return MergeError(
        MERGE_INVALID_PROVIDER_TIMING,
        f"{label} №{number} частини {window.core_start_ms} мс: "
        f"некоректний час провайдера ({detail})",
        chunk_core_start_ms=window.core_start_ms,
    )


def _phrase_key(text: str) -> str:
    """Case-, punctuation- and whitespace-insensitive form used to compare phrases."""
    folded = unicodedata.normalize("NFKC", text).casefold()
    kept = "".join(char for char in folded if unicodedata.category(char)[0] not in "PZC")
    return kept or folded.strip()


def _distance_to_core(unit: _Unit, window: ChunkWindow) -> int:
    """0 when the midpoint is in the half-open core, else a positive distance (doubled ms)."""
    twice_mid = unit.start_ms + unit.end_ms
    low = 2 * window.core_start_ms
    high = 2 * window.core_end_ms
    if twice_mid < low:
        return low - twice_mid
    if twice_mid >= high:
        return twice_mid - high + 1
    return 0


def _gap(first: _Unit, second: _Unit) -> int:
    """Distance between two intervals; negative when they overlap."""
    return max(first.start_ms, second.start_ms) - min(first.end_ms, second.end_ms)


def _reconcile(windows: Sequence[ChunkWindow], units: Sequence[Sequence[_Unit]]) -> list[_Unit]:
    """Pick the items that go into the transcript: owned ones, with seam duplicates merged.

    Items of different chunks form one occurrence when both lie in the audio that
    both chunks heard, have the same phrase key and close intervals; matching is
    one-to-one per chunk pair, closest pairs first, so a real quick repeat
    survives. An occurrence is represented by the version whose midpoint is
    nearest to its own core (earlier chunk on ties) and is always kept, so a word
    on the seam is neither lost nor doubled. A word seen by one chunk only is
    kept when its midpoint is in the core.
    """
    members: dict[int, list[_Unit]] = {}
    cluster_of: dict[int, int] = {}
    for chunk_units in units:
        for unit in chunk_units:
            members[unit.uid] = [unit]
            cluster_of[unit.uid] = unit.uid

    for first_index, first_window in enumerate(windows):
        for second_index in range(first_index + 1, len(windows)):
            second_window = windows[second_index]
            shared_start = max(first_window.upload_start_ms, second_window.upload_start_ms)
            shared_end = min(first_window.upload_end_ms, second_window.upload_end_ms)
            if shared_start >= shared_end:
                continue
            heard_first = [
                unit
                for unit in units[first_index]
                if unit.start_ms < shared_end and unit.end_ms > shared_start
            ]
            heard_second = [
                unit
                for unit in units[second_index]
                if unit.start_ms < shared_end and unit.end_ms > shared_start
            ]
            candidates = sorted(
                (
                    (
                        abs(unit.start_ms - other.start_ms) + abs(unit.end_ms - other.end_ms),
                        unit,
                        other,
                    )
                    for unit in heard_first
                    for other in heard_second
                    if unit.key == other.key and _gap(unit, other) <= DUPLICATE_GAP_MS
                ),
                key=lambda pair: (pair[0], pair[1].seq, pair[2].seq),
            )
            for _cost, unit, other in candidates:
                if _can_join(members[cluster_of[unit.uid]], members[cluster_of[other.uid]]):
                    _join(members, cluster_of, unit, other)

    kept: list[_Unit] = []
    for cluster in members.values():
        if len(cluster) == 1:
            only = cluster[0]
            if _distance_to_core(only, windows[only.chunk]) == 0:
                kept.append(only)
        else:
            kept.append(
                min(
                    cluster,
                    key=lambda unit: (_distance_to_core(unit, windows[unit.chunk]), unit.chunk),
                )
            )
    return kept


def _can_join(first: Sequence[_Unit], second: Sequence[_Unit]) -> bool:
    """Two occurrences may merge only if they have no chunk in common."""
    return not {unit.chunk for unit in first} & {unit.chunk for unit in second}


def _join(
    members: dict[int, list[_Unit]], cluster_of: dict[int, int], first: _Unit, second: _Unit
) -> None:
    target = cluster_of[first.uid]
    source = cluster_of[second.uid]
    for unit in members[source]:
        cluster_of[unit.uid] = target
    members[target].extend(members.pop(source))


def _assemble(
    kept: Sequence[_Unit], prefix: str, windows: Sequence[ChunkWindow]
) -> tuple[TimedText, ...]:
    ordered = sorted(kept, key=lambda unit: (unit.start_ms, unit.end_ms, unit.chunk, unit.seq))
    return tuple(
        TimedText(
            id=f"{prefix}{ordinal + 1}",
            ordinal=ordinal,
            start_ms=unit.start_ms,
            end_ms=unit.end_ms,
            text=unit.text,
            speaker=(
                SpeakerKey(windows[unit.chunk].core_start_ms, unit.speaker)
                if unit.speaker is not None
                else None
            ),
            confidence=unit.confidence,
        )
        for ordinal, unit in enumerate(ordered)
    )


def _ends_sentence(text: str) -> bool:
    """True when ``text`` ends with ``. ! ? …`` possibly followed by closing quotes/brackets."""
    stripped = text.rstrip(_CLOSERS)
    return bool(stripped) and stripped[-1] in _SENTENCE_END


class _Spoken(Protocol):
    """What the segment rules read from a word: a merged ``TimedText`` or a chunk ``_Unit``."""

    @property
    def start_ms(self) -> int: ...

    @property
    def end_ms(self) -> int: ...

    @property
    def text(self) -> str: ...

    @property
    def speaker(self) -> object: ...


def _group_words[W: _Spoken](words: Sequence[W]) -> list[list[W]]:
    """Split words (in start order) into segment-sized groups.

    A segment ends after a word that ends a sentence, before a word that follows a
    pause of ``PAUSE_BREAK_MS`` or more, at a speaker change, and before a word that
    would stretch it beyond ``MAX_SEGMENT_MS`` (or beyond ``MAX_TEXT_CHARS`` of text).
    """
    groups: list[list[W]] = []
    for word in words:
        if groups:
            group = groups[-1]
            previous = group[-1]
            text_chars = sum(len(member.text) for member in group) + len(group)
            if not (
                _ends_sentence(previous.text)
                or word.start_ms - previous.end_ms >= PAUSE_BREAK_MS
                or word.speaker != previous.speaker
                or word.end_ms - group[0].start_ms > MAX_SEGMENT_MS
                or text_chars + len(word.text) > MAX_TEXT_CHARS
            ):
                group.append(word)
                continue
        groups.append([word])
    return groups


def _segments_from_words(words: Sequence[TimedText]) -> tuple[TimedText, ...]:
    """Group merged words (in order) into segments ``s1..``.

    Grouping rules: ``_group_words``. A segment's text is its words joined by one space;
    its interval is ``[first.start_ms, last.end_ms)``; its speaker is that of its words.
    """
    return tuple(
        TimedText(
            id=f"s{ordinal + 1}",
            ordinal=ordinal,
            start_ms=group[0].start_ms,
            end_ms=max(word.end_ms for word in group),
            text=" ".join(member.text for member in group),
            speaker=group[0].speaker,
        )
        for ordinal, group in enumerate(_group_words(words))
    )


def _segment_units_from_words(words: Sequence[_Unit], uids: Iterator[int]) -> list[_Unit]:
    """Segments of one chunk built from its own words by the same rules as the merged ones.

    Used at ``segment`` level for a chunk that has word timing, so it does not depend on
    what the provider calls a segment. Context words are included: ownership by midpoint
    and seam deduplication are applied later, like for provider segments.
    """
    ordered = sorted(words, key=lambda word: (word.start_ms, word.end_ms, word.seq))
    segments: list[_Unit] = []
    for seq, group in enumerate(_group_words(ordered)):
        text = " ".join(member.text for member in group)
        segments.append(
            _Unit(
                uid=next(uids),
                chunk=group[0].chunk,
                seq=seq,
                start_ms=group[0].start_ms,
                end_ms=max(word.end_ms for word in group),
                text=text,
                key=_phrase_key(text),
                speaker=group[0].speaker,
                confidence=None,
            )
        )
    return segments


def _pick_language(ordered: Sequence[ChunkResult]) -> str:
    """Most frequent provider language of succeeded chunks (earliest wins ties)."""
    counts: dict[str, int] = {}
    for result in ordered:
        language = (result.language or "").strip()
        if (
            result.state == "succeeded"
            and LANGUAGE_MIN_CHARS <= len(language) <= LANGUAGE_MAX_CHARS
        ):
            counts[language] = counts.get(language, 0) + 1
    if not counts:
        return UNDETERMINED_LANGUAGE
    top = max(counts.values())
    return next(language for language, count in counts.items() if count == top)
