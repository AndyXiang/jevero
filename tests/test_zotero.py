"""Zotero local API tests. HTTP is mocked; Zotero's SQLite is never touched."""

from __future__ import annotations

import httpx
import pytest

from jevero.models import PolicyActions
from jevero.zotero import (
    LOCAL_BASE_URL,
    ZoteroClient,
    ZoteroItem,
    ZoteroLocalApiDisabledError,
    ZoteroNotFoundError,
    ZoteroReadError,
    ZoteroWriteError,
    merge_tags,
    normalize_item,
)


def make_client(handler) -> ZoteroClient:
    return ZoteroClient(
        base_url=LOCAL_BASE_URL,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def _unused_handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
    raise AssertionError(f"no request expected, got {request.method} {request.url}")


def _item(payload: dict) -> ZoteroItem:
    return ZoteroItem(
        key=payload["key"], version=payload["version"], data=payload["data"]
    )


# --------------------------------------------------------------------------- #
# pure helpers
# --------------------------------------------------------------------------- #


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

    assert merged == [
        "agent/processed",
        "my own tag",
        "to-read",
        "topic/nrqcd",
        "topic/quarkonium",
    ]


def test_merge_tags_removes_only_the_requested_tags():
    existing = ["agent/processed", "agent/error", "to-read"]
    actions = PolicyActions(add_tags={"agent/processed"}, remove_tags={"agent/error"})

    assert merge_tags(existing, actions) == ["agent/processed", "to-read"]


def test_merge_tags_is_idempotent():
    existing = ["agent/processed", "topic/nrqcd"]
    actions = PolicyActions(add_tags={"agent/processed"})

    assert merge_tags(existing, actions) == sorted(existing)


# --------------------------------------------------------------------------- #
# reads
# --------------------------------------------------------------------------- #


def test_client_targets_the_local_user_library():
    with make_client(_unused_handler) as client:
        assert client.library_prefix == "/users/0"


def test_constructed_client_defaults_to_the_local_api():
    """No transport injection: exercise the real construction path."""
    client = ZoteroClient()
    try:
        assert client.base_url == LOCAL_BASE_URL
        assert client.library_prefix == "/users/0"
    finally:
        client.close()


def test_iter_papers_skips_attachments_and_notes():
    page = [
        {"key": "AAAA1111", "version": 1, "data": {"itemType": "journalArticle", "title": "A"}},
        {"key": "BBBB2222", "version": 2, "data": {"itemType": "attachment", "title": "PDF"}},
        {"key": "CCCC3333", "version": 3, "data": {"itemType": "note", "note": "hello"}},
        {"key": "DDDD4444", "version": 4, "data": {"itemType": "conferencePaper", "title": "D"}},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/users/0/collections/INBOX123/items/top"
        assert request.headers["Zotero-API-Version"] == "3"
        # Reads need no credentials on the local API.
        assert "Authorization" not in request.headers
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
        assert request.url.path == "/api/users/0/collections"
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


def test_disabled_local_api_reports_how_to_enable_it():
    """A 403 from the local API is a Zotero preference, not a bad request."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="Local API is not enabled")

    with make_client(handler) as client:
        with pytest.raises(ZoteroLocalApiDisabledError) as excinfo:
            client.find_collection_key("00 Inbox")

    message = str(excinfo.value)
    assert "Settings -> Advanced" in message
    assert "communicate with Zotero" in message


def test_unreachable_zotero_reports_the_running_hint():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with make_client(handler) as client:
        with pytest.raises(ZoteroReadError, match="is the Zotero desktop app running"):
            client.find_collection_key("00 Inbox")


# --------------------------------------------------------------------------- #
# writes: deliberately unavailable until the local key flow exists
# --------------------------------------------------------------------------- #


def test_writes_are_not_available_yet(zotero_item_payload: dict):
    with make_client(_unused_handler) as client:
        assert client.supports_write is False

        with pytest.raises(ZoteroWriteError, match="api/local/authorize"):
            client.apply_actions(
                _item(zotero_item_payload), PolicyActions(add_tags={"agent/processed"})
            )


def test_empty_actions_are_a_no_op_and_do_not_raise(zotero_item_payload: dict):
    with make_client(_unused_handler) as client:
        assert client.apply_actions(_item(zotero_item_payload), PolicyActions()) == [
            "to-read",
            "topic/quarkonium",
            "my own tag",
        ]
