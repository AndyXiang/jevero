"""Deterministic projection of tags onto Zotero collections.

Routing does not judge anything: it reads the tags a paper already has and says
where that paper belongs. Keeping it a pure function of ``(tags, config)`` means
the mapping can be re-run at any time, offline and for free, and that the same
tags always produce the same membership.

Collections are never inferred from probabilities. The thresholds are
interpreted once, in ``policy.py``, and this module only reads the result.
"""

from __future__ import annotations

from collections.abc import Iterable

from .config import Config

TOPIC_PREFIX = "topic/"
ROLE_PREFIX = "role/"
REVIEW_STATE = "agent/review"
PROCESSED_STATE = "agent/processed"


def target_paths(tags: Iterable[str], config: Config) -> list[str]:
    """Collection paths a paper with ``tags`` belongs in.

    Only successfully processed papers are routed: ``agent/processed`` is the
    gate, so a paper that failed (``agent/error``) is never filed as if it had
    been classified. Paths are returned sorted for stable output.

    A paper in review is filed in its topic collection *and* the review queue:
    the topic collections are the archive, the review queue is a work queue.
    """
    seen = set(tags)
    if PROCESSED_STATE not in seen:
        return []

    settings = config.collections
    paths: set[str] = set()

    for tag in seen:
        if tag.startswith(TOPIC_PREFIX) and len(tag) > len(TOPIC_PREFIX):
            paths.add(_join(settings.topics_parent, tag[len(TOPIC_PREFIX) :]))
        elif (
            settings.route_roles
            and tag.startswith(ROLE_PREFIX)
            and len(tag) > len(ROLE_PREFIX)
        ):
            paths.add(_join(settings.roles_parent, tag[len(ROLE_PREFIX) :]))

    in_review = any(
        tag == REVIEW_STATE or tag.startswith(REVIEW_STATE + "/") for tag in seen
    )
    if in_review and settings.review_collection:
        paths.add(settings.review_collection)

    return sorted(paths)


def _join(parent: str, name: str) -> str:
    parent = parent.strip("/")
    return f"{parent}/{name}" if parent else name
