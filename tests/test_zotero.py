"""Zotero layer tests. HTTP is mocked; Zotero's SQLite is never touched."""

from __future__ import annotations

import json

import httpx
import pytest

from jevero.models import PolicyActions
from jevero.zotero import (
    ZoteroClient,
    ZoteroError,
    ZoteroItem,
    ZoteroNotFoundError,
    ZoteroReadError,
    ZoteroWriteError,
    merge_tags,
    normalize_item,
)

LIBRARY_ID = "123456"


def make_client(handler, *, api_key: str | None = "zotero-key", backend: str = "web") -> ZoteroClient:
    return ZoteroClient(
        library_type="user",
        library_id=LIBRARY_ID,
        api_key=api_key,
        backend=backend,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def test_normalize_item_extracts_the_internal_model(zotero_item_payload: dict):
    paper = normalize_item(zotero_item_payload["data"])

    assert paper.zotero_key == "ABCD2345"
    assert paper.title == "Energy Correlators in Heavy Quarkonium Production"
    assert paper.authors == ["Ada Lovelace", "Emmy Noether"]
    assert paper.year == 2019
    assert paper.doi == "10.1000/example.doi"
    assert paper.arxiv_id == "1905.01234"
    assert paper.has_abstract


def test_normalize_item_handles_missing_fields():
    paper = normalize_item({"key": "ZZZZ9999", "title": "Minimal", "date": "n.d."})

    assert paper.abstract is None
    assert paper.authors == []
    assert paper.year is None
    assert paper.doi is None
    assert paper.arxiv_id is None
    assert not paper.has_abstract


def test_normalize_item_reads_a_single_field_creator():
    paper = normalize_item(
        {
            "key": "ZZZZ9999",
            "title": "Collaboration paper",
            "creators": [{"creatorType": "author", "name": "CMS Collaboration"}],
        }
    )

    assert paper.authors == ["CMS Collaboration"]


def test_merge_tags_preserves_unrelated_existing_tags():
    existing = ["to-read", "topic/quarkonium", "my own tag"]
    actions = PolicyActions(add_tags={"topic/nrqcd", "agent/processed"})

    merged = merge_tags(existing, actions)

    assert merged == ["agent/processed", "my own tag", "to-read", "topic/nrqcd", "topic/quarkonium"]


def test_merge_tags_removes_only_the_requested_tags():
    existing = ["agent/processed", "agent/error", "to-read"]
    actions = PolicyActions(add_tags={"agent/processed"}, remove_tags={"agent/error"})

    assert merge_tags(existing, actions) == ["agent/processed", "to-read"]


def test_merge_tags_is_idempotent():
    existing = ["agent/processed", "topic/nrqcd"]
    actions = PolicyActions(add_tags={"agent/processed"})

    assert merge_tags(existing, actions) == sorted(existing)


def test_apply_actions_writes_the_complete_tag_list(zotero_item_payload: dict):
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["version"] = request.headers.get("If-Unmodified-Since-Version")
        seen["api_version"] = request.headers.get("Zotero-API-Version")
        seen["body"] = json.loads(request.content)
        return httpx.Response(204)

    item = _item(zotero_item_payload)
    actions = PolicyActions(
        add_tags={"topic/nrqcd", "agent/processed"}, remove_tags={"topic/quarkonium"}
    )

    with make_client(handler) as client:
        result = client.apply_actions(item, actions)

    assert seen["method"] == "PATCH"
    assert seen["url"] == f"https://api.zotero.org/users/{LIBRARY_ID}/items/ABCD2345"
    assert seen["version"] == "417"
    assert seen["api_version"] == "3"
    tags = [entry["tag"] for entry in seen["body"]["tags"]]
    # Unrelated tags must survive: Zotero replaces the whole array.
    assert tags == ["agent/processed", "my own tag", "to-read", "topic/nrqcd"]
    assert result == tags


def test_apply_actions_does_nothing_when_there_is_nothing_to_do(zotero_item_payload: dict):
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("no request expected for empty actions")

    with make_client(handler) as client:
        assert client.apply_actions(_item(zotero_item_payload), PolicyActions()) == [
            "to-read",
            "topic/quarkonium",
            "my own tag",
        ]


def test_apply_actions_refuses_without_a_key(zotero_item_payload: dict):
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("no write may be attempted without a key")

    with make_client(handler, api_key=None, backend="local") as client:
        assert client.supports_write is False
        with pytest.raises(ZoteroWriteError, match="API key"):
            client.apply_actions(
                _item(zotero_item_payload), PolicyActions(add_tags={"agent/processed"})
            )


def test_apply_actions_reports_a_version_conflict(zotero_item_payload: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(412, text="Precondition Failed")

    with make_client(handler) as client:
        with pytest.raises(ZoteroWriteError, match="changed since it was read"):
            client.apply_actions(
                _item(zotero_item_payload), PolicyActions(add_tags={"agent/processed"})
            )


def test_apply_actions_reports_other_write_failures(zotero_item_payload: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="Forbidden")

    with make_client(handler) as client:
        with pytest.raises(ZoteroWriteError, match="HTTP 403"):
            client.apply_actions(
                _item(zotero_item_payload), PolicyActions(add_tags={"agent/processed"})
            )


def test_iter_papers_skips_attachments_and_notes():
    page = [
        {"key": "AAAA1111", "version": 1, "data": {"itemType": "journalArticle", "title": "A"}},
        {"key": "BBBB2222", "version": 2, "data": {"itemType": "attachment", "title": "PDF"}},
        {"key": "CCCC3333", "version": 3, "data": {"itemType": "note", "note": "hello"}},
        {"key": "DDDD4444", "version": 4, "data": {"itemType": "conferencePaper", "title": "D"}},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/users/{LIBRARY_ID}/collections/INBOX123/items/top"
        return httpx.Response(200, json=page)

    with make_client(handler) as client:
        papers = list(client.iter_papers(collection_key="INBOX123"))

    assert [item.key for item in papers] == ["AAAA1111", "DDDD4444"]


def test_iter_papers_honours_the_limit():
    page = [
        {"key": f"AAAA{i:04d}", "version": i, "data": {"itemType": "journalArticle"}}
        for i in range(5)
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=page)

    with make_client(handler) as client:
        papers = list(client.iter_papers(collection_key="INBOX123", limit=2))

    assert len(papers) == 2


def test_find_collection_key_matches_by_name():
    collections = [
        {"key": "OTHER111", "data": {"name": "Archive"}},
        {"key": "INBOX123", "data": {"name": "00 Inbox"}},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/users/{LIBRARY_ID}/collections"
        return httpx.Response(200, json=collections)

    with make_client(handler) as client:
        assert client.find_collection_key("00 Inbox") == "INBOX123"


def test_find_collection_key_reports_a_missing_collection():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    with make_client(handler) as client:
        with pytest.raises(ZoteroNotFoundError, match="00 Inbox"):
            client.find_collection_key("00 Inbox")


def test_get_item_reports_a_not_found_item():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="Not Found")

    with make_client(handler) as client:
        with pytest.raises(ZoteroNotFoundError, match="MISSING1"):
            client.get_item("MISSING1")


def test_read_errors_are_not_silently_swallowed():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    with make_client(handler) as client:
        with pytest.raises(ZoteroReadError, match="HTTP 500"):
            client.get_item("ABCD2345")


def test_local_backend_uses_the_desktop_api_and_no_key():
    with ZoteroClient(
        library_type="user",
        library_id="",
        backend="local",
        http_client=httpx.Client(transport=httpx.MockTransport(_unused_handler)),
    ) as client:
        assert client.library_prefix == "/users/0"
        assert client.supports_write is False


def test_web_backend_requires_a_library_id():
    with pytest.raises(ZoteroError, match="library ID"):
        ZoteroClient(library_type="user", library_id="", backend="web")


def test_group_library_prefix():
    with make_client(_unused_handler, backend="web") as client:
        assert client.library_prefix == f"/users/{LIBRARY_ID}"

    with ZoteroClient(
        library_type="group",
        library_id="42",
        backend="web",
        http_client=httpx.Client(transport=httpx.MockTransport(_unused_handler)),
    ) as client:
        assert client.library_prefix == "/groups/42"


def test_constructed_client_defaults_to_the_web_api(monkeypatch: pytest.MonkeyPatch):
    """No transport injection: exercise the real client construction path."""
    client = ZoteroClient(library_type="user", library_id=LIBRARY_ID, backend="web")
    try:
        assert client.library_prefix == f"/users/{LIBRARY_ID}"
    finally:
        client.close()


def _unused_handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
    raise AssertionError(f"no request expected, got {request.method} {request.url}")


def _item(payload: dict) -> ZoteroItem:
    """Build the item object the read path would produce for this fixture."""
    return ZoteroItem(
        key=payload["key"], version=payload["version"], data=payload["data"]
    )
