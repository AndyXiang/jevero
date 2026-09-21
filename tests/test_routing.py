"""Routing tests: tags in, collection paths out. Pure, no I/O."""

from __future__ import annotations

from jevero.config import Config
from jevero.routing import is_managed_path, target_paths


def with_collections(config: Config, **changes) -> Config:
    """A config with different `collections` settings, for the same taxonomy."""
    return config.model_copy(
        update={"collections": config.collections.model_copy(update=changes)}
    )


def test_unprocessed_papers_are_not_routed(config: Config):
    """A failed paper has no trustworthy tags to project."""
    assert target_paths(["agent/error", "topic/loop-integrals"], config) == []


def test_topic_tags_map_under_the_topics_parent(config: Config):
    assert target_paths(["agent/processed", "topic/loop-integrals"], config) == [
        "02 Topics/loop-integrals"
    ]


def test_role_tags_map_under_the_roles_parent(config: Config):
    assert target_paths(["agent/processed", "role/method"], config) == [
        "03 Roles/method"
    ]


def test_roles_can_be_left_as_tags_only(config: Config):
    stub = with_collections(config, route_roles=False)

    assert target_paths(["agent/processed", "role/method"], stub) == []
    assert target_paths(["agent/processed", "topic/nrqcd"], stub) == ["02 Topics/nrqcd"]


def test_review_reasons_all_share_one_queue(config: Config):
    for tags in (
        ["agent/processed", "agent/review"],
        ["agent/processed", "agent/review", "agent/review/ambiguous"],
        ["agent/processed", "agent/review", "agent/review/taxonomy-gap"],
        ["agent/processed", "agent/review/missing-abstract"],
    ):
        assert target_paths(tags, config) == ["04 Review"], tags


def test_a_reviewed_paper_is_filed_in_both_its_topic_and_the_queue(config: Config):
    paths = target_paths(
        ["agent/processed", "topic/nrqcd", "agent/review", "agent/review/ambiguous"],
        config,
    )

    assert paths == ["02 Topics/nrqcd", "04 Review"]


def test_several_tags_produce_several_collections(config: Config):
    paths = target_paths(
        ["agent/processed", "topic/nrqcd", "topic/quarkonium", "role/core"], config
    )

    assert paths == ["02 Topics/nrqcd", "02 Topics/quarkonium", "03 Roles/core"]


def test_output_is_sorted_and_deduplicated(config: Config):
    paths = target_paths(
        ["topic/nrqcd", "topic/nrqcd", "agent/processed", "role/core"], config
    )

    assert paths == ["02 Topics/nrqcd", "03 Roles/core"]


def test_custom_parents_are_honoured(config: Config):
    stub = with_collections(
        config,
        topics_parent="Topics",
        roles_parent="",
        review_collection="Review",
    )

    assert target_paths(["agent/processed", "topic/nrqcd"], stub) == ["Topics/nrqcd"]
    # An empty roles parent puts role collections at the root.
    assert target_paths(["agent/processed", "role/core"], stub) == ["core"]
    assert target_paths(["agent/processed", "agent/review"], stub) == ["Review"]


def test_review_can_be_kept_out_of_collections(config: Config):
    stub = with_collections(config, review_collection="")

    assert target_paths(["agent/processed", "agent/review"], stub) == []


def test_prefix_like_tags_are_ignored(config: Config):
    """`topic/` alone has no name, and unrelated tags are not routes."""
    assert target_paths(["agent/processed", "topic/", "to-read"], config) == []


def test_only_the_configured_namespaces_are_managed(config: Config):
    """Pruning must never touch folders the reader made by hand."""
    assert is_managed_path("02 Topics/nrqcd", config)
    assert is_managed_path("03 Roles/method", config)
    assert is_managed_path("04 Review", config)

    assert not is_managed_path("00 Inbox", config)
    assert not is_managed_path("01 Projects/AmpNet", config)
    assert not is_managed_path("02 Topics", config)  # the parent is not a target


def test_role_collections_are_not_managed_when_routing_roles_is_off(config: Config):
    stub = with_collections(config, route_roles=False)

    assert not is_managed_path("03 Roles/method", stub)
    assert is_managed_path("02 Topics/nrqcd", stub)
