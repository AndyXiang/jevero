"""Config loading and validation tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from jevero.config import (
    Config,
    ConfigError,
    MissingEnvironmentError,
    load_config,
    openrouter_api_key,
    store_zotero_write_key,
    zotero_write_key,
)

def write_config(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def test_loads_a_valid_config(config: Config):
    assert set(config.topics) == {"quarkonium", "nrqcd"}
    assert set(config.kinds) == {"core", "method"}
    assert set(config.coverage) == {"covered", "missing-topic", "irrelevant"}
    assert config.thresholds.apply_floor == 0.60
    assert config.thresholds.topic_max == 3
    assert config.thresholds.covered_apply == 0.70
    assert config.zotero.base_url == "http://127.0.0.1:23119/api"
    assert config.zotero.inbox_collection == "00 Inbox"
    assert config.classification.allow_title_only is False


def test_deferred_projects_dimension_is_rejected_loudly(tmp_path: Path):
    """A dimension that is not implemented must not be silently ignored."""
    path = write_config(
        tmp_path,
        "topics:\n  nrqcd:\n    description: ok\n"
        "projects:\n  qec:\n    description: ok\n"
        "coverage: [covered, missing-topic, irrelevant]\n",
    )

    with pytest.raises(ConfigError, match="projects"):
        load_config(path)


def test_unknown_config_keys_are_rejected(tmp_path: Path):
    path = write_config(
        tmp_path,
        "topics:\n  nrqcd:\n    description: ok\n"
        "coverage: [covered, missing-topic, irrelevant]\n"
        "hthresholds:\n  review: 0.55\n",
    )

    with pytest.raises(ConfigError, match="hthresholds"):
        load_config(path)


def test_unknown_threshold_is_rejected(tmp_path: Path):
    path = write_config(
        tmp_path,
        "topics:\n  nrqcd:\n    description: ok\n"
        "coverage: [covered, missing-topic, irrelevant]\n"
        "thresholds:\n  project_apply: 0.85\n",
    )

    with pytest.raises(ConfigError, match="project_apply"):
        load_config(path)


def test_missing_file_reports_the_path(tmp_path: Path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "absent.yaml")


def test_invalid_yaml_is_reported(tmp_path: Path):
    path = write_config(tmp_path, "topics: [unclosed\n")

    with pytest.raises(ConfigError, match="not valid YAML"):
        load_config(path)


def test_empty_file_is_reported(tmp_path: Path):
    path = write_config(tmp_path, "")

    with pytest.raises(ConfigError, match="is empty"):
        load_config(path)


def test_empty_description_is_rejected(tmp_path: Path):
    path = write_config(
        tmp_path,
        "topics:\n  nrqcd:\n    description: '   '\n"
        "coverage: [covered, missing-topic, irrelevant]\n",
    )

    with pytest.raises(ConfigError, match="description must not be empty"):
        load_config(path)


def test_tag_unsafe_names_are_rejected(tmp_path: Path):
    path = write_config(
        tmp_path,
        "topics:\n  'NRQCD/extra':\n    description: bad name\n"
        "coverage: [covered, missing-topic, irrelevant]\n",
    )

    with pytest.raises(ConfigError, match="tag-safe"):
        load_config(path)


def test_empty_taxonomy_is_rejected(tmp_path: Path):
    path = write_config(
        tmp_path,
        "topics: {}\n"
        "coverage: [covered, missing-topic, irrelevant]\n",
    )

    with pytest.raises(ConfigError, match="at least one entry"):
        load_config(path)


def test_apply_threshold_must_exceed_review_threshold(tmp_path: Path):
    path = write_config(
        tmp_path,
        "topics:\n  nrqcd:\n    description: ok\n"
        "coverage: [covered, missing-topic, irrelevant]\n"
        "thresholds:\n  apply_floor: 0.5\n  review: 0.5\n",
    )

    with pytest.raises(ConfigError, match="greater than"):
        load_config(path)


def test_covered_apply_must_exceed_review_threshold(tmp_path: Path):
    path = write_config(
        tmp_path,
        "topics:\n  nrqcd:\n    description: ok\n"
        "coverage: [covered, missing-topic, irrelevant]\n"
        "thresholds:\n  covered_apply: 0.5\n  review: 0.5\n",
    )

    with pytest.raises(ConfigError, match="covered_apply"):
        load_config(path)


def test_thresholds_must_be_probabilities(tmp_path: Path):
    path = write_config(
        tmp_path,
        "topics:\n  nrqcd:\n    description: ok\n"
        "coverage: [covered, missing-topic, irrelevant]\n"
        "thresholds:\n  irrelevant: 1.4\n",
    )

    with pytest.raises(ConfigError, match="must lie in"):
        load_config(path)


def test_coverage_states_are_required(tmp_path: Path):
    """Without all three states the policy could not separate scope from gap."""
    path = write_config(
        tmp_path,
        "topics:\n  nrqcd:\n    description: ok\n"
        "coverage: [covered, irrelevant]\n",
    )

    with pytest.raises(ConfigError, match="coverage must define the states"):
        load_config(path)


def test_kinds_accept_both_list_and_mapping_forms(tmp_path: Path):
    path = write_config(
        tmp_path,
        "topics:\n  nrqcd:\n    description: ok\n"
        "kinds:\n  - core\n"
        "coverage: [covered, missing-topic, irrelevant]\n",
    )
    as_list = load_config(path)
    assert as_list.kinds == {"core": ""}

    path = write_config(
        tmp_path,
        "topics:\n  nrqcd:\n    description: ok\n"
        "kinds:\n  core: Central to a project.\n"
        "coverage:\n  covered: Fits.\n  missing-topic: Gap.\n  irrelevant: Out.\n",
    )
    as_mapping = load_config(path)
    assert as_mapping.kinds == {"core": "Central to a project."}
    assert as_mapping.coverage["missing-topic"] == "Gap."

    # The nested form mirrors how topics and projects are written.
    path = write_config(
        tmp_path,
        "topics:\n  nrqcd:\n    description: ok\n"
        "kinds:\n  core:\n    description: Central to a project.\n"
        "coverage:\n  covered:\n    description: Fits.\n"
        "  missing-topic:\n    description: Gap.\n"
        "  irrelevant:\n    description: Out.\n",
    )
    nested = load_config(path)
    assert nested.kinds == {"core": "Central to a project."}
    assert nested.coverage["irrelevant"] == "Out."


def test_top_level_must_be_a_mapping(tmp_path: Path):
    path = write_config(tmp_path, "- just\n- a\n- list\n")

    with pytest.raises(ConfigError, match="mapping at the top level"):
        load_config(path)


def test_zotero_config_has_no_web_api_credentials(tmp_path: Path):
    """The active client must not accept Web API settings any more."""
    path = write_config(
        tmp_path,
        "topics:\n  nrqcd:\n    description: ok\n"
        "coverage: [covered, missing-topic, irrelevant]\n"
        "zotero:\n  api_key: secret\n",
    )

    with pytest.raises(ConfigError, match="api_key"):
        load_config(path)


def test_openrouter_key_is_required(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    with pytest.raises(MissingEnvironmentError, match="OPENROUTER_API_KEY"):
        openrouter_api_key()


def test_openrouter_key_is_read_from_the_environment(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")

    assert openrouter_api_key() == "sk-test"


def test_store_write_key_creates_env_with_owner_only_mode(tmp_path: Path):
    env = tmp_path / ".env"

    path = store_zotero_write_key("local-key-value", env)

    assert path == env
    assert env.read_text(encoding="utf-8") == "ZOTERO_LOCAL_WRITE_KEY=local-key-value\n"
    assert env.stat().st_mode & 0o777 == 0o600


def test_store_write_key_replaces_only_its_own_line(tmp_path: Path):
    env = tmp_path / ".env"
    env.write_text(
        "OPENROUTER_API_KEY=keep-me\nZOTERO_LOCAL_WRITE_KEY=old\n# comment\n",
        encoding="utf-8",
    )

    store_zotero_write_key("new", env)

    text = env.read_text(encoding="utf-8")
    assert "OPENROUTER_API_KEY=keep-me" in text
    assert "ZOTERO_LOCAL_WRITE_KEY=new" in text
    assert "old" not in text
    assert "# comment" in text


def test_write_key_is_read_from_the_environment(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("ZOTERO_LOCAL_WRITE_KEY", raising=False)
    assert zotero_write_key() is None

    monkeypatch.setenv("ZOTERO_LOCAL_WRITE_KEY", "stored")
    assert zotero_write_key() == "stored"


def test_an_empty_write_key_counts_as_absent(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ZOTERO_LOCAL_WRITE_KEY", "   ")
    assert zotero_write_key() is None


def test_retired_words_are_loaded(tmp_path: Path):
    path = write_config(
        tmp_path,
        "topics:\n  nrqcd:\n    description: ok\n"
        "kinds: [method]\n"
        "coverage: [covered, missing-topic, irrelevant]\n"
        "retired_topics: [perturbative-qcd]\n"
        "retired_kinds: [core]\n",
    )

    config = load_config(path)

    assert config.retired_topics == frozenset({"perturbative-qcd"})
    assert config.retired_kinds == frozenset({"core"})


def test_a_word_cannot_be_both_live_and_retired(tmp_path: Path):
    """Otherwise the plan would add and remove the same tag."""
    path = write_config(
        tmp_path,
        "topics:\n  nrqcd:\n    description: ok\n"
        "coverage: [covered, missing-topic, irrelevant]\n"
        "retired_topics: [nrqcd]\n",
    )

    with pytest.raises(ConfigError, match="either in the vocabulary or retired"):
        load_config(path)


def test_retired_names_must_be_tag_safe(tmp_path: Path):
    path = write_config(
        tmp_path,
        "topics:\n  nrqcd:\n    description: ok\n"
        "coverage: [covered, missing-topic, irrelevant]\n"
        "retired_topics: [Not Safe]\n",
    )

    with pytest.raises(ConfigError, match="tag-safe"):
        load_config(path)


def test_topic_guaranteed_must_exceed_the_floor(tmp_path: Path):
    """Otherwise the guaranteed band swallows the ranked band and the cap is moot."""
    path = write_config(
        tmp_path,
        "topics:\n  nrqcd:\n    description: ok\n"
        "coverage: [covered, missing-topic, irrelevant]\n"
        "thresholds:\n  apply_floor: 0.6\n  topic_guaranteed: 0.6\n",
    )

    with pytest.raises(ConfigError, match="topic_guaranteed"):
        load_config(path)


def test_retired_prefixes_must_be_namespaces(tmp_path: Path):
    path = write_config(
        tmp_path,
        "topics:\n  nrqcd:\n    description: ok\n"
        "coverage: [covered, missing-topic, irrelevant]\n"
        "retired_prefixes: [role]\n",
    )

    with pytest.raises(ConfigError, match="name/"):
        load_config(path)


def test_kind_floor_may_not_be_looser_than_the_shared_floor(tmp_path: Path):
    path = write_config(
        tmp_path,
        "topics:\n  nrqcd:\n    description: ok\n"
        "coverage: [covered, missing-topic, irrelevant]\n"
        "thresholds:\n  apply_floor: 0.6\n  kind_floor: 0.5\n",
    )

    with pytest.raises(ConfigError, match="kind_floor"):
        load_config(path)
