"""POST /v1/transcripts/import, POST /v1/transcripts/{id}/corrections, GET /v1/transcripts/{id},
GET /v1/transcripts (FR-019, FR-020, T-022, T-024)."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response

from clipforge.services import transcripts as transcript_service
from clipforge.services.runtime import Runtime
from clipforge.storage import NewSegment, NewWord, TranscriptRepository, TranscriptRunRepository

from .support import BACKEND_DIR, RuntimeFactory, client_for
from .test_storage_support import add_project, add_source

DURATION_MS = 12000
EXAMPLE = BACKEND_DIR.parents[1] / "contracts" / "transcript.example.json"

SRT = """1
00:00:01,000 --> 00:00:03,000
Спробую
перестрибнути.

2
00:00:05,000 --> 00:00:07,000
<i>Не вийшло!</i>
"""

VTT = """WEBVTT

NOTE це коментар

00:01.000 --> 00:03.000 align:start
<v Іван>Привіт</v>

друга
00:00:05.000 --> 00:00:07.000
Другий рядок
"""


def example() -> dict[str, Any]:
    document: dict[str, Any] = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    return document


def long_document() -> dict[str, Any]:
    """25 words of 100 ms and 8 segments of 300 ms: the words w1..w24 fall into s1..s8, w25 into
    none."""
    return {
        "schema_version": 1,
        "timing_level": "word",
        "language": "uk",
        "duration_ms": DURATION_MS,
        "words": [
            {
                "id": f"w{n}",
                "ordinal": n - 1,
                "start_ms": (n - 1) * 100,
                "end_ms": n * 100,
                "text": f"слово{n}",
            }
            for n in range(1, 26)
        ],
        "segments": [
            {
                "id": f"s{m}",
                "ordinal": m - 1,
                "start_ms": (m - 1) * 300,
                "end_ms": m * 300,
                "text": f"репліка {m}",
            }
            for m in range(1, 9)
        ],
        "coverage": [{"start_ms": 0, "end_ms": 2500, "state": "succeeded"}],
    }


def seed_source(runtime: Runtime, *, duration_ms: int = DURATION_MS) -> str:
    """A registered source without probing a file: a transcript only needs its stored duration."""
    with runtime.database.write_session() as session:
        return add_source(session, add_project(session).id, duration_ms=duration_ms).id


def import_json(client: TestClient, source_id: str, document: Any, **extra: Any) -> Response:
    body = {"source_id": source_id, "format": "normalized_json", "document": document, **extra}
    return client.post("/v1/transcripts/import", json=body)


def import_text(client: TestClient, source_id: str, fmt: str, text: str, **extra: Any) -> Response:
    body = {"source_id": source_id, "format": fmt, "text": text, **extra}
    return client.post("/v1/transcripts/import", json=body)


def created(response: Response) -> dict[str, Any]:
    assert response.status_code == 201, response.text
    transcript: dict[str, Any] = response.json()
    return transcript


def error_of(response: Response) -> dict[str, Any]:
    error: dict[str, Any] = response.json()["error"]
    return error


def listed(client: TestClient, source_id: str) -> list[dict[str, Any]]:
    response = client.get("/v1/transcripts", params={"source_id": source_id})
    assert response.status_code == 200, response.text
    items: list[dict[str, Any]] = response.json()["items"]
    return items


@pytest.fixture
def source_id(runtime: Runtime) -> str:
    return seed_source(runtime)


# --- normalized JSON -----------------------------------------------------------------------------


def test_normalized_json_import_keeps_word_timing(client: TestClient, source_id: str) -> None:
    transcript = created(import_json(client, source_id, example()))
    assert transcript["timing_level"] == "word"  # T-022
    assert transcript["source_id"] == source_id
    assert transcript["revision"] == 1
    assert transcript["origin"] == "import"
    assert (transcript["run_id"], transcript["parent_id"]) == (None, None)
    assert transcript["language"] == "uk"
    assert transcript["duration_ms"] == DURATION_MS
    assert (transcript["word_count"], transcript["segment_count"]) == (4, 2)
    assert transcript["coverage"] == [{"start_ms": 0, "end_ms": 12000, "state": "succeeded"}]
    assert transcript["metadata"] == {"format": "normalized_json", "offset_ms": 0}
    assert transcript["id"]
    assert transcript["created_at"].endswith("Z")

    detail = client.get(f"/v1/transcripts/{transcript['id']}")
    assert detail.status_code == 200
    body = detail.json()
    assert {key: body[key] for key in transcript} == transcript
    assert (body["offset"], body["limit"], body["next_offset"]) == (0, 200, None)
    assert body["words"] == [
        {
            "id": f"w{n}",
            "ordinal": n - 1,
            "start_ms": start,
            "end_ms": end,
            "text": word,
            "chunk_id": None,
            "speaker_local": None,
            "confidence": None,
        }
        for n, start, end, word in (
            (1, 1000, 2000, "Спробую"),
            (2, 2000, 3000, "перестрибнути."),
            (3, 5000, 6000, "Не"),
            (4, 6000, 7000, "вийшло!"),
        )
    ]
    assert body["segments"] == [
        {
            "id": "s1",
            "ordinal": 0,
            "start_ms": 1000,
            "end_ms": 3000,
            "text": "Спробую перестрибнути.",
            "word_ids": ["w1", "w2"],
            "timing_level": "word",
        },
        {
            "id": "s2",
            "ordinal": 1,
            "start_ms": 5000,
            "end_ms": 7000,
            "text": "Не вийшло!",
            "word_ids": ["w3", "w4"],
            "timing_level": "word",
        },
    ]


def test_a_segment_level_json_document_has_no_words(client: TestClient, source_id: str) -> None:
    document = example()
    document["timing_level"] = "segment"
    document["words"] = []
    transcript = created(import_json(client, source_id, document))
    assert transcript["timing_level"] == "segment"
    assert transcript["word_count"] == 0
    detail = client.get(f"/v1/transcripts/{transcript['id']}").json()
    assert detail["words"] == []
    assert [(s["id"], s["word_ids"], s["timing_level"]) for s in detail["segments"]] == [
        ("s1", [], "segment"),
        ("s2", [], "segment"),
    ]


def test_the_duration_of_the_source_replaces_the_one_in_the_document(
    client: TestClient, source_id: str
) -> None:
    document = example()
    document["duration_ms"] = 99_000
    transcript = created(import_json(client, source_id, document))
    assert transcript["duration_ms"] == DURATION_MS


def test_json_offset_shifts_times_and_clips_the_coverage(
    client: TestClient, source_id: str
) -> None:
    transcript = created(import_json(client, source_id, example(), offset_ms=500))
    assert transcript["metadata"] == {"format": "normalized_json", "offset_ms": 500}
    assert transcript["coverage"] == [{"start_ms": 500, "end_ms": 12000, "state": "succeeded"}]
    detail = client.get(f"/v1/transcripts/{transcript['id']}").json()
    assert [(w["start_ms"], w["end_ms"]) for w in detail["words"]] == [
        (1500, 2500),
        (2500, 3500),
        (5500, 6500),
        (6500, 7500),
    ]
    assert [(s["start_ms"], s["end_ms"]) for s in detail["segments"]] == [
        (1500, 3500),
        (5500, 7500),
    ]

    earlier = created(import_json(client, source_id, example(), offset_ms=-1000))
    assert earlier["metadata"]["offset_ms"] == -1000
    assert earlier["coverage"] == [{"start_ms": 0, "end_ms": 11000, "state": "succeeded"}]
    detail = client.get(f"/v1/transcripts/{earlier['id']}").json()
    first_word = detail["words"][0]
    assert (first_word["start_ms"], first_word["end_ms"]) == (0, 1000)


def test_json_outside_the_source_is_422_and_saves_nothing(
    client: TestClient, source_id: str
) -> None:
    document = example()
    document["words"][3]["end_ms"] = 13000  # T-024
    response = import_json(client, source_id, document)
    assert response.status_code == 422
    error = error_of(response)
    assert error["code"] == "TRANSCRIPT_OUT_OF_BOUNDS"
    assert error["details"] == {"field": "document", "path": "words[3]"}
    assert error["retryable"] is False
    assert "words[3]" in error["message"]
    assert listed(client, source_id) == []


def test_an_offset_that_pushes_the_text_out_of_the_source_is_422(
    client: TestClient, source_id: str
) -> None:
    for offset in (6000, -1500):
        response = import_json(client, source_id, example(), offset_ms=offset)
        assert response.status_code == 422, offset
        assert error_of(response)["code"] == "TRANSCRIPT_OUT_OF_BOUNDS"
    assert listed(client, source_id) == []


@pytest.mark.parametrize(
    ("change", "code", "path"),
    [
        (lambda d: d.pop("language"), "TRANSCRIPT_SCHEMA_INVALID", "language"),
        (lambda d: d.update(schema_version=2), "TRANSCRIPT_SCHEMA_INVALID", "schema_version"),
        (lambda d: d.update(extra=1), "TRANSCRIPT_SCHEMA_INVALID", "extra"),
        (
            lambda d: d["words"][0].update(start_ms=1.5),
            "TRANSCRIPT_SCHEMA_INVALID",
            "words[0].start_ms",
        ),
        (lambda d: d["words"][1].update(id="w1"), "TRANSCRIPT_ID_DUPLICATE", "words[1]"),
        (
            lambda d: d["words"][1].update(start_ms=3500, end_ms=3000),
            "TRANSCRIPT_INTERVAL_REVERSED",
            "words[1]",
        ),
        (lambda d: d["words"][2].update(ordinal=1), "TRANSCRIPT_ORDER_INVALID", "words[2]"),
        (lambda d: d.update(timing_level="segment"), "TRANSCRIPT_TIMING_LEVEL_MISMATCH", "words"),
    ],
)
def test_invalid_json_documents_are_422_with_the_domain_code(
    client: TestClient,
    source_id: str,
    change: Any,
    code: str,
    path: str,
) -> None:
    document = example()
    change(document)
    response = import_json(client, source_id, document)
    assert response.status_code == 422
    error = error_of(response)
    assert error["code"] == code
    assert error["details"] == {"field": "document", "path": path}
    assert listed(client, source_id) == []


@pytest.mark.parametrize("document", [[], "текст", 7, None])
def test_a_document_that_is_not_an_object_is_422(
    client: TestClient, source_id: str, document: Any
) -> None:
    response = import_json(client, source_id, document)
    assert response.status_code == 422
    assert error_of(response)["code"] == "TRANSCRIPT_SCHEMA_INVALID"


# --- SRT / VTT -----------------------------------------------------------------------------------


def test_srt_import_is_segment_level(client: TestClient, source_id: str) -> None:
    transcript = created(import_text(client, source_id, "srt", SRT))
    assert transcript["timing_level"] == "segment"
    assert transcript["language"] == "und"
    assert (transcript["word_count"], transcript["segment_count"]) == (0, 2)
    assert transcript["coverage"] == [{"start_ms": 0, "end_ms": DURATION_MS, "state": "succeeded"}]
    assert transcript["metadata"] == {"format": "srt", "offset_ms": 0}
    detail = client.get(f"/v1/transcripts/{transcript['id']}").json()
    assert detail["words"] == []
    assert detail["segments"] == [
        {
            "id": "s1",
            "ordinal": 0,
            "start_ms": 1000,
            "end_ms": 3000,
            "text": "Спробую перестрибнути.",
            "word_ids": [],
            "timing_level": "segment",
        },
        {
            "id": "s2",
            "ordinal": 1,
            "start_ms": 5000,
            "end_ms": 7000,
            "text": "Не вийшло!",
            "word_ids": [],
            "timing_level": "segment",
        },
    ]


def test_vtt_import_accepts_notes_settings_and_identifiers(
    client: TestClient, source_id: str
) -> None:
    transcript = created(import_text(client, source_id, "vtt", VTT))
    assert transcript["timing_level"] == "segment"
    assert transcript["metadata"] == {"format": "vtt", "offset_ms": 0}
    detail = client.get(f"/v1/transcripts/{transcript['id']}").json()
    assert [(s["id"], s["start_ms"], s["end_ms"], s["text"]) for s in detail["segments"]] == [
        ("s1", 1000, 3000, "Привіт"),
        ("s2", 5000, 7000, "Другий рядок"),
    ]


def test_text_offset_shifts_every_cue_once(client: TestClient, source_id: str) -> None:
    transcript = created(import_text(client, source_id, "srt", SRT, offset_ms=500))
    assert transcript["metadata"] == {"format": "srt", "offset_ms": 500}
    # A shifted coverage is clipped to the source, here it still fits entirely.
    assert transcript["coverage"] == [{"start_ms": 0, "end_ms": DURATION_MS, "state": "succeeded"}]
    detail = client.get(f"/v1/transcripts/{transcript['id']}").json()
    assert [(s["start_ms"], s["end_ms"]) for s in detail["segments"]] == [
        (1500, 3500),
        (5500, 7500),
    ]


@pytest.mark.parametrize(
    ("fmt", "text", "code", "line"),
    [
        ("srt", "", "TRANSCRIPT_EMPTY", None),
        ("srt", "   \n\n", "TRANSCRIPT_EMPTY", None),
        ("srt", "просто текст\n", "TRANSCRIPT_CUE_MALFORMED", 1),
        ("srt", "1\n00:00:01,000 --> 00:00:01,000\nа\n", "TRANSCRIPT_INTERVAL_REVERSED", 2),
        ("srt", "1\n00:00:05,000 --> 00:00:04,000\nа\n", "TRANSCRIPT_INTERVAL_REVERSED", 2),
        ("srt", "1\n00:00:10,000 --> 00:00:12,001\nа\n", "TRANSCRIPT_OUT_OF_BOUNDS", 2),
        ("srt", "1\n00:00:10,000 --> 00:00:13,000\nа\n", "TRANSCRIPT_OUT_OF_BOUNDS", 2),
        ("srt", "1\n00:00:aa,000 --> 00:00:02,000\nа\n", "TRANSCRIPT_TIMESTAMP_INVALID", 2),
        ("srt", "1\n00:00:01,000 --> 00:00:02,000\n<i></i>\n", "TRANSCRIPT_NO_CUES", None),
        ("vtt", "00:00:01.000 --> 00:00:02.000\nа\n", "TRANSCRIPT_VTT_HEADER_MISSING", 1),
        ("vtt", "WEBVTT\n\n00:01,000 --> 00:02,000\nа\n", "TRANSCRIPT_TIMESTAMP_INVALID", 3),
    ],
)
def test_invalid_text_is_422_with_the_domain_code_and_line(
    client: TestClient,
    source_id: str,
    fmt: str,
    text: str,
    code: str,
    line: int | None,
) -> None:
    response = import_text(client, source_id, fmt, text)
    assert response.status_code == 422
    error = error_of(response)
    assert error["code"] == code
    assert error["details"] == {"field": "text"} | ({"line": line} if line is not None else {})
    assert error["message"]
    assert error["retryable"] is False
    assert listed(client, source_id) == []


def test_srt_end_at_the_exact_source_end_is_accepted(client: TestClient, source_id: str) -> None:
    created(import_text(client, source_id, "srt", "1\n00:00:09,000 --> 00:00:12,000\nфінал\n"))


def test_a_text_offset_that_leaves_the_source_is_422(client: TestClient, source_id: str) -> None:
    response = import_text(client, source_id, "srt", SRT, offset_ms=5001)
    assert response.status_code == 422
    assert error_of(response)["code"] == "TRANSCRIPT_OUT_OF_BOUNDS"
    assert listed(client, source_id) == []


# --- request shape, limits, unknown ids ----------------------------------------------------------


def test_an_unknown_source_is_404(client: TestClient) -> None:
    for response in (
        import_json(client, "missing", example()),
        import_text(client, "missing", "srt", SRT),
        client.get("/v1/transcripts", params={"source_id": "missing"}),
    ):
        assert response.status_code == 404
        assert error_of(response)["code"] == "SOURCE_NOT_FOUND"
        assert error_of(response)["message"] == "Джерело не знайдено"


def test_an_unknown_transcript_is_404(client: TestClient) -> None:
    response = client.get("/v1/transcripts/missing")
    assert response.status_code == 404
    assert error_of(response)["code"] == "TRANSCRIPT_NOT_FOUND"
    assert client.get("/v1/transcripts/import").status_code == 404


@pytest.mark.parametrize(
    "body",
    [
        {"format": "srt", "text": SRT},  # no source_id
        {"source_id": "s", "text": SRT},  # no format
        {"source_id": "s", "format": "txt", "text": "а"},
        {"source_id": "s", "format": "srt"},  # no text
        {"source_id": "s", "format": "srt", "text": 5},
        {"source_id": "s", "format": "srt", "text": SRT, "document": {}},
        {"source_id": "s", "format": "normalized_json", "text": SRT},
        {"source_id": "s", "format": "normalized_json"},  # no document
        {"source_id": "s", "format": "srt", "text": SRT, "offset_ms": 1.5},
        {"source_id": "s", "format": "srt", "text": SRT, "offset_ms": "5"},
        {"source_id": "s", "format": "srt", "text": SRT, "offset_ms": True},
        {"source_id": "s", "format": "srt", "text": SRT, "offset_ms": 2**31},
        {"source_id": "s", "format": "srt", "text": SRT, "offset_ms": -(2**31)},
        {"source_id": 7, "format": "srt", "text": SRT},
        {"source_id": "s", "format": "srt", "text": SRT, "language": "uk"},
    ],
)
def test_a_malformed_request_is_422(client: TestClient, body: dict[str, Any]) -> None:
    response = client.post("/v1/transcripts/import", json=body)
    assert response.status_code == 422
    assert error_of(response)["code"] == "VALIDATION_ERROR"


def test_a_body_that_is_not_json_is_422(client: TestClient) -> None:
    response = client.post(
        "/v1/transcripts/import",
        content=b"WEBVTT",
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422
    assert error_of(response)["code"] == "VALIDATION_ERROR"


def test_too_much_text_is_422(
    client: TestClient, source_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(transcript_service, "MAX_IMPORT_TEXT_CHARS", len(SRT))
    created(import_text(client, source_id, "srt", SRT))  # exactly at the cap
    response = import_text(client, source_id, "srt", SRT + " ")
    assert response.status_code == 422
    error = error_of(response)
    assert error["code"] == "TRANSCRIPT_TOO_LARGE"
    assert error["details"] == {"field": "text"}
    assert len(listed(client, source_id)) == 1


def test_too_many_json_items_is_422(
    client: TestClient, source_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(transcript_service, "MAX_IMPORT_ITEMS", 7)  # 4 words + 2 segments + 1 span
    created(import_json(client, source_id, example()))
    document = example()
    document["coverage"].append({"start_ms": 12000, "end_ms": 12001, "state": "failed"})
    response = import_json(client, source_id, document)
    assert response.status_code == 422
    error = error_of(response)
    assert error["code"] == "TRANSCRIPT_TOO_LARGE"
    assert error["details"] == {"field": "document"}
    assert len(listed(client, source_id)) == 1


def test_a_lone_surrogate_in_the_text_is_422_not_a_server_error(
    client: TestClient, source_id: str
) -> None:
    # The escape \ud800 is valid JSON, but the decoded string cannot be stored or encoded.
    json_body: dict[str, Any] = {
        "source_id": source_id,
        "format": "normalized_json",
        "document": example(),
    }
    json_body["document"]["words"][2]["text"] = "ZZ"
    srt_body = {
        "source_id": source_id,
        "format": "srt",
        "text": "1\n00:00:01,000 --> 00:00:02,000\nZZ\n",
    }
    for body in (json_body, srt_body):
        raw = json.dumps(body).replace("ZZ", "\\ud800").encode("ascii")
        response = client.post(
            "/v1/transcripts/import", content=raw, headers={"Content-Type": "application/json"}
        )
        assert response.status_code == 422, response.text
        assert error_of(response)["code"] == "TRANSCRIPT_TEXT_INVALID"
        assert listed(client, source_id) == []


def test_transcript_routes_need_the_session_token(anonymous: TestClient, source_id: str) -> None:
    body = {"source_id": source_id, "format": "srt", "text": SRT}
    assert anonymous.post("/v1/transcripts/import", json=body).status_code == 401
    assert anonymous.get("/v1/transcripts", params={"source_id": source_id}).status_code == 401
    assert anonymous.get("/v1/transcripts/x").status_code == 401


def test_import_works_without_media_tools(make_runtime: RuntimeFactory) -> None:
    runtime = make_runtime(with_tools=False)
    client = client_for(runtime)
    transcript = created(import_text(client, seed_source(runtime), "srt", SRT))
    assert transcript["segment_count"] == 2


# --- revisions -----------------------------------------------------------------------------------


def test_every_import_is_a_new_revision_and_the_old_one_stays_as_it_was(
    client: TestClient, source_id: str
) -> None:
    first = created(import_json(client, source_id, example()))
    before = client.get(f"/v1/transcripts/{first['id']}").json()
    second = created(import_text(client, source_id, "srt", SRT))
    third = created(import_json(client, source_id, example(), offset_ms=100))

    assert [first["revision"], second["revision"], third["revision"]] == [1, 2, 3]
    assert len({first["id"], second["id"], third["id"]}) == 3
    assert client.get(f"/v1/transcripts/{first['id']}").json() == before
    assert (second["timing_level"], third["timing_level"]) == ("segment", "word")


def test_revisions_are_numbered_per_source(runtime: Runtime, client: TestClient) -> None:
    first, second = seed_source(runtime), seed_source(runtime)
    numbers = [
        created(import_text(client, source, "srt", SRT))["revision"]
        for source in (first, second, first)
    ]
    assert numbers == [1, 1, 2]
    assert [t["revision"] for t in listed(client, first)] == [1, 2]
    assert [t["revision"] for t in listed(client, second)] == [1]


def test_concurrent_imports_get_distinct_consecutive_revisions(
    runtime: Runtime, source_id: str
) -> None:
    workers = 5
    # One app per thread. FastAPI builds each app's routes on its first request, and its
    # warnings.catch_warnings() is not thread-safe: pyproject.toml ignores the warning that races.
    clients = [client_for(runtime) for _ in range(workers)]

    def attempt(number: int) -> Response:
        return import_text(clients[number], source_id, "srt", SRT)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        responses = list(pool.map(attempt, range(workers)))

    assert [r.status_code for r in responses] == [201] * workers
    assert sorted(r.json()["revision"] for r in responses) == list(range(1, workers + 1))
    assert len({r.json()["id"] for r in responses}) == workers


# --- GET /v1/transcripts/{id} --------------------------------------------------------------------


def test_segments_and_words_are_paged_by_offset_and_limit(
    client: TestClient, source_id: str
) -> None:
    transcript = created(import_json(client, source_id, long_document()))
    assert (transcript["word_count"], transcript["segment_count"]) == (25, 8)
    url = f"/v1/transcripts/{transcript['id']}"

    def page(**params: int) -> dict[str, Any]:
        response = client.get(url, params=params)
        assert response.status_code == 200, response.text
        body: dict[str, Any] = response.json()
        return body

    first = page(offset=0, limit=10)
    assert [s["id"] for s in first["segments"]] == [f"s{m}" for m in range(1, 9)]
    assert [w["id"] for w in first["words"]] == [f"w{n}" for n in range(1, 11)]
    assert (first["offset"], first["limit"], first["next_offset"]) == (0, 10, 10)
    assert first["word_count"] == 25
    assert first["segments"][0]["word_ids"] == ["w1", "w2", "w3"]
    assert first["segments"][7]["word_ids"] == ["w22", "w23", "w24"]

    second = page(offset=10, limit=10)
    assert second["segments"] == []
    assert [w["id"] for w in second["words"]] == [f"w{n}" for n in range(11, 21)]
    assert second["next_offset"] == 20

    last = page(offset=20, limit=10)
    assert [w["id"] for w in last["words"]] == [f"w{n}" for n in range(21, 26)]
    assert last["next_offset"] is None

    beyond = page(offset=25, limit=10)
    assert (beyond["segments"], beyond["words"], beyond["next_offset"]) == ([], [], None)

    whole = page(offset=0, limit=25)
    assert (len(whole["segments"]), len(whole["words"]), whole["next_offset"]) == (8, 25, None)

    # Paging both lists with the same window covers every item exactly once.
    seen: list[str] = []
    offset = 0
    while True:
        body = page(offset=offset, limit=7)
        seen += [item["id"] for item in (*body["segments"], *body["words"])]
        if body["next_offset"] is None:
            break
        offset = body["next_offset"]
    assert sorted(seen) == sorted([f"s{m}" for m in range(1, 9)] + [f"w{n}" for n in range(1, 26)])


def test_the_default_page_holds_up_to_200_items_per_list(
    client: TestClient, source_id: str
) -> None:
    document = long_document()
    words = [
        {
            "id": f"w{n}",
            "ordinal": n - 1,
            "start_ms": (n - 1) * 10,
            "end_ms": (n - 1) * 10 + 5,
            "text": "а",
        }
        for n in range(1, 251)
    ]
    document["words"] = words
    document["segments"] = []
    document["coverage"] = [{"start_ms": 0, "end_ms": 2500, "state": "succeeded"}]
    transcript = created(import_json(client, source_id, document))
    body = client.get(f"/v1/transcripts/{transcript['id']}").json()
    assert (len(body["words"]), body["limit"], body["next_offset"]) == (200, 200, 200)
    rest = client.get(f"/v1/transcripts/{transcript['id']}", params={"offset": 200}).json()
    assert (len(rest["words"]), rest["next_offset"]) == (50, None)


@pytest.mark.parametrize(
    "params",
    [
        {"limit": 0},
        {"limit": 1001},
        {"limit": "x"},
        {"offset": -1},
        {"offset": 2**31},
        {"offset": "x"},
    ],
)
def test_bad_paging_parameters_are_422(
    client: TestClient, source_id: str, params: dict[str, Any]
) -> None:
    transcript = created(import_text(client, source_id, "srt", SRT))
    response = client.get(f"/v1/transcripts/{transcript['id']}", params=params)
    assert response.status_code == 422
    assert error_of(response)["code"] == "VALIDATION_ERROR"


def test_the_largest_window_is_accepted(client: TestClient, source_id: str) -> None:
    transcript = created(import_text(client, source_id, "srt", SRT))
    response = client.get(f"/v1/transcripts/{transcript['id']}", params={"limit": 1000})
    assert response.status_code == 200
    assert response.json()["limit"] == 1000


# --- GET /v1/transcripts -------------------------------------------------------------------------


def test_the_list_holds_the_revisions_of_one_source_in_order(
    runtime: Runtime, client: TestClient
) -> None:
    source, other = seed_source(runtime), seed_source(runtime)
    ids = [
        created(import_text(client, source, "srt", SRT))["id"],
        created(import_json(client, source, example()))["id"],
        created(import_text(client, source, "vtt", VTT))["id"],
    ]
    created(import_text(client, other, "srt", SRT))

    items = listed(client, source)
    assert [item["id"] for item in items] == ids
    assert [item["revision"] for item in items] == [1, 2, 3]
    assert [item["timing_level"] for item in items] == ["segment", "word", "segment"]
    assert all(item["source_id"] == source for item in items)
    # The list carries metadata only: the same shape as the import response.
    assert "words" not in items[0]
    detail = client.get(f"/v1/transcripts/{ids[1]}").json()
    assert items[1] == {key: detail[key] for key in items[1]}
    assert listed(client, seed_source(runtime)) == []


def test_the_list_is_paged_by_cursor(runtime: Runtime, client: TestClient) -> None:
    source = seed_source(runtime)
    ids = [created(import_text(client, source, "srt", SRT))["id"] for _ in range(5)]

    seen: list[str] = []
    cursor: str | None = None
    pages = 0
    while True:
        params: dict[str, Any] = {"source_id": source, "limit": 2}
        if cursor is not None:
            params["cursor"] = cursor
        body = client.get("/v1/transcripts", params=params).json()
        seen += [item["id"] for item in body["items"]]
        pages += 1
        cursor = body["next_cursor"]
        if cursor is None:
            break
    assert seen == ids
    assert pages == 3


def test_the_list_rejects_bad_parameters(runtime: Runtime, client: TestClient) -> None:
    source = seed_source(runtime)
    other = seed_source(runtime)
    foreign = created(import_text(client, other, "srt", SRT))["id"]

    assert client.get("/v1/transcripts").status_code == 422  # source_id is required
    assert client.get("/v1/transcripts", params={"source_id": ""}).status_code == 422
    for limit in (0, 201):
        response = client.get("/v1/transcripts", params={"source_id": source, "limit": limit})
        assert response.status_code == 422
    for cursor in ("missing", foreign):  # a revision of another source is not a cursor here
        response = client.get("/v1/transcripts", params={"source_id": source, "cursor": cursor})
        assert response.status_code == 422
        assert error_of(response)["code"] == "INVALID_CURSOR"


# --- POST /v1/transcripts/{id}/corrections (FR-020) ----------------------------------------------


def correct(client: TestClient, transcript_id: str, edits: list[dict[str, Any]]) -> Response:
    return client.post(f"/v1/transcripts/{transcript_id}/corrections", json={"edits": edits})


def full(client: TestClient, transcript_id: str) -> dict[str, Any]:
    """The whole revision: one window of the largest size."""
    response = client.get(f"/v1/transcripts/{transcript_id}", params={"limit": 1000})
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    assert body["next_offset"] is None
    return body


def without_text(detail: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Everything of the words and segments except their text: ids, ordinals, times, speaker,
    confidence, chunk, word ids."""
    return {
        kind: [
            {key: value for key, value in item.items() if key != "text"} for item in detail[kind]
        ]
        for kind in ("words", "segments")
    }


def texts(detail: dict[str, Any], kind: str) -> list[str]:
    return [item["text"] for item in detail[kind]]


def assert_invalid(response: Response, field: str) -> None:
    assert response.status_code == 422, response.text
    error = error_of(response)
    assert error["code"] == "VALIDATION_ERROR"
    assert error["details"] == {"field": field}
    assert error["message"]
    assert error["retryable"] is False


def document_of_words(count: int, *, text: str = "а", segment: bool = False) -> dict[str, Any]:
    """``count`` words ``w1..`` of 10 ms and, with ``segment``, one segment ``s1`` over them all."""
    return {
        "schema_version": 1,
        "timing_level": "word",
        "language": "uk",
        "duration_ms": DURATION_MS,
        "words": [
            {
                "id": f"w{n}",
                "ordinal": n - 1,
                "start_ms": (n - 1) * 10,
                "end_ms": n * 10,
                "text": text,
            }
            for n in range(1, count + 1)
        ],
        "segments": [
            {"id": "s1", "ordinal": 0, "start_ms": 0, "end_ms": count * 10, "text": "репліка"}
        ]
        if segment
        else [],
        "coverage": [{"start_ms": 0, "end_ms": count * 10, "state": "succeeded"}],
    }


def seed_stt_revision(runtime: Runtime, source: str) -> tuple[str, str]:
    """An STT revision with speaker labels, confidences and a chunk on its words:
    ``(revision_id, chunk_id)``."""
    with runtime.database.write_session() as session:
        run = TranscriptRunRepository(session).add_run(
            source_id=source, model="m", provider="p", options_hash="h", status="succeeded"
        )
        chunk = TranscriptRunRepository(session).add_chunk(
            run_id=run.id,
            core_start_ms=0,
            core_end_ms=DURATION_MS,
            upload_start_ms=0,
            upload_end_ms=DURATION_MS,
            state="succeeded",
        )
        revision = TranscriptRepository(session).add_revision(
            source_id=source,
            origin="stt",
            run_id=run.id,
            timing_level="word",
            language="en",
            duration_ms=DURATION_MS,
            coverage=[{"start_ms": 0, "end_ms": DURATION_MS, "state": "succeeded"}],
            metadata={"model": "m"},
            words=[
                NewWord(
                    id="w1",
                    ordinal=0,
                    start_ms=0,
                    end_ms=700,
                    text="Hello",
                    chunk_id=chunk.id,
                    speaker_local="A",
                    confidence=0.9,
                ),
                NewWord(
                    id="w2",
                    ordinal=1,
                    start_ms=700,
                    end_ms=1500,
                    text="world",
                    chunk_id=chunk.id,
                    speaker_local="B",
                    confidence=0.25,
                ),
                NewWord(
                    id="w3",
                    ordinal=2,
                    start_ms=4000,
                    end_ms=4001,
                    text="again",
                    chunk_id=chunk.id,
                    speaker_local=None,
                    confidence=None,
                ),
            ],
            segments=[
                NewSegment(
                    id="s1",
                    ordinal=0,
                    start_ms=0,
                    end_ms=1500,
                    text="Hello world",
                    word_ids=["w1", "w2"],
                ),
                NewSegment(
                    id="s2", ordinal=1, start_ms=4000, end_ms=4001, text="again", word_ids=["w3"]
                ),
            ],
        )
        return revision.id, chunk.id


def test_a_word_correction_is_a_new_revision_that_keeps_every_time(
    client: TestClient, source_id: str
) -> None:
    parent = created(import_json(client, source_id, example()))
    before = full(client, parent["id"])

    child = created(
        correct(
            client,
            parent["id"],
            [{"word_id": "w2", "text": "  перестрибнув  "}, {"word_id": "w4", "text": "<b>ні</b>"}],
        )
    )

    assert child["origin"] == "correction"
    assert child["parent_id"] == parent["id"]
    assert child["run_id"] is None
    assert child["revision"] == 2
    assert child["id"] != parent["id"]
    assert child["created_at"].endswith("Z")
    for key in (
        "source_id",
        "timing_level",
        "language",
        "duration_ms",
        "word_count",
        "segment_count",
        "coverage",
        "metadata",
    ):
        assert child[key] == parent[key], key

    after = full(client, child["id"])
    assert {key: after[key] for key in child} == child
    # Ids, ordinals, start/end, speaker, confidence, chunk and the word ids of segments are equal.
    assert without_text(after) == without_text(before)
    # The text is trimmed and stored as written: markup is not interpreted.
    assert texts(after, "words") == ["Спробую", "перестрибнув", "Не", "<b>ні</b>"]
    # A segment with an edited word takes its text from its words, joined by one space.
    assert texts(after, "segments") == ["Спробую перестрибнув", "Не <b>ні</b>"]


def test_the_parent_revision_is_not_changed(client: TestClient, source_id: str) -> None:
    parent = created(import_json(client, source_id, example()))
    before = full(client, parent["id"])

    child = created(correct(client, parent["id"], [{"word_id": "w1", "text": "інше"}]))

    assert full(client, parent["id"]) == before
    items = listed(client, source_id)
    assert [(item["id"], item["revision"], item["origin"]) for item in items] == [
        (parent["id"], 1, "import"),
        (child["id"], 2, "correction"),
    ]
    assert items[0] == parent


def test_a_word_correction_with_the_same_text_is_still_a_new_revision(
    client: TestClient, source_id: str
) -> None:
    # ADR 036: nothing is compared with the old text, so an edit that changes nothing is a revision.
    parent = created(import_json(client, source_id, example()))
    before = full(client, parent["id"])
    assert texts(before, "words")[0] == "Спробую"

    same = created(correct(client, parent["id"], [{"word_id": "w1", "text": "Спробую"}]))
    assert (same["origin"], same["parent_id"], same["revision"]) == ("correction", parent["id"], 2)
    assert same["id"] != parent["id"]
    after = full(client, same["id"])
    assert after["words"] == before["words"]
    assert after["segments"] == before["segments"]

    # The text that is the same only after the trim counts the same way, on the new latest revision.
    padded = created(correct(client, same["id"], [{"word_id": "w1", "text": "  Спробую\t"}]))
    assert (padded["parent_id"], padded["revision"]) == (same["id"], 3)
    assert full(client, padded["id"])["words"] == before["words"]

    assert [(item["id"], item["revision"]) for item in listed(client, source_id)] == [
        (parent["id"], 1),
        (same["id"], 2),
        (padded["id"], 3),
    ]
    # Like any correction, it makes the corrected revision stale.
    stale = correct(client, parent["id"], [{"word_id": "w1", "text": "пізно"}])
    assert stale.status_code == 409, stale.text
    assert error_of(stale)["details"] == {"latest_id": padded["id"]}


def test_a_segment_correction_with_the_same_text_is_still_a_new_revision(
    client: TestClient, source_id: str
) -> None:
    parent = created(import_text(client, source_id, "srt", SRT))
    before = full(client, parent["id"])
    assert texts(before, "segments") == ["Спробую перестрибнути.", "Не вийшло!"]

    same = created(correct(client, parent["id"], [{"segment_id": "s2", "text": " Не вийшло! "}]))

    assert (same["origin"], same["parent_id"], same["revision"]) == ("correction", parent["id"], 2)
    assert same["id"] != parent["id"]
    after = full(client, same["id"])
    assert after["segments"] == before["segments"]
    assert after["words"] == before["words"] == []
    assert [item["revision"] for item in listed(client, source_id)] == [1, 2]


def test_every_time_of_a_large_revision_is_copied_when_all_words_change(
    client: TestClient, source_id: str
) -> None:
    parent = created(import_json(client, source_id, long_document()))
    before = full(client, parent["id"])
    edits = [{"word_id": f"w{n}", "text": f"нове{n}"} for n in range(1, 26)]

    child = created(correct(client, parent["id"], edits))

    after = full(client, child["id"])
    assert (child["word_count"], child["segment_count"]) == (25, 8)
    assert without_text(after) == without_text(before)
    assert texts(after, "words") == [f"нове{n}" for n in range(1, 26)]
    assert [(w["start_ms"], w["end_ms"]) for w in after["words"]] == [
        ((n - 1) * 100, n * 100) for n in range(1, 26)
    ]
    assert [(s["start_ms"], s["end_ms"]) for s in after["segments"]] == [
        ((m - 1) * 300, m * 300) for m in range(1, 9)
    ]


def test_only_segments_with_an_edited_word_get_their_text_again(
    client: TestClient, source_id: str
) -> None:
    # w1..w3 are in s1, w22..w24 in s8 and w25 is in no segment; the segment texts of this document
    # ("репліка m") are not the join of the words, so a recomputed one is easy to tell.
    parent = created(import_json(client, source_id, long_document()))
    child = created(
        correct(
            client,
            parent["id"],
            [{"word_id": "w2", "text": "друге"}, {"word_id": "w25", "text": "зайве"}],
        )
    )
    after = full(client, child["id"])
    assert texts(after, "segments") == [
        "слово1 друге слово3",
        *[f"репліка {m}" for m in range(2, 9)],
    ]
    assert after["words"][24]["text"] == "зайве"


def test_an_srt_import_is_corrected_by_segment_id(client: TestClient, source_id: str) -> None:
    parent = created(import_text(client, source_id, "srt", SRT))
    before = full(client, parent["id"])

    child = created(
        correct(client, parent["id"], [{"segment_id": "s2", "text": "  Не вийшло, але майже!  "}])
    )

    assert (child["origin"], child["parent_id"], child["revision"]) == (
        "correction",
        parent["id"],
        2,
    )
    assert (child["timing_level"], child["word_count"], child["segment_count"]) == ("segment", 0, 2)
    assert child["metadata"] == parent["metadata"] == {"format": "srt", "offset_ms": 0}
    after = full(client, child["id"])
    assert after["words"] == []
    assert without_text(after) == without_text(before)
    assert texts(after, "segments") == ["Спробую перестрибнути.", "Не вийшло, але майже!"]
    assert [(s["start_ms"], s["end_ms"]) for s in after["segments"]] == [(1000, 3000), (5000, 7000)]
    assert texts(full(client, parent["id"]), "segments") == ["Спробую перестрибнути.", "Не вийшло!"]


def test_a_correction_keeps_speaker_confidence_and_chunk(runtime: Runtime) -> None:
    client = client_for(runtime)
    source = seed_source(runtime)
    stt_id, chunk_id = seed_stt_revision(runtime, source)
    before = full(client, stt_id)

    child = created(correct(client, stt_id, [{"word_id": "w2", "text": "World!"}]))

    assert (child["origin"], child["parent_id"], child["run_id"]) == ("correction", stt_id, None)
    assert child["metadata"] == {"model": "m"}
    after = full(client, child["id"])
    assert without_text(after) == without_text(before)
    assert [(w["chunk_id"], w["speaker_local"], w["confidence"]) for w in after["words"]] == [
        (chunk_id, "A", 0.9),
        (chunk_id, "B", 0.25),
        (chunk_id, None, None),
    ]
    assert texts(after, "words") == ["Hello", "World!", "again"]
    assert texts(after, "segments") == ["Hello World!", "again"]


def test_corrections_chain_and_only_the_latest_revision_of_a_source_can_be_corrected(
    client: TestClient, source_id: str
) -> None:
    first = created(import_json(client, source_id, example()))
    second = created(correct(client, first["id"], [{"word_id": "w1", "text": "один"}]))
    third = created(correct(client, second["id"], [{"word_id": "w1", "text": "два"}]))
    assert [first["revision"], second["revision"], third["revision"]] == [1, 2, 3]
    assert (second["parent_id"], third["parent_id"]) == (first["id"], second["id"])
    assert texts(full(client, third["id"]), "words")[0] == "два"
    assert texts(full(client, second["id"]), "words")[0] == "один"

    for stale in (first, second):
        response = correct(client, stale["id"], [{"word_id": "w1", "text": "пізно"}])
        assert response.status_code == 409, response.text
        error = error_of(response)
        assert error["code"] == "TRANSCRIPT_REVISION_STALE"
        assert error["details"] == {"latest_id": third["id"]}
        assert error["message"]
        assert error["retryable"] is False

    # A newer import of any kind makes the corrected one stale too.
    newer = created(import_text(client, source_id, "srt", SRT))
    response = correct(client, third["id"], [{"word_id": "w1", "text": "пізно"}])
    assert response.status_code == 409
    assert error_of(response)["details"] == {"latest_id": newer["id"]}
    assert [item["id"] for item in listed(client, source_id)] == [
        first["id"],
        second["id"],
        third["id"],
        newer["id"],
    ]


def test_staleness_is_per_source(runtime: Runtime, client: TestClient) -> None:
    source, other = seed_source(runtime), seed_source(runtime)
    parent = created(import_json(client, source, example()))
    created(import_json(client, other, example()))  # a newer revision of another source
    created(correct(client, parent["id"], [{"word_id": "w1", "text": "ок"}]))


def test_a_revision_added_while_the_correction_is_prepared_makes_it_stale(
    runtime: Runtime, client: TestClient, source_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = created(import_json(client, source_id, example()))
    original = transcript_service._apply_edits
    interloper: list[str] = []

    def interleaved(*args: Any, **kwargs: Any) -> Any:
        # Runs after the correction has read its parent and before it takes the write lock.
        interloper.append(
            runtime.transcripts.import_text(
                source_id=source_id, format="srt", text=SRT, offset_ms=0
            ).id
        )
        return original(*args, **kwargs)

    monkeypatch.setattr(transcript_service, "_apply_edits", interleaved)
    response = correct(client, parent["id"], [{"word_id": "w1", "text": "нове"}])
    assert response.status_code == 409, response.text
    assert error_of(response)["code"] == "TRANSCRIPT_REVISION_STALE"
    assert error_of(response)["details"] == {"latest_id": interloper[0]}
    assert [item["id"] for item in listed(client, source_id)] == [parent["id"], interloper[0]]


def test_concurrent_corrections_of_one_revision_make_exactly_one(
    runtime: Runtime, source_id: str
) -> None:
    workers = 4
    # One app per thread; see test_concurrent_imports_get_distinct_consecutive_revisions.
    clients = [client_for(runtime) for _ in range(workers)]
    parent = created(import_json(clients[0], source_id, example()))

    def attempt(number: int) -> Response:
        return correct(clients[number], parent["id"], [{"word_id": "w1", "text": f"v{number}"}])

    with ThreadPoolExecutor(max_workers=workers) as pool:
        responses = list(pool.map(attempt, range(workers)))

    assert sorted(r.status_code for r in responses) == [201] + [409] * (workers - 1)
    winner = next(r.json() for r in responses if r.status_code == 201)
    assert winner["revision"] == 2
    for response in responses:
        if response.status_code == 409:
            assert error_of(response)["code"] == "TRANSCRIPT_REVISION_STALE"
            assert error_of(response)["details"] == {"latest_id": winner["id"]}
    assert [item["revision"] for item in listed(clients[0], source_id)] == [1, 2]


def test_a_correction_with_500_edits_is_accepted(client: TestClient, source_id: str) -> None:
    parent = created(import_json(client, source_id, document_of_words(500)))
    edits = [{"word_id": f"w{n}", "text": f"нове{n}"} for n in range(1, 501)]
    child = created(correct(client, parent["id"], edits))
    assert child["word_count"] == 500
    after = full(client, child["id"])
    assert texts(after, "words") == [f"нове{n}" for n in range(1, 501)]
    assert (after["words"][499]["start_ms"], after["words"][499]["end_ms"]) == (4990, 5000)


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"edits": []},
        {"edits": "w1"},
        {"edits": None},
        {"edits": [None]},
        {"edits": [{"word_id": "w1"}]},  # no text
        {"edits": [{"word_id": "w1", "text": 5}]},
        {"edits": [{"word_id": 7, "text": "а"}]},
        {"edits": [{"segment_id": ["s1"], "text": "а"}]},
        {"edits": [{"word_id": "w1", "text": "а", "start_ms": 5}]},  # a time cannot be edited
        {"edits": [{"word_id": "w1", "text": "а", "end_ms": 5}]},
        {"edits": [{"word_id": "w1", "text": "а", "ordinal": 1}]},
        {"edits": [{"word_id": "w1", "text": "а"}], "language": "en"},
        {"edits": [{"word_id": "w1", "text": "а"}] * 501},
    ],
)
def test_a_malformed_correction_request_is_422(
    client: TestClient, source_id: str, body: dict[str, Any]
) -> None:
    parent = created(import_json(client, source_id, example()))
    response = client.post(f"/v1/transcripts/{parent['id']}/corrections", json=body)
    assert response.status_code == 422
    assert error_of(response)["code"] == "VALIDATION_ERROR"
    assert len(listed(client, source_id)) == 1


def test_a_correction_with_several_bad_fields_lists_all_of_them(
    client: TestClient, source_id: str
) -> None:
    parent = created(import_json(client, source_id, example()))
    secret = "секретний-текст"
    body = {
        "edits": [
            {"word_id": 7, "text": 5},
            {"word_id": "w1", "text": secret, "start_ms": 5},
            {"segment_id": ["s1"], "text": "а"},
        ],
        "language": "en",
    }
    response = client.post(f"/v1/transcripts/{parent['id']}/corrections", json=body)

    assert response.status_code == 422, response.text
    error = error_of(response)
    assert error["code"] == "VALIDATION_ERROR"
    assert error["message"]
    assert error["retryable"] is False
    # Every bad field is reported, by location and type only: no value, message or input.
    assert error["details"] == {
        "fields": [
            {"loc": ["body", "edits", 0, "word_id"], "type": "string_type"},
            {"loc": ["body", "edits", 0, "text"], "type": "string_type"},
            {"loc": ["body", "edits", 1, "start_ms"], "type": "extra_forbidden"},
            {"loc": ["body", "edits", 2, "segment_id"], "type": "string_type"},
            {"loc": ["body", "language"], "type": "extra_forbidden"},
        ]
    }
    assert secret not in response.text
    assert len(listed(client, source_id)) == 1


def test_a_correction_body_that_is_not_json_is_422(client: TestClient, source_id: str) -> None:
    parent = created(import_json(client, source_id, example()))
    response = client.post(
        f"/v1/transcripts/{parent['id']}/corrections",
        content=b"w1",
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422
    assert error_of(response)["code"] == "VALIDATION_ERROR"


def test_correcting_an_unknown_transcript_is_404(client: TestClient) -> None:
    response = correct(client, "missing", [{"word_id": "w1", "text": "а"}])
    assert response.status_code == 404
    assert error_of(response)["code"] == "TRANSCRIPT_NOT_FOUND"
    assert error_of(response)["message"] == "Транскрипт не знайдено"


@pytest.mark.parametrize(
    ("edits", "field"),
    [
        ([{"word_id": "w9", "text": "а"}], "edits[0].word_id"),
        ([{"word_id": "w1", "text": "а"}, {"word_id": "w9", "text": "б"}], "edits[1].word_id"),
        ([{"word_id": "s1", "text": "а"}], "edits[0].word_id"),  # a segment id is not a word id
        ([{"word_id": "", "text": "а"}], "edits[0].word_id"),
        ([{"word_id": "w1", "text": "а"}, {"word_id": "w1", "text": "б"}], "edits[1].word_id"),
        (
            [
                {"word_id": "w2", "text": "а"},
                {"word_id": "w3", "text": "б"},
                {"word_id": "w2", "text": "в"},
            ],
            "edits[2].word_id",
        ),
        ([{"word_id": "w1", "segment_id": "s1", "text": "а"}], "edits[0]"),
        ([{"text": "а"}], "edits[0]"),
        ([{"word_id": None, "segment_id": None, "text": "а"}], "edits[0]"),
        ([{"segment_id": "s1", "text": "а"}], "edits[0].segment_id"),
        (
            [{"word_id": "w1", "text": "а"}, {"segment_id": "s2", "text": "б"}],
            "edits[1].segment_id",
        ),
    ],
)
def test_bad_ids_in_a_word_correction_are_422_and_save_nothing(
    client: TestClient, source_id: str, edits: list[dict[str, Any]], field: str
) -> None:
    parent = created(import_json(client, source_id, example()))
    assert_invalid(correct(client, parent["id"], edits), field)
    assert len(listed(client, source_id)) == 1


@pytest.mark.parametrize(
    ("edits", "field"),
    [
        ([{"segment_id": "s9", "text": "а"}], "edits[0].segment_id"),
        (
            [{"segment_id": "s1", "text": "а"}, {"segment_id": "w1", "text": "б"}],
            "edits[1].segment_id",
        ),
        (
            [{"segment_id": "s1", "text": "а"}, {"segment_id": "s1", "text": "б"}],
            "edits[1].segment_id",
        ),
        (
            [{"word_id": "w1", "text": "а"}],
            "edits[0].word_id",
        ),  # a segment-level revision: no words
        (
            [{"segment_id": "s1", "text": "а"}, {"word_id": "w1", "text": "б"}],
            "edits[1].word_id",
        ),
        ([{"word_id": "w1", "segment_id": "s1", "text": "а"}], "edits[0]"),
        ([{"text": "а"}], "edits[0]"),
    ],
)
def test_bad_ids_in_a_segment_correction_are_422_and_save_nothing(
    client: TestClient, source_id: str, edits: list[dict[str, Any]], field: str
) -> None:
    parent = created(import_text(client, source_id, "srt", SRT))
    assert_invalid(correct(client, parent["id"], edits), field)
    assert len(listed(client, source_id)) == 1


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "\t\n ",
        "а\nб",
        "а\r\nб",
        "а\tб",
        "а\x00б",
        "а\x1bб",
        "а\x7fб",
        "а\u0085б",
        "а б",
        "а б",
        "а" * 201,
        "  " + "а" * 201,
    ],
)
def test_a_bad_word_text_is_422_with_its_field(
    client: TestClient, source_id: str, text: str
) -> None:
    parent = created(import_json(client, source_id, example()))
    response = correct(
        client, parent["id"], [{"word_id": "w1", "text": "ок"}, {"word_id": "w2", "text": text}]
    )
    assert_invalid(response, "edits[1].text")
    assert len(listed(client, source_id)) == 1


@pytest.mark.parametrize(
    "text",
    ["", "  \t ", "а\nб", "а\x00б", "а б", "а" * 2001, " " + "а" * 2001],
)
def test_a_bad_segment_text_is_422_with_its_field(
    client: TestClient, source_id: str, text: str
) -> None:
    parent = created(import_text(client, source_id, "srt", SRT))
    response = correct(client, parent["id"], [{"segment_id": "s2", "text": text}])
    assert_invalid(response, "edits[0].text")
    assert len(listed(client, source_id)) == 1


def test_the_text_limits_are_inclusive_and_measured_after_the_trim(
    client: TestClient, source_id: str
) -> None:
    words = created(import_json(client, source_id, example()))
    word = "ї" * 200
    child = created(correct(client, words["id"], [{"word_id": "w1", "text": f"  {word}\t"}]))
    assert texts(full(client, child["id"]), "words")[0] == word

    segments = created(import_text(client, source_id, "srt", SRT))
    segment = "є" * 2000
    child = created(correct(client, segments["id"], [{"segment_id": "s1", "text": f" {segment} "}]))
    assert texts(full(client, child["id"]), "segments")[0] == segment


def test_unusual_but_printable_text_is_accepted_as_it_is(
    client: TestClient, source_id: str
) -> None:
    parent = created(import_json(client, source_id, example()))
    odd = "a b‍👩‍👩 ñ–“&amp;” {\\an8}"
    child = created(correct(client, parent["id"], [{"word_id": "w3", "text": f" {odd} "}]))
    # The text is plain: nothing is decoded or removed; only the outer whitespace is trimmed.
    assert texts(full(client, child["id"]), "words")[2] == odd


def test_a_lone_surrogate_in_a_correction_is_422_not_a_server_error(
    client: TestClient, source_id: str
) -> None:
    parent = created(import_json(client, source_id, example()))
    body = {"edits": [{"word_id": "w1", "text": "ZZ"}]}
    raw = json.dumps(body).replace("ZZ", "\\ud800").encode("ascii")
    response = client.post(
        f"/v1/transcripts/{parent['id']}/corrections",
        content=raw,
        headers={"Content-Type": "application/json"},
    )
    assert_invalid(response, "edits[0].text")
    assert len(listed(client, source_id)) == 1


def test_a_segment_that_grows_past_2000_characters_from_its_words_is_422(
    client: TestClient, source_id: str
) -> None:
    # 11 one-letter words in one segment: the joined text is 11 + 10 spaces long.
    parent = created(import_json(client, source_id, document_of_words(11, segment=True)))
    long_word = "ж" * 200

    # 10 words of 200 letters, one word of 1 and 10 spaces: 2011 characters.
    too_long = [{"word_id": f"w{n}", "text": long_word} for n in range(1, 11)]
    assert_invalid(correct(client, parent["id"], too_long), "edits[0].text")
    assert len(listed(client, source_id)) == 1

    # 9 words of 200 letters, two of 1 and 10 spaces: 1812 characters.
    child = created(correct(client, parent["id"], too_long[:9]))
    segment = full(client, child["id"])["segments"][0]
    assert len(segment["text"]) == 9 * 200 + 2 + 10
    assert segment["text"].startswith(f"{long_word} {long_word}")
    assert (segment["start_ms"], segment["end_ms"]) == (0, 110)


def test_error_messages_do_not_repeat_the_request(client: TestClient, source_id: str) -> None:
    parent = created(import_json(client, source_id, example()))
    for edits in (
        [{"word_id": "секретний-id", "text": "ок"}],
        [{"word_id": "w1", "text": "секрет\x1b"}],
        [{"segment_id": "секретний-id", "text": "ок"}],
    ):
        response = correct(client, parent["id"], edits)
        assert response.status_code == 422
        assert "секрет" not in response.text


def test_correction_routes_need_the_session_token(anonymous: TestClient, source_id: str) -> None:
    response = anonymous.post(
        "/v1/transcripts/x/corrections", json={"edits": [{"word_id": "w1", "text": "а"}]}
    )
    assert response.status_code == 401
