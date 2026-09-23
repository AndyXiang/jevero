"""Policy tests: probability in, tag actions out. No network, no Zotero."""

from __future__ import annotations

import pytest

from jevero.models import ClassificationResult, InputMode
from jevero.policy import (
    REASON_AMBIGUOUS,
    REASON_COVERAGE,
    REASON_MISSING_ABSTRACT,
    REASON_TAXONOMY_GAP,
    REVIEW_REASONS,
    EXTRA_ERROR,
    EXTRA_FINGERPRINT,
    REVIEW_PREFIX,
    PolicyError,
    error_actions,
    plan,
    review_actions,
)


def test_probability_above_threshold_applies_tag(config, result_factory):
    result = result_factory(topics={"quarkonium": 0.97, "nrqcd": 0.91})
    actions = plan(result, config)

    assert "topic/quarkonium" in actions.add_tags
    assert "topic/nrqcd" in actions.add_tags
    assert EXTRA_FINGERPRINT in actions.extra


def test_threshold_is_inclusive_at_the_boundary(config, result_factory):
    """`apply_floor` is inclusive; just under it is the review band."""
    at_threshold = result_factory(topics={"nrqcd": 0.60})
    just_below = result_factory(topics={"nrqcd": 0.5999})

    assert "topic/nrqcd" in plan(at_threshold, config).add_tags
    below_actions = plan(just_below, config)
    assert "topic/nrqcd" not in below_actions.add_tags
    assert REASON_AMBIGUOUS in below_actions.add_tags


def test_probability_below_review_band_is_ignored(config, result_factory):
    result = result_factory(topics={"quarkonium": 0.99, "nrqcd": 0.10})
    actions = plan(result, config)

    assert "topic/nrqcd" not in actions.add_tags
    assert not any(t.startswith(REVIEW_PREFIX) for t in actions.add_tags)


def test_multiple_topics_can_apply_at_once(config, result_factory):
    result = result_factory(topics={"quarkonium": 0.97, "nrqcd": 0.91})
    actions = plan(result, config)

    assert {"topic/quarkonium", "topic/nrqcd"} <= actions.add_tags
    assert not any(t.startswith(REVIEW_PREFIX) for t in actions.add_tags)


def test_missing_topic_reports_a_taxonomy_gap_and_never_invents_a_topic(
    config, result_factory
):
    result = result_factory(
        coverage={"covered": 0.1, "missing-topic": 0.70, "irrelevant": 0.05}
    )
    actions = plan(result, config)

    assert REASON_TAXONOMY_GAP in actions.add_tags
    assert any(t.startswith(REVIEW_PREFIX) for t in actions.add_tags)
    assert EXTRA_FINGERPRINT in actions.extra
    # Expanding the taxonomy is a human decision: no invented topic may appear.
    assert not any(tag.startswith("topic/") for tag in actions.add_tags)
    assert "topic/other" not in actions.add_tags


def test_missing_topic_below_threshold_does_not_report_a_gap(config, result_factory):
    result = result_factory(
        topics={"quarkonium": 0.95},
        coverage={"covered": 0.9, "missing-topic": 0.49, "irrelevant": 0.05},
    )
    actions = plan(result, config)

    assert REASON_TAXONOMY_GAP not in actions.add_tags
    assert "topic/quarkonium" in actions.add_tags


def test_irrelevant_is_processed_without_a_topic(config, result_factory):
    result = result_factory(
        coverage={"covered": 0.1, "missing-topic": 0.05, "irrelevant": 0.80}
    )
    actions = plan(result, config)

    assert actions.add_tags == set()


def test_out_of_scope_needs_no_coverage_confidence(config, result_factory):
    """A clearly out-of-scope paper is a decision, not a question."""
    result = result_factory(
        coverage={"covered": 0.05, "missing-topic": 0.60, "irrelevant": 0.85}
    )
    actions = plan(result, config)

    assert actions.add_tags == set()


def test_ambiguous_classification_is_reported(config, result_factory):
    result = result_factory(topics={"nrqcd": 0.58})
    actions = plan(result, config)

    assert REASON_AMBIGUOUS in actions.add_tags
    assert any(t.startswith(REVIEW_PREFIX) for t in actions.add_tags)
    assert EXTRA_FINGERPRINT in actions.extra
    assert "topic/nrqcd" not in actions.add_tags


def test_a_decided_dimension_does_not_report_its_weaker_candidates(
    config, result_factory
):
    """`quarkonium` 0.97 already decided the topics; a second candidate sitting
    in the review band is "maybe also this", not a reason to interrupt a human."""
    result = result_factory(topics={"quarkonium": 0.97, "nrqcd": 0.57})
    actions = plan(result, config)

    assert "topic/quarkonium" in actions.add_tags
    assert REASON_AMBIGUOUS not in actions.add_tags
    assert not any(t.startswith(REVIEW_PREFIX) for t in actions.add_tags)


def test_kinds_do_not_gate_review(config, result_factory):
    """Kinds are facets: "no confident kind" is normal, not a reason to report."""
    result = result_factory(topics={"quarkonium": 0.97}, kinds={"method": 0.45})
    actions = plan(result, config)

    assert "topic/quarkonium" in actions.add_tags
    assert REASON_AMBIGUOUS not in actions.add_tags
    assert not any(t.startswith(REVIEW_PREFIX) for t in actions.add_tags)
    assert not any(tag.startswith("kind/") for tag in actions.add_tags)


def test_undecided_topics_are_still_reported(config, result_factory):
    """A paper with no confident topic is genuinely undecided."""
    result = result_factory(topics={"quarkonium": 0.57}, kinds={"method": 0.99})
    actions = plan(result, config)

    assert REASON_AMBIGUOUS in actions.add_tags
    assert "kind/method" in actions.add_tags


def test_unconvincing_covered_is_reported(config, result_factory):
    """Nothing applied, and `covered` says the taxonomy does not fit either."""
    result = result_factory(
        coverage={"covered": 0.40, "missing-topic": 0.30, "irrelevant": 0.30}
    )
    actions = plan(result, config)

    assert REASON_COVERAGE in actions.add_tags
    assert REASON_TAXONOMY_GAP not in actions.add_tags


def test_unconvincing_covered_is_reported_even_when_a_topic_applies(
    config, result_factory
):
    """Otherwise a confident topic tag would hide a taxonomy that fits poorly."""
    result = result_factory(
        topics={"quarkonium": 0.95},
        coverage={"covered": 0.50, "missing-topic": 0.20, "irrelevant": 0.10},
    )
    actions = plan(result, config)

    assert "topic/quarkonium" in actions.add_tags
    assert REASON_COVERAGE in actions.add_tags


def test_covered_at_its_threshold_is_confident(config, result_factory):
    result = result_factory(
        coverage={"covered": 0.70, "missing-topic": 0.20, "irrelevant": 0.10}
    )
    actions = plan(result, config)

    assert REASON_COVERAGE not in actions.add_tags


def test_title_only_input_reports_missing_abstract(config, result_factory):
    result = result_factory(topics={"quarkonium": 0.99}, kinds={"core": 0.99})
    actions = plan(result, config, input_mode=InputMode.TITLE_ONLY)

    assert REASON_MISSING_ABSTRACT in actions.add_tags
    assert any(t.startswith(REVIEW_PREFIX) for t in actions.add_tags)
    assert "topic/quarkonium" in actions.add_tags


def test_full_input_with_confident_judgements_is_not_reported(config, result_factory):
    result = result_factory(topics={"quarkonium": 0.99}, kinds={"core": 0.9})
    actions = plan(result, config, input_mode=InputMode.FULL)

    assert all(reason not in actions.add_tags for reason in REVIEW_REASONS)


def test_a_role_below_the_floor_is_not_applied(config, result_factory):
    """Kinds use the same membership floor as topics, then their own cap."""
    result = result_factory(topics={"quarkonium": 0.9}, kinds={"core": 0.59})
    actions = plan(result, config)

    assert "topic/quarkonium" in actions.add_tags
    assert "kind/core" not in actions.add_tags


def test_review_state_never_appears_without_a_reason(config, result_factory):
    """The umbrella tag is only useful if every flagged paper says why."""
    scenarios = [
        {"topics": {"nrqcd": 0.58}},
        {"coverage": {"covered": 0.3, "missing-topic": 0.2, "irrelevant": 0.2}},
        {"coverage": {"covered": 0.1, "missing-topic": 0.9, "irrelevant": 0.05}},
    ]
    for scenario in scenarios:
        actions = plan(result_factory(**scenario), config)
        assert any(t.startswith(REVIEW_PREFIX) for t in actions.add_tags)
        assert actions.add_tags & REVIEW_REASONS, scenario


def test_missing_configured_entry_is_rejected(config, result_factory):
    result = result_factory()
    broken = ClassificationResult(
        topics={"quarkonium": 0.9},  # nrqcd missing
        kinds=result.kinds,
        coverage=result.coverage,
    )

    with pytest.raises(PolicyError, match="missing configured entries"):
        plan(broken, config)


def test_unconfigured_entry_is_rejected(config, result_factory):
    result = result_factory()
    broken = result.model_copy(update={"topics": {**result.topics, "lattice-qcd": 0.9}})

    with pytest.raises(PolicyError, match="not configured"):
        plan(broken, config)


def test_error_actions_clear_the_stamp_and_record_why():
    """A failure must be retried, not frozen: no stamp, reason in `extra`."""
    actions = error_actions("HTTP 500")

    assert actions.extra == {EXTRA_FINGERPRINT: None, EXTRA_ERROR: "HTTP 500"}
    assert actions.add_tags == set()
    assert REVIEW_REASONS <= actions.remove_tags


def test_review_actions_record_the_reason_without_stamping():
    actions = review_actions()

    assert actions.add_tags == {REASON_MISSING_ABSTRACT}
    assert actions.extra[EXTRA_FINGERPRINT] is None


def test_plan_is_deterministic(config, result_factory):
    result = result_factory(topics={"quarkonium": 0.9, "nrqcd": 0.9})

    assert plan(result, config) == plan(result, config)


def test_reclassification_converges(config, result_factory):
    """A re-run replaces the managed state instead of accumulating it.

    `merge_tags` is the write-side arithmetic, so this is the whole loop: plan,
    apply to a tag list, plan again.
    """
    from jevero.zotero import merge_tags

    stale = [
        "review/ambiguous",
        "topic/loop-integrals",
    ]
    actions = plan(result_factory(topics={"quarkonium": 0.97}), config)

    after_first = merge_tags(stale, actions)
    after_second = merge_tags(after_first, actions)

    # The superseded review reason is gone, and nothing else was touched...
    assert set(after_first) == {
        "topic/loop-integrals",
        "topic/quarkonium",
    }
    # ...and a second run changes nothing.
    assert after_second == after_first


def test_known_names_are_reconciled_because_the_namespaces_are_machine_owned(
    config, result_factory
):
    """`topic/*` and `kind/*` state the current judgement, not the union of runs."""
    actions = plan(result_factory(topics={"quarkonium": 0.97}), config)

    assert "topic/quarkonium" in actions.add_tags
    # Configured, but this paper is not about it: the tag has to go away.
    assert "topic/nrqcd" in actions.remove_tags
    assert "kind/method" in actions.remove_tags


def test_tags_outside_the_vocabulary_are_never_removed(config, result_factory):
    """Only names we know about may be deleted; the reader's own tags are theirs."""
    actions = plan(result_factory(topics={"quarkonium": 0.97}), config)

    assert "topic/loop-integrals" not in actions.remove_tags
    assert "topic/not-a-topic" not in actions.remove_tags


def test_retired_words_are_removed_and_never_added():
    from jevero.config import Config

    cfg = Config.model_validate({**BIG_TAXONOMY, "retired_topics": ["zeta"]})
    result = ClassificationResult(
        topics={name: 0.0 for name in cfg.topics},
        kinds={"core": 0.0},
        coverage={"covered": 0.9, "missing-topic": 0.05, "irrelevant": 0.05},
    )

    actions = plan(result, cfg)

    assert "topic/zeta" in actions.remove_tags
    assert "topic/zeta" not in actions.add_tags


def test_the_plan_records_the_judgement_stamp(config, result_factory):
    """The stamp travels with the plan, to be written to `extra` (not a tag)."""
    actions = plan(
        result_factory(topics={"quarkonium": 0.97}), config, fingerprint="9f3c1a77"
    )

    assert actions.extra[EXTRA_FINGERPRINT] == "9f3c1a77"
    assert not [tag for tag in actions.add_tags if "meta" in tag]


def test_topics_at_or_above_the_guaranteed_band_beat_the_cap():
    """A paper with four genuine topics must not lose one to a counting rule."""
    from jevero.config import Config

    cfg = Config.model_validate(
        {
            **BIG_TAXONOMY,
            "thresholds": {"topic_max": 2, "topic_guaranteed": 0.95},
        }
    )
    result = ClassificationResult(
        topics={
            "alpha": 0.99,
            "beta": 0.97,
            "gamma": 0.96,
            "delta": 0.80,
            "epsilon": 0.70,
        },
        kinds={"core": 0.0},
        coverage={"covered": 0.9, "missing-topic": 0.05, "irrelevant": 0.05},
    )

    actions = plan(result, cfg)

    # Three are guaranteed, so all three stay even though the cap is two; the
    # ranked band fills what is left of the cap, which is nothing.
    assert {t for t in actions.add_tags if t.startswith("topic/")} == {
        "topic/alpha",
        "topic/beta",
        "topic/gamma",
    }


def test_the_ranked_band_fills_what_the_guaranteed_band_leaves():
    from jevero.config import Config

    cfg = Config.model_validate(
        {
            **BIG_TAXONOMY,
            "thresholds": {"topic_max": 3, "topic_guaranteed": 0.95},
        }
    )
    result = ClassificationResult(
        topics={
            "alpha": 0.99,  # guaranteed
            "beta": 0.90,  # ranked, first
            "gamma": 0.85,  # ranked, second
            "delta": 0.84,  # ranked, but the cap is full
            "epsilon": 0.30,  # below the floor
        },
        kinds={"core": 0.0},
        coverage={"covered": 0.9, "missing-topic": 0.05, "irrelevant": 0.05},
    )

    actions = plan(result, cfg)

    assert {t for t in actions.add_tags if t.startswith("topic/")} == {
        "topic/alpha",
        "topic/beta",
        "topic/gamma",
    }


def test_retired_namespaces_are_cleared_wholesale(config, result_factory):
    """A rename has to migrate the library, not just stop writing the old names."""
    stub = config.model_copy(update={"retired_prefixes": frozenset({"role/"})})

    actions = plan(result_factory(topics={"quarkonium": 0.97}), stub)

    assert "role/" in actions.remove_tag_prefixes


BIG_TAXONOMY = {
    "topics": {
        name: {"description": f"A paper about {name}."}
        for name in ("alpha", "beta", "gamma", "delta", "epsilon")
    },
    "kinds": ["core"],
    "coverage": ["covered", "missing-topic", "irrelevant"],
    "thresholds": {"topic_max": 3},
}


def test_topic_cap_keeps_the_strongest_topics():
    """Topic judgements are independent, so umbrella topics would ride along."""
    from jevero.config import Config

    big = Config.model_validate(BIG_TAXONOMY)
    result = ClassificationResult(
        topics={
            "alpha": 0.99,
            "beta": 0.93,
            "gamma": 0.88,
            "delta": 0.87,
            "epsilon": 0.86,
        },
        kinds={"core": 0.1},
        coverage={"covered": 0.9, "missing-topic": 0.05, "irrelevant": 0.05},
    )

    actions = plan(result, big)

    assert actions.add_tags == {
        "topic/alpha",
        "topic/beta",
        "topic/gamma",
    }


def test_topic_cap_does_not_add_topics_below_the_threshold():
    from jevero.config import Config

    big = Config.model_validate(BIG_TAXONOMY)
    result = ClassificationResult(
        topics={"alpha": 0.99, "beta": 0.5, "gamma": 0.4, "delta": 0.3, "epsilon": 0.2},
        kinds={"core": 0.1},
        coverage={"covered": 0.9, "missing-topic": 0.05, "irrelevant": 0.05},
    )

    actions = plan(result, big)

    assert [t for t in actions.add_tags if t.startswith("topic/")] == ["topic/alpha"]


def test_topic_max_must_be_positive(tmp_path):
    from jevero.config import ConfigError, load_config

    path = tmp_path / "config.yaml"
    path.write_text(
        "topics:\n  alpha:\n    description: ok\n"
        "coverage: [covered, missing-topic, irrelevant]\n"
        "thresholds:\n  topic_max: 0\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="topic_max"):
        load_config(path)
