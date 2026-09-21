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
from jevero.policy import PROCESSING_STATES
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
        self.created: list[str] = []
        self.membership: list[tuple[set[str], set[str]]] = []
        self._paths: dict[str, str] = {}
        # Distinct keys per collection name, so "is this the inbox?" is a real
        # question rather than an artefact of the fake.
        self._scope_keys: dict[str, str] = {"00 Inbox": COLLECTION_KEY}

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
        return self._scope_keys.setdefault(name, f"SCOPE{len(self._scope_keys):03d}")

    def collection_paths(self) -> dict[str, str]:
        return dict(self._paths)

    def collection_key_for_path(self, path: str) -> str | None:
        return self._paths.get(path)

    def ensure_collection_path(self, path: str) -> str:
        existing = self._paths.get(path)
        if existing:
            return existing
        self.created.append(path)
        key = f"KEY{len(self.created):05d}"
        self._paths[path] = key
        return key

    def apply_membership(self, item, *, add, remove) -> list[str]:
        self.membership.append((set(add), set(remove)))
        return sorted(set(item.data.get("collections") or []) | add - remove)

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
    # The plan reports the whole managed state, so a re-run clears any state tag
    # an older policy left behind instead of accumulating them.
    assert zotero.applied[0].remove_tags == PROCESSING_STATES - {"agent/processed"}


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


# --------------------------------------------------------------------------- #
# route: tags -> collections (no classifier involved)
# --------------------------------------------------------------------------- #

CLASSIFIED_ITEM = {
    "key": "ABCD2345",
    "version": 419,
    "data": {
        **ITEM["data"],
        "tags": [
            {"tag": "topic/loop-integrals"},
            {"tag": "agent/processed"},
            {"tag": "agent/review"},
            {"tag": "agent/review/ambiguous"},
        ],
        "collections": [COLLECTION_KEY],
    },
}


def test_route_dry_run_writes_nothing(monkeypatch, config_path: Path):
    zotero = FakeZotero(CLASSIFIED_ITEM)
    wire(monkeypatch, zotero, FakeJevClient())

    result = CliRunner().invoke(app, ["route", "--config", str(config_path)])

    assert result.exit_code == 0, result.output
    assert "02 Topics/loop-integrals" in result.output
    assert "04 Review" in result.output
    assert "does not exist yet" in result.output
    assert zotero.created == []
    assert zotero.membership == []


def test_route_apply_creates_targets_and_files_the_paper(
    monkeypatch, config_path: Path
):
    zotero = FakeZotero(CLASSIFIED_ITEM)
    wire(monkeypatch, zotero, FakeJevClient())

    result = CliRunner().invoke(app, ["route", "--config", str(config_path), "--apply"])

    assert result.exit_code == 0, result.output
    assert zotero.created == ["02 Topics/loop-integrals", "04 Review"]
    assert zotero.membership == [({"KEY00001", "KEY00002"}, set())]


def test_route_skips_papers_that_were_never_classified(monkeypatch, config_path: Path):
    zotero = FakeZotero(ITEM)  # no agent/processed tag
    wire(monkeypatch, zotero, FakeJevClient())

    result = CliRunner().invoke(app, ["route", "--config", str(config_path), "--apply"])

    assert result.exit_code == 0, result.output
    assert "0 routed, 1 unchanged" in result.output
    assert zotero.membership == []


def test_route_does_not_create_what_already_exists(monkeypatch, config_path: Path):
    zotero = FakeZotero(CLASSIFIED_ITEM)
    zotero._paths = {"02 Topics/loop-integrals": "EXIST111", "04 Review": "EXIST222"}
    wire(monkeypatch, zotero, FakeJevClient())

    result = CliRunner().invoke(app, ["route", "--config", str(config_path), "--apply"])

    assert result.exit_code == 0, result.output
    assert zotero.created == []
    assert zotero.membership == [({"EXIST111", "EXIST222"}, set())]


def test_route_can_remove_routed_papers_from_the_inbox(
    monkeypatch, config_path: Path, tmp_path: Path
):
    routed_config = tmp_path / "config.yaml"
    routed_config.write_text(
        config_path.read_text()
        + "\ncollections:\n  remove_from_inbox: true\n",
        encoding="utf-8",
    )
    zotero = FakeZotero(CLASSIFIED_ITEM)
    wire(monkeypatch, zotero, FakeJevClient())

    result = CliRunner().invoke(app, ["route", "--config", str(routed_config), "--apply"])

    assert result.exit_code == 0, result.output
    assert zotero.membership == [({"KEY00001", "KEY00002"}, {COLLECTION_KEY})]


def test_route_and_apply_flags_are_mutually_exclusive(config_path: Path):
    result = CliRunner().invoke(
        app, ["route", "--config", str(config_path), "--apply", "--dry-run"]
    )

    assert result.exit_code != 0


def test_route_prune_drops_managed_membership_the_tags_no_longer_point_at(
    monkeypatch, config_path: Path, tmp_path: Path
):
    """Convergence: the review tag is gone, so 04 Review must go too."""
    zotero = FakeZotero(CLASSIFIED_WITHOUT_REVIEW)
    zotero._paths = {
        "02 Topics/loop-integrals": "TOPIC111",
        "04 Review": "REVIEW11",
    }
    zotero._item["data"]["collections"] = [COLLECTION_KEY, "REVIEW11"]
    wire(monkeypatch, zotero, FakeJevClient())

    result = CliRunner().invoke(
        app, ["route", "--config", str(config_path), "--apply", "--prune"]
    )

    assert result.exit_code == 0, result.output
    assert zotero.membership == [({"TOPIC111"}, {"REVIEW11"})]


def test_route_prune_never_touches_collections_we_do_not_manage(
    monkeypatch, config_path: Path
):
    """A folder the reader made by hand must survive pruning."""
    zotero = FakeZotero(CLASSIFIED_WITHOUT_REVIEW)
    zotero._paths = {
        "02 Topics/loop-integrals": "TOPIC111",
        "04 Review": "REVIEW11",
        "01 Projects/AmpNet": "MINE0001",
    }
    zotero._item["data"]["collections"] = [COLLECTION_KEY, "REVIEW11", "MINE0001"]
    wire(monkeypatch, zotero, FakeJevClient())

    result = CliRunner().invoke(
        app, ["route", "--config", str(config_path), "--apply", "--prune"]
    )

    assert result.exit_code == 0, result.output
    add, remove = zotero.membership[0]
    assert add == {"TOPIC111"}
    assert remove == {"REVIEW11"}
    assert "MINE0001" not in add | remove


def test_route_without_prune_keeps_stale_membership(
    monkeypatch, config_path: Path
):
    zotero = FakeZotero(CLASSIFIED_WITHOUT_REVIEW)
    zotero._paths = {"02 Topics/loop-integrals": "TOPIC111", "04 Review": "REVIEW11"}
    zotero._item["data"]["collections"] = [COLLECTION_KEY, "REVIEW11"]
    wire(monkeypatch, zotero, FakeJevClient())

    result = CliRunner().invoke(
        app, ["route", "--config", str(config_path), "--apply"]
    )

    assert result.exit_code == 0, result.output
    add, remove = zotero.membership[0]
    assert add == {"TOPIC111"}
    # Without --prune the stale review membership is left alone.
    assert remove == set()


def test_route_prune_dry_run_shows_the_removal_but_writes_nothing(
    monkeypatch, config_path: Path
):
    zotero = FakeZotero(CLASSIFIED_WITHOUT_REVIEW)
    zotero._paths = {"02 Topics/loop-integrals": "TOPIC111", "04 Review": "REVIEW11"}
    zotero._item["data"]["collections"] = [COLLECTION_KEY, "REVIEW11"]
    wire(monkeypatch, zotero, FakeJevClient())

    result = CliRunner().invoke(
        app, ["route", "--config", str(config_path), "--prune"]
    )

    assert result.exit_code == 0, result.output
    assert "- 04 Review" in result.output
    assert zotero.membership == []
    assert zotero.created == []


CLASSIFIED_WITHOUT_REVIEW = {
    "key": "ABCD2345",
    "version": 420,
    "data": {
        **ITEM["data"],
        "tags": [{"tag": "topic/loop-integrals"}, {"tag": "agent/processed"}],
        "collections": [COLLECTION_KEY],
    },
}


# --------------------------------------------------------------------------- #
# wider scopes: --collection and --all, and the confirmation they require
# --------------------------------------------------------------------------- #


def test_all_scope_asks_for_confirmation_and_aborts_by_default(
    monkeypatch, config_path: Path, confidence: JevOutcome
):
    zotero = FakeZotero(ITEM)
    jev = FakeJevClient(outcome=confidence)
    wire(monkeypatch, zotero, jev)

    result = CliRunner().invoke(
        app, ["process", "--config", str(config_path), "--all", "--apply"], input="\n"
    )

    assert result.exit_code == 1
    assert "WARNING" in result.output
    assert "the whole library" in result.output
    assert zotero.applied == []
    assert jev.calls == 0  # refused before spending anything


def test_all_scope_proceeds_when_confirmed(
    monkeypatch, config_path: Path, confidence: JevOutcome
):
    zotero = FakeZotero(ITEM)
    jev = FakeJevClient(outcome=confidence)
    wire(monkeypatch, zotero, jev)

    result = CliRunner().invoke(
        app, ["process", "--config", str(config_path), "--all", "--apply"], input="y\n"
    )

    assert result.exit_code == 0, result.output
    assert "WARNING" in result.output
    assert len(zotero.applied) == 1
    assert jev.calls == 1


def test_collection_scope_also_warns(config_path: Path, monkeypatch, confidence: JevOutcome):
    zotero = FakeZotero(ITEM)
    wire(monkeypatch, zotero, FakeJevClient(outcome=confidence))

    result = CliRunner().invoke(
        app,
        [
            "process",
            "--config",
            str(config_path),
            "--collection",
            "02 Topics/Physics",
            "--apply",
        ],
        input="n\n",
    )

    assert result.exit_code == 1
    assert "02 Topics/Physics" in result.output
    assert zotero.applied == []


def test_inbox_scope_never_warns(monkeypatch, config_path: Path, confidence: JevOutcome):
    zotero = FakeZotero(ITEM)
    wire(monkeypatch, zotero, FakeJevClient(outcome=confidence))

    result = CliRunner().invoke(
        app, ["process", "--config", str(config_path), "--apply"]
    )

    assert result.exit_code == 0, result.output
    assert "WARNING" not in result.output


def test_dry_run_on_the_whole_library_does_not_ask_anything(
    monkeypatch, config_path: Path, confidence: JevOutcome
):
    zotero = FakeZotero(ITEM)
    wire(monkeypatch, zotero, FakeJevClient(outcome=confidence))

    result = CliRunner().invoke(
        app, ["process", "--config", str(config_path), "--all", "--dry-run"]
    )

    assert result.exit_code == 0, result.output
    assert "WARNING" not in result.output
    assert "the whole library" in result.output
    assert zotero.applied == []


def test_collection_and_all_are_mutually_exclusive(config_path: Path):
    result = CliRunner().invoke(
        app,
        ["process", "--config", str(config_path), "--all", "--collection", "X", "--dry-run"],
    )

    assert result.exit_code != 0


def test_route_all_scope_warns_and_aborts(
    monkeypatch, config_path: Path
):
    zotero = FakeZotero(CLASSIFIED_ITEM)
    wire(monkeypatch, zotero, FakeJevClient())

    result = CliRunner().invoke(
        app, ["route", "--config", str(config_path), "--all", "--apply"], input="\n"
    )

    assert result.exit_code == 1
    assert "WARNING" in result.output
    assert zotero.membership == []
    assert zotero.created == []


def test_route_all_scope_applies_when_confirmed(monkeypatch, config_path: Path):
    zotero = FakeZotero(CLASSIFIED_ITEM)
    wire(monkeypatch, zotero, FakeJevClient())

    result = CliRunner().invoke(
        app, ["route", "--config", str(config_path), "--all", "--apply"], input="y\n"
    )

    assert result.exit_code == 0, result.output
    assert zotero.membership == [({"KEY00001", "KEY00002"}, set())]


def test_client_picks_up_a_stored_write_key(monkeypatch, config: Config):
    monkeypatch.setenv("ZOTERO_LOCAL_WRITE_KEY", "stored-key")

    client = main_module._zotero_client(config)

    assert client._write_key == "stored-key"


def test_a_granted_key_is_written_to_env(monkeypatch, tmp_path: Path):
    """The callback that makes the next run silent."""
    monkeypatch.chdir(tmp_path)

    main_module._remember_write_key("fresh-key")

    assert (tmp_path / ".env").read_text(encoding="utf-8").startswith(
        "ZOTERO_LOCAL_WRITE_KEY=fresh-key"
    )
