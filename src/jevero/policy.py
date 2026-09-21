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
#: What kind of paper this is (theory / method / overview / ...). Named ``kind``
#: rather than ``role`` because that is what the dimension actually asks: after
#: ``role/core`` was retired, nothing here is about the reader's work any more.
TAG_KIND_PREFIX = "kind/"

#: Why a paper needs a human. This is the one piece of machine output a reader is
#: meant to *read and filter on* inside Zotero, so it stays a tag namespace — the
#: ``04 Review`` collection is the queue, and these tags are the reasons inside it.
#:
#: ``ambiguous``    the topics stayed undecided: nothing cleared the floor, yet
#:                  the best guess was plausible -> calibrate a threshold or
#:                  rewrite a description;
#: ``coverage``     the coverage judgement itself is unconvincing (``covered``
#:                  is low) -> read the paper and decide what it is;
#: ``taxonomy-gap`` in scope, but no configured topic fits -> decide whether
#:                  ``config.yaml`` needs a new topic (a human decision);
#: ``missing-abstract`` too little text to judge -> supply metadata or accept
#:                  a title-only classification.
REVIEW_PREFIX = "review/"
REASON_AMBIGUOUS = REVIEW_PREFIX + "ambiguous"
REASON_COVERAGE = REVIEW_PREFIX + "coverage"
REASON_TAXONOMY_GAP = REVIEW_PREFIX + "taxonomy-gap"
REASON_MISSING_ABSTRACT = REVIEW_PREFIX + "missing-abstract"

REVIEW_REASONS: frozenset[str] = frozenset(
    {REASON_AMBIGUOUS, REASON_COVERAGE, REASON_TAXONOMY_GAP, REASON_MISSING_ABSTRACT}
)

#: Machine bookkeeping keys in Zotero's free-text ``extra`` field. Everything the
#: tool needs but a reader would never browse by lives here rather than in a tag:
#:
#: ``fingerprint`` which model, prompt, vocabulary and thresholds produced the
#:                 current tags. Its presence *is* "this paper was judged"; a
#:                 different value means the judgement is out of date.
#: ``error``       the last attempt failed, and why. Cleared by the next success,
#:                 so a failure is retried rather than frozen in place.
EXTRA_FINGERPRINT = "jevero-fingerprint"
EXTRA_ERROR = "jevero-error"


class PolicyError(Exception):
    """Raised when a classification result cannot be evaluated safely."""


def plan(
    result: ClassificationResult,
    config: Config,
    *,
    input_mode: InputMode = InputMode.FULL,
    fingerprint: str | None = None,
) -> PolicyActions:
    """Translate one classification result into tag actions.

    Args:
        result: probabilities for every configured topic, kind, and coverage
            state.
        config: the validated configuration supplying taxonomy and thresholds.
        input_mode: ``TITLE_ONLY`` results always carry the
            ``review/missing-abstract`` reason, so a thin-evidence classification
            is never silently trusted.
        fingerprint: judgement digest to record in the item's ``extra`` field. It
            supersedes whatever was there, which is how staleness is detected
            without a local database.

    Raises:
        PolicyError: the result is missing entries the configuration requires,
            or contains names that are not configured.
    """
    _check_dimensions(result, config)
    thresholds = config.thresholds

    applied_topics = _select_topics(result, config)
    applied_kinds = _above(result.kinds, config.kinds, thresholds.kind_floor)[
        : thresholds.kind_max
    ]

    actions = PolicyActions()
    actions.add_tags.update(TAG_TOPIC_PREFIX + name for name in applied_topics)
    actions.add_tags.update(TAG_KIND_PREFIX + name for name in applied_kinds)

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

    actions.add_tags.update(reasons)
    # `extra` carries the bookkeeping a reader would never browse by: the
    # judgement stamp, and the absence of a past failure. The stamp's presence is
    # what "judged" means, so there is no processed tag to write.
    actions.extra[EXTRA_FINGERPRINT] = fingerprint
    actions.extra[EXTRA_ERROR] = None

    return _reconcile_vocabulary(_reconcile_review(actions), config)


def _select_topics(result: ClassificationResult, config: Config) -> list[str]:
    """Which topics a paper gets: a guaranteed band, then a ranked band.

    Independent judgements have no natural cut-off, so selection is split into two
    questions that do have answers:

    * at or above ``topic_guaranteed`` a topic is headline material for the paper;
      it is applied whatever the cap says, so a paper with four genuine topics can
      never lose one to a counting rule;
    * between ``apply_floor`` and that band, topics are ranked and only the
      strongest fill what the guaranteed band left of ``topic_max``.

    The floor sits in the empty valley of the judgement distribution, so noise
    cannot flip membership; the cap decides focus, which makes the result
    independent of how many words the vocabulary happens to contain.
    """
    thresholds = config.thresholds
    ranked = _above(result.topics, config.topics, thresholds.apply_floor)
    selected = [
        name for name in ranked if result.topics[name] >= thresholds.topic_guaranteed
    ]
    for name in ranked:
        if len(selected) >= thresholds.topic_max:
            break
        if name not in selected:
            selected.append(name)
    return selected


def vocabularies(config: Config) -> dict[str, set[str]]:
    """Managed tag names, grouped for reporting: topics, kinds, retired."""
    return {
        "topics": {TAG_TOPIC_PREFIX + name for name in config.topics},
        "kinds": {TAG_KIND_PREFIX + name for name in config.kinds},
        "retired": {TAG_TOPIC_PREFIX + name for name in config.retired_topics}
        | {TAG_KIND_PREFIX + name for name in config.retired_kinds},
    }


def _reconcile_review(actions: PolicyActions) -> PolicyActions:
    """Report the full set of review reasons, so a stale one cannot linger.

    Review reasons are a small closed set, so convergence is just "every reason
    the plan did not ask for goes away". Without it a paper that stops being
    ambiguous would keep the reason a previous run wrote.
    """
    actions.remove_tags |= REVIEW_REASONS - actions.add_tags
    return actions


def _reconcile_vocabulary(actions: PolicyActions, config: Config) -> PolicyActions:
    """Report the full machine-owned vocabulary, so superseded words go away.

    ``topic/*`` and ``kind/*`` are machine-owned (see AGENTS.md): every run states
    what these namespaces should contain, and a name the plan does not ask for is
    removed. That is what makes retiring a word clean up after itself instead of
    leaving tags that no longer mean anything.

    Only names we know about are ever removed — the current vocabulary, plus
    anything listed as retired. A ``topic/...`` tag the reader typed themselves is
    neither, so it survives untouched. Whole namespaces that were renamed are
    cleared through ``retired_prefixes``, because their old names cannot be
    enumerated one by one.
    """
    known = vocabularies(config)
    managed = known["topics"] | known["kinds"] | known["retired"]
    actions.remove_tags |= managed - actions.add_tags
    actions.remove_tag_prefixes |= set(config.retired_prefixes)
    return actions


def error_actions(reason: str) -> PolicyActions:
    """Actions for a paper whose processing failed.

    A failure clears the judgement stamp and its review reasons — the paper is
    back to "not judged", so the next run retries it — and records why in
    ``extra``. It is never filed into a collection, because nothing trustworthy
    can be said about where it belongs.
    """
    actions = PolicyActions(remove_tags=set(REVIEW_REASONS))
    actions.extra[EXTRA_FINGERPRINT] = None
    actions.extra[EXTRA_ERROR] = reason
    return actions


def review_actions() -> PolicyActions:
    """Actions for a paper that was skipped instead of classified.

    Used when an abstract is missing and title-only classification is disabled:
    the paper is not judged (no stamp), and the reason records why.
    """
    return _reconcile_review(
        PolicyActions(
            add_tags={REASON_MISSING_ABSTRACT},
            extra={EXTRA_FINGERPRINT: None, EXTRA_ERROR: None},
        )
    )


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

    Only topics are considered. Kinds are facets, so "no confident kind" is a
    normal outcome rather than something to report, and with several kinds
    review would fire on almost every paper. Kinds still produce tags; they just
    do not decide whether a human is needed.
    """
    thresholds = config.thresholds
    dimensions = ((result.topics, config.topics, thresholds.apply_floor),)
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
        ("kinds", result.kinds, config.kinds),
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
