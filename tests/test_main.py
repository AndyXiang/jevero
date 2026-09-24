"""End-to-end pipeline tests: dry-run must not mutate anything.

The Zotero side is a fake implementing the same small surface ``main`` uses.
The real local client cannot write yet, so the apply path is exercised against
the fake while the client's own refusal is tested in ``test_zotero.py``.
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
from typer.testing import CliRunner

from jevero import main as main_module
from jevero.config import Config
from jevero.jev import JevOutcome, JevResponseError, JevTransportError
from jevero.main import app
from jevero.models import PolicyActions, Usage
from jevero.policy import EXTRA_ERROR, EXTRA_FINGERPRINT
from jevero.zotero import (
    ZoteroClient,
    ZoteroCollection,
    ZoteroError,
    ZoteroItem,
    ZoteroWriteError,
    merge_extra,
    merge_tags,
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
        "collections": [COLLECTION_KEY],
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
    "data": {
        **ITEM["data"],
        "extra": "jevero-fingerprint: oldstamp",  # judged, but by an older setup
    },
}


def up_to_date_item(config_path: Path, key: str = "ABCD2345") -> dict:
    """A processed item whose stamp matches the configuration: nothing to do."""
    from jevero.config import load_config as _load_config
    from jevero.jev import fingerprint

    stamp = fingerprint(_load_config(config_path))
    return {
        "key": key,
        "version": 600,
        "data": {
            **ITEM["data"],
            "key": key,
            "extra": f"jevero-fingerprint: {stamp}",
        },
    }


class FakeJevClient:
    """Stands in for the classifier; optionally fails."""

    def __init__(self, outcome: JevOutcome | None = None, error: Exception | None = None):
        self._outcome = outcome
        self._error = error
        self.calls = 0
        self.texts: list[str] = []

    def classify(self, paper, config, *, full_text=""):
        self.calls += 1
        self.texts.append(full_text)
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
        extra_items: tuple[dict, ...] = (),
        supports_write: bool = True,
        writes_available: bool = True,
    ):
        # Deep-copied: the fixtures are module globals, and this fake now
        # mutates the item when a write happens.
        self._item = copy.deepcopy(item)
        self._extra = [copy.deepcopy(other) for other in extra_items]
        self._supports_write = supports_write
        self._writes_available = writes_available
        #: Indexed text per item key, as Zotero would report it.
        self.full_texts: dict[str, str] = {}
        self.text_reads: list[str] = []
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
        keys = (set(item.data.get("collections") or []) | add) - remove
        self._item["data"]["collections"] = sorted(keys)
        return sorted(keys)

    def iter_papers(self, *, collection_key=None, limit=None):
        self.read_paths.append(f"items:{collection_key}")
        yielded = 0
        for row in [self._item, *self._extra]:
            membership = row["data"].get("collections") or []
            if collection_key is not None and collection_key not in membership:
                continue
            yield ZoteroItem(key=row["key"], version=row["version"], data=row["data"])
            yielded += 1
            if limit is not None and yielded >= limit:
                return

    def full_text(self, key: str) -> str:
        self.text_reads.append(key)
        return self.full_texts.get(key, "")

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
        row = next(
            r for r in [self._item, *self._extra] if r["key"] == item.key
        )
        tags = merge_tags(item.tags, actions)
        row["data"]["tags"] = [{"tag": tag} for tag in tags]
        if actions.extra:
            row["data"]["extra"] = merge_extra(row["data"].get("extra"), actions.extra)
        return tags

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
    answers[question_key("kind", "core")] = {"type": "noul", "noul": 0.88}
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
        app, ["process", "--config", str(config_path), "--dry-run", "--verbose"]
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
    assert zotero.applied[0].add_tags == {"kind/core", "topic/quarkonium"}
    # The judgement stamp goes to `extra`, out of the tag panel, and any earlier
    # failure is cleared by this success.
    assert zotero.applied[0].extra[EXTRA_FINGERPRINT]
    assert zotero.applied[0].extra[EXTRA_ERROR] is None


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
    assert zotero.applied[0].add_tags == {"review/missing-abstract"}
    # Never stamped, so the paper is retried once metadata arrives.
    assert zotero.applied[0].extra[EXTRA_FINGERPRINT] is None


def test_a_bad_model_answer_marks_only_that_paper(monkeypatch, config_path: Path):
    """A malformed answer is this paper's problem, so it is marked and skipped."""
    zotero = FakeZotero(ITEM)
    wire(monkeypatch, zotero, FakeJevClient(error=JevResponseError("no answer")))

    result = CliRunner().invoke(app, ["process", "--config", str(config_path), "--apply"])

    assert result.exit_code == 1
    assert "classification failed" in result.output
    assert zotero.applied[0].add_tags == set()
    assert zotero.applied[0].extra[EXTRA_FINGERPRINT] is None
    assert "no answer" in (zotero.applied[0].extra[EXTRA_ERROR] or "")


def test_an_unreachable_classifier_aborts_and_marks_nothing(
    monkeypatch, config_path: Path
):
    """A transport failure says nothing about the paper, so nothing is written."""
    zotero = FakeZotero(ITEM)
    wire(monkeypatch, zotero, FakeJevClient(error=JevTransportError("ssl eof")))

    result = CliRunner().invoke(app, ["process", "--config", str(config_path), "--apply"])

    assert result.exit_code == 2
    assert "classifier unreachable" in result.output
    assert "no paper was marked failed" in result.output
    assert zotero.applied == []


def test_already_processed_papers_are_skipped(
    monkeypatch, config_path: Path, confidence: JevOutcome
):
    """Up to date means nothing to do: no classifier call, no write."""
    zotero = FakeZotero(up_to_date_item(config_path))
    jev = FakeJevClient(outcome=confidence)
    wire(monkeypatch, zotero, jev)

    result = CliRunner().invoke(app, ["process", "--config", str(config_path), "--apply"])

    assert result.exit_code == 0, result.output
    assert zotero.applied == []
    assert jev.calls == 0


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


STALE_ITEM = {
    "key": "EFGH6789",
    "version": 500,
    "data": {
        **ITEM["data"],
        "key": "EFGH6789",
        "extra": "jevero-fingerprint: oldstamp",
        "collections": [],  # routed out of the inbox; staleness must still find it
    },
}

#: Judged before fingerprints existed: the old namespace, and no stamp at all.
UNSTAMPED_ITEM = {
    "key": "IJKL0123",
    "version": 502,
    "data": {
        **ITEM["data"],
        "key": "IJKL0123",
        "tags": [{"tag": "agent/processed"}],
        "collections": [],
    },
}


def test_a_processed_paper_without_a_stamp_counts_as_out_of_date(
    monkeypatch, config_path: Path, confidence: JevOutcome
):
    """Otherwise the first run after stamps were introduced would do nothing."""
    zotero = FakeZotero(ITEM, extra_items=(UNSTAMPED_ITEM,))
    wire(monkeypatch, zotero, FakeJevClient(outcome=confidence))

    result = CliRunner().invoke(
        app, ["process", "--config", str(config_path), "--dry-run"]
    )

    assert result.exit_code == 0, result.output
    assert "Candidates: 2 papers" in result.output
    assert "1 of them were judged with another model" in result.output


def test_an_out_of_date_paper_is_re_judged_without_asking(
    monkeypatch, config_path: Path, confidence: JevOutcome
):
    """A vocabulary, prompt, or model change reaches the papers it invalidated.

    The stale paper is not in the inbox, which is the point: routing is what moves
    papers out of the inbox, so an inbox-only scope would never re-judge them.
    """
    zotero = FakeZotero(ITEM, extra_items=(STALE_ITEM,))
    wire(monkeypatch, zotero, FakeJevClient(outcome=confidence))

    result = CliRunner().invoke(
        app, ["process", "--config", str(config_path), "--dry-run", "--verbose"]
    )

    assert result.exit_code == 0, result.output
    assert "Candidates: 2 papers" in result.output
    assert "1 of them were judged with another model" in result.output
    assert "was oldstamp" in result.output  # the superseded stamp is shown


def test_an_up_to_date_paper_is_left_alone(
    monkeypatch, config_path: Path, confidence: JevOutcome
):
    """No configuration change means nothing to do, and no spend."""
    zotero = FakeZotero(up_to_date_item(config_path, key="EFGH6789"))
    jev = FakeJevClient(outcome=confidence)
    wire(monkeypatch, zotero, jev)

    result = CliRunner().invoke(
        app, ["process", "--config", str(config_path), "--dry-run"]
    )

    assert result.exit_code == 0, result.output
    assert "Candidates: 0 papers" in result.output
    assert jev.calls == 0


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
            {"tag": "topic/quarkonium"},
            {"tag": "review/ambiguous"},
        ],
        "extra": "jevero-fingerprint: abc12345",
        "collections": [COLLECTION_KEY],
    },
}


def test_route_dry_run_writes_nothing(monkeypatch, config_path: Path):
    zotero = FakeZotero(CLASSIFIED_ITEM)
    wire(monkeypatch, zotero, FakeJevClient())

    result = CliRunner().invoke(app, ["route", "--config", str(config_path)])

    assert result.exit_code == 0, result.output
    assert "02 Topics/quarkonium" in result.output
    assert "04 Review" in result.output
    assert "(new)" in result.output
    assert zotero.created == []
    assert zotero.membership == []


def test_route_apply_creates_targets_and_files_the_paper(
    monkeypatch, config_path: Path
):
    zotero = FakeZotero(CLASSIFIED_ITEM)
    wire(monkeypatch, zotero, FakeJevClient())

    result = CliRunner().invoke(app, ["route", "--config", str(config_path), "--apply"])

    assert result.exit_code == 0, result.output
    assert zotero.created == ["02 Topics/quarkonium", "04 Review"]
    assert zotero.membership == [({"KEY00001", "KEY00002"}, set())]


def test_route_skips_papers_that_were_never_classified(monkeypatch, config_path: Path):
    zotero = FakeZotero(ITEM)  # never judged: no stamp in `extra`
    wire(monkeypatch, zotero, FakeJevClient())

    result = CliRunner().invoke(app, ["route", "--config", str(config_path), "--apply"])

    assert result.exit_code == 0, result.output
    assert "0 routed, 1 unchanged" in result.output
    assert zotero.membership == []


def test_route_does_not_create_what_already_exists(monkeypatch, config_path: Path):
    zotero = FakeZotero(CLASSIFIED_ITEM)
    zotero._paths = {"02 Topics/quarkonium": "EXIST111", "04 Review": "EXIST222"}
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
        "02 Topics/quarkonium": "TOPIC111",
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
        "02 Topics/quarkonium": "TOPIC111",
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
    zotero._paths = {"02 Topics/quarkonium": "TOPIC111", "04 Review": "REVIEW11"}
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
    zotero._paths = {"02 Topics/quarkonium": "TOPIC111", "04 Review": "REVIEW11"}
    zotero._item["data"]["collections"] = [COLLECTION_KEY, "REVIEW11"]
    wire(monkeypatch, zotero, FakeJevClient())

    result = CliRunner().invoke(
        app, ["route", "--config", str(config_path), "--prune"]
    )

    assert result.exit_code == 0, result.output
    assert "← 04 Review" in result.output
    assert zotero.membership == []
    assert zotero.created == []


CLASSIFIED_WITHOUT_REVIEW = {
    "key": "ABCD2345",
    "version": 420,
    "data": {
        **ITEM["data"],
        "tags": [{"tag": "topic/quarkonium"}],
        "extra": "jevero-fingerprint: abc12345",
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


# --------------------------------------------------------------------------- #
# run: classify the inbox, then file it
# --------------------------------------------------------------------------- #


def test_run_dry_run_writes_nothing(monkeypatch, config_path: Path, confidence: JevOutcome):
    zotero = FakeZotero(ITEM)
    wire(monkeypatch, zotero, FakeJevClient(outcome=confidence))

    result = CliRunner().invoke(app, ["run", "--config", str(config_path), "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "[dry-run] no Zotero changes" in result.output
    assert zotero.applied == []
    assert zotero.created == []
    assert zotero.membership == []


def test_run_classifies_then_files(monkeypatch, config_path: Path, confidence: JevOutcome):
    zotero = FakeZotero(ITEM)
    wire(monkeypatch, zotero, FakeJevClient(outcome=confidence))

    result = CliRunner().invoke(app, ["run", "--config", str(config_path)])

    assert result.exit_code == 0, result.output
    # classified...
    assert len(zotero.applied) == 1
    assert "topic/quarkonium" in zotero.applied[0].add_tags
    # ...then filed into the collections its tags point at
    assert zotero.created == ["02 Topics/quarkonium", "03 Kinds/core"]
    assert zotero.membership == [({"KEY00001", "KEY00002"}, set())]
    assert "Filed: 1 routed, 0 unchanged" in result.output


def test_run_no_move_classifies_only(monkeypatch, config_path: Path, confidence: JevOutcome):
    zotero = FakeZotero(ITEM)
    wire(monkeypatch, zotero, FakeJevClient(outcome=confidence))

    result = CliRunner().invoke(
        app, ["run", "--config", str(config_path), "--no-move", "--verbose"]
    )

    assert result.exit_code == 0, result.output
    assert len(zotero.applied) == 1
    assert zotero.membership == []
    assert zotero.created == []
    assert "classified but kept" in result.output


def test_run_reports_papers_that_need_metadata(
    monkeypatch, config_path: Path, confidence: JevOutcome
):
    zotero = FakeZotero(ITEM_WITHOUT_ABSTRACT)
    jev = FakeJevClient(outcome=confidence)
    wire(monkeypatch, zotero, jev)

    result = CliRunner().invoke(
        app, ["run", "--config", str(config_path), "--verbose"]
    )

    assert result.exit_code == 0, result.output
    assert jev.calls == 0  # never classified: no abstract
    assert "1 skipped" in result.output
    assert "1 need metadata" in result.output
    assert zotero.membership == []


def test_run_empties_the_inbox_when_configured_to_move(
    monkeypatch, config_path: Path, tmp_path: Path, confidence: JevOutcome
):
    moving_config = tmp_path / "config.yaml"
    moving_config.write_text(
        config_path.read_text() + "\ncollections:\n  remove_from_inbox: true\n",
        encoding="utf-8",
    )
    zotero = FakeZotero(ITEM)
    wire(monkeypatch, zotero, FakeJevClient(outcome=confidence))

    result = CliRunner().invoke(
        app, ["run", "--config", str(moving_config), "--verbose"]
    )

    assert result.exit_code == 0, result.output
    assert zotero.membership == [({"KEY00001", "KEY00002"}, {COLLECTION_KEY})]
    assert "Inbox is now empty." in result.output


def test_run_does_not_ask_for_confirmation(monkeypatch, config_path: Path, confidence: JevOutcome):
    """It is inbox-scoped, so the wide-scope warning must not appear."""
    zotero = FakeZotero(ITEM)
    wire(monkeypatch, zotero, FakeJevClient(outcome=confidence))

    result = CliRunner().invoke(
        app, ["run", "--config", str(config_path), "--verbose"]
    )

    assert result.exit_code == 0, result.output
    assert "WARNING" not in result.output


def test_run_writes_by_default_and_dry_run_does_not(
    monkeypatch, config_path: Path, confidence: JevOutcome
):
    """`run` needs no --apply: that is the point of the command."""
    help_text = CliRunner().invoke(app, ["run", "--help"]).output
    assert "WRITES TO ZOTERO BY DEFAULT" in help_text

    zotero = FakeZotero(ITEM)
    wire(monkeypatch, zotero, FakeJevClient(outcome=confidence))
    result = CliRunner().invoke(app, ["run", "--config", str(config_path), "--dry-run"])
    assert zotero.applied == []

    zotero = FakeZotero(ITEM)
    wire(monkeypatch, zotero, FakeJevClient(outcome=confidence))
    result = CliRunner().invoke(
        app, ["run", "--config", str(config_path), "--verbose"]
    )
    assert len(zotero.applied) == 1


def test_limit_counts_papers_to_process_not_items_scanned(
    monkeypatch, config_path: Path, confidence: JevOutcome
):
    """An up-to-date paper must not eat one of the --limit slots."""
    zotero = FakeZotero(up_to_date_item(config_path, key="AAAA0001"), extra_items=(ITEM,))
    jev = FakeJevClient(outcome=confidence)
    wire(monkeypatch, zotero, jev)

    result = CliRunner().invoke(
        app, ["process", "--config", str(config_path), "--apply", "--limit", "1"]
    )

    assert result.exit_code == 0, result.output
    assert len(zotero.applied) == 1  # the unprocessed one, not the skipped one
    assert jev.calls == 1


def test_stale_count_respects_the_limit(
    monkeypatch, config_path: Path, confidence: JevOutcome
):
    """`--limit 1` must not claim three out-of-date papers it never listed."""
    items = []
    for key in ("AAAA0001", "BBBB0002", "CCCC0003"):
        item = copy.deepcopy(CLASSIFIED_ITEM)
        item["key"] = key
        item["data"]["key"] = key
        items.append(item)
    zotero = FakeZotero(items[0], extra_items=tuple(items[1:]))
    wire(monkeypatch, zotero, FakeJevClient(outcome=confidence))

    result = CliRunner().invoke(
        app, ["process", "--config", str(config_path), "--apply", "--limit", "1"]
    )

    assert result.exit_code == 0, result.output
    assert "Candidates: 1 papers" in result.output
    assert "1 of them were judged with another model" in result.output
    assert "3 of them" not in result.output


def test_run_never_moves_a_paper_it_could_not_file(
    monkeypatch, config_path: Path, tmp_path: Path
):
    """Regression: only *filed* papers may leave the inbox.

    A previous `_route` walked every inbox item and, with remove_from_inbox on,
    dropped the inbox membership of items it could not file — so an
    failed paper silently vanished from the inbox while never landing in
    a collection.
    """
    moving_config = tmp_path / "config.yaml"
    moving_config.write_text(
        config_path.read_text() + "\ncollections:\n  remove_from_inbox: true\n",
        encoding="utf-8",
    )
    zotero = FakeZotero(ITEM)
    wire(monkeypatch, zotero, FakeJevClient(error=JevResponseError("no answer")))

    result = CliRunner().invoke(
        app, ["run", "--config", str(moving_config), "--verbose"]
    )

    assert result.exit_code == 1  # the paper failed, so the run reports it
    assert zotero.applied[0].add_tags == set()
    # Nothing was filed, so nothing may have been moved out of the inbox.
    assert zotero.membership == []
    assert "Inbox now holds 1 papers" in result.output


def test_route_leaves_unroutable_papers_where_they_are(
    monkeypatch, config_path: Path, tmp_path: Path
):
    moving_config = tmp_path / "config.yaml"
    moving_config.write_text(
        config_path.read_text() + "\ncollections:\n  remove_from_inbox: true\n",
        encoding="utf-8",
    )
    errored = {
        "key": "ABCD2345",
        "version": 420,
        "data": {
            **ITEM["data"],
            "extra": "jevero-fingerprint: abc12345\njevero-error: HTTP 500",
        },
    }
    untagged = {
        "key": "EFGH6789",
        "version": 6,
        "data": {**ITEM["data"], "key": "EFGH6789", "tags": []},
    }
    zotero = FakeZotero(errored, extra_items=(untagged,))
    wire(monkeypatch, zotero, FakeJevClient())

    result = CliRunner().invoke(
        app, ["route", "--config", str(moving_config), "--apply"]
    )

    assert result.exit_code == 0, result.output
    assert zotero.membership == []
    assert "0 routed, 2 unchanged" in result.output


def test_a_paper_judged_before_stamps_still_counts_as_out_of_date(
    monkeypatch, config_path: Path, confidence: JevOutcome
):
    """Otherwise the library that predates fingerprints would look up to date.

    It carries `agent/processed` from the old namespace, and no stamp at all, so
    without the retired-prefix rule it is neither up to date nor out of date — and
    the first run after the change would silently do nothing.
    """
    legacy = {
        "key": "LEGACY01",
        "version": 700,
        "data": {
            **ITEM["data"],
            "key": "LEGACY01",
            "tags": [{"tag": "agent/processed"}, {"tag": "role/theory"}],
            "collections": [],
        },
    }
    zotero = FakeZotero(up_to_date_item(config_path, key="AAAA0001"), extra_items=(legacy,))
    jev = FakeJevClient(outcome=confidence)
    wire(monkeypatch, zotero, jev)

    result = CliRunner().invoke(
        app, ["process", "--config", str(config_path), "--dry-run"]
    )

    assert result.exit_code == 0, result.output
    assert "Candidates: 1 papers" in result.output
    assert "1 of them were judged with another model" in result.output
    assert jev.calls == 1


def test_the_paper_text_reaches_the_classifier(
    monkeypatch, config_path: Path, confidence: JevOutcome
):
    """The framework a paper is built on is usually named only in its text."""
    zotero = FakeZotero(ITEM)
    zotero.full_texts["ABCD2345"] = "I. INTRODUCTION We work in NRQCD."
    jev = FakeJevClient(outcome=confidence)
    wire(monkeypatch, zotero, jev)

    result = CliRunner().invoke(
        app, ["process", "--config", str(config_path), "--dry-run", "--verbose"]
    )

    assert result.exit_code == 0, result.output
    assert jev.texts == ["I. INTRODUCTION We work in NRQCD."]
    assert zotero.text_reads == ["ABCD2345"]
    assert "characters of the paper's text" in result.output


def test_the_paper_text_is_bounded_by_the_configuration(
    monkeypatch, config_path: Path, confidence: JevOutcome, tmp_path: Path
):
    """The excerpt has to fit the model's context, so it is capped in config."""
    bounded = tmp_path / "config.yaml"
    bounded.write_text(
        config_path.read_text(encoding="utf-8").replace(
            "  allow_title_only: false",
            "  allow_title_only: false\n  full_text:\n    max_chars: 12",
        ),
        encoding="utf-8",
    )
    zotero = FakeZotero(ITEM)
    zotero.full_texts["ABCD2345"] = "0123456789abcdefghij"
    jev = FakeJevClient(outcome=confidence)
    wire(monkeypatch, zotero, jev)

    result = CliRunner().invoke(
        app, ["process", "--config", str(bounded), "--dry-run", "--verbose"]
    )

    assert result.exit_code == 0, result.output
    assert jev.texts == ["0123456789ab"]


def test_a_paper_without_indexed_text_is_still_judged(
    monkeypatch, config_path: Path, confidence: JevOutcome
):
    """Losing the text is not a reason to fail or skip a paper."""
    zotero = FakeZotero(ITEM)  # no full_texts entry
    jev = FakeJevClient(outcome=confidence)
    wire(monkeypatch, zotero, jev)

    result = CliRunner().invoke(
        app, ["process", "--config", str(config_path), "--dry-run", "--verbose"]
    )

    assert result.exit_code == 0, result.output
    assert jev.texts == [""]
    assert "characters of the paper's text" not in result.output


def test_the_text_is_not_read_when_it_is_switched_off(
    monkeypatch, config_path: Path, confidence: JevOutcome, tmp_path: Path
):
    off = tmp_path / "config.yaml"
    off.write_text(
        config_path.read_text(encoding="utf-8").replace(
            "  allow_title_only: false",
            "  allow_title_only: false\n  full_text:\n    enabled: false",
        ),
        encoding="utf-8",
    )
    zotero = FakeZotero(ITEM)
    jev = FakeJevClient(outcome=confidence)
    wire(monkeypatch, zotero, jev)

    result = CliRunner().invoke(app, ["process", "--config", str(off), "--dry-run"])

    assert result.exit_code == 0, result.output
    assert zotero.text_reads == []
    assert jev.texts == [""]


# --------------------------------------------------------------------------- #
# output: compact by default, everything behind --verbose
# --------------------------------------------------------------------------- #


def test_the_default_output_shows_tags_not_probabilities(
    monkeypatch, config_path: Path, confidence: JevOutcome
):
    """One paper is a title, its tags, and nothing else: the rest is diagnostics."""
    zotero = FakeZotero(ITEM)
    wire(monkeypatch, zotero, FakeJevClient(outcome=confidence))

    result = CliRunner().invoke(
        app, ["process", "--config", str(config_path), "--dry-run"]
    )

    assert result.exit_code == 0, result.output
    assert "[ABCD2345] Energy Correlators in Heavy Quarkonium Production" in result.output
    assert "+topic/quarkonium" in result.output
    assert "+kind/core" in result.output
    # The judgement itself is not printed unless asked for.
    assert "APPLY" not in result.output
    assert "0.96" not in result.output
    assert "Topics" not in result.output


def test_verbose_prints_the_whole_judgement(
    monkeypatch, config_path: Path, confidence: JevOutcome
):
    zotero = FakeZotero(ITEM)
    wire(monkeypatch, zotero, FakeJevClient(outcome=confidence))

    result = CliRunner().invoke(
        app, ["process", "--config", str(config_path), "--dry-run", "--verbose"]
    )

    assert result.exit_code == 0, result.output
    for heading in ("Topics", "Kinds", "Coverage", "Planned tags"):
        assert heading in result.output
    assert "APPLY" in result.output
    assert "Vocabulary usage" in result.output


def test_the_summary_reports_the_run_and_who_is_waiting(
    monkeypatch, config_path: Path, confidence: JevOutcome
):
    """Deliberately short: vocabulary and the inbox census belong to `status`."""
    zotero = FakeZotero(ITEM)
    wire(monkeypatch, zotero, FakeJevClient(outcome=confidence))

    result = CliRunner().invoke(app, ["run", "--config", str(config_path)])

    assert result.exit_code == 0, result.output
    assert "1 processed, 0 flagged, 0 unchanged, 0 skipped, 0 failed" in result.output
    assert "Filed: 1 routed, 0 unchanged" in result.output
    assert "classifier cost:" in result.output
    assert "Review: nothing waiting for a human" in result.output
    assert "Vocabulary usage" not in result.output
    assert "Inbox now holds" not in result.output


def test_the_summary_names_the_papers_that_need_a_human(
    monkeypatch, config_path: Path, confidence: JevOutcome
):
    zotero = FakeZotero(ITEM_WITHOUT_ABSTRACT)
    wire(monkeypatch, zotero, FakeJevClient(outcome=confidence))

    result = CliRunner().invoke(app, ["run", "--config", str(config_path)])

    assert result.exit_code == 0, result.output
    assert "Review: 1 paper need a human" in result.output


def test_status_reports_the_library_and_the_vocabulary(
    monkeypatch, config_path: Path
):
    """The vocabulary usage view, free: stored tags only, no classifier call."""
    zotero = FakeZotero(CLASSIFIED_ITEM)
    zotero._paths = {"02 Topics/quarkonium": "TOPIC111", "04 Review": "REVIEW11"}
    jev = FakeJevClient()
    wire(monkeypatch, zotero, jev)

    result = CliRunner().invoke(app, ["status", "--config", str(config_path)])

    assert result.exit_code == 0, result.output
    assert jev.calls == 0
    assert "Library" in result.output
    # This item's stamp is not the current one, so it counts as out of date.
    assert "1 out of date" in result.output
    assert "review 1  (ambiguous 1)" in result.output
    assert "topic/quarkonium" in result.output
    # A word on no paper is the signal that the vocabulary is not earning its keep.
    assert "never applied" in result.output
    assert "02 Topics/quarkonium" in result.output


def test_status_counts_an_up_to_date_paper_as_judged(
    monkeypatch, config_path: Path
):
    zotero = FakeZotero(up_to_date_item(config_path))
    wire(monkeypatch, zotero, FakeJevClient())

    result = CliRunner().invoke(app, ["status", "--config", str(config_path)])

    assert result.exit_code == 0, result.output
    assert "1 judged up to date   0 out of date" in result.output
