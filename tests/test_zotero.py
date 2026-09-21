"""Zotero local API tests. HTTP is mocked; Zotero's SQLite is never touched.

The write tests script the three real requests of the local write flow: the
instance probe (``GET /api/``), the authorization dialog
(``POST /api/local/authorize``), and the versioned tag write (``PATCH``).
"""

from __future__ import annotations

import json

import httpx
import pytest

from jevero.models import PolicyActions
from jevero.zotero import (
    LOCAL_BASE_URL,
    ZoteroAuthorizationDeniedError,
    ZoteroAuthorizationError,
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
# writes: runtime authorization, then a versioned PATCH
# --------------------------------------------------------------------------- #


class LocalApi:
    """Scripted local API: instance probe, authorization dialog, tag write."""

    def __init__(
        self,
        *,
        server_id: str | None = "srv-1",
        zotero_version: str | None = "10.0.1",
        remember: bool = True,
        authorize_status: int = 200,
        patch_status: int = 204,
        reject_first_patch_with_401: bool = False,
    ) -> None:
        self.server_id = server_id
        self.zotero_version = zotero_version
        self.remember = remember
        self.authorize_status = authorize_status
        self.patch_status = patch_status
        self.reject_first_patch_with_401 = reject_first_patch_with_401
        self.authorize_calls = 0
        self.patch_headers: list[httpx.Headers] = []
        self.patch_bodies: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == "/api/":
            headers = {}
            if self.server_id:
                headers["Zotero-Server-ID"] = self.server_id
            if self.zotero_version:
                headers["X-Zotero-Version"] = self.zotero_version
            return httpx.Response(200, text="Zotero is running", headers=headers)

        if request.method == "POST" and path == "/api/local/authorize":
            self.authorize_calls += 1
            if self.authorize_status != 200:
                return httpx.Response(self.authorize_status, text="denied")
            return httpx.Response(
                200, json={"key": f"local-key-{self.authorize_calls}", "remember": self.remember}
            )

        if request.method == "PATCH":
            self.patch_headers.append(request.headers)
            self.patch_bodies.append(json.loads(request.content))
            if self.reject_first_patch_with_401 and len(self.patch_headers) == 1:
                return httpx.Response(401, text="key consumed")
            return httpx.Response(self.patch_status)

        return httpx.Response(404, text=f"unexpected {request.method} {path}")


def test_unauthorized_zotero_without_a_server_id_cannot_write(
    zotero_item_payload: dict,
):
    """Zotero 9 and earlier expose the local API read-only."""
    api = LocalApi(server_id=None, zotero_version="9.0.6")

    with make_client(api.handler) as client:
        assert client.supports_write is True  # the flow exists...
        with pytest.raises(ZoteroWriteError, match="Zotero 10"):
            client.ensure_writes_available()

        with pytest.raises(ZoteroWriteError, match="9.0.6"):
            client.apply_actions(
                _item(zotero_item_payload), PolicyActions(add_tags={"agent/processed"})
            )

    assert api.patch_headers == []


def test_ensure_writes_available_passes_when_the_server_id_is_reported():
    api = LocalApi()

    with make_client(api.handler) as client:
        client.ensure_writes_available()

        assert client.server_id == "srv-1"
        assert client.zotero_version == "10.0.1"


def test_authorization_requires_always_allow():
    """A single-use key would mean one dialog per paper."""
    api = LocalApi(remember=False)

    with make_client(api.handler) as client:
        with pytest.raises(ZoteroAuthorizationError, match="Always Allow"):
            client.authorize_writes()


def test_authorization_accepts_always_allow():
    api = LocalApi(remember=True)

    with make_client(api.handler) as client:
        assert client.authorize_writes() == "local-key-1"


def test_authorization_request_carries_the_server_id_and_app_name():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/local/authorize":
            seen["headers"] = request.headers
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={"key": "k", "remember": True})
        return httpx.Response(200, text="", headers={"Zotero-Server-ID": "srv-9"})

    with make_client(handler) as client:
        client.authorize_writes()

    assert seen["headers"]["Zotero-Server-ID"] == "srv-9"
    assert seen["body"] == {"appName": "jevero"}


def test_authorization_denied_by_the_user():
    api = LocalApi(authorize_status=403)

    with make_client(api.handler) as client:
        with pytest.raises(ZoteroAuthorizationDeniedError, match="denied"):
            client.authorize_writes()


def test_authorization_on_a_unsupported_zotero():
    api = LocalApi(authorize_status=404)

    with make_client(api.handler) as client:
        with pytest.raises(ZoteroAuthorizationError, match="Zotero 10"):
            client.authorize_writes()


def test_authorization_rate_limited_dialogs():
    api = LocalApi(authorize_status=429)

    with make_client(api.handler) as client:
        with pytest.raises(ZoteroAuthorizationError, match="429"):
            client.authorize_writes()


def test_apply_actions_authorizes_once_then_writes_merged_tags(
    zotero_item_payload: dict,
):
    api = LocalApi()
    item = _item(zotero_item_payload)
    actions = PolicyActions(
        add_tags={"topic/nrqcd", "agent/processed"}, remove_tags={"topic/quarkonium"}
    )

    with make_client(api.handler) as client:
        merged = client.apply_actions(item, actions)
        # Second write reuses the remembered key: no second dialog.
        client.apply_actions(item, PolicyActions(add_tags={"role/core"}))

    assert api.authorize_calls == 1
    assert api.patch_headers[0]["If-Unmodified-Since-Version"] == "417"
    assert api.patch_headers[0]["Zotero-Server-ID"] == "srv-1"
    assert api.patch_headers[0]["Zotero-API-Key"] == "local-key-1"
    tags = [entry["tag"] for entry in api.patch_bodies[0]["tags"]]
    assert tags == ["agent/processed", "my own tag", "to-read", "topic/nrqcd"]
    assert merged == tags


def test_apply_actions_reauthorizes_once_after_a_401(zotero_item_payload: dict):
    api = LocalApi(reject_first_patch_with_401=True)

    with make_client(api.handler) as client:
        merged = client.apply_actions(
            _item(zotero_item_payload), PolicyActions(add_tags={"agent/processed"})
        )

    assert api.authorize_calls == 2
    assert len(api.patch_headers) == 2
    assert api.patch_headers[1]["Zotero-API-Key"] == "local-key-2"
    assert "agent/processed" in merged


def test_apply_actions_reports_a_changed_item(zotero_item_payload: dict):
    api = LocalApi(patch_status=412)

    with make_client(api.handler) as client:
        with pytest.raises(ZoteroWriteError, match="changed since it was read"):
            client.apply_actions(
                _item(zotero_item_payload), PolicyActions(add_tags={"agent/processed"})
            )


def test_apply_actions_does_not_write_when_tags_are_already_correct(
    zotero_item_payload: dict,
):
    """Nothing to change means no dialog and no request."""
    api = LocalApi()

    with make_client(api.handler) as client:
        result = client.apply_actions(
            _item(zotero_item_payload), PolicyActions(add_tags={"to-read"})
        )

    assert api.authorize_calls == 0
    assert api.patch_headers == []
    assert result == ["to-read", "topic/quarkonium", "my own tag"]


def test_empty_actions_are_a_no_op_and_do_not_raise(zotero_item_payload: dict):
    with make_client(_unused_handler) as client:
        assert client.apply_actions(_item(zotero_item_payload), PolicyActions()) == [
            "to-read",
            "topic/quarkonium",
            "my own tag",
        ]
