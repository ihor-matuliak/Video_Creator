"""POST /v1/projects, GET /v1/projects, PATCH /v1/projects/{id} (FR-001)."""

from __future__ import annotations

import re
from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient

from clipforge.services.runtime import Runtime
from clipforge.storage import ClipRepository, ExportRepository, ProjectRepository

from .test_storage_support import add_clip, add_source, counting_clock

STAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z")


def create(client: TestClient, name: object) -> tuple[int, dict[str, object]]:
    response = client.post("/v1/projects", json={"name": name})
    body: dict[str, object] = response.json()
    return response.status_code, body


def patch_project(
    client: TestClient, project_id: object, payload: object = None
) -> tuple[int, dict[str, object]]:
    """PATCH with ``payload`` as the JSON body; ``None`` sends no body at all."""
    response = client.patch(f"/v1/projects/{project_id}", json=payload)
    body: dict[str, object] = response.json()
    return response.status_code, body


def error_of(body: dict[str, object]) -> dict[str, object]:
    error = body["error"]
    assert isinstance(error, dict)
    return error


def listed_ids(client: TestClient, **params: str) -> list[object]:
    return [item["id"] for item in client.get("/v1/projects", params=params).json()["items"]]


def listed_names(client: TestClient, **params: str) -> list[object]:
    return [item["name"] for item in client.get("/v1/projects", params=params).json()["items"]]


def listed_item(client: TestClient, project_id: object) -> dict[str, object]:
    items = client.get("/v1/projects", params={"include_archived": "true"}).json()["items"]
    (item,) = (item for item in items if item["id"] == project_id)
    result: dict[str, object] = item
    return result


def at(stamp: str) -> Callable[[], str]:
    """A clock stuck at ``<stamp>``, a full ISO timestamp."""
    return lambda: stamp


def seed(runtime: Runtime, *names: str) -> list[str]:
    """Projects with strictly increasing creation times: the list order is the argument order
    (POST uses the real clock, whose ticks can tie)."""
    with runtime.database.write_session() as session:
        repo = ProjectRepository(session, now=counting_clock())
        return [repo.add(name).id for name in names]


def test_create_returns_201_with_id_and_trimmed_name(client: TestClient) -> None:
    response = client.post("/v1/projects", json={"name": "  Мій перший проєкт \t"})
    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "Мій перший проєкт"
    assert isinstance(body["id"], str)
    assert body["archived_at"] is None
    assert body["created_at"] == body["updated_at"]


@pytest.mark.parametrize(
    "name",
    ["x", "я", "  x  ", "a" * 120, "я" * 120, "  " + "a" * 120 + "  ", "Проєкт \U0001f600"],
)
def test_names_with_1_to_120_characters_after_trim_are_accepted(
    client: TestClient, name: str
) -> None:
    status, body = create(client, name)
    assert status == 201
    assert body["name"] == name.strip()
    assert 1 <= len(str(body["name"])) <= 120


@pytest.mark.parametrize("name", ["", " ", "   \t\n ", "a" * 121, "я" * 121, " " + "a" * 121])
def test_names_outside_1_to_120_after_trim_are_422(client: TestClient, name: str) -> None:
    status, body = create(client, name)
    assert status == 422
    error = body["error"]
    assert isinstance(error, dict)
    assert error["code"] == "INVALID_PROJECT_NAME"
    assert error["retryable"] is False
    assert error["details"] == {"field": "name"}
    assert error["message"] == "Назва проєкту має містити від 1 до 120 символів"


def test_rejected_name_is_not_stored_or_echoed(client: TestClient) -> None:
    secret = "sk-or-v1-" + "z" * 120
    response = client.post("/v1/projects", json={"name": secret})
    assert response.status_code == 422
    assert secret not in response.text
    assert client.get("/v1/projects").json() == {"items": [], "next_cursor": None}


def test_name_with_nul_or_lone_surrogate_is_422(client: TestClient) -> None:
    for raw in (b'{"name":"a\\u0000b"}', b'{"name":"\\ud800"}'):
        response = client.post(
            "/v1/projects", content=raw, headers={"Content-Type": "application/json"}
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "INVALID_PROJECT_NAME"


@pytest.mark.parametrize("payload", [{}, {"name": 5}, {"name": None}, {"name": "ok", "id": "x"}])
def test_malformed_body_is_422_validation_error(client: TestClient, payload: object) -> None:
    response = client.post("/v1/projects", json=payload)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_empty_list(client: TestClient) -> None:
    response = client.get("/v1/projects")
    assert response.status_code == 200
    assert response.json() == {"items": [], "next_cursor": None}


def test_cursor_walks_every_project_once_in_list_order(client: TestClient) -> None:
    created = {create(client, f"Проєкт {n}")[1]["id"] for n in range(5)}
    # The clock is coarse, so projects created in one tick are ordered by id: the unpaged list
    # is the reference order.
    everything = client.get("/v1/projects", params={"limit": "200"}).json()["items"]
    keys = [(item["created_at"], item["id"]) for item in everything]
    assert keys == sorted(keys)

    seen: list[object] = []
    cursor: str | None = None
    pages = 0
    while True:
        params = {"limit": "2"} | ({"cursor": cursor} if cursor else {})
        body = client.get("/v1/projects", params=params).json()
        assert len(body["items"]) <= 2
        seen.extend(item["id"] for item in body["items"])
        pages += 1
        cursor = body["next_cursor"]
        if cursor is None:
            break
    assert seen == [item["id"] for item in everything]
    assert set(seen) == created
    assert pages == 3


def test_last_full_page_has_no_cursor(client: TestClient) -> None:
    for n in range(4):
        create(client, f"P{n}")
    body = client.get("/v1/projects", params={"limit": "4"}).json()
    assert len(body["items"]) == 4
    assert body["next_cursor"] is None


def test_default_page_is_50(client: TestClient) -> None:
    for n in range(51):
        create(client, f"P{n}")
    body = client.get("/v1/projects").json()
    assert len(body["items"]) == 50
    assert body["next_cursor"] == body["items"][-1]["id"]


@pytest.mark.parametrize("limit", ["0", "-1", "201", "abc"])
def test_bad_limit_is_422(client: TestClient, limit: str) -> None:
    response = client.get("/v1/projects", params={"limit": limit})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_unknown_cursor_is_422(client: TestClient) -> None:
    create(client, "A")
    response = client.get("/v1/projects", params={"cursor": "no-such-project"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_CURSOR"
    assert "no-such-project" not in response.text


def test_archived_projects_are_not_listed(client: TestClient, runtime: Runtime) -> None:
    keep = create(client, "Лишається")[1]["id"]
    gone = create(client, "Архівний")[1]["id"]
    with runtime.database.write_session() as session:
        ProjectRepository(session).archive(str(gone))
    assert [p["id"] for p in client.get("/v1/projects").json()["items"]] == [keep]


def test_include_archived_must_be_a_boolean(client: TestClient) -> None:
    response = client.get("/v1/projects", params={"include_archived": "maybe"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_rename_trims_the_name_and_keeps_the_identity(client: TestClient) -> None:
    created = create(client, "Старий")[1]
    status, body = patch_project(client, created["id"], {"name": "  Нова назва \t"})
    assert status == 200
    assert body["id"] == created["id"]
    assert body["name"] == "Нова назва"
    assert body["created_at"] == created["created_at"]
    assert str(body["updated_at"]) >= str(created["updated_at"])
    assert body["archived"] is False
    assert body["archived_at"] is None
    assert listed_ids(client) == [created["id"]]


@pytest.mark.parametrize(
    "name", ["x", "я", "a" * 120, "  " + "a" * 120 + "  ", "Проєкт \U0001f600"]
)
def test_rename_accepts_1_to_120_characters_after_trim(client: TestClient, name: str) -> None:
    project_id = create(client, "Початковий")[1]["id"]
    status, body = patch_project(client, project_id, {"name": name})
    assert status == 200
    assert body["name"] == name.strip()


def test_patch_response_is_the_post_shape_plus_archived(client: TestClient) -> None:
    created = create(client, "A")[1]
    _, body = patch_project(client, created["id"], {"name": "B"})
    assert set(body) == set(created) | {"archived"}


@pytest.mark.parametrize("name", ["", " ", "\t\n ", "a" * 121, "я" * 121, " " + "a" * 121])
def test_rename_to_an_invalid_name_is_422_and_changes_nothing(
    client: TestClient, name: str
) -> None:
    created = create(client, "Початковий")[1]
    status, body = patch_project(client, created["id"], {"name": name})
    assert status == 422
    error = error_of(body)
    assert error["code"] == "INVALID_PROJECT_NAME"
    assert error["retryable"] is False
    assert error["details"] == {"field": "name"}
    assert error["message"] == "Назва проєкту має містити від 1 до 120 символів"
    assert client.get("/v1/projects").json()["items"][0]["name"] == "Початковий"


def test_rename_with_nul_or_lone_surrogate_is_422(client: TestClient) -> None:
    project_id = create(client, "Початковий")[1]["id"]
    for raw in (b'{"name":"a\\u0000b"}', b'{"name":"\\ud800"}'):
        response = client.patch(
            f"/v1/projects/{project_id}",
            content=raw,
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "INVALID_PROJECT_NAME"
    assert client.get("/v1/projects").json()["items"][0]["name"] == "Початковий"


def test_rejected_rename_is_not_echoed(client: TestClient) -> None:
    project_id = create(client, "Початковий")[1]["id"]
    secret = "sk-or-v1-" + "z" * 120
    response = client.patch(f"/v1/projects/{project_id}", json={"name": secret})
    assert response.status_code == 422
    assert secret not in response.text
    assert client.get("/v1/projects").json()["items"][0]["name"] == "Початковий"


@pytest.mark.parametrize(
    "payload",
    [{"name": 5}, {"archived": "yes"}, {"archived": 1}, {"name": "ok", "id": "x"}],
)
def test_malformed_patch_body_is_422_validation_error(client: TestClient, payload: object) -> None:
    project_id = create(client, "A")[1]["id"]
    status, body = patch_project(client, project_id, payload)
    assert status == 422
    assert error_of(body)["code"] == "VALIDATION_ERROR"


def test_archive_hides_the_project_from_the_default_list(client: TestClient) -> None:
    keep = create(client, "Лишається")[1]["id"]
    gone = create(client, "Архівний")[1]["id"]
    status, body = patch_project(client, gone, {"archived": True})
    assert status == 200
    assert body["archived"] is True
    assert isinstance(body["archived_at"], str)
    assert body["name"] == "Архівний"
    assert listed_ids(client) == [keep]
    assert listed_ids(client, include_archived="false") == [keep]
    assert set(listed_ids(client, include_archived="true")) == {keep, gone}


def test_include_archived_shows_the_archive_timestamp(client: TestClient) -> None:
    gone = create(client, "Архівний")[1]["id"]
    _, archived = patch_project(client, gone, {"archived": True})
    shown = client.get("/v1/projects", params={"include_archived": "true"}).json()["items"]
    assert shown[0]["id"] == gone
    assert shown[0]["archived_at"] == archived["archived_at"]


def test_unarchive_brings_the_project_back(client: TestClient) -> None:
    project_id = create(client, "Повернеться")[1]["id"]
    patch_project(client, project_id, {"archived": True})
    status, body = patch_project(client, project_id, {"archived": False})
    assert status == 200
    assert body["archived"] is False
    assert body["archived_at"] is None
    assert listed_ids(client) == [project_id]


def test_rename_and_archive_in_one_request(client: TestClient) -> None:
    project_id = create(client, "Старе")[1]["id"]
    status, body = patch_project(client, project_id, {"name": "Нове", "archived": True})
    assert status == 200
    assert body["name"] == "Нове"
    assert body["archived"] is True
    assert listed_ids(client) == []
    assert listed_ids(client, include_archived="true") == [project_id]


@pytest.mark.parametrize("payload", [{"name": "Нове"}, {"archived": True}])
def test_unknown_project_is_404(client: TestClient, payload: object) -> None:
    create(client, "A")
    status, body = patch_project(client, "no-such-project", payload)
    assert status == 404
    error = error_of(body)
    assert error["code"] == "PROJECT_NOT_FOUND"
    assert error["message"] == "Проєкт не знайдено"
    assert "no-such-project" not in str(body)


@pytest.mark.parametrize(
    "payload", [{}, None, {"name": None, "archived": None}, {"archived": False}]
)
def test_empty_or_unchanged_request_is_422_no_changes(client: TestClient, payload: object) -> None:
    created = create(client, "Без змін")[1]
    status, body = patch_project(client, created["id"], payload)
    assert status == 422
    error = error_of(body)
    assert error["code"] == "NO_CHANGES"
    assert error["details"] == {"field": "body"}
    assert client.get("/v1/projects").json()["items"][0]["updated_at"] == created["updated_at"]


def test_renaming_to_the_same_name_is_no_changes(client: TestClient) -> None:
    created = create(client, "Назва")[1]
    status, body = patch_project(client, created["id"], {"name": "  Назва  "})
    assert status == 422
    assert error_of(body)["code"] == "NO_CHANGES"


def test_archiving_twice_is_no_changes_and_keeps_archived_at(client: TestClient) -> None:
    project_id = create(client, "A")[1]["id"]
    _, first = patch_project(client, project_id, {"archived": True})
    status, body = patch_project(client, project_id, {"archived": True})
    assert status == 422
    assert error_of(body)["code"] == "NO_CHANGES"
    shown = client.get("/v1/projects", params={"include_archived": "true"}).json()["items"]
    assert shown[0]["archived_at"] == first["archived_at"]


def test_patch_without_token_is_401_and_changes_nothing(
    client: TestClient, anonymous: TestClient
) -> None:
    project_id = create(client, "Захищений")[1]["id"]
    response = anonymous.patch(f"/v1/projects/{project_id}", json={"name": "Зламаний"})
    assert response.status_code == 401
    assert client.get("/v1/projects").json()["items"][0]["name"] == "Захищений"


# --- search (q) -------------------------------------------------------------------------------


def test_search_finds_a_substring_of_the_name_ignoring_case(client: TestClient) -> None:
    for name in ("Київ запис", "Одеса", "Straße", "ІГОР"):
        create(client, name)
    assert listed_names(client, q="київ") == ["Київ запис"]
    assert listed_names(client, q="КИЇВ") == ["Київ запис"]
    assert listed_names(client, q="ЗАПИС") == ["Київ запис"]
    assert listed_names(client, q="ігор") == ["ІГОР"]
    assert listed_names(client, q="STRASSE") == ["Straße"]
    assert listed_names(client, q="  одес  ") == ["Одеса"]


def test_search_without_a_match_is_an_empty_page(client: TestClient) -> None:
    create(client, "Київ запис")
    assert client.get("/v1/projects", params={"q": "харків"}).json() == {
        "items": [],
        "next_cursor": None,
    }


@pytest.mark.parametrize("q", ["", " ", "\t \n"])
def test_empty_search_does_not_filter(client: TestClient, runtime: Runtime, q: str) -> None:
    seed(runtime, "Перший", "Другий")
    assert listed_names(client, q=q) == ["Перший", "Другий"]


def test_search_treats_like_wildcards_as_text(client: TestClient, runtime: Runtime) -> None:
    seed(runtime, "100% готово", "a_b", "abc")
    assert listed_names(client, q="%") == ["100% готово"]
    assert listed_names(client, q="_") == ["a_b"]


def test_search_follows_a_rename(client: TestClient) -> None:
    project_id = create(client, "Старе")[1]["id"]
    patch_project(client, project_id, {"name": "Нове Львів"})
    assert listed_ids(client, q="старе") == []
    assert listed_ids(client, q="львів") == [project_id]


def test_search_of_200_characters_is_accepted_and_201_is_422(client: TestClient) -> None:
    create(client, "я" * 120)
    assert client.get("/v1/projects", params={"q": "я" * 200}).status_code == 200
    long = "Київ" * 51
    assert len(long) == 204
    response = client.get("/v1/projects", params={"q": long})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert long not in response.text
    assert client.get("/v1/projects", params={"q": "я" * 201}).status_code == 422


@pytest.mark.parametrize("raw", ["%00", "a%00b", "%ED%A0%80", "%FF"])
def test_search_with_nul_or_invalid_utf8_matches_nothing(client: TestClient, raw: str) -> None:
    create(client, "Проєкт")
    response = client.get(f"/v1/projects?q={raw}")
    assert response.status_code == 200
    assert response.json() == {"items": [], "next_cursor": None}


def test_search_with_include_archived(client: TestClient, runtime: Runtime) -> None:
    visible, hidden, lviv = seed(runtime, "Київ відкритий", "Київ архівний", "Львів")
    patch_project(client, hidden, {"archived": True})
    assert listed_ids(client, q="київ") == [visible]
    assert listed_ids(client, q="київ", include_archived="false") == [visible]
    assert listed_ids(client, q="київ", include_archived="true") == [visible, hidden]
    assert listed_ids(client, q="львів", include_archived="true") == [lviv]


def test_search_pages_with_a_cursor_cover_every_match_once(
    client: TestClient, runtime: Runtime
) -> None:
    names = [f"Київ {n}" if n % 2 == 0 else f"Львів {n}" for n in range(11)]
    created = seed(runtime, *names)
    matches = [
        project_id for project_id, name in zip(created, names, strict=True) if "Київ" in name
    ]
    assert len(matches) == 6

    seen: list[object] = []
    cursor: str | None = None
    pages = 0
    while True:
        params = {"q": "київ", "limit": "2"} | ({"cursor": cursor} if cursor else {})
        body = client.get("/v1/projects", params=params).json()
        assert 1 <= len(body["items"]) <= 2
        assert all("Київ" in item["name"] for item in body["items"])
        seen.extend(item["id"] for item in body["items"])
        pages += 1
        cursor = body["next_cursor"]
        if cursor is None:
            break
    assert seen == matches
    assert pages == 3


def test_search_cursor_may_point_to_a_project_the_search_does_not_show(
    client: TestClient, runtime: Runtime
) -> None:
    first, other, second = seed(runtime, "Київ 1", "Львів", "Київ 2")
    assert listed_ids(client, q="київ", cursor=other) == [second]
    assert listed_ids(client, q="київ", cursor=first) == [second]
    response = client.get("/v1/projects", params={"q": "київ", "cursor": "no-such-project"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_CURSOR"


def test_search_pages_with_archived_projects_and_a_cursor(
    client: TestClient, runtime: Runtime
) -> None:
    ids = seed(runtime, *(f"Київ {n}" for n in range(5)))
    patch_project(client, ids[1], {"archived": True})
    patch_project(client, ids[3], {"archived": True})
    page = client.get("/v1/projects", params={"q": "київ", "limit": "2"}).json()
    assert [item["id"] for item in page["items"]] == [ids[0], ids[2]]
    page = client.get(
        "/v1/projects", params={"q": "київ", "limit": "2", "cursor": page["next_cursor"]}
    ).json()
    assert [item["id"] for item in page["items"]] == [ids[4]]
    assert page["next_cursor"] is None
    everything = client.get(
        "/v1/projects", params={"q": "київ", "limit": "2", "include_archived": "true"}
    ).json()
    assert [item["id"] for item in everything["items"]] == ids[:2]
    assert everything["next_cursor"] == ids[1]


# --- source_count, clip_count and last_activity_at --------------------------------------------

LATER = "2999-01-01T10:00:00.000000Z"
LATEST = "2999-01-01T11:00:00.000000Z"
EARLIER = "2000-01-01T10:00:00.000000Z"


def test_a_new_project_has_no_sources_or_clips(client: TestClient) -> None:
    created = create(client, "Порожній")[1]
    item = listed_item(client, created["id"])
    assert item["source_count"] == 0
    assert item["clip_count"] == 0
    assert item["last_activity_at"] == created["updated_at"]
    assert isinstance(item["last_activity_at"], str)
    assert STAMP.fullmatch(item["last_activity_at"])


def test_list_item_is_the_post_shape_plus_the_figures(client: TestClient) -> None:
    created = create(client, "A")[1]
    item = listed_item(client, created["id"])
    assert set(item) == set(created) | {"source_count", "clip_count", "last_activity_at"}
    for key, value in created.items():
        assert item[key] == value


def test_post_and_patch_do_not_carry_the_figures(client: TestClient) -> None:
    created = create(client, "A")[1]
    _, patched = patch_project(client, created["id"], {"name": "B"})
    figures = {"source_count", "clip_count", "last_activity_at"}
    assert not figures & set(created)
    assert not figures & set(patched)
    assert set(created) == {"id", "name", "created_at", "updated_at", "archived_at"}
    assert set(patched) == set(created) | {"archived"}


def test_counts_and_last_activity_follow_a_source_a_clip_and_an_export_batch(
    client: TestClient, runtime: Runtime
) -> None:
    project_id = str(create(client, "Проєкт")[1]["id"])
    other_id = str(create(client, "Інший")[1]["id"])

    with runtime.database.write_session() as session:
        first = add_source(session, project_id, now=at(LATER))
    item = listed_item(client, project_id)
    assert (item["source_count"], item["clip_count"]) == (1, 0)
    assert item["last_activity_at"] == LATER

    with runtime.database.write_session() as session:
        second = add_source(session, project_id, now=at(EARLIER))
        add_clip(session, first.id, now=at(LATER))
        add_clip(session, second.id, start_ms=6_000, end_ms=9_000, now=at(LATEST))
    item = listed_item(client, project_id)
    assert (item["source_count"], item["clip_count"]) == (2, 2)
    assert item["last_activity_at"] == LATEST

    batch_at = "2999-01-01T12:00:00.000000Z"
    with runtime.database.write_session() as session:
        ExportRepository(session, now=at(batch_at)).add_batch(
            project_id=project_id, output_dir="C:\\Експорт", profile_json={}, status="queued"
        )
    item = listed_item(client, project_id)
    assert (item["source_count"], item["clip_count"]) == (2, 2)
    assert item["last_activity_at"] == batch_at

    # Another project is not affected by any of it.
    untouched = listed_item(client, other_id)
    assert (untouched["source_count"], untouched["clip_count"]) == (0, 0)
    assert untouched["last_activity_at"] != batch_at


def test_a_clip_edit_moves_last_activity(client: TestClient, runtime: Runtime) -> None:
    project_id = str(create(client, "Проєкт")[1]["id"])
    with runtime.database.write_session() as session:
        source = add_source(session, project_id, now=at(LATER))
        clip = add_clip(session, source.id, now=at(LATER))
    assert listed_item(client, project_id)["last_activity_at"] == LATER
    with runtime.database.write_session() as session:
        ClipRepository(session, now=at(LATEST)).update(
            clip.id, expected_revision=1, title="Нова назва"
        )
    item = listed_item(client, project_id)
    assert item["clip_count"] == 1
    assert item["last_activity_at"] == LATEST


def test_renaming_after_the_last_activity_makes_the_rename_the_last_activity(
    client: TestClient, runtime: Runtime
) -> None:
    project_id = str(create(client, "Проєкт")[1]["id"])
    with runtime.database.write_session() as session:
        source = add_source(session, project_id, now=at(EARLIER))
        add_clip(session, source.id, now=at(EARLIER))
    _, renamed = patch_project(client, project_id, {"name": "Нова назва"})
    item = listed_item(client, project_id)
    assert item["last_activity_at"] == renamed["updated_at"]
    assert (item["source_count"], item["clip_count"]) == (1, 1)


def test_figures_of_a_whole_page_match_each_project(client: TestClient, runtime: Runtime) -> None:
    ids = seed(runtime, *(f"Проєкт {n}" for n in range(4)))
    with runtime.database.write_session() as session:
        # Project n has n sources with n clips each.
        for n, project_id in enumerate(ids):
            for _ in range(n):
                source = add_source(session, project_id, now=at(LATER))
                for k in range(n):
                    add_clip(
                        session,
                        source.id,
                        start_ms=k * 1_000,
                        end_ms=k * 1_000 + 500,
                        now=at(LATEST),
                    )
    items = client.get("/v1/projects").json()["items"]
    assert [item["id"] for item in items] == ids
    assert [(item["source_count"], item["clip_count"]) for item in items] == [
        (0, 0),
        (1, 1),
        (2, 4),
        (3, 9),
    ]
    assert items[0]["last_activity_at"] == items[0]["updated_at"]
    assert [item["last_activity_at"] for item in items[1:]] == [LATEST] * 3


def test_figures_are_present_on_searched_and_archived_items(
    client: TestClient, runtime: Runtime
) -> None:
    project_id = str(create(client, "Київ запис")[1]["id"])
    with runtime.database.write_session() as session:
        add_clip(session, add_source(session, project_id, now=at(EARLIER)).id, now=at(EARLIER))
    patch_project(client, project_id, {"archived": True})
    (item,) = client.get("/v1/projects", params={"q": "київ", "include_archived": "true"}).json()[
        "items"
    ]
    assert (item["source_count"], item["clip_count"]) == (1, 1)
    # Archiving is the project's own update, so it is the latest activity.
    assert item["last_activity_at"] == item["updated_at"]
