"""Transcripts (FR-019, FR-020): import SRT/VTT and normalized JSON, read, list, correct text.

Every import and every correction writes a new immutable revision; earlier revisions of the source
stay as they were. Importing needs no API key and no media tools: only the stored duration of the
source.

The text is parsed by ``domain.transcript_import`` outside any write lock, then stored in one
``Database.write_session()``. A failed import saves nothing.

Error messages of an import are the Ukrainian ``message`` of the domain error: they name the
line or the JSON path that is wrong and may quote the offending id or timestamp. Everything else
is fixed text.

A correction (``correct``) changes only text. The new revision copies every time, ordinal, speaker
label, confidence and chunk of its parent; the parent is not touched. Only the latest revision of a
source can be corrected: a correction of an older one is a conflict that names the latest.
"""

from __future__ import annotations

import unicodedata
from bisect import bisect_left
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Final

from clipforge.domain.transcript_import import (
    NormalizedTranscript,
    TranscriptImportError,
    TranscriptItem,
    parse_srt,
    parse_vtt,
    validate_normalized_json,
)
from clipforge.domain.transcript_merge import MAX_TEXT_CHARS
from clipforge.services.errors import (
    ErrorKind,
    ServiceError,
    conflict,
    invalid,
    invalid_cursor,
    not_found,
)
from clipforge.services.text import clean_text, is_storable
from clipforge.storage import (
    Database,
    NewSegment,
    NewWord,
    Page,
    Source,
    SourceRepository,
    TranscriptRepository,
    TranscriptRevision,
    TranscriptSegment,
    TranscriptWord,
)
from clipforge.storage.repositories import MAX_PAGE_SIZE as READ_BATCH

# An SRT/VTT file of a ten-hour recording is about 1.5 million characters.
MAX_IMPORT_TEXT_CHARS: Final = 5_000_000
# Entries of words, segments and coverage together in a normalized JSON document.
MAX_IMPORT_ITEMS: Final = 400_000

IMPORT_ORIGIN: Final = "import"
CORRECTION_ORIGIN: Final = "correction"
FORMAT_JSON: Final = "normalized_json"
_JSON_FIELD: Final = "document"
_TEXT_FIELD: Final = "text"

# Edits of one correction request.
MAX_EDITS: Final = 500
MAX_WORD_TEXT_CHARS: Final = 200
# A segment is limited to ``MAX_TEXT_CHARS`` (2000, ADR 030) whether it is edited directly or
# recomputed from its words.
MAX_SEGMENT_TEXT_CHARS: Final = MAX_TEXT_CHARS
_EDIT_CODE: Final = "VALIDATION_ERROR"
# Control and surrogate characters, and the line and paragraph separators that act as line breaks.
_FORBIDDEN_CATEGORIES: Final = frozenset({"Cc", "Cs", "Zl", "Zp"})


@dataclass(frozen=True, slots=True)
class TextEdit:
    """One text correction: ``word_id`` for a revision with words, ``segment_id`` for a
    segment-level one. The API passes either; ``correct`` checks that exactly one is set."""

    word_id: str | None
    segment_id: str | None
    text: str


@dataclass(frozen=True, slots=True)
class TranscriptWindow:
    """A revision with one window of its segments and of its words."""

    revision: TranscriptRevision
    segments: list[TranscriptSegment]
    words: list[TranscriptWord]


class TranscriptService:
    def __init__(self, database: Database) -> None:
        self._database = database

    def import_text(
        self, *, source_id: str, format: str, text: str, offset_ms: int
    ) -> TranscriptRevision:
        """Import SRT (``format="srt"``) or WebVTT (``"vtt"``) as a segment-level revision."""
        if len(text) > MAX_IMPORT_TEXT_CHARS:
            raise invalid(
                "TRANSCRIPT_TOO_LARGE",
                f"Файл завеликий: максимум {MAX_IMPORT_TEXT_CHARS} символів",
                field=_TEXT_FIELD,
            )
        if format == "srt":
            parse = parse_srt
        elif format == "vtt":
            parse = parse_vtt
        else:
            raise invalid("INVALID_TRANSCRIPT_FORMAT", "Невідомий формат файлу", field="format")
        return self._import(
            source_id,
            field=_TEXT_FIELD,
            metadata={"format": format, "offset_ms": offset_ms},
            parse=lambda duration_ms: parse(text, duration_ms=duration_ms, offset_ms=offset_ms),
        )

    def import_document(
        self, *, source_id: str, document: object, offset_ms: int
    ) -> TranscriptRevision:
        """Import a normalized JSON document (``contracts/transcript.schema.json``)."""
        if _item_count(document) > MAX_IMPORT_ITEMS:
            raise invalid(
                "TRANSCRIPT_TOO_LARGE",
                f"Документ завеликий: максимум {MAX_IMPORT_ITEMS} елементів",
                field=_JSON_FIELD,
            )
        return self._import(
            source_id,
            field=_JSON_FIELD,
            metadata={"format": FORMAT_JSON, "offset_ms": offset_ms},
            parse=lambda duration_ms: validate_normalized_json(
                document, duration_ms=duration_ms, offset_ms=offset_ms
            ),
        )

    def correct(self, transcript_id: str, edits: Sequence[TextEdit]) -> TranscriptRevision:
        """Save ``edits`` as a new ``correction`` revision whose ``parent_id`` is ``transcript_id``.

        A revision with words (``timing_level`` ``word``) takes edits by ``word_id`` and its
        segments that contain an edited word get their text again from their words, joined by one
        space (the merge rule); other segments keep their text. A segment-level revision takes
        edits by ``segment_id``. Nothing else changes: no time, ordinal, speaker, confidence or
        chunk. A request with a bad edit saves nothing.

        Raises ``TRANSCRIPT_NOT_FOUND`` (404), ``TRANSCRIPT_REVISION_STALE`` (409) when the
        revision is not the latest of its source, ``VALIDATION_ERROR`` (422) with
        ``details.field`` such as ``edits[2].text`` for an edit that cannot be applied.
        """
        with self._database.session() as session:
            transcripts = TranscriptRepository(session)
            parent = _require_revision(transcripts, transcript_id)
            _require_latest(transcripts, parent)
            words = _read_all(
                lambda offset, limit: transcripts.list_words(parent.id, offset=offset, limit=limit)
            )
            segments = _read_all(
                lambda offset, limit: transcripts.list_segments(
                    parent.id, offset=offset, limit=limit
                )
            )
        # The parent is immutable, so the new rows are built outside the write lock; only "still
        # the latest" has to be decided under it.
        new_words, new_segments = _apply_edits(parent.timing_level, words, segments, edits)
        with self._database.write_session() as session:
            transcripts = TranscriptRepository(session)
            _require_latest(transcripts, parent)
            return transcripts.add_revision(
                source_id=parent.source_id,
                origin=CORRECTION_ORIGIN,
                parent_id=parent.id,
                timing_level=parent.timing_level,
                language=parent.language,
                duration_ms=parent.duration_ms,
                coverage=parent.coverage,
                metadata=parent.metadata_json,
                words=new_words,
                segments=new_segments,
            )

    def get(self, transcript_id: str) -> TranscriptRevision:
        with self._database.session() as session:
            return _require_revision(TranscriptRepository(session), transcript_id)

    def get_window(self, transcript_id: str, *, offset: int, limit: int) -> TranscriptWindow:
        """The revision with the segments and the words ``[offset, offset + limit)`` of their
        ordinal order (``offset`` counts items, not ordinals)."""
        with self._database.session() as session:
            transcripts = TranscriptRepository(session)
            revision = _require_revision(transcripts, transcript_id)
            return TranscriptWindow(
                revision=revision,
                segments=transcripts.list_segments(revision.id, offset=offset, limit=limit),
                words=transcripts.list_words(revision.id, offset=offset, limit=limit),
            )

    def list_page(
        self, source_id: str, *, limit: int, cursor: str | None
    ) -> Page[TranscriptRevision]:
        with self._database.session() as session:
            _require_source(SourceRepository(session), source_id)
            try:
                return TranscriptRepository(session).list_page_for_source(
                    source_id, limit=limit, after=cursor
                )
            except ValueError:
                raise invalid_cursor() from None

    def _import(
        self,
        source_id: str,
        *,
        field: str,
        metadata: dict[str, Any],
        parse: Callable[[int], NormalizedTranscript],
    ) -> TranscriptRevision:
        with self._database.session() as session:
            duration_ms = _require_source(SourceRepository(session), source_id).duration_ms
        try:
            transcript = parse(duration_ms)
        except TranscriptImportError as error:
            raise _import_error(error, field) from None
        if not _is_storable(transcript):
            raise invalid(
                "TRANSCRIPT_TEXT_INVALID", "Транскрипт містить недопустимі символи", field=field
            )
        words = [_new_word(word) for word in transcript.words]
        segments = [
            NewSegment(
                id=segment.id,
                ordinal=segment.ordinal,
                start_ms=segment.start_ms,
                end_ms=segment.end_ms,
                text=segment.text,
                word_ids=word_ids,
            )
            for segment, word_ids in zip(
                transcript.segments,
                _word_ids_by_segment(transcript.words, transcript.segments),
                strict=True,
            )
        ]
        with self._database.write_session() as session:
            return TranscriptRepository(session).add_revision(
                source_id=source_id,
                origin=IMPORT_ORIGIN,
                timing_level=transcript.timing_level,
                language=transcript.language,
                duration_ms=transcript.duration_ms,
                coverage=[span.to_json() for span in transcript.coverage],
                metadata=metadata,
                words=words,
                segments=segments,
            )


def _item_count(document: object) -> int:
    """Entries of the three lists of a document; 0 when it is not shaped like one (the schema
    check reports that)."""
    if not isinstance(document, dict):
        return 0
    return sum(
        len(entries)
        for key in ("words", "segments", "coverage")
        if isinstance(entries := document.get(key), list)
    )


def _new_word(word: TranscriptItem) -> NewWord:
    return NewWord(
        id=word.id,
        ordinal=word.ordinal,
        start_ms=word.start_ms,
        end_ms=word.end_ms,
        text=word.text,
    )


def _word_ids_by_segment(
    words: Sequence[TranscriptItem], segments: Sequence[TranscriptItem]
) -> list[list[str]]:
    """For each segment the ids of the words that lie inside it: ``seg.start <= word.start`` and
    ``word.end <= seg.end``. The document has no explicit mapping, so this is derived from time;
    overlapping segments may share a word, and a word that crosses a segment border belongs to
    none. Words are ordered by start, so the search starts at the first candidate."""
    starts = [word.start_ms for word in words]
    mapping: list[list[str]] = []
    for segment in segments:
        ids: list[str] = []
        for index in range(bisect_left(starts, segment.start_ms), len(words)):
            word = words[index]
            if word.start_ms >= segment.end_ms:
                break
            if word.end_ms <= segment.end_ms:
                ids.append(word.id)
        mapping.append(ids)
    return mapping


def _is_storable(transcript: NormalizedTranscript) -> bool:
    """Every string that is about to be stored; SQLite cannot hold NUL or lone surrogates."""
    return is_storable(transcript.language) and all(
        is_storable(item.id) and is_storable(item.text)
        for item in (*transcript.words, *transcript.segments)
    )


def _read_all[T](read: Callable[[int, int], list[T]]) -> list[T]:
    """Every item of a windowed read ``read(offset, limit)``, one repository window at a time."""
    items: list[T] = []
    while True:
        batch = read(len(items), READ_BATCH)
        items += batch
        if len(batch) < READ_BATCH:
            return items


def _require_latest(transcripts: TranscriptRepository, revision: TranscriptRevision) -> None:
    """409 unless ``revision`` is the newest of its source (the highest revision number)."""
    latest = revision
    cursor: str | None = None
    while True:
        page = transcripts.list_page_for_source(revision.source_id, limit=READ_BATCH, after=cursor)
        if page.items:
            latest = page.items[-1]
        if page.next_cursor is None:
            break
        cursor = page.next_cursor
    if latest.id != revision.id:
        raise conflict(
            "TRANSCRIPT_REVISION_STALE",
            "Транскрипт уже має новішу ревізію. Оновіть дані та повторіть",
            details={"latest_id": latest.id},
        )


def _apply_edits(
    timing_level: str,
    words: Sequence[TranscriptWord],
    segments: Sequence[TranscriptSegment],
    edits: Sequence[TextEdit],
) -> tuple[list[NewWord], list[NewSegment]]:
    """The words and segments of the correction: those of the parent with the edited text.

    Edits are checked in request order and the first bad one is reported with the
    ``edits[i]...`` path of its field. Messages are fixed text.
    """
    by_words = timing_level == "word"
    known = {item.id for item in (words if by_words else segments)}
    id_key = "word_id" if by_words else "segment_id"
    other_key = "segment_id" if by_words else "word_id"
    replacements: dict[str, str] = {}
    positions: dict[str, int] = {}
    for index, edit in enumerate(edits):
        path = f"edits[{index}]"
        if (edit.word_id is None) == (edit.segment_id is None):
            raise invalid(
                _EDIT_CODE,
                "Правка має містити рівно одне з полів: word_id або segment_id",
                field=path,
            )
        target = edit.word_id if by_words else edit.segment_id
        if target is None:  # the other id is set: it does not fit this revision
            raise invalid(
                _EDIT_CODE,
                "Ця ревізія має слова: правте їх через word_id"
                if by_words
                else "Ця ревізія містить лише репліки: правте їх через segment_id",
                field=f"{path}.{other_key}",
            )
        subject = "Слово" if by_words else "Репліка"
        if target not in known:
            raise invalid(
                _EDIT_CODE,
                f"{subject} не знайдено в цій ревізії",
                field=f"{path}.{id_key}",
            )
        if target in replacements:
            raise invalid(_EDIT_CODE, f"{subject} повторюється в правках", field=f"{path}.{id_key}")
        replacements[target] = _edit_text(
            edit.text,
            field=f"{path}.text",
            label="Текст слова" if by_words else "Текст репліки",
            max_length=MAX_WORD_TEXT_CHARS if by_words else MAX_SEGMENT_TEXT_CHARS,
        )
        positions[target] = index

    if not by_words:
        return [], [
            NewSegment(
                id=segment.id,
                ordinal=segment.ordinal,
                start_ms=segment.start_ms,
                end_ms=segment.end_ms,
                text=replacements.get(segment.id, segment.text),
                word_ids=list(segment.word_ids),
            )
            for segment in segments
        ]

    new_words = [
        NewWord(
            id=word.id,
            ordinal=word.ordinal,
            start_ms=word.start_ms,
            end_ms=word.end_ms,
            text=replacements.get(word.id, word.text),
            chunk_id=word.chunk_id,
            speaker_local=word.speaker_local,
            confidence=word.confidence,
        )
        for word in words
    ]
    texts = {word.id: word.text for word in new_words}
    new_segments: list[NewSegment] = []
    for segment in segments:
        text = segment.text
        touched = [positions[word_id] for word_id in segment.word_ids if word_id in positions]
        if touched:
            text = " ".join(texts[word_id] for word_id in segment.word_ids)
            if len(text) > MAX_SEGMENT_TEXT_CHARS:
                raise invalid(
                    _EDIT_CODE,
                    f"Репліка після правки довша за {MAX_SEGMENT_TEXT_CHARS} символів",
                    field=f"edits[{min(touched)}].text",
                )
        new_segments.append(
            NewSegment(
                id=segment.id,
                ordinal=segment.ordinal,
                start_ms=segment.start_ms,
                end_ms=segment.end_ms,
                text=text,
                word_ids=list(segment.word_ids),
            )
        )
    return new_words, new_segments


def _edit_text(value: str, *, field: str, label: str, max_length: int) -> str:
    """Plain text, trimmed: 1..``max_length`` characters without control characters."""
    text = clean_text(value, field=field, code=_EDIT_CODE, label=label, max_length=max_length)
    if any(unicodedata.category(char) in _FORBIDDEN_CATEGORIES for char in text):
        raise invalid(_EDIT_CODE, f"{label} містить недопустимі символи", field=field)
    return text


def _import_error(error: TranscriptImportError, field: str) -> ServiceError:
    details: dict[str, Any] = {"field": field}
    if error.line is not None:
        details["line"] = error.line
    if error.path is not None:
        details["path"] = error.path
    return ServiceError(ErrorKind.INVALID, error.code, error.message, details=details)


def _require_source(sources: SourceRepository, source_id: str) -> Source:
    source = sources.get(source_id)
    if source is None:
        raise not_found("SOURCE_NOT_FOUND", "Джерело не знайдено")
    return source


def _require_revision(transcripts: TranscriptRepository, transcript_id: str) -> TranscriptRevision:
    revision = transcripts.get(transcript_id)
    if revision is None:
        raise not_found("TRANSCRIPT_NOT_FOUND", "Транскрипт не знайдено")
    return revision
