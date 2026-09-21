"""Shared HTTP client construction.

A boring helper, but a necessary one: both clients need the same timeout, and
both have to survive a real environment quirk.

Some shells export ``NO_PROXY`` with bracketed IPv6 entries such as ``[::1]``.
httpx parses ``NO_PROXY`` through its URL parser and rejects those entries, so
``httpx.Client()`` raises ``InvalidURL`` before a single request is made. We
retry once with the unreadable entries dropped, which keeps proxy use and
localhost bypass intact.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

import httpx

_PROXY_ENV_VARS = ("NO_PROXY", "no_proxy")


def create_client(timeout_seconds: float) -> httpx.Client:
    """Build an ``httpx.Client``, tolerating an unparseable ``NO_PROXY``."""
    try:
        return httpx.Client(timeout=timeout_seconds)
    except httpx.InvalidURL:
        with _sanitized_proxy_environment():
            return httpx.Client(timeout=timeout_seconds)


@contextmanager
def _sanitized_proxy_environment() -> Iterator[None]:
    saved = {name: os.environ.get(name) for name in _PROXY_ENV_VARS}
    try:
        for name, value in saved.items():
            if value is None:
                continue
            os.environ[name] = ",".join(
                entry
                for entry in (raw.strip() for raw in value.split(","))
                if entry and not entry.startswith("[")
            )
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
