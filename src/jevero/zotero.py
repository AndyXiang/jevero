"""All Zotero access lives here: the desktop local API only.

Requests go to the Zotero desktop client's local implementation of the Web API
on ``localhost:23119`` under ``/api/``. Zotero's SQLite database is never opened
or modified.

Why local only:

* reads need no credentials and no network, so ``--dry-run`` works offline and
  is never rate limited;
* writes use a key Zotero grants at runtime through a confirmation dialog
  (``POST /api/local/authorize``). That key is unrelated to a zotero.org API
  key, cannot be created in advance, and is kept in memory only.

The zotero.org Web API client is archived in ``archive/zotero_web_api.py``.

Details that drive the shape of this module:

* the local API must be enabled in Zotero's preferences, otherwise every
  request returns ``403 Forbidden``;
* **local writes exist in Zotero 10+ only.** Zotero identifies itself with a
  ``Zotero-Server-ID`` response header; without it, ``ensure_writes_available``
  refuses before any work is done;
* every write must echo ``Zotero-Server-ID`` (otherwise ``428``) and carry
  ``If-Unmodified-Since-Version``; a ``412`` means the item changed underneath;
* ``tags`` is a *complete list* on write, not a patch, so unrelated existing
  tags must be merged back in and never dropped;
* local object versions have **no relation** to Web API versions, so the two
  backends must never share cached versions.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from .http import create_client
from .models import PaperRecord, PolicyActions

LOCAL_BASE_URL = "http://127.0.0.1:23119/api"
#: The local API serves the locally logged-in user as library ``0``.
LOCAL_USER_ID = "0"
#: Only API version 3 exists locally, and only one version at a time.
ZOTERO_API_VERSION = "3"
PAGE_SIZE = 100
#: Asks Zotero for a local write key; shows a confirmation dialog (Zotero 10+).
AUTHORIZE_PATH = "/local/authorize"
#: Shown in that dialog, so the user knows who is asking to modify the library.
APP_NAME = "jevero"

#: Item types that are not papers and must never be classified.
_NON_PAPER_TYPES = frozenset({"attachment", "note", "annotation"})

_YEAR_PATTERN = re.compile(r"(?<!\d)(1[5-9]\d{2}|20\d{2})(?!\d)")
_ARXIV_PATTERN = re.compile(
    r"arxiv[:\s/]*(?P<id>[a-z.-]+/\d{7}|\d{4}\.\d{4,5})", re.IGNORECASE
)

_ENABLE_LOCAL_API_HINT = (
    "enable it in Zotero: Settings -> Advanced -> "
    "'Allow other applications on this computer to communicate with Zotero'"
)


class ZoteroError(Exception):
    """Base class for Zotero failures."""


class ZoteroReadError(ZoteroError):
    """A read failed: transport, HTTP status, or an unexpected payload."""


class ZoteroWriteError(ZoteroError):
    """A write failed, was refused, or is not available on this Zotero."""


class ZoteroAuthorizationError(ZoteroWriteError):
    """Zotero would not grant a usable local write key."""


class ZoteroAuthorizationDeniedError(ZoteroAuthorizationError):
    """The user denied the Zotero write-authorization dialog."""


class ZoteroNotFoundError(ZoteroReadError):
    """A requested collection or item does not exist."""


class ZoteroCollectionAmbiguousError(ZoteroReadError):
    """More than one collection matches the configured name."""


@dataclass(frozen=True)
class ZoteroCollection:
    """One collection; nesting is expressed through ``parent_key``."""

    key: str
    name: str
    parent_key: str | None = None


def collection_path(
    collection: ZoteroCollection, by_key: Mapping[str, ZoteroCollection]
) -> str:
    """``Parent/Child`` path for a collection, for disambiguating names."""
    parts = [collection.name]
    parent_key = collection.parent_key
    seen = {collection.key}
    while parent_key and parent_key in by_key and parent_key not in seen:
        seen.add(parent_key)
        parent = by_key[parent_key]
        parts.append(parent.name)
        parent_key = parent.parent_key
    return "/".join(reversed(parts))


class ZoteroLocalApiDisabledError(ZoteroReadError):
    """The local API is switched off in Zotero's preferences."""


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
    full list that should end up on the item. It stays here, and unit tested,
    even while writes are unavailable: it is the part that must not be wrong
    once writes are enabled.
    """
    tags = {tag for tag in existing if tag}
    tags |= {tag for tag in actions.add_tags if tag}
    tags -= {tag for tag in actions.remove_tags if tag}
    return sorted(tags)


class ZoteroClient:
    """Read the local Zotero library through the desktop local API."""

    def __init__(
        self,
        *,
        base_url: str = LOCAL_BASE_URL,
        timeout_seconds: float = 30.0,
        app_name: str = APP_NAME,
        http_client: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.app_name = app_name
        self._http = http_client or create_client(timeout_seconds)
        self._owns_client = http_client is None
        self._server_id: str | None = None
        self._zotero_version: str | None = None
        self._write_key: str | None = None

    def __enter__(self) -> ZoteroClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_client:
            self._http.close()

    @property
    def supports_write(self) -> bool:
        """Whether this client implements tag writes at all.

        The flow is implemented, but whether the *running* Zotero allows it is
        a property of that instance: local writes exist in Zotero 10+ only. Call
        :meth:`ensure_writes_available` before doing work that assumes writes,
        so an unsupported Zotero fails fast instead of per paper.
        """
        return True

    @property
    def zotero_version(self) -> str | None:
        """Version string reported by the desktop app, if it reports one."""
        return self._zotero_version

    @property
    def server_id(self) -> str | None:
        """The desktop instance identity, cached from any response header."""
        return self._server_id

    @property
    def library_prefix(self) -> str:
        return f"/users/{LOCAL_USER_ID}"

    def list_collections(self) -> list[ZoteroCollection]:
        """Every collection in the library, nested ones included.

        The local API returns a flat list; nesting is expressed by
        ``parentCollection``, so callers get both the name and the parent key.
        """
        collections: list[ZoteroCollection] = []
        for raw in self._iter_pages(f"{self.library_prefix}/collections"):
            data = raw.get("data") or {}
            key = raw.get("key") or data.get("key")
            name = data.get("name")
            if not key or not isinstance(name, str):
                continue
            parent = data.get("parentCollection")
            collections.append(
                ZoteroCollection(
                    key=str(key),
                    name=name,
                    parent_key=str(parent) if parent else None,
                )
            )
        return collections

    def find_collection_key(self, name: str) -> str:
        """Resolve a configured collection to its key.

        ``name`` is matched against the collection name first, then against the
        full path such as ``02 Topics/Physics``. A name that matches more than
        one collection is an error rather than a silent first match: writing
        tags to the wrong collection's papers would be a real mistake.
        """
        collections = self.list_collections()
        by_key = {collection.key: collection for collection in collections}

        for describe, matches in (
            ("named", [c for c in collections if c.name == name]),
            ("at path", [c for c in collections if collection_path(c, by_key) == name]),
        ):
            if len(matches) == 1:
                return matches[0].key
            if len(matches) > 1:
                keys = ", ".join(sorted(c.key for c in matches))
                raise ZoteroCollectionAmbiguousError(
                    f"{len(matches)} collections are {describe} {name!r} "
                    f"(keys: {keys}); rename one, or use a full path like "
                    f"'Parent/Child'"
                )

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

    def ensure_writes_available(self) -> None:
        """Fail fast when the running Zotero cannot accept local writes.

        Local writes exist in Zotero 10+, which identifies itself with a
        ``Zotero-Server-ID`` response header. Zotero 9 and earlier expose the
        local API read-only, so ``--apply`` must refuse before spending a
        classifier request rather than discovering it per paper.
        """
        if self.server_id is not None:
            return
        self._probe_instance()
        if self.server_id is not None:
            return
        version = f" (Zotero {self._zotero_version})" if self._zotero_version else ""
        raise ZoteroWriteError(
            f"this Zotero{version} does not support local API writes: no "
            "Zotero-Server-ID header, which Zotero 10+ adds. Upgrade Zotero to "
            "10 or later to write tags locally, or restore the archived Web API "
            "client (archive/README.md) and use a zotero.org API key"
        )

    def authorize_writes(self, *, require_remember: bool = True) -> str:
        """Ask Zotero for a local write key, showing a confirmation dialog.

        The dialog is the user's consent, and it names this application. A key
        granted with "Allow" is single-use, which would mean one dialog per
        paper, so "Always Allow" is required by default; the granted key is kept
        in memory only and never written to disk.

        Raises:
            ZoteroAuthorizationDeniedError: the user pressed Deny.
            ZoteroAuthorizationError: single-use key, unsupported Zotero, or
                rate-limited dialogs.
        """
        server_id = self._require_server_id()
        response = self._request(
            "POST",
            AUTHORIZE_PATH,
            json={"appName": self.app_name},
            headers={"Zotero-Server-ID": server_id},
        )

        if response.status_code == 403:
            raise ZoteroAuthorizationDeniedError(
                "the Zotero confirmation dialog was denied; jevero cannot write "
                "tags without it"
            )
        if response.status_code == 404:
            raise ZoteroAuthorizationError(
                "this Zotero has no /api/local/authorize endpoint, so local "
                "writes are unsupported; Zotero 10+ is required"
            )
        if response.status_code == 429:
            raise ZoteroAuthorizationError(
                "Zotero is rate-limiting authorization dialogs (HTTP 429); wait "
                "a minute, then retry"
            )
        if response.status_code >= 400:
            raise ZoteroAuthorizationError(
                f"local write authorization failed with HTTP "
                f"{response.status_code}: {_snippet(response)}"
            )

        payload = _json_object(response, "authorization")
        key = payload.get("key")
        if not isinstance(key, str) or not key:
            raise ZoteroAuthorizationError(
                "Zotero did not return a local API key; refusing to write"
            )

        if require_remember and payload.get("remember") is not True:
            raise ZoteroAuthorizationError(
                'Zotero granted a single-use key because "Always Allow" was not '
                'chosen. jevero requires "Always Allow": a single-use key would '
                "show one confirmation dialog per paper. Re-run --apply and "
                'choose "Always Allow" in the Zotero dialog.'
            )

        self._write_key = key
        return key

    def apply_actions(self, item: ZoteroItem, actions: PolicyActions) -> list[str]:
        """Write tags for one item, preserving every unrelated existing tag.

        Authorizes on the first write (one Zotero dialog), then reuses the
        remembered key. A ``401`` means the key was consumed or revoked, so the
        write is retried once with a fresh authorization.
        """
        if actions.is_empty:
            return item.tags

        merged = merge_tags(item.tags, actions)
        if merged == sorted(item.tags):
            return item.tags

        server_id = self._require_server_id()
        if self._write_key is None:
            self.authorize_writes()
        assert self._write_key is not None  # set by authorize_writes

        response = self._patch_tags(item, merged, self._write_key, server_id)
        if response.status_code == 401:
            # Single-use or revoked key: authorize again and retry exactly once.
            self._write_key = None
            self.authorize_writes()
            assert self._write_key is not None
            response = self._patch_tags(item, merged, self._write_key, server_id)

        if response.status_code == 412:
            raise ZoteroWriteError(
                f"item {item.key} changed since it was read; re-run to retry"
            )
        if response.status_code == 401:
            raise ZoteroWriteError(
                f"Zotero still rejected the write to {item.key} after "
                "re-authorizing; check Settings -> Advanced -> "
                "'Clear Write Authorizations' and retry"
            )
        if response.status_code >= 400:
            raise ZoteroWriteError(
                f"writing tags to {item.key} failed with HTTP "
                f"{response.status_code}: {_snippet(response)}"
            )
        return merged

    def _patch_tags(
        self, item: ZoteroItem, tags: list[str], key: str, server_id: str
    ) -> httpx.Response:
        return self._request(
            "PATCH",
            f"{self.library_prefix}/items/{item.key}",
            json={"tags": [{"tag": tag} for tag in tags]},
            headers={
                "Zotero-API-Key": key,
                "Zotero-Server-ID": server_id,
                "If-Unmodified-Since-Version": str(item.version),
            },
        )

    def _require_server_id(self) -> str:
        self._probe_instance()
        if self._server_id is None:
            self.ensure_writes_available()
        assert self._server_id is not None
        return self._server_id

    def _probe_instance(self) -> None:
        """Read ``GET /api/`` once for the instance identity and version."""
        if self._server_id is not None or self._zotero_version is not None:
            return
        response = self._request("GET", "/")
        if response.status_code >= 400:
            raise ZoteroReadError(
                f"GET {self.base_url}/ failed with HTTP "
                f"{response.status_code}: {_snippet(response)}"
            )

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

    def _get_json(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        response = self._request("GET", path, params=params)
        if response.status_code == 403:
            raise ZoteroLocalApiDisabledError(
                f"Zotero refused {path} with 403 Forbidden; the local API is "
                f"probably disabled. To {_ENABLE_LOCAL_API_HINT}."
            )
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
        if headers:
            request_headers.update(headers)
        try:
            response = self._http.request(
                method,
                self.base_url + path,
                params=params,
                json=json,
                headers=request_headers,
            )
        except httpx.HTTPError as exc:
            raise ZoteroReadError(
                f"could not reach the Zotero local API at {self.base_url} "
                f"(is the Zotero desktop app running?): {exc}"
            ) from exc
        self._capture_instance(response)
        return response

    def _capture_instance(self, response: httpx.Response) -> None:
        """Cache the instance identity and version from any response.

        ``Zotero-Server-ID`` exists only in Zotero 10+, so its presence is also
        how write support is detected.
        """
        server_id = response.headers.get("Zotero-Server-ID")
        if server_id:
            self._server_id = server_id
        version = response.headers.get("X-Zotero-Version")
        if version:
            self._zotero_version = version


def _json_object(response: httpx.Response, what: str) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise ZoteroReadError(f"{what} response was not JSON") from exc
    if not isinstance(payload, dict):
        raise ZoteroReadError(
            f"{what} response must be a JSON object, got {type(payload).__name__}"
        )
    return payload


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
