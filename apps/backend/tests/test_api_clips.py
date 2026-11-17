"""POST/GET /v1/clips, GET/PATCH /v1/clips/{id}, POST /v1/clips/{id}/review."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response

from clipforge.services.runtime import Runtime
from clipforge.storage import AnalysisRunRepository, ClipRepository

from .support import client_for
from .test_storage_support import add_project, add_source

DURATION_MS = 12000
CATEGORIES = ["funny", "action", "fails", "unexpected", "game_details"]


def seed_source(runtime: Runtime, *, duration_ms: int = DURATION_MS) -> str:
    """A registered source without probing a file: clips only need its stored duration."""
    with runtime.database.write_session() as session:
        return add_source(session, add_project(session).id, duration_ms=duration_ms).id


def body(source_id: str, **changes: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "source_id": source_id,
        "title": "Тестовий фрагмент",
        "start_ms": 4120,
        "end_ms": 7880,
        "categories": ["fails"],
        "evidence_type": "manual",
    }
    payload.update(changes)
    return payload


def create(client: TestClient, source_id: str, **changes: Any) -> dict[str, Any]:
    response = client.post("/v1/clips", json=body(source_id, **changes))
    assert response.status_code == 201, response.text
    clip: dict[str, Any] = response.json()
    return clip


def error_of(response: Response) -> dict[str, Any]:
    error: dict[str, Any] = response.json()["error"]
    return error


def patch(client: TestClient, clip: dict[str, Any], **changes: Any) -> Response:
    payload = {"expected_revision": clip["revision"], **changes}
    return client.patch(f"/v1/clips/{clip['id']}", json=payload)


def review(client: TestClient, clip: dict[str, Any], status: str) -> Response:
    payload = {"expected_revision": clip["revision"], "status": status}
    return client.post(f"/v1/clips/{clip['id']}/review", json=payload)


@pytest.fixture
def source_id(runtime: Runtime) -> str:
    return seed_source(runtime)


# --- create --------------------------------------------------------------------------------


def test_manual_clip_is_created_as_suggested_revision_1(client: TestClient, source_id: str) -> None:
    clip = create(client, source_id, title="  Мій момент  ")
    assert clip["revision"] == 1
    assert clip["review_status"] == "suggested"
    assert clip["evidence_type"] == "manual"
    assert clip["evidence_ids"] == []
    assert clip["title"] == "Мій момент"
    assert (clip["start_ms"], clip["end_ms"]) == (4120, 7880)
    assert clip["categories"] == ["fails"]
    assert clip["source_id"] == source_id
    assert clip["visual_review_required"] is False
    assert clip["export_state"] == "none"
    assert clip["analysis_run_id"] is None
    assert clip["recommended_start_ms"] is None
    assert clip["created_at"] == clip["updated_at"]
    assert client.get(f"/v1/clips/{clip['id']}").json() == clip


@pytest.mark.parametrize(
    ("start_ms", "end_ms", "reason"),
    [
        (-1, 3000, "INTERVAL_NEGATIVE"),
        (4000, 4000, "INTERVAL_END_NOT_AFTER_START"),
        (5000, 4000, "INTERVAL_END_NOT_AFTER_START"),
        (0, DURATION_MS + 1, "INTERVAL_END_AFTER_DURATION"),
        (DURATION_MS, DURATION_MS + 1, "INTERVAL_END_AFTER_DURATION"),
        (0, -5, "INTERVAL_NEGATIVE"),
    ],
)
def test_invalid_interval_is_422(
    client: TestClient, source_id: str, start_ms: int, end_ms: int, reason: str
) -> None:
    response = client.post(
        "/v1/clips", json=body(source_id, start_ms=start_ms, end_ms=end_ms, categories=[])
    )
    assert response.status_code == 422
    error = error_of(response)
    assert error["code"] == "INVALID_INTERVAL"
    assert error["details"]["reason"] == reason
    assert error["retryable"] is False
    assert client.get("/v1/clips").json()["items"] == []


@pytest.mark.parametrize(
    ("start_ms", "end_ms"),
    [(9000, DURATION_MS), (0, DURATION_MS), (0, 1), (DURATION_MS - 1, DURATION_MS)],
)
def test_interval_up_to_the_exact_source_end_is_accepted(
    client: TestClient, source_id: str, start_ms: int, end_ms: int
) -> None:
    clip = create(client, source_id, start_ms=start_ms, end_ms=end_ms)
    assert (clip["start_ms"], clip["end_ms"]) == (start_ms, end_ms)


@pytest.mark.parametrize(
    "bad",
    [1.5, 3000.0, "3000", True, None, 2**70],
    ids=["float", "int-float", "str", "bool", "null", "huge"],
)
def test_interval_must_be_integer_milliseconds(
    client: TestClient, source_id: str, bad: object
) -> None:
    response = client.post("/v1/clips", json=body(source_id, end_ms=bad))
    assert response.status_code == 422
    assert error_of(response)["code"] in ("VALIDATION_ERROR", "INVALID_INTERVAL")


@pytest.mark.parametrize("length", [1, 2, 160])
def test_title_of_1_to_160_characters_is_accepted(
    client: TestClient, source_id: str, length: int
) -> None:
    title = "я" * length
    assert create(client, source_id, title=f"  {title}  ")["title"] == title


@pytest.mark.parametrize("title", ["", "   ", "\t\n", "я" * 161, " " + "a" * 161])
def test_title_outside_1_to_160_is_422(client: TestClient, source_id: str, title: str) -> None:
    response = client.post("/v1/clips", json=body(source_id, title=title))
    assert response.status_code == 422
    error = error_of(response)
    assert error["code"] == "INVALID_CLIP_TITLE"
    assert error["message"] == "Назва кліпу має містити від 1 до 160 символів"
    assert error["details"] == {"field": "title"}


def test_every_category_from_the_enum_is_accepted(client: TestClient, source_id: str) -> None:
    clip = create(client, source_id, categories=CATEGORIES)
    assert clip["categories"] == CATEGORIES


def test_empty_category_list_is_allowed(client: TestClient, source_id: str) -> None:
    assert create(client, source_id, categories=[])["categories"] == []


@pytest.mark.parametrize(
    ("categories", "reason"),
    [
        (["boring"], "CATEGORY_UNKNOWN"),
        (["fails", "Fails"], "CATEGORY_UNKNOWN"),
        (["fails", "fails"], "CATEGORY_DUPLICATE"),
        ([""], "CATEGORY_UNKNOWN"),
    ],
)
def test_unknown_or_repeated_category_is_422(
    client: TestClient, source_id: str, categories: list[str], reason: str
) -> None:
    response = client.post("/v1/clips", json=body(source_id, categories=categories))
    assert response.status_code == 422
    error = error_of(response)
    assert error["code"] == "INVALID_CATEGORIES"
    assert error["details"] == {"field": "categories", "reason": reason}


def test_categories_must_be_a_list_of_strings(client: TestClient, source_id: str) -> None:
    for bad in ("fails", [1], None, {"fails": True}):
        response = client.post("/v1/clips", json=body(source_id, categories=bad))
        assert response.status_code == 422
        assert error_of(response)["code"] == "VALIDATION_ERROR"


@pytest.mark.parametrize("evidence", ["transcript", "visual", "", None])
def test_only_manual_evidence_can_be_posted(
    client: TestClient, source_id: str, evidence: object
) -> None:
    response = client.post("/v1/clips", json=body(source_id, evidence_type=evidence))
    assert response.status_code == 422
    assert error_of(response)["code"] == "VALIDATION_ERROR"


def test_unknown_fields_and_missing_fields_are_422(client: TestClient, source_id: str) -> None:
    for extra in ({"revision": 5}, {"review_status": "approved"}, {"export_state": "none"}):
        assert client.post("/v1/clips", json=body(source_id, **extra)).status_code == 422
    for missing in ("source_id", "title", "start_ms", "end_ms", "categories", "evidence_type"):
        payload = body(source_id)
        del payload[missing]
        assert client.post("/v1/clips", json=payload).status_code == 422, missing


def test_unknown_source_is_404(client: TestClient) -> None:
    response = client.post("/v1/clips", json=body("no-such-source"))
    assert response.status_code == 404
    assert error_of(response)["code"] == "SOURCE_NOT_FOUND"


def test_validation_errors_do_not_echo_the_title(client: TestClient, source_id: str) -> None:
    secret = "sk-or-v1-" + "q" * 200
    response = client.post("/v1/clips", json=body(source_id, title=secret))
    assert response.status_code == 422
    assert secret not in response.text


def test_get_unknown_clip_is_404(client: TestClient) -> None:
    response = client.get("/v1/clips/nope")
    assert response.status_code == 404
    assert error_of(response)["code"] == "CLIP_NOT_FOUND"


# --- list ----------------------------------------------------------------------------------


def test_list_filters_by_source_review_status_and_category(
    client: TestClient, runtime: Runtime
) -> None:
    first, second = seed_source(runtime), seed_source(runtime)
    a = create(client, first, title="a", categories=["fails"])
    b = create(client, first, title="b", categories=["funny", "fails"])
    c = create(client, first, title="c", categories=[])
    d = create(client, second, title="d", categories=["fails"])
    approved = review(client, b, "approved").json()
    rejected = review(client, d, "rejected").json()

    def ids(**params: str) -> list[str]:
        response = client.get("/v1/clips", params=params)
        assert response.status_code == 200
        return [item["id"] for item in response.json()["items"]]

    # Same-tick clips are ordered by id, so compare as sets.
    assert set(ids()) == {a["id"], b["id"], c["id"], d["id"]}
    assert set(ids(source_id=first)) == {a["id"], b["id"], c["id"]}
    assert ids(source_id=second) == [d["id"]]
    assert ids(source_id="no-such-source") == []
    assert set(ids(review_status="suggested")) == {a["id"], c["id"]}
    assert ids(review_status="approved") == [approved["id"]]
    assert ids(review_status="rejected") == [rejected["id"]]
    assert set(ids(category="fails")) == {a["id"], b["id"], d["id"]}
    assert ids(category="funny") == [b["id"]]
    assert ids(category="action") == []
    assert ids(source_id=first, category="fails", review_status="suggested") == [a["id"]]
    assert ids(source_id=second, review_status="approved") == []


def test_list_cursor_works_together_with_filters(client: TestClient, runtime: Runtime) -> None:
    source = seed_source(runtime)
    wanted = [create(client, source, title=f"w{n}", categories=["action"])["id"] for n in range(5)]
    for n in range(3):
        create(client, source, title=f"o{n}", categories=["funny"])

    filters = {"source_id": source, "category": "action"}
    reference = [
        item["id"]
        for item in client.get("/v1/clips", params={**filters, "limit": "200"}).json()["items"]
    ]
    assert set(reference) == set(wanted)

    seen: list[str] = []
    cursor: str | None = None
    pages = 0
    while True:
        params = {**filters, "limit": "2"}
        if cursor:
            params["cursor"] = cursor
        page = client.get("/v1/clips", params=params).json()
        assert len(page["items"]) <= 2
        seen.extend(item["id"] for item in page["items"])
        pages += 1
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert seen == reference
    assert pages == 3


@pytest.mark.parametrize(
    ("params", "code"),
    [
        ({"review_status": "done"}, "INVALID_REVIEW_STATUS"),
        ({"category": "boring"}, "INVALID_CATEGORIES"),
        ({"cursor": "no-such-clip"}, "INVALID_CURSOR"),
    ],
)
def test_list_rejects_unknown_filter_values(
    client: TestClient, source_id: str, params: dict[str, str], code: str
) -> None:
    create(client, source_id)
    response = client.get("/v1/clips", params=params)
    assert response.status_code == 422
    assert error_of(response)["code"] == code


@pytest.mark.parametrize("limit", ["0", "201", "x"])
def test_list_limit_is_bounded(client: TestClient, limit: str) -> None:
    assert client.get("/v1/clips", params={"limit": limit}).status_code == 422


# --- PATCH ---------------------------------------------------------------------------------


def test_patch_updates_fields_and_bumps_the_revision(client: TestClient, source_id: str) -> None:
    clip = create(client, source_id)
    response = patch(
        client, clip, title="Оновлено", start_ms=1000, end_ms=5000, categories=["action", "funny"]
    )
    assert response.status_code == 200
    updated = response.json()
    assert updated["revision"] == 2
    assert updated["title"] == "Оновлено"
    assert (updated["start_ms"], updated["end_ms"]) == (1000, 5000)
    assert updated["categories"] == ["action", "funny"]
    assert updated["review_status"] == "suggested"
    assert updated["updated_at"] > clip["updated_at"]
    assert updated["created_at"] == clip["created_at"]
    assert client.get(f"/v1/clips/{clip['id']}").json() == updated


def test_patch_with_one_bound_is_checked_against_the_other(
    client: TestClient, source_id: str
) -> None:
    clip = create(client, source_id, start_ms=4000, end_ms=8000)
    assert patch(client, clip, end_ms=4000).status_code == 422
    assert patch(client, clip, start_ms=8000).status_code == 422
    assert patch(client, clip, end_ms=DURATION_MS + 1).status_code == 422
    assert patch(client, clip, start_ms=-1).status_code == 422
    stored = client.get(f"/v1/clips/{clip['id']}").json()
    assert stored == clip  # a rejected edit changes nothing, not even the revision

    response = patch(client, clip, start_ms=7999)
    assert response.status_code == 200
    assert (response.json()["start_ms"], response.json()["end_ms"]) == (7999, 8000)
    response = patch(client, response.json(), end_ms=DURATION_MS)
    assert response.json()["end_ms"] == DURATION_MS


def test_patch_can_clear_categories_and_null_means_unchanged(
    client: TestClient, source_id: str
) -> None:
    clip = create(client, source_id, categories=["fails", "action"])
    response = patch(client, clip, categories=[], title=None, start_ms=None, end_ms=5000)
    assert response.status_code == 200
    updated = response.json()
    assert updated["categories"] == []
    assert updated["title"] == clip["title"]
    assert updated["start_ms"] == clip["start_ms"]
    assert updated["end_ms"] == 5000


def test_patch_validates_title_and_categories(client: TestClient, source_id: str) -> None:
    clip = create(client, source_id)
    response = patch(client, clip, title="  ")
    assert response.status_code == 422
    assert error_of(response)["code"] == "INVALID_CLIP_TITLE"
    response = patch(client, clip, categories=["boring"])
    assert response.status_code == 422
    assert error_of(response)["code"] == "INVALID_CATEGORIES"
    assert client.get(f"/v1/clips/{clip['id']}").json()["revision"] == 1


def test_patch_without_editable_fields_is_422(client: TestClient, source_id: str) -> None:
    clip = create(client, source_id)
    for payload in ({"expected_revision": 1}, {"expected_revision": 1, "title": None}):
        response = client.patch(f"/v1/clips/{clip['id']}", json=payload)
        assert response.status_code == 422
        assert error_of(response)["code"] == "NO_CHANGES"


@pytest.mark.parametrize(
    "payload",
    [
        {"title": "x"},
        {"expected_revision": 0, "title": "x"},
        {"expected_revision": "1", "title": "x"},
        {"expected_revision": 1, "title": "x", "review_status": "approved"},
        {"expected_revision": 1, "title": "x", "revision": 9},
        {"expected_revision": 1, "title": "x", "export_state": "verified"},
        {"expected_revision": 1, "title": "x", "evidence_type": "transcript"},
        {"expected_revision": 1, "start_ms": 1.5},
    ],
)
def test_patch_rejects_unknown_and_non_editable_fields(
    client: TestClient, source_id: str, payload: dict[str, Any]
) -> None:
    clip = create(client, source_id)
    response = client.patch(f"/v1/clips/{clip['id']}", json=payload)
    assert response.status_code == 422
    assert error_of(response)["code"] == "VALIDATION_ERROR"
    assert client.get(f"/v1/clips/{clip['id']}").json() == clip


def test_patch_unknown_clip_is_404(client: TestClient) -> None:
    response = client.patch("/v1/clips/nope", json={"expected_revision": 1, "title": "x"})
    assert response.status_code == 404
    assert error_of(response)["code"] == "CLIP_NOT_FOUND"


def test_stale_revision_is_409_and_the_winners_data_is_kept(
    client: TestClient, source_id: str
) -> None:
    clip = create(client, source_id)
    winner = patch(client, clip, title="Переможець", end_ms=6000)
    assert winner.status_code == 200

    loser = patch(client, clip, title="Застаріле", start_ms=100)
    assert loser.status_code == 409
    error = error_of(loser)
    assert error["code"] == "STALE_REVISION"
    assert error["details"] == {"current_revision": 2}
    assert error["retryable"] is False

    stored = client.get(f"/v1/clips/{clip['id']}").json()
    assert stored == winner.json()
    assert stored["title"] == "Переможець"
    assert stored["start_ms"] == clip["start_ms"]
    assert stored["revision"] == 2

    # The caller can retry with the revision from the error.
    retry = patch(client, stored, title="Після оновлення")
    assert retry.status_code == 200
    assert retry.json()["revision"] == 3


def test_stale_revision_is_checked_before_the_values(client: TestClient, source_id: str) -> None:
    clip = create(client, source_id)
    patch(client, clip, title="Новий")
    response = patch(client, clip, title="  ")
    assert response.status_code == 409


def test_concurrent_patches_with_the_same_revision_have_exactly_one_winner(
    runtime: Runtime, source_id: str
) -> None:
    client = client_for(runtime)
    clip = create(client, source_id)
    workers = 6

    # Build the apps up front: FastAPI's warnings.catch_warnings() is not thread-safe.
    clients = [client_for(runtime) for _ in range(workers)]

    def attempt(number: int) -> Response:
        return patch(clients[number], clip, title=f"Автор {number}")

    with ThreadPoolExecutor(max_workers=workers) as pool:
        responses = list(pool.map(attempt, range(workers)))

    statuses = sorted(response.status_code for response in responses)
    assert statuses == [200] + [409] * (workers - 1)
    winner = next(response for response in responses if response.status_code == 200).json()
    stored = client.get(f"/v1/clips/{clip['id']}").json()
    assert stored == winner
    assert stored["revision"] == 2


# --- review --------------------------------------------------------------------------------


def test_review_changes_status_and_bumps_the_revision(client: TestClient, source_id: str) -> None:
    clip = create(client, source_id)
    approved = review(client, clip, "approved")
    assert approved.status_code == 200
    assert approved.json()["review_status"] == "approved"
    assert approved.json()["revision"] == 2
    assert (approved.json()["start_ms"], approved.json()["title"]) == (
        clip["start_ms"],
        clip["title"],
    )

    rejected = review(client, approved.json(), "rejected")
    assert (rejected.json()["review_status"], rejected.json()["revision"]) == ("rejected", 3)
    back = review(client, rejected.json(), "suggested")
    assert (back.json()["review_status"], back.json()["revision"]) == ("suggested", 4)
    assert client.get(f"/v1/clips/{clip['id']}").json() == back.json()


def test_review_with_a_stale_revision_is_409(client: TestClient, source_id: str) -> None:
    clip = create(client, source_id)
    assert review(client, clip, "approved").status_code == 200
    response = review(client, clip, "rejected")
    assert response.status_code == 409
    assert error_of(response)["code"] == "STALE_REVISION"
    assert client.get(f"/v1/clips/{clip['id']}").json()["review_status"] == "approved"


def test_review_invalidates_a_pending_edit(client: TestClient, source_id: str) -> None:
    clip = create(client, source_id)
    review(client, clip, "approved")
    assert patch(client, clip, title="Застаріла правка").status_code == 409


@pytest.mark.parametrize(
    "payload",
    [
        {"expected_revision": 1, "status": "maybe"},
        {"expected_revision": 1, "status": "verified"},
        {"expected_revision": 1},
        {"status": "approved"},
        {"expected_revision": 1, "status": "approved", "title": "x"},
    ],
)
def test_review_rejects_bad_bodies(
    client: TestClient, source_id: str, payload: dict[str, Any]
) -> None:
    clip = create(client, source_id)
    response = client.post(f"/v1/clips/{clip['id']}/review", json=payload)
    assert response.status_code == 422
    assert client.get(f"/v1/clips/{clip['id']}").json() == clip


def test_review_unknown_clip_is_404(client: TestClient) -> None:
    response = client.post(
        "/v1/clips/nope/review", json={"expected_revision": 1, "status": "approved"}
    )
    assert response.status_code == 404
    assert error_of(response)["code"] == "CLIP_NOT_FOUND"


# --- export state --------------------------------------------------------------------------


def set_export_state(runtime: Runtime, clip_id: str, state: str) -> None:
    with runtime.database.write_session() as session:
        ClipRepository(session).set_export_state(clip_id, state)


def test_changing_bounds_of_a_verified_export_marks_it_stale(
    client: TestClient, runtime: Runtime, source_id: str
) -> None:
    clip = create(client, source_id)
    set_export_state(runtime, clip["id"], "verified")
    assert client.get(f"/v1/clips/{clip['id']}").json()["export_state"] == "verified"

    response = patch(client, clip, end_ms=7000)
    assert response.status_code == 200
    updated = response.json()
    assert updated["export_state"] == "stale"
    assert updated["end_ms"] == 7000
    assert updated["revision"] == 2  # one edit, one revision
    assert client.get(f"/v1/clips/{clip['id']}").json()["export_state"] == "stale"


def test_each_bound_marks_a_verified_export_stale(
    client: TestClient, runtime: Runtime, source_id: str
) -> None:
    for field, value in (("start_ms", 4121), ("end_ms", 7879)):
        clip = create(client, source_id)
        set_export_state(runtime, clip["id"], "verified")
        assert patch(client, clip, **{field: value}).json()["export_state"] == "stale"


def test_edits_that_keep_the_bounds_leave_a_verified_export_alone(
    client: TestClient, runtime: Runtime, source_id: str
) -> None:
    clip = create(client, source_id)
    set_export_state(runtime, clip["id"], "verified")

    renamed = patch(client, clip, title="Нова назва", categories=["funny"])
    assert renamed.json()["export_state"] == "verified"
    same_bounds = patch(client, renamed.json(), start_ms=clip["start_ms"], end_ms=clip["end_ms"])
    assert same_bounds.status_code == 200
    assert same_bounds.json()["export_state"] == "verified"
    reviewed = review(client, same_bounds.json(), "approved")
    assert reviewed.json()["export_state"] == "verified"


@pytest.mark.parametrize("state", ["none", "queued", "running", "failed", "cancelled", "stale"])
def test_only_a_verified_export_becomes_stale(
    client: TestClient, runtime: Runtime, source_id: str, state: str
) -> None:
    clip = create(client, source_id)
    set_export_state(runtime, clip["id"], state)
    assert patch(client, clip, end_ms=9000).json()["export_state"] == state


def test_rejected_bounds_do_not_touch_the_export_state(
    client: TestClient, runtime: Runtime, source_id: str
) -> None:
    clip = create(client, source_id)
    set_export_state(runtime, clip["id"], "verified")
    assert patch(client, clip, end_ms=DURATION_MS + 1).status_code == 422
    assert client.get(f"/v1/clips/{clip['id']}").json()["export_state"] == "verified"


# --- analysis_stale (ADR 041 §11) ----------------------------------------------------------------

PRICE = {"prompt_usd_per_mtok": "1", "completion_usd_per_mtok": "2", "max_tokens": 4096}


def import_transcript(client: TestClient, source_id: str) -> dict[str, Any]:
    document = {
        "schema_version": 1,
        "timing_level": "word",
        "language": "en",
        "duration_ms": DURATION_MS,
        "words": [
            {"id": "w1", "ordinal": 0, "start_ms": 4000, "end_ms": 4500, "text": "hello"},
            {"id": "w2", "ordinal": 1, "start_ms": 4500, "end_ms": 5000, "text": "world"},
        ],
        "segments": [
            {"id": "s1", "ordinal": 0, "start_ms": 4000, "end_ms": 5000, "text": "hello world"}
        ],
        "coverage": [{"start_ms": 0, "end_ms": DURATION_MS, "state": "succeeded"}],
    }
    response = client.post(
        "/v1/transcripts/import",
        json={"source_id": source_id, "format": "normalized_json", "document": document},
    )
    assert response.status_code == 201, response.text
    transcript: dict[str, Any] = response.json()
    return transcript


def seed_analysis_clip(runtime: Runtime, source_id: str, transcript_id: str, revision: int) -> str:
    """A clip that a completed analysis run read ``transcript_id`` at ``revision`` suggested. No
    route creates such a clip before the run endpoints of M4, so the rows are written directly."""
    with runtime.database.write_session() as session:
        run = AnalysisRunRepository(session).add_run(
            source_id=source_id,
            transcript_id=transcript_id,
            transcript_revision=revision,
            profile_json={"categories": ["funny"]},
            model="test/selector",
            provider="openrouter",
            prompt_version="sha256:0000000000000000",
            schema_version=1,
            unit_level="word",
            price_snapshot_json=PRICE,
            status="completed",
        )
        clip = ClipRepository(session).add(
            source_id=source_id,
            title="Із аналізу",
            start_ms=3_000,
            end_ms=6_000,
            categories=["funny"],
            evidence_type="transcript",
            evidence_ids=["w1"],
            analysis_run_id=run.id,
            evidence_start_ms=4_000,
            evidence_end_ms=5_000,
        )
        return clip.id


def test_a_manual_clip_is_not_analysis_stale(client: TestClient, source_id: str) -> None:
    clip = create(client, source_id)
    assert clip["analysis_stale"] is False
    assert client.get(f"/v1/clips/{clip['id']}").json()["analysis_stale"] is False


def test_a_clip_of_a_run_on_the_newest_revision_is_not_stale(
    client: TestClient, runtime: Runtime, source_id: str
) -> None:
    transcript = import_transcript(client, source_id)
    clip_id = seed_analysis_clip(runtime, source_id, transcript["id"], transcript["revision"])
    stored = client.get(f"/v1/clips/{clip_id}").json()
    assert stored["analysis_run_id"] is not None
    assert stored["analysis_stale"] is False


def test_a_correction_makes_the_run_clip_stale_and_moves_no_bounds(
    client: TestClient, runtime: Runtime, source_id: str
) -> None:
    transcript = import_transcript(client, source_id)
    clip_id = seed_analysis_clip(runtime, source_id, transcript["id"], transcript["revision"])
    manual = create(client, source_id)
    before = client.get(f"/v1/clips/{clip_id}").json()
    assert before["analysis_stale"] is False

    correction = client.post(
        f"/v1/transcripts/{transcript['id']}/corrections",
        json={"edits": [{"word_id": "w1", "text": "hi"}]},
    )
    assert correction.status_code == 201, correction.text
    assert correction.json()["revision"] == 2

    after = client.get(f"/v1/clips/{clip_id}").json()
    assert after["analysis_stale"] is True
    assert after["start_ms"] == before["start_ms"] == 3_000
    assert after["end_ms"] == before["end_ms"] == 6_000
    assert after["revision"] == before["revision"]

    # The flag is per clip, and the page of one source shows both clips with their own flags.
    page = client.get("/v1/clips", params={"source_id": source_id}).json()["items"]
    assert {item["id"]: item["analysis_stale"] for item in page} == {
        clip_id: True,
        manual["id"]: False,
    }
