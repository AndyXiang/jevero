"""End-to-end pipeline tests: dry-run must not mutate anything."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from jevero import main as main_module
from jevero.config import Config
from jevero.jev import JevOutcome, JevTransportError
from jevero.main import app
from jevero.models import Usage
from jevero.zotero import ZoteroClient, ZoteroError

LIBRARY_ID = "123456"
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


class FakeJevClient:
    """Stands in for the classifier; optionally fails."""

    def __init__(self, outcome: JevOutcome | None = None, error: Exception | None = None):
        self._outcome = outcome
        self._error = error

    def classify(self, paper, config):
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


class RecordingZotero:
    """A real ZoteroClient over a mock transport, recording every request."""

    def __init__(self, item: dict):
        self.requests: list[httpx.Request] = []
        self._item = item

    def _handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == f"/users/{LIBRARY_ID}/collections":
            return httpx.Response(
                200, json=[{"key": COLLECTION_KEY, "data": {"name": "00 Inbox"}}]
            )
        if path == f"/users/{LIBRARY_ID}/collections/{COLLECTION_KEY}/items/top":
            return httpx.Response(200, json=[self._item])
        if path == f"/users/{LIBRARY_ID}/items/{self._item['key']}":
            return httpx.Response(200, json=self._item)
        if request.method == "PATCH":
            return httpx.Response(204)
        return httpx.Response(404, text="unexpected path")

    @property
    def writes(self) -> list[httpx.Request]:
        return [request for request in self.requests if request.method == "PATCH"]

    def client(self) -> ZoteroClient:
        return ZoteroClient(
            library_type="user",
            library_id=LIBRARY_ID,
            api_key="zotero-key",
            backend="web",
            http_client=httpx.Client(transport=httpx.MockTransport(self._handler)),
        )


@pytest.fixture
def confidence(config: Config) -> JevOutcome:
    from jevero.jev import build_questions, question_key

    answers = {
        key: {"type": "noul", "noul": 0.05} for key in build_questions(config)
    }
    answers[question_key("topic", "quarkonium")] = {"type": "noul", "noul": 0.96}
    answers[question_key("role", "core")] = {"type": "noul", "noul": 0.88}
    answers[question_key("coverage", "covered")] = {"type": "noul", "noul": 0.95}

    from jevero.jev import parse_answers

    return JevOutcome(
        result=parse_answers(answers, config),
        model="typesafe/jev-1.13-20260917",
        provider="TypeSafe",
        request_id="gen-dec-1",
        usage=Usage(input_tokens=476, output_tokens=70, cost=0.00002),
    )


def wire(monkeypatch, config: Config, zotero: RecordingZotero, jev: FakeJevClient) -> None:
    monkeypatch.setattr(main_module, "_zotero_client", lambda _config: zotero.client())
    monkeypatch.setattr(main_module, "_jev_client", lambda _config: jev)


def test_dry_run_performs_no_mutations(
    monkeypatch, config: Config, config_path: Path, confidence: JevOutcome
):
    zotero = RecordingZotero(ITEM)
    wire(monkeypatch, config, zotero, FakeJevClient(outcome=confidence))

    result = CliRunner().invoke(
        app, ["process", "--config", str(config_path), "--dry-run"]
    )

    assert result.exit_code == 0, result.output
    assert zotero.writes == []
    assert "topic/quarkonium" in result.output
    assert "APPLY" in result.output
    assert "no Zotero changes will be made" in result.output


def test_apply_writes_only_policy_tags(
    monkeypatch, config: Config, config_path: Path, confidence: JevOutcome
):
    zotero = RecordingZotero(ITEM)
    wire(monkeypatch, config, zotero, FakeJevClient(outcome=confidence))

    result = CliRunner().invoke(app, ["process", "--config", str(config_path), "--apply"])

    assert result.exit_code == 0, result.output
    assert len(zotero.writes) == 1
    body = json.loads(zotero.writes[0].content)
    tags = [entry["tag"] for entry in body["tags"]]
    assert tags == [
        "agent/processed",
        "my own tag",
        "role/core",
        "to-read",
        "topic/quarkonium",
    ]


def test_apply_and_dry_run_together_are_refused(config_path: Path):
    result = CliRunner().invoke(
        app, ["process", "--config", str(config_path), "--apply", "--dry-run"]
    )

    assert result.exit_code != 0


def test_missing_abstract_is_skipped_and_flagged_not_processed(
    monkeypatch, config: Config, config_path: Path, confidence: JevOutcome
):
    zotero = RecordingZotero(ITEM_WITHOUT_ABSTRACT)
    wire(monkeypatch, config, zotero, FakeJevClient(outcome=confidence))

    result = CliRunner().invoke(app, ["process", "--config", str(config_path), "--apply"])

    assert result.exit_code == 0, result.output
    assert "skipped: no abstract" in result.output
    tags = [entry["tag"] for entry in json.loads(zotero.writes[0].content)["tags"]]
    assert tags == [
        "agent/review",
        "agent/review/missing-abstract",
        "my own tag",
        "to-read",
    ]


def test_classifier_failure_is_reported_and_marked(
    monkeypatch, config: Config, config_path: Path
):
    zotero = RecordingZotero(ITEM)
    wire(monkeypatch, config, zotero, FakeJevClient(error=JevTransportError("upstream down")))

    result = CliRunner().invoke(app, ["process", "--config", str(config_path), "--apply"])

    assert result.exit_code == 1
    assert "classification failed" in result.output
    tags = [entry["tag"] for entry in json.loads(zotero.writes[0].content)["tags"]]
    assert tags == ["agent/error", "my own tag", "to-read"]


def test_already_processed_papers_are_skipped(
    monkeypatch, config: Config, config_path: Path, confidence: JevOutcome
):
    processed = {
        "key": "ABCD2345",
        "version": 418,
        "data": {**ITEM["data"], "tags": [{"tag": "agent/processed"}]},
    }
    zotero = RecordingZotero(processed)
    wire(monkeypatch, config, zotero, FakeJevClient(outcome=confidence))

    result = CliRunner().invoke(app, ["process", "--config", str(config_path), "--apply"])

    assert result.exit_code == 0, result.output
    assert zotero.writes == []


def test_setup_failure_exits_cleanly_without_a_traceback(
    monkeypatch, config: Config, config_path: Path
):
    def unavailable(_config: Config) -> ZoteroClient:
        raise ZoteroError("no Zotero library ID; set ZOTERO_LIBRARY_ID")

    monkeypatch.setattr(main_module, "_zotero_client", unavailable)

    result = CliRunner().invoke(
        app, ["process", "--config", str(config_path), "--dry-run"]
    )

    assert result.exit_code == 2
    assert "no Zotero library ID" in result.output
    assert "Traceback" not in result.output
