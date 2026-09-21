"""HTTP client construction tests, including the NO_PROXY environment quirk."""

from __future__ import annotations

import pytest

from jevero.http import create_client


def test_create_client_builds_a_working_client():
    with create_client(5.0) as client:
        assert client.timeout.connect == 5.0


def test_bracketed_ipv6_no_proxy_entries_do_not_break_construction(
    monkeypatch: pytest.MonkeyPatch,
):
    """Regression test: httpx rejects ``[::1]`` in NO_PROXY and used to crash."""
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1,::1,[::1]")
    monkeypatch.setenv("no_proxy", "localhost,127.0.0.1,::1,[::1]")

    with create_client(5.0) as client:
        assert client is not None


def test_the_environment_is_restored_after_the_fallback(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("NO_PROXY", "localhost,[::1]")

    create_client(5.0).close()

    import os

    assert os.environ["NO_PROXY"] == "localhost,[::1]"
