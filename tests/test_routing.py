"""Routing tests: tags in, collection paths out. Pure, no I/O."""

from __future__ import annotations

from jevero.config import Config
from jevero.routing import foreign_tags, is_managed_path, target_paths


def with_collections(config: Config, **changes) -> Config:
    """A config with different `collections` settings, for the same taxonomy."""
    return config.model_copy(
        update={"collections": config.collections.model_copy(update=changes)}
    )


def test_an_unjudged_paper_is_not_routed(config: Config):
    """A paper with no judgement stamp has no trustworthy tags to project.

    "Was this judged" comes from the item's `extra` field, not from a tag: a
    processed marker is bookkeeping, and a reader would never browse by it.
    """
    assert target_paths(["topic/quarkonium"], config, judged=False) == []


def test_topic_tags_map_under_the_topics_parent(config: Config):
    assert target_paths(["topic/quarkonium"], config, judged=True) == [
        "02 Topics/quarkonium"
    ]


def test_kind_tags_map_under_the_kinds_parent(config: Config):
    assert target_paths(["kind/method"], config, judged=True) == ["03 Kinds/method"]


def test_kinds_can_be_left_as_tags_only(config: Config):
    stub = with_collections(config, route_kinds=False)

    assert target_paths(["kind/method"], stub, judged=True) == []
    assert target_paths(["topic/nrqcd"], stub, judged=True) == ["02 Topics/nrqcd"]


def test_review_reasons_all_share_one_queue(config: Config):
    """Every reason lands in the single queue; the tag keeps the why."""
    for tags in (
        ["review/ambiguous"],
        ["review/coverage"],
        ["review/taxonomy-gap"],
        ["review/missing-abstract"],
    ):
        assert target_paths(tags, config, judged=True) == ["04 Review"], tags


def test_a_reviewed_paper_is_filed_in_both_its_topic_and_the_queue(config: Config):
    paths = target_paths(
        ["topic/nrqcd", "review/ambiguous"], config, judged=True
    )

    assert paths == ["02 Topics/nrqcd", "04 Review"]


def test_several_tags_produce_several_collections(config: Config):
    paths = target_paths(
        ["topic/nrqcd", "topic/quarkonium", "kind/core"], config, judged=True
    )

    assert paths == ["02 Topics/nrqcd", "02 Topics/quarkonium", "03 Kinds/core"]


def test_output_is_sorted_and_deduplicated(config: Config):
    paths = target_paths(
        ["topic/nrqcd", "topic/nrqcd", "kind/core"], config, judged=True
    )

    assert paths == ["02 Topics/nrqcd", "03 Kinds/core"]


def test_custom_parents_are_honoured(config: Config):
    stub = with_collections(
        config,
        topics_parent="Topics",
        kinds_parent="",
        review_collection="Review",
    )

    assert target_paths(["topic/nrqcd"], stub, judged=True) == ["Topics/nrqcd"]
    # An empty kinds parent puts kind collections at the root.
    assert target_paths(["kind/core"], stub, judged=True) == ["core"]
    assert target_paths(["review/ambiguous"], stub, judged=True) == ["Review"]


def test_review_can_be_kept_out_of_collections(config: Config):
    stub = with_collections(config, review_collection="")

    assert target_paths(["review/ambiguous"], stub, judged=True) == []


def test_prefix_like_tags_are_ignored(config: Config):
    """`topic/` alone has no name, and unrelated tags are not routes."""
    assert target_paths(["topic/", "to-read"], config, judged=True) == []


def test_a_word_outside_the_vocabulary_is_not_routed(config: Config):
    """The vocabulary is closed, or a retired word would recreate its folder."""
    stub = config.model_copy(update={"retired_topics": frozenset({"perturbative-qcd"})})

    assert target_paths(["topic/perturbative-qcd"], stub, judged=True) == []
    assert target_paths(["topic/hand-made"], config, judged=True) == []


def test_foreign_tags_are_reported_but_retired_ones_are_known(config: Config):
    stub = config.model_copy(update={"retired_topics": frozenset({"perturbative-qcd"})})
    tags = [
        "topic/nrqcd",
        "topic/perturbative-qcd",  # retired: known, and on its way out
        "topic/hand-made",  # neither configured nor retired
        "kind/core",
        "kind/mine",
    ]

    assert foreign_tags(tags, stub) == ["kind/mine", "topic/hand-made"]


def test_only_the_configured_namespaces_are_managed(config: Config):
    """Pruning must never touch folders the reader made by hand."""
    assert is_managed_path("02 Topics/nrqcd", config)
    assert is_managed_path("03 Kinds/method", config)
    assert is_managed_path("04 Review", config)

    assert not is_managed_path("00 Inbox", config)
    assert not is_managed_path("01 Projects/AmpNet", config)
    assert not is_managed_path("02 Topics", config)  # the parent is not a target


def test_kind_collections_are_not_managed_when_routing_kinds_is_off(config: Config):
    stub = with_collections(config, route_kinds=False)

    assert not is_managed_path("03 Kinds/method", stub)
    assert is_managed_path("02 Topics/nrqcd", stub)
