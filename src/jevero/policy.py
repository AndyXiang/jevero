"""Deterministic policy: probabilities in, Zotero tag actions out.

This module is the only place allowed to turn semantic judgments into
mutations. It performs no I/O, knows nothing about HTTP, OpenRouter, or Zotero
response shapes, and is a pure function of ``(ClassificationResult, Config)``.

Everything it emits must be explainable by an explicit threshold in
``config.yaml``. If a tag cannot be traced to a comparison below, it does not
belong in this file.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from .config import Config
from .models import (
    COVERAGE_COVERED,
    COVERAGE_IRRELEVANT,
    COVERAGE_MISSING_TOPIC,
    ClassificationResult,
    InputMode,
    PolicyActions,
)

TAG_TOPIC_PREFIX = "topic/"
TAG_ROLE_PREFIX = "role/"

#: Agent state markers. The MVP uses tags as state; there is no local DB.
#:
#: A paper gets exactly one *state* tag, and a review state additionally gets
#: one or more *reason* tags. State and reason are split so that "show me
#: everything waiting on a human" is a single tag query (``agent/review``)
#: while each reason still forms its own work queue.
STATE_PROCESSED = "agent/processed"
STATE_REVIEW = "agent/review"
STATE_ERROR = "agent/error"

#: Why a paper needs a human. Every reason maps to a different next action,
#: which is the whole point of naming them separately.
#:
#: ``ambiguous``    the topics stayed undecided: nothing cleared
#:                  ``topic_apply``, yet the best guess was plausible ->
#:                  calibrate a threshold or rewrite a description;
#: ``coverage``     the coverage judgement itself is unconvincing (``covered``
#:                  is low) -> read the paper and decide what it is;
#: ``taxonomy-gap`` in scope, but no configured topic fits -> decide whether
#:                  ``config.yaml`` needs a new topic (a human decision);
#: ``missing-abstract`` too little text to judge -> supply metadata or accept
#:                  a title-only classification.
REASON_AMBIGUOUS = "agent/review/ambiguous"
REASON_COVERAGE = "agent/review/coverage"
REASON_TAXONOMY_GAP = "agent/review/taxonomy-gap"
REASON_MISSING_ABSTRACT = "agent/review/missing-abstract"

REVIEW_REASONS: frozenset[str] = frozenset(
    {REASON_AMBIGUOUS, REASON_COVERAGE, REASON_TAXONOMY_GAP, REASON_MISSING_ABSTRACT}
)

PROCESSING_STATES: frozenset[str] = frozenset(
    {STATE_PROCESSED, STATE_REVIEW, STATE_ERROR}
) | REVIEW_REASONS


class PolicyError(Exception):
    """Raised when a classification result cannot be evaluated safely."""


def plan(
    result: ClassificationResult,
    config: Config,
    *,
    input_mode: InputMode = InputMode.FULL,
) -> PolicyActions:
    """Translate one classification result into tag actions.

    Args:
        result: probabilities for every configured topic, role, and coverage
            state.
        config: the validated configuration supplying taxonomy and thresholds.
        input_mode: ``TITLE_ONLY`` results always carry the
            ``agent/review/missing-abstract`` reason, so a thin-evidence
            classification is never silently trusted.

    Raises:
        PolicyError: the result is missing entries the configuration requires,
            or contains names that are not configured.
    """
    _check_dimensions(result, config)
    thresholds = config.thresholds

    applied_topics = _above(result.topics, config.topics, thresholds.topic_apply)
    applied_roles = _above(result.roles, config.roles, thresholds.role_apply)

    actions = PolicyActions()
    actions.add_tags.update(TAG_TOPIC_PREFIX + name for name in applied_topics)
    actions.add_tags.update(TAG_ROLE_PREFIX + name for name in applied_roles)

    # Coverage is modelled separately from topic membership: there is no
    # `topic/other`. Each coverage state is compared against its own threshold,
    # so the reasons below stay individually explainable.
    out_of_scope = result.coverage[COVERAGE_IRRELEVANT] >= thresholds.irrelevant
    taxonomy_gap = (
        result.coverage[COVERAGE_MISSING_TOPIC] >= thresholds.missing_topic_review
        and not out_of_scope
    )
    coverage_confident = result.coverage[COVERAGE_COVERED] >= thresholds.covered_apply
    # `covered` is the taxonomy's own adequacy judgement. When it is not
    # convincing, and neither of the specific stories (out of scope, missing
    # topic) applies, the result is reported rather than acted on.
    coverage_unclear = not coverage_confident and not out_of_scope and not taxonomy_gap

    reasons: set[str] = set()
    if _has_ambiguous(result, config):
        reasons.add(REASON_AMBIGUOUS)
    if taxonomy_gap:
        reasons.add(REASON_TAXONOMY_GAP)
    if coverage_unclear:
        reasons.add(REASON_COVERAGE)
    if input_mode is InputMode.TITLE_ONLY:
        reasons.add(REASON_MISSING_ABSTRACT)

    if reasons:
        actions.add_tags.add(STATE_REVIEW)
        actions.add_tags.update(reasons)

    # A paper that was classified successfully is marked processed even when it
    # is out of scope or flagged for review; the flags are what the human reads.
    actions.add_tags.add(STATE_PROCESSED)
    return _converged(actions)


def error_actions() -> PolicyActions:
    """Actions for a paper whose processing failed.

    The paper leaves the processed and review queues and lands in exactly one
    queue, ``agent/error``, so a stale review reason cannot linger. It stays in
    the candidate set and remains recoverable.
    """
    return _converged(PolicyActions(add_tags={STATE_ERROR}))


def review_actions() -> PolicyActions:
    """Actions for a paper that was skipped instead of classified.

    Used when an abstract is missing and title-only classification is disabled:
    the paper is never treated as processed, and the reason records why.
    """
    return _converged(
        PolicyActions(add_tags={STATE_REVIEW, REASON_MISSING_ABSTRACT})
    )


def _converged(actions: PolicyActions) -> PolicyActions:
    """Report the full managed state, so a re-run removes superseded state tags.

    ``add_tags`` alone is not enough: a plan that no longer wants
    ``agent/review`` would leave the tag a previous run (or an older policy)
    wrote, and the library would accumulate the union of every judgement ever
    made. Removing every managed state tag the plan does not ask for makes
    reclassification converge instead of accumulating.

    Only the ``agent/*`` namespace is managed this way. ``topic/*`` and
    ``role/*`` stay add-only: a human may have added them by hand, and the
    model's probabilities drift by a few hundredths between runs, so removing
    them would delete intent and flap.
    """
    actions.remove_tags |= PROCESSING_STATES - actions.add_tags
    return actions


def _above(
    probabilities: Mapping[str, float],
    configured: Mapping[str, object],
    threshold: float,
) -> list[str]:
    """Configured names whose probability reaches ``threshold``, most likely first."""
    selected = [name for name in configured if probabilities[name] >= threshold]
    selected.sort(key=lambda name: (-probabilities[name], name))
    return selected


def _has_ambiguous(result: ClassificationResult, config: Config) -> bool:
    """True when the topics could not be decided at all.

    A dimension that produced at least one applied tag counts as decided: a
    second, weaker candidate in the review band ("maybe also this") is not worth
    interrupting a human for. A dimension with nothing applied whose best
    candidate is merely plausible is genuinely undecided, and gets reported.

    Only topics are considered. Roles are facets, so "no confident role" is a
    normal outcome rather than something to report, and with several roles
    review would fire on almost every paper. Roles still produce tags; they just
    do not decide whether a human is needed.
    """
    thresholds = config.thresholds
    dimensions = ((result.topics, config.topics, thresholds.topic_apply),)
    for probabilities, configured, apply_threshold in dimensions:
        if any(probabilities[name] >= apply_threshold for name in configured):
            continue  # this dimension is decided
        if any(probabilities[name] >= thresholds.review for name in configured):
            return True
    return False


def _check_dimensions(result: ClassificationResult, config: Config) -> None:
    """Reject results that are missing configured names or invent new ones."""
    dimensions = (
        ("topics", result.topics, config.topics),
        ("roles", result.roles, config.roles),
        ("coverage", result.coverage, config.coverage),
    )
    for label, probabilities, configured in dimensions:
        missing = _sorted_missing(configured, probabilities)
        if missing:
            raise PolicyError(
                f"{label} is missing configured entries: {', '.join(missing)}"
            )
        unknown = _sorted_missing(probabilities, configured)
        if unknown:
            raise PolicyError(
                f"{label} contains entries that are not configured: "
                f"{', '.join(unknown)}"
            )


def _sorted_missing(expected: Iterable[str], actual: Mapping[str, object]) -> list[str]:
    return sorted(name for name in expected if name not in actual)
