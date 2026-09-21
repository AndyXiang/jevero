"""Shared fixtures. Policy tests never touch the network."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jevero.config import Config, load_config
from jevero.models import ClassificationResult

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def config_path() -> Path:
    return FIXTURES / "config_min.yaml"


@pytest.fixture
def config(config_path: Path) -> Config:
    return load_config(config_path)


@pytest.fixture
def zotero_item_payload() -> dict:
    return json.loads((FIXTURES / "zotero_item.json").read_text(encoding="utf-8"))


@pytest.fixture
def result_factory(config: Config):
    """Build a complete result: every configured name present, default 0.0.

    Coverage defaults to ``covered`` so a test that cares about one dimension
    does not accidentally trigger the coverage rules.
    """

    def _make(
        *,
        topics: dict[str, float] | None = None,
        kinds: dict[str, float] | None = None,
        coverage: dict[str, float] | None = None,
    ) -> ClassificationResult:
        default_coverage = {"covered": 0.9, "missing-topic": 0.05, "irrelevant": 0.05}
        return ClassificationResult(
            topics={name: 0.0 for name in config.topics} | (topics or {}),
            kinds={name: 0.0 for name in config.kinds} | (kinds or {}),
            coverage=default_coverage | (coverage or {}),
        )

    return _make
