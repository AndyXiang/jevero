"""Deterministic projection of tags onto Zotero collections.

Routing does not judge anything: it reads the tags a paper already has and says
where that paper belongs. Keeping it a pure function of ``(tags, config)`` means
the mapping can be re-run at any time, offline and for free, and that the same
tags always produce the same membership.

Collections are never inferred from probabilities. The thresholds are
interpreted once, in ``policy.py``, and this module only reads the result.

The vocabulary is closed: only names in ``config.yaml`` become collections. A
``topic/...`` tag left behind by a word that was deleted, or typed by hand, is
reported rather than turned into a directory — otherwise a retired word would
quietly keep resurrecting its collection.
"""

from __future__ import annotations

from collections.abc import Iterable

from .config import Config

TOPIC_PREFIX = "topic/"
KIND_PREFIX = "kind/"
REVIEW_PREFIX = "review/"


def target_paths(tags: Iterable[str], config: Config, *, judged: bool) -> list[str]:
    """Collection paths a paper with ``tags`` belongs in.

    ``judged`` says whether the paper carries a judgement stamp, and is the gate:
    a paper that failed classification is never filed as if it had been
    understood. Routing reads that from the item's ``extra`` field rather than
    from a tag, because "was this judged" is bookkeeping, not something a reader
    would browse by.

    A paper in review is filed in its topic collection *and* the review queue:
    the topic collections are the archive, the review queue is a work queue.

    Only configured names are routed. Retired words are deliberately excluded:
    their tags are on their way out, and projecting them would recreate the
    collection that retiring the word was meant to remove.
    """
    if not judged:
        return []

    seen = set(tags)
    settings = config.collections
    paths: set[str] = set()

    for tag in seen:
        if tag.startswith(TOPIC_PREFIX):
            name = tag[len(TOPIC_PREFIX) :]
            if name in config.topics:
                paths.add(_join(settings.topics_parent, name))
        elif settings.route_kinds and tag.startswith(KIND_PREFIX):
            name = tag[len(KIND_PREFIX) :]
            if name in config.kinds:
                paths.add(_join(settings.kinds_parent, name))

    if any(tag.startswith(REVIEW_PREFIX) for tag in seen) and settings.review_collection:
        paths.add(settings.review_collection)

    return sorted(paths)


def foreign_tags(tags: Iterable[str], config: Config) -> list[str]:
    """Managed-namespace tags that are neither configured nor retired.

    These are the reader's own ``topic/...`` / ``kind/...`` tags. Nothing removes
    them (only known names may be deleted) and nothing routes them, so they are
    worth reporting: either they belong in ``config.yaml``, or they are a typo.
    """
    known_topics = set(config.topics) | set(config.retired_topics)
    known_kinds = set(config.kinds) | set(config.retired_kinds)
    out: set[str] = set()
    for tag in tags:
        if tag.startswith(TOPIC_PREFIX) and tag[len(TOPIC_PREFIX) :] not in known_topics:
            out.add(tag)
        elif tag.startswith(KIND_PREFIX) and tag[len(KIND_PREFIX) :] not in known_kinds:
            out.add(tag)
    return sorted(out)


def is_managed_path(path: str, config: Config) -> bool:
    """True for collections this tool is responsible for.

    Only these may ever be pruned. Anything the reader made by hand — an
    ordinary folder, a collection outside the configured parents — is never
    touched, so routing can converge without damaging their organisation.
    """
    settings = config.collections
    if settings.review_collection and path == settings.review_collection:
        return True
    for parent in _managed_parents(config):
        if path.startswith(parent.rstrip("/") + "/"):
            return True
    return False


def _managed_parents(config: Config) -> list[str]:
    settings = config.collections
    parents = []
    if settings.topics_parent:
        parents.append(settings.topics_parent)
    if settings.route_kinds and settings.kinds_parent:
        parents.append(settings.kinds_parent)
    return parents


def _join(parent: str, name: str) -> str:
    parent = parent.strip("/")
    return f"{parent}/{name}" if parent else name
