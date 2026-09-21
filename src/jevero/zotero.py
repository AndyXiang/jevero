"""All Zotero access lives here.

Supported surface is the Zotero Web API v3 (or, read-only, the desktop local
API on port 23119). Zotero's SQLite database is never opened or modified.

Two details drive the shape of this module:

* Items carry a ``version``. Writes must send ``If-Unmodified-Since-Version`` so
  a concurrent edit is rejected with HTTP 412 instead of being overwritten.
* ``tags`` is a *complete list* on write, not a patch. Unrelated existing tags
  must therefore be merged back in, never dropped.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import httpx

from .http import create_client
from .models import PaperRecord, PolicyActions

WEB_BASE_URL = "https://api.zotero.org"
LOCAL_BASE_URL = "http://127.0.0.1:23119/api"
#: The local API always serves the logged-in user as library ``0``.
LOCAL_LIBRARY_ID = "0"
ZOTERO_API_VERSION = "3"
PAGE_SIZE = 100

#: Item types that are not papers and must never be classified.
_NON_PAPER_TYPES = frozenset({"attachment", "note", "annotation"})

_YEAR_PATTERN = re.compile(r"(?<!\d)(1[5-9]\d{2}|20\d{2})(?!\d)")
_ARXIV_PATTERN = re.compile(
    r"arxiv[:\s/]*(?P<id>[a-z.-]+/\d{7}|\d{4}\.\d{4,5})", re.IGNORECASE
)


class ZoteroError(Exception):
    """Base class for Zotero failures."""


class ZoteroReadError(ZoteroError):
    """A read failed: transport, HTTP status, or an unexpected payload."""


class ZoteroWriteError(ZoteroError):
    """A write failed or was refused."""


class ZoteroNotFoundError(ZoteroReadError):
    """A requested collection or item does not exist."""


@dataclass(frozen=True)
class ZoteroItem:
    """One Zotero item, with the version needed for a safe write."""

    key: str
    version: int
    data: dict[str, Any]

    @property
    def tags(self) -> list[str]:
        raw = self.data.get("tags") or []
        return [str(tag.get("tag", "")) for tag in raw if tag.get("tag")]

    def to_paper(self) -> PaperRecord:
        return normalize_item(self.data)


def normalize_item(data: dict[str, Any]) -> PaperRecord:
    """Convert raw Zotero item data into the internal paper model.

    Pure function: no HTTP, no version handling. Everything downstream of this
    point works with ``PaperRecord``, not Zotero's field names.
    """
    creators = data.get("creators") or []
    authors = [
        name
        for creator in creators
        if isinstance(creator, dict)
        for name in [_creator_name(creator)]
        if name and creator.get("creatorType", "author") in {"author", ""}
    ]
    if not authors:
        authors = [
            name
            for creator in creators
            if isinstance(creator, dict)
            for name in [_creator_name(creator)]
            if name
        ]

    return PaperRecord(
        zotero_key=str(data.get("key") or ""),
        title=str(data.get("title") or "").strip(),
        abstract=_optional_text(data.get("abstractNote")),
        authors=authors,
        year=_year(data.get("date")),
        doi=_optional_text(data.get("DOI")),
        arxiv_id=_arxiv_id(data),
    )


def merge_tags(existing: list[str], actions: PolicyActions) -> list[str]:
    """Apply actions to a tag list, preserving unrelated tags.

    Zotero replaces the whole tag array on write, so this must always be the
    full list that should end up on the item.
    """
    tags = {tag for tag in existing if tag}
    tags |= {tag for tag in actions.add_tags if tag}
    tags -= {tag for tag in actions.remove_tags if tag}
    return sorted(tags)


class ZoteroClient:
    """Read and write a Zotero library through the supported HTTP API."""

    def __init__(
        self,
        *,
        library_type: str = "user",
        library_id: str,
        api_key: str | None = None,
        backend: str = "web",
        base_url: str | None = None,
        timeout_seconds: float = 30.0,
        http_client: httpx.Client | None = None,
    ) -> None:
        if library_type not in {"user", "group"}:
            raise ZoteroError(f"unsupported library_type {library_type!r}")
        if backend not in {"web", "local"}:
            raise ZoteroError(f"unsupported backend {backend!r}")
        if backend == "web" and not library_id:
            raise ZoteroError(
                "no Zotero library ID; set ZOTERO_LIBRARY_ID or zotero.library_id"
            )

        self.backend = backend
        self.library_type = library_type
        self.library_id = library_id or LOCAL_LIBRARY_ID
        self._api_key = api_key
        self._base_url = (base_url or _default_base_url(backend)).rstrip("/")
        self._http = http_client or create_client(timeout_seconds)
        self._owns_client = http_client is None

    def __enter__(self) -> ZoteroClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_client:
            self._http.close()

    @property
    def supports_write(self) -> bool:
        """Writes need an API key on either backend.

        The desktop local API is read-only before Zotero 10; from Zotero 10 the
        same write methods exist behind a locally granted key.
        """
        return self._api_key is not None

    @property
    def library_prefix(self) -> str:
        return f"/{self.library_type}s/{self.library_id}"

    def find_collection_key(self, name: str) -> str:
        """Resolve a collection name such as ``00 Inbox`` to its key."""
        for collection in self._iter_pages(f"{self.library_prefix}/collections"):
            data = collection.get("data") or {}
            if data.get("name") == name:
                return str(collection.get("key") or data.get("key"))
        raise ZoteroNotFoundError(f"no Zotero collection named {name!r}")

    def iter_papers(
        self, *, collection_key: str | None = None, limit: int | None = None
    ) -> Iterator[ZoteroItem]:
        """Yield top-level items, skipping attachments and notes."""
        path = f"{self.library_prefix}/collections/{collection_key}/items/top"
        if collection_key is None:
            path = f"{self.library_prefix}/items/top"

        yielded = 0
        for raw in self._iter_pages(path):
            item = _to_item(raw)
            if item is None or item.data.get("itemType") in _NON_PAPER_TYPES:
                continue
            yield item
            yielded += 1
            if limit is not None and yielded >= limit:
                return

    def get_item(self, key: str) -> ZoteroItem:
        """Read one item, including the version required for a write."""
        payload = self._get_json(f"{self.library_prefix}/items/{key}")
        if not isinstance(payload, dict):
            raise ZoteroReadError(f"unexpected item payload for {key}")
        item = _to_item(payload)
        if item is None:
            raise ZoteroReadError(f"item {key} has no usable data")
        return item

    def apply_actions(self, item: ZoteroItem, actions: PolicyActions) -> list[str]:
        """Write tags for one item, preserving every unrelated existing tag.

        Returns the resulting tag list. Raises ``ZoteroWriteError`` on refusal,
        including the HTTP 412 raised when the item changed since it was read.
        """
        if actions.is_empty:
            return item.tags
        if not self.supports_write:
            raise ZoteroWriteError(
                "no Zotero API key; writes need ZOTERO_API_KEY "
                "(use --dry-run to inspect without one)"
            )

        merged = merge_tags(item.tags, actions)
        if merged == sorted(item.tags):
            return item.tags

        response = self._request(
            "PATCH",
            f"{self.library_prefix}/items/{item.key}",
            json={"tags": [{"tag": tag} for tag in merged]},
            headers={"If-Unmodified-Since-Version": str(item.version)},
        )
        if response.status_code == 412:
            raise ZoteroWriteError(
                f"item {item.key} changed since it was read; re-run to retry"
            )
        if response.status_code >= 400:
            raise ZoteroWriteError(
                f"writing tags to {item.key} failed with HTTP "
                f"{response.status_code}: {_snippet(response)}"
            )
        return merged

    def _iter_pages(self, path: str) -> Iterator[dict[str, Any]]:
        start = 0
        while True:
            payload = self._get_json(
                path, params={"limit": PAGE_SIZE, "start": start, "format": "json"}
            )
            if not isinstance(payload, list):
                raise ZoteroReadError(
                    f"expected a list from {path}, got {type(payload).__name__}"
                )
            for entry in payload:
                if isinstance(entry, dict):
                    yield entry
            if len(payload) < PAGE_SIZE:
                return
            start += PAGE_SIZE

    def _get_json(
        self, path: str, *, params: dict[str, Any] | None = None
    ) -> Any:
        response = self._request("GET", path, params=params)
        if response.status_code == 404:
            raise ZoteroNotFoundError(f"Zotero returned 404 for {path}")
        if response.status_code >= 400:
            raise ZoteroReadError(
                f"GET {path} failed with HTTP {response.status_code}: "
                f"{_snippet(response)}"
            )
        try:
            return response.json()
        except ValueError as exc:
            raise ZoteroReadError(f"GET {path} did not return JSON") from exc

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        request_headers = {"Zotero-API-Version": ZOTERO_API_VERSION}
        if self._api_key:
            request_headers["Authorization"] = f"Bearer {self._api_key}"
        if headers:
            request_headers.update(headers)
        try:
            return self._http.request(
                method,
                self._base_url + path,
                params=params,
                json=json,
                headers=request_headers,
            )
        except httpx.HTTPError as exc:
            raise ZoteroReadError(
                f"could not reach Zotero at {self._base_url}: {exc}"
            ) from exc


def _default_base_url(backend: str) -> str:
    return LOCAL_BASE_URL if backend == "local" else WEB_BASE_URL


def _to_item(raw: dict[str, Any]) -> ZoteroItem | None:
    data = raw.get("data")
    key = raw.get("key") or (data or {}).get("key")
    if not isinstance(data, dict) or not key:
        return None
    version = raw.get("version", data.get("version"))
    return ZoteroItem(key=str(key), version=int(version or 0), data=data)


def _creator_name(creator: dict[str, Any]) -> str | None:
    single = _optional_text(creator.get("name"))
    if single:
        return single
    parts = [
        _optional_text(creator.get("firstName")),
        _optional_text(creator.get("lastName")),
    ]
    joined = " ".join(part for part in parts if part)
    return joined or None


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _year(value: Any) -> int | None:
    text = _optional_text(value)
    if not text:
        return None
    match = _YEAR_PATTERN.search(text)
    return int(match.group(1)) if match else None


def _arxiv_id(data: dict[str, Any]) -> str | None:
    archive_id = _optional_text(data.get("archiveID"))
    if archive_id:
        match = _ARXIV_PATTERN.search(archive_id)
        if match:
            return match.group("id")
    for field in ("extra", "url", "libraryCatalog"):
        match = _ARXIV_PATTERN.search(_optional_text(data.get(field)) or "")
        if match:
            return match.group("id")
    return None


def _snippet(response: httpx.Response, limit: int = 300) -> str:
    text = " ".join((response.text or "").split())
    return text[:limit]
