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
    STATE_ERROR,
    STATE_PROCESSED,
    STATE_REVIEW,
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
    assert STATE_PROCESSED in actions.add_tags


def test_threshold_is_inclusive_at_the_boundary(config, result_factory):
    at_threshold = result_factory(topics={"nrqcd": 0.85})
    just_below = result_factory(topics={"nrqcd": 0.8499})

    assert "topic/nrqcd" in plan(at_threshold, config).add_tags
    # 0.8499 is not applied, but it is in the review band, so it is reported.
    below_actions = plan(just_below, config)
    assert "topic/nrqcd" not in below_actions.add_tags
    assert REASON_AMBIGUOUS in below_actions.add_tags


def test_probability_below_review_band_is_ignored(config, result_factory):
    result = result_factory(topics={"quarkonium": 0.99, "nrqcd": 0.10})
    actions = plan(result, config)

    assert "topic/nrqcd" not in actions.add_tags
    assert STATE_REVIEW not in actions.add_tags


def test_multiple_topics_can_apply_at_once(config, result_factory):
    result = result_factory(topics={"quarkonium": 0.97, "nrqcd": 0.91})
    actions = plan(result, config)

    assert {"topic/quarkonium", "topic/nrqcd"} <= actions.add_tags
    assert STATE_REVIEW not in actions.add_tags


def test_missing_topic_reports_a_taxonomy_gap_and_never_invents_a_topic(
    config, result_factory
):
    result = result_factory(
        coverage={"covered": 0.1, "missing-topic": 0.70, "irrelevant": 0.05}
    )
    actions = plan(result, config)

    assert REASON_TAXONOMY_GAP in actions.add_tags
    assert STATE_REVIEW in actions.add_tags
    assert STATE_PROCESSED in actions.add_tags
    # Expanding the taxonomy is a human decision: no invented topic may appear.
    assert not any(tag.startswith("topic/") for tag in actions.add_tags)
    assert "topic/other" not in actions.add_tags


def test_missing_topic_below_threshold_does_not_report_a_gap(config, result_factory):
    result = result_factory(
        topics={"quarkonium": 0.95},
        coverage={"covered": 0.9, "missing-topic": 0.69, "irrelevant": 0.05},
    )
    actions = plan(result, config)

    assert REASON_TAXONOMY_GAP not in actions.add_tags
    assert "topic/quarkonium" in actions.add_tags


def test_irrelevant_is_processed_without_a_topic(config, result_factory):
    result = result_factory(
        coverage={"covered": 0.1, "missing-topic": 0.05, "irrelevant": 0.80}
    )
    actions = plan(result, config)

    assert actions.add_tags == {STATE_PROCESSED}


def test_out_of_scope_needs_no_coverage_confidence(config, result_factory):
    """A clearly out-of-scope paper is a decision, not a question."""
    result = result_factory(
        coverage={"covered": 0.05, "missing-topic": 0.60, "irrelevant": 0.85}
    )
    actions = plan(result, config)

    assert actions.add_tags == {STATE_PROCESSED}


def test_ambiguous_classification_is_reported(config, result_factory):
    result = result_factory(topics={"nrqcd": 0.60})
    actions = plan(result, config)

    assert REASON_AMBIGUOUS in actions.add_tags
    assert STATE_REVIEW in actions.add_tags
    assert STATE_PROCESSED in actions.add_tags
    assert "topic/nrqcd" not in actions.add_tags


def test_a_decided_dimension_does_not_report_its_weaker_candidates(
    config, result_factory
):
    """`loop-integrals` 0.89 already decided the topics; a second candidate
    sitting at 0.61 is "maybe also this", not a reason to interrupt a human."""
    result = result_factory(topics={"quarkonium": 0.97, "nrqcd": 0.61})
    actions = plan(result, config)

    assert "topic/quarkonium" in actions.add_tags
    assert REASON_AMBIGUOUS not in actions.add_tags
    assert STATE_REVIEW not in actions.add_tags


def test_an_undecided_dimension_is_still_reported(config, result_factory):
    """Roles resolved nothing here, so the paper is genuinely undecided."""
    result = result_factory(topics={"quarkonium": 0.97}, roles={"method": 0.79})
    actions = plan(result, config)

    assert "topic/quarkonium" in actions.add_tags
    assert REASON_AMBIGUOUS in actions.add_tags


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
    result = result_factory(topics={"quarkonium": 0.99}, roles={"core": 0.99})
    actions = plan(result, config, input_mode=InputMode.TITLE_ONLY)

    assert REASON_MISSING_ABSTRACT in actions.add_tags
    assert STATE_REVIEW in actions.add_tags
    assert "topic/quarkonium" in actions.add_tags


def test_full_input_with_confident_judgements_is_not_reported(config, result_factory):
    result = result_factory(topics={"quarkonium": 0.99}, roles={"core": 0.9})
    actions = plan(result, config, input_mode=InputMode.FULL)

    assert all(reason not in actions.add_tags for reason in REVIEW_REASONS)


def test_roles_use_their_own_threshold(config, result_factory):
    result = result_factory(topics={"quarkonium": 0.9}, roles={"core": 0.84})
    actions = plan(result, config)

    assert "topic/quarkonium" in actions.add_tags
    assert "role/core" not in actions.add_tags


def test_review_state_never_appears_without_a_reason(config, result_factory):
    """The umbrella tag is only useful if every flagged paper says why."""
    scenarios = [
        {"topics": {"nrqcd": 0.60}},
        {"coverage": {"covered": 0.3, "missing-topic": 0.2, "irrelevant": 0.2}},
        {"coverage": {"covered": 0.1, "missing-topic": 0.9, "irrelevant": 0.05}},
    ]
    for scenario in scenarios:
        actions = plan(result_factory(**scenario), config)
        assert STATE_REVIEW in actions.add_tags
        assert actions.add_tags & REVIEW_REASONS, scenario


def test_missing_configured_entry_is_rejected(config, result_factory):
    result = result_factory()
    broken = ClassificationResult(
        topics={"quarkonium": 0.9},  # nrqcd missing
        roles=result.roles,
        coverage=result.coverage,
    )

    with pytest.raises(PolicyError, match="missing configured entries"):
        plan(broken, config)


def test_unconfigured_entry_is_rejected(config, result_factory):
    result = result_factory()
    broken = result.model_copy(update={"topics": {**result.topics, "lattice-qcd": 0.9}})

    with pytest.raises(PolicyError, match="not configured"):
        plan(broken, config)


def test_error_actions_leave_exactly_one_queue():
    actions = error_actions()

    assert actions.add_tags == {STATE_ERROR}
    assert STATE_PROCESSED in actions.remove_tags
    assert STATE_REVIEW in actions.remove_tags
    assert REVIEW_REASONS <= actions.remove_tags


def test_review_actions_record_the_reason_and_do_not_mark_processed():
    actions = review_actions()

    assert actions.add_tags == {STATE_REVIEW, REASON_MISSING_ABSTRACT}
    assert STATE_PROCESSED not in actions.add_tags


def test_plan_is_deterministic(config, result_factory):
    result = result_factory(topics={"quarkonium": 0.9, "nrqcd": 0.9})

    assert plan(result, config) == plan(result, config)
