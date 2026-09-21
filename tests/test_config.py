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
)

def write_config(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def test_loads_a_valid_config(config: Config):
    assert set(config.topics) == {"quarkonium", "nrqcd"}
    assert set(config.roles) == {"core", "method"}
    assert set(config.coverage) == {"covered", "missing-topic", "irrelevant"}
    assert config.thresholds.topic_apply == 0.85
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
        "thresholds:\n  topic_apply: 0.5\n  review: 0.5\n",
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


def test_roles_accept_both_list_and_mapping_forms(tmp_path: Path):
    path = write_config(
        tmp_path,
        "topics:\n  nrqcd:\n    description: ok\n"
        "roles:\n  - core\n"
        "coverage: [covered, missing-topic, irrelevant]\n",
    )
    as_list = load_config(path)
    assert as_list.roles == {"core": ""}

    path = write_config(
        tmp_path,
        "topics:\n  nrqcd:\n    description: ok\n"
        "roles:\n  core: Central to a project.\n"
        "coverage:\n  covered: Fits.\n  missing-topic: Gap.\n  irrelevant: Out.\n",
    )
    as_mapping = load_config(path)
    assert as_mapping.roles == {"core": "Central to a project."}
    assert as_mapping.coverage["missing-topic"] == "Gap."

    # The nested form mirrors how topics and projects are written.
    path = write_config(
        tmp_path,
        "topics:\n  nrqcd:\n    description: ok\n"
        "roles:\n  core:\n    description: Central to a project.\n"
        "coverage:\n  covered:\n    description: Fits.\n"
        "  missing-topic:\n    description: Gap.\n"
        "  irrelevant:\n    description: Out.\n",
    )
    nested = load_config(path)
    assert nested.roles == {"core": "Central to a project."}
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
