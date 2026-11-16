"""POST /v1/transcripts/import, POST /v1/transcripts/{id}/corrections, GET /v1/transcripts/{id},
GET /v1/transcripts."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field

from clipforge.api.pagination import DEFAULT_PAGE_SIZE, Cursor, Limit
from clipforge.services.runtime import Runtime
from clipforge.services.transcripts import MAX_EDITS, TextEdit
from clipforge.storage import TranscriptRevision, TranscriptSegment, TranscriptWord

# Items of the segment and the word list of one GET /v1/transcripts/{id} page.
DEFAULT_WINDOW_SIZE = 200
MAX_WINDOW_SIZE = 1000
MAX_OFFSET = 2**31 - 1

WindowLimit = Annotated[int, Query(ge=1, le=MAX_WINDOW_SIZE)]
WindowOffset = Annotated[int, Query(ge=0, le=MAX_OFFSET)]
SourceIdQuery = Annotated[str, Query(min_length=1, max_length=200)]
# Millisecond offsets are stored in 32-bit columns: a larger shift can only leave the source.
OffsetMs = Annotated[int, Field(ge=-MAX_OFFSET, le=MAX_OFFSET)]


class _ImportBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    source_id: str
    offset_ms: OffsetMs = 0


class JsonImport(_ImportBody):
    format: Literal["normalized_json"]
    # Checked against contracts/transcript.schema.json by the domain, which reports in Ukrainian.
    document: Any


class TextImport(_ImportBody):
    format: Literal["srt", "vtt"]
    text: str


TranscriptImport = Annotated[JsonImport | TextImport, Field(discriminator="format")]


class EditBody(BaseModel):
    """``word_id`` for a revision with words, ``segment_id`` for a segment-level one: exactly one,
    and which one fits is decided against the revision (a 422 with ``details.field``)."""

    model_config = ConfigDict(extra="forbid", strict=True)

    word_id: str | None = None
    segment_id: str | None = None
    text: str


class CorrectionBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    edits: Annotated[list[EditBody], Field(min_length=1, max_length=MAX_EDITS)]


class CoverageSpanOut(BaseModel):
    start_ms: int
    end_ms: int
    state: str


class TranscriptOut(BaseModel):
    """``id`` names one immutable revision; ``revision`` counts the source's revisions from 1."""

    id: str
    source_id: str
    revision: int
    origin: str
    run_id: str | None
    parent_id: str | None
    timing_level: str
    language: str
    duration_ms: int
    word_count: int
    segment_count: int
    coverage: list[CoverageSpanOut]
    metadata: dict[str, Any]
    created_at: str


class SegmentOut(BaseModel):
    id: str
    ordinal: int
    start_ms: int
    end_ms: int
    text: str
    word_ids: list[str]
    timing_level: str


class WordOut(BaseModel):
    id: str
    ordinal: int
    start_ms: int
    end_ms: int
    text: str
    chunk_id: str | None
    speaker_local: str | None
    confidence: float | None


class TranscriptDetailOut(TranscriptOut):
    """The revision with items ``[offset, offset + limit)`` of its segments and of its words.

    ``next_offset`` is the ``offset`` of the next page, or ``null`` when neither list has more.
    """

    offset: int
    limit: int
    next_offset: int | None
    segments: list[SegmentOut]
    words: list[WordOut]


class TranscriptPage(BaseModel):
    items: list[TranscriptOut]
    next_cursor: str | None


def transcript_out(revision: TranscriptRevision) -> TranscriptOut:
    return TranscriptOut(
        id=revision.id,
        source_id=revision.source_id,
        revision=revision.revision,
        origin=revision.origin,
        run_id=revision.run_id,
        parent_id=revision.parent_id,
        timing_level=revision.timing_level,
        language=revision.language,
        duration_ms=revision.duration_ms,
        word_count=revision.word_count,
        segment_count=revision.segment_count,
        coverage=[CoverageSpanOut(**span) for span in revision.coverage],
        metadata=revision.metadata_json,
        created_at=revision.created_at,
    )


def segment_out(segment: TranscriptSegment) -> SegmentOut:
    return SegmentOut(
        id=segment.id,
        ordinal=segment.ordinal,
        start_ms=segment.start_ms,
        end_ms=segment.end_ms,
        text=segment.text,
        word_ids=segment.word_ids,
        timing_level=segment.timing_level,
    )


def word_out(word: TranscriptWord) -> WordOut:
    return WordOut(
        id=word.id,
        ordinal=word.ordinal,
        start_ms=word.start_ms,
        end_ms=word.end_ms,
        text=word.text,
        chunk_id=word.chunk_id,
        speaker_local=word.speaker_local,
        confidence=word.confidence,
    )


def build_router(runtime: Runtime) -> APIRouter:
    router = APIRouter()

    # Plain `def` routes run in FastAPI's thread pool: SQLite calls block.
    @router.post("/transcripts/import", status_code=201, response_model=TranscriptOut)
    def import_transcript(body: TranscriptImport) -> TranscriptOut:
        if isinstance(body, JsonImport):
            revision = runtime.transcripts.import_document(
                source_id=body.source_id, document=body.document, offset_ms=body.offset_ms
            )
        else:
            revision = runtime.transcripts.import_text(
                source_id=body.source_id,
                format=body.format,
                text=body.text,
                offset_ms=body.offset_ms,
            )
        return transcript_out(revision)

    @router.post(
        "/transcripts/{transcript_id}/corrections", status_code=201, response_model=TranscriptOut
    )
    def correct_transcript(transcript_id: str, body: CorrectionBody) -> TranscriptOut:
        revision = runtime.transcripts.correct(
            transcript_id,
            [
                TextEdit(word_id=edit.word_id, segment_id=edit.segment_id, text=edit.text)
                for edit in body.edits
            ],
        )
        return transcript_out(revision)

    @router.get("/transcripts", response_model=TranscriptPage)
    def list_transcripts(
        source_id: SourceIdQuery,
        limit: Limit = DEFAULT_PAGE_SIZE,
        cursor: Cursor = None,
    ) -> TranscriptPage:
        page = runtime.transcripts.list_page(source_id, limit=limit, cursor=cursor)
        return TranscriptPage(
            items=[transcript_out(revision) for revision in page.items],
            next_cursor=page.next_cursor,
        )

    @router.get("/transcripts/{transcript_id}", response_model=TranscriptDetailOut)
    def get_transcript(
        transcript_id: str,
        offset: WindowOffset = 0,
        limit: WindowLimit = DEFAULT_WINDOW_SIZE,
    ) -> TranscriptDetailOut:
        window = runtime.transcripts.get_window(transcript_id, offset=offset, limit=limit)
        revision = window.revision
        end = offset + limit
        has_more = end < max(revision.segment_count, revision.word_count)
        return TranscriptDetailOut(
            **transcript_out(revision).model_dump(),
            offset=offset,
            limit=limit,
            next_offset=end if has_more else None,
            segments=[segment_out(segment) for segment in window.segments],
            words=[word_out(word) for word in window.words],
        )

    return router
