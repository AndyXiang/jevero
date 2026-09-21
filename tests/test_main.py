"""End-to-end pipeline tests: dry-run must not mutate anything.

The Zotero side is a fake implementing the same small surface ``main`` uses.
The real local client cannot write yet, so the apply path is exercised against
the fake while the client's own refusal is tested in ``test_zotero.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from jevero import main as main_module
from jevero.config import Config
from jevero.jev import JevOutcome, JevTransportError
from jevero.main import app
from jevero.models import PolicyActions, Usage
from jevero.zotero import (
    ZoteroClient,
    ZoteroCollection,
    ZoteroError,
    ZoteroItem,
    ZoteroWriteError,
)

COLLECTION_KEY = "INBOX123"

ITEM = {
    "key": "ABCD2345",
    "version": 417,
    "data": {
        "key": "ABCD2345",
        "version": 417,
        "itemType": "journalArticle",
        "title": "Energy Correlators in Heavy Quarkonium Production",
        "abstractNote": "We study energy correlators for quarkonium production.",
        "creators": [{"creatorType": "author", "firstName": "Ada", "lastName": "Lovelace"}],
        "date": "2019",
        "tags": [{"tag": "to-read"}, {"tag": "my own tag"}],
    },
}

ITEM_WITHOUT_ABSTRACT = {
    "key": "ABCD2345",
    "version": 417,
    "data": {**ITEM["data"], "abstractNote": ""},
}

ITEM_ALREADY_PROCESSED = {
    "key": "ABCD2345",
    "version": 418,
    "data": {**ITEM["data"], "tags": [{"tag": "agent/processed"}]},
}


class FakeJevClient:
    """Stands in for the classifier; optionally fails."""

    def __init__(self, outcome: JevOutcome | None = None, error: Exception | None = None):
        self._outcome = outcome
        self._error = error
        self.calls = 0

    def classify(self, paper, config):
        self.calls += 1
        if self._error is not None:
            raise self._error
        assert self._outcome is not None
        return self._outcome

    def __enter__(self):
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def close(self) -> None:
        return None


class FakeZotero:
    """Records the actions ``main`` would write, without writing anything."""

    def __init__(
        self,
        item: dict,
        *,
        supports_write: bool = True,
        writes_available: bool = True,
    ):
        self._item = item
        self._supports_write = supports_write
        self._writes_available = writes_available
        self.applied: list[PolicyActions] = []
        self.read_paths: list[str] = []

    @property
    def supports_write(self) -> bool:
        return self._supports_write

    @property
    def item(self) -> ZoteroItem:
        return ZoteroItem(
            key=self._item["key"], version=self._item["version"], data=self._item["data"]
        )

    def list_collections(self):
        self.read_paths.append("collections")
        return [
            ZoteroCollection(key=COLLECTION_KEY, name="00 Inbox"),
            ZoteroCollection(key="TOPICS11", name="02 Topics"),
            ZoteroCollection(key="PHYS2222", name="Physics", parent_key="TOPICS11"),
        ]

    def find_collection_key(self, name: str) -> str:
        self.read_paths.append(f"collections:{name}")
        return COLLECTION_KEY

    def iter_papers(self, *, collection_key=None, limit=None):
        self.read_paths.append(f"items:{collection_key}")
        yield self.item

    def get_item(self, key: str) -> ZoteroItem:
        self.read_paths.append(f"item:{key}")
        return self.item

    def ensure_writes_available(self) -> None:
        if not self._writes_available:
            raise ZoteroWriteError(
                "this Zotero (Zotero 9.0.6) does not support local API writes"
            )

    def apply_actions(self, item: ZoteroItem, actions: PolicyActions) -> list[str]:
        self.applied.append(actions)
        return sorted(set(item.tags) | actions.add_tags)

    def __enter__(self) -> FakeZotero:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def close(self) -> None:
        return None


@pytest.fixture
def confidence(config: Config) -> JevOutcome:
    from jevero.jev import build_questions, parse_answers, question_key

    answers = {key: {"type": "noul", "noul": 0.05} for key in build_questions(config)}
    answers[question_key("topic", "quarkonium")] = {"type": "noul", "noul": 0.96}
    answers[question_key("role", "core")] = {"type": "noul", "noul": 0.88}
    answers[question_key("coverage", "covered")] = {"type": "noul", "noul": 0.95}

    return JevOutcome(
        result=parse_answers(answers, config),
        model="typesafe/jev-1.13-20260917",
        provider="TypeSafe",
        request_id="gen-dec-1",
        usage=Usage(input_tokens=476, output_tokens=70, cost=0.00002),
    )


def wire(monkeypatch, zotero: FakeZotero, jev: FakeJevClient) -> None:
    monkeypatch.setattr(main_module, "_zotero_client", lambda _config: zotero)
    monkeypatch.setattr(main_module, "_jev_client", lambda _config: jev)


def test_dry_run_performs_no_mutations(
    monkeypatch, config_path: Path, confidence: JevOutcome
):
    zotero = FakeZotero(ITEM)
    wire(monkeypatch, zotero, FakeJevClient(outcome=confidence))

    result = CliRunner().invoke(
        app, ["process", "--config", str(config_path), "--dry-run"]
    )

    assert result.exit_code == 0, result.output
    assert zotero.applied == []
    assert "topic/quarkonium" in result.output
    assert "APPLY" in result.output
    assert "no Zotero changes will be made" in result.output


def test_apply_writes_only_policy_tags(
    monkeypatch, config_path: Path, confidence: JevOutcome
):
    zotero = FakeZotero(ITEM)
    wire(monkeypatch, zotero, FakeJevClient(outcome=confidence))

    result = CliRunner().invoke(app, ["process", "--config", str(config_path), "--apply"])

    assert result.exit_code == 0, result.output
    assert len(zotero.applied) == 1
    assert zotero.applied[0].add_tags == {
        "agent/processed",
        "role/core",
        "topic/quarkonium",
    }
    assert zotero.applied[0].remove_tags == set()


def test_apply_fails_fast_when_writes_are_unavailable(
    monkeypatch, config_path: Path, confidence: JevOutcome
):
    """The local client cannot authorize writes yet, so --apply must refuse."""
    zotero = FakeZotero(ITEM, supports_write=False)
    wire(monkeypatch, zotero, FakeJevClient(outcome=confidence))

    result = CliRunner().invoke(app, ["process", "--config", str(config_path), "--apply"])

    assert result.exit_code == 2
    assert "cannot write" in result.output
    assert zotero.applied == []


def test_apply_reports_an_unsupported_zotero_before_classifying(
    monkeypatch, config_path: Path
):
    """Zotero < 10 must be reported before any classifier spend."""
    zotero = FakeZotero(ITEM, writes_available=False)
    jev = FakeJevClient(error=JevTransportError("must not be called"))
    wire(monkeypatch, zotero, jev)

    result = CliRunner().invoke(app, ["process", "--config", str(config_path), "--apply"])

    assert result.exit_code == 2
    assert "does not support local API writes" in result.output
    assert jev.calls == 0
    assert zotero.applied == []


def test_dry_run_does_not_require_writable_zotero(
    monkeypatch, config_path: Path, confidence: JevOutcome
):
    zotero = FakeZotero(ITEM, writes_available=False)
    wire(monkeypatch, zotero, FakeJevClient(outcome=confidence))

    result = CliRunner().invoke(
        app, ["process", "--config", str(config_path), "--dry-run"]
    )

    assert result.exit_code == 0, result.output


def test_apply_and_dry_run_together_are_refused(config_path: Path):
    result = CliRunner().invoke(
        app, ["process", "--config", str(config_path), "--apply", "--dry-run"]
    )

    assert result.exit_code != 0


def test_missing_abstract_is_skipped_and_flagged_not_processed(
    monkeypatch, config_path: Path, confidence: JevOutcome
):
    zotero = FakeZotero(ITEM_WITHOUT_ABSTRACT)
    wire(monkeypatch, zotero, FakeJevClient(outcome=confidence))

    result = CliRunner().invoke(app, ["process", "--config", str(config_path), "--apply"])

    assert result.exit_code == 0, result.output
    assert "skipped: no abstract" in result.output
    assert zotero.applied[0].add_tags == {
        "agent/review",
        "agent/review/missing-abstract",
    }
    # Never marked processed, so the paper is retried once metadata arrives.
    assert "agent/processed" not in zotero.applied[0].add_tags


def test_classifier_failure_is_reported_and_marked(monkeypatch, config_path: Path):
    zotero = FakeZotero(ITEM)
    wire(monkeypatch, zotero, FakeJevClient(error=JevTransportError("upstream down")))

    result = CliRunner().invoke(app, ["process", "--config", str(config_path), "--apply"])

    assert result.exit_code == 1
    assert "classification failed" in result.output
    assert zotero.applied[0].add_tags == {"agent/error"}
    assert "agent/processed" in zotero.applied[0].remove_tags


def test_already_processed_papers_are_skipped(
    monkeypatch, config_path: Path, confidence: JevOutcome
):
    zotero = FakeZotero(ITEM_ALREADY_PROCESSED)
    wire(monkeypatch, zotero, FakeJevClient(outcome=confidence))

    result = CliRunner().invoke(app, ["process", "--config", str(config_path), "--apply"])

    assert result.exit_code == 0, result.output
    assert zotero.applied == []


def test_include_processed_reconsiders_them(
    monkeypatch, config_path: Path, confidence: JevOutcome
):
    zotero = FakeZotero(ITEM_ALREADY_PROCESSED)
    wire(monkeypatch, zotero, FakeJevClient(outcome=confidence))

    result = CliRunner().invoke(
        app,
        ["process", "--config", str(config_path), "--apply", "--include-processed"],
    )

    assert result.exit_code == 0, result.output
    assert len(zotero.applied) == 1


def test_collections_command_lists_paths_and_marks_the_configured_one(
    monkeypatch, config_path: Path
):
    zotero = FakeZotero(ITEM)
    wire(monkeypatch, zotero, FakeJevClient())

    result = CliRunner().invoke(
        app, ["collections", "--config", str(config_path), "--no-counts"]
    )

    assert result.exit_code == 0, result.output
    assert "zotero.inbox_collection = '00 Inbox'" in result.output
    assert "02 Topics/Physics" in result.output
    assert "<- configured" in result.output


def test_setup_failure_exits_cleanly_without_a_traceback(
    monkeypatch, config_path: Path
):
    def unavailable(_config: Config) -> ZoteroClient:
        raise ZoteroError("could not reach the Zotero local API")

    monkeypatch.setattr(main_module, "_zotero_client", unavailable)

    result = CliRunner().invoke(
        app, ["process", "--config", str(config_path), "--dry-run"]
    )

    assert result.exit_code == 2
    assert "could not reach the Zotero local API" in result.output
    assert "Traceback" not in result.output
