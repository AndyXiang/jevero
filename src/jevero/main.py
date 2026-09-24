"""CLI orchestration.

    find candidate papers -> fetch metadata -> call Jev -> evaluate policy
    -> print dry-run -> optionally apply

Safety rules enforced here:

* dry-run is the default; ``--apply`` is required for any mutation;
* the classifier never mutates Zotero, and this module never invents tags —
  every tag printed comes from ``policy.py``;
* one failing paper does not abort the run; it is reported and left
  recoverable.
"""

from __future__ import annotations

import os
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

import typer
from pydantic import ValidationError

from .config import (
    Config,
    ConfigError,
    load_config,
    load_environment,
    openrouter_api_key,
    store_zotero_write_key,
    zotero_write_key,
)
from .jev import JevClient, JevError, JevOutcome, JevTransportError, fingerprint
from .models import InputMode, PaperRecord, PolicyActions
from .routing import foreign_tags, is_managed_path, target_paths
from .policy import (
    EXTRA_ERROR,
    EXTRA_FINGERPRINT,
    REASON_MISSING_ABSTRACT,
    REVIEW_PREFIX,
    TAG_KIND_PREFIX,
    TAG_TOPIC_PREFIX,
    error_actions,
    plan,
    review_actions,
    vocabularies,
)
from .zotero import (
    ZoteroClient,
    ZoteroError,
    ZoteroItem,
    ZoteroNotFoundError,
    ZoteroWriteError,
    collection_path,
)

DEFAULT_CONFIG = Path("config.yaml")

app = typer.Typer(
    add_completion=False,
    help="Classify Zotero papers with Jev and write deterministic tags back.",
)


@dataclass
class PaperPlan:
    """One paper's classification and the actions it implies."""

    item: ZoteroItem
    paper: PaperRecord
    actions: PolicyActions
    outcome: JevOutcome | None = None
    note: str | None = None
    #: Characters of the paper's own text that were sent, and how many it has.
    text_sent: int = 0
    text_available: int = 0


@dataclass
class RunSummary:
    processed: int = 0
    flagged: int = 0
    skipped: int = 0
    failed: int = 0
    cost: float = 0.0
    #: How often each topic/kind tag came out of this run, and how many papers
    #: were judged. Printed at the end: a word that never fires, or one that
    #: fires on almost every paper, is a vocabulary problem, and it should be
    #: visible the moment it appears rather than months later.
    applied: Counter[str] = field(default_factory=Counter)
    considered: int = 0
    #: Papers judged because their fingerprint was out of date, so a run that
    #: re-classifies the library says why it did.
    stale: int = 0
    #: Set when the classifier became unreachable: the run stops, because a
    #: transport failure says nothing about any individual paper.
    unavailable: str | None = None


@dataclass
class RouteSummary:
    routed: int = 0
    skipped: int = 0
    failed: int = 0
    #: `topic/...` / `kind/...` tags that are neither configured nor retired.
    #: Never routed (the vocabulary is closed) and worth reporting.
    foreign: set[str] = field(default_factory=set)


@app.command()
def process(
    config_path: Path = typer.Option(
        DEFAULT_CONFIG, "--config", "-c", help="Path to config.yaml."
    ),
    apply: bool = typer.Option(
        False, "--apply", help="Write tags to Zotero. Without it, nothing changes."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Explicitly inspect only. This is the default."
    ),
    limit: int = typer.Option(0, "--limit", help="Stop after N papers (0 = no limit)."),
    include_processed: bool = typer.Option(
        False, "--include-processed", help="Reconsider papers already tagged agent/processed."
    ),
    collection: str | None = typer.Option(
        None,
        "--collection",
        help="Process this collection (name or 'Parent/Child') instead of the inbox.",
    ),
    whole_library: bool = typer.Option(
        False, "--all", help="Process every top-level item in the library."
    ),
) -> None:
    """Classify papers, by default those in the configured Inbox collection."""
    if apply and dry_run:
        raise typer.BadParameter("--apply and --dry-run are mutually exclusive")
    if collection and whole_library:
        raise typer.BadParameter("--collection and --all are mutually exclusive")
    if limit < 0:
        raise typer.BadParameter("--limit must not be negative")

    mutate = apply
    if not mutate:
        typer.secho("[dry-run] no Zotero changes will be made", fg=typer.colors.YELLOW)

    try:
        config = load_config(config_path)
        load_environment()
        summary = _run(
            config,
            mutate=mutate,
            limit=limit or None,
            include_processed=include_processed,
            collection=collection,
            whole_library=whole_library,
        )
    except ConfigError as exc:
        typer.secho(f"configuration error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from exc
    except (ZoteroError, JevError) as exc:
        # Setup failures (missing credentials, unreachable library) are exit
        # codes, not tracebacks.
        typer.secho(f"error: {exc}", fg=typer.colors.RED, err=True)
        if isinstance(exc, ZoteroNotFoundError):
            typer.secho(
                "    run `jevero collections` to list collection names and paths",
                fg=typer.colors.YELLOW,
                err=True,
            )
        raise typer.Exit(code=2) from exc

    typer.echo("")
    typer.echo(
        f"{summary.processed} processed, {summary.flagged} flagged, "
        f"{summary.skipped} skipped, {summary.failed} failed"
    )
    if summary.cost:
        typer.echo(f"classifier cost: ${summary.cost:.6f}")
    _render_vocabulary(config, summary)
    if summary.unavailable:
        typer.secho(
            f"stopped: the classifier is unreachable ({summary.unavailable}); no "
            "paper was marked failed, so a re-run retries them",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=2)
    if summary.failed:
        raise typer.Exit(code=1)


@app.command()
def collections(
    config_path: Path = typer.Option(
        DEFAULT_CONFIG, "--config", "-c", help="Path to config.yaml."
    ),
    counts: bool = typer.Option(
        True, "--counts/--no-counts", help="Count papers per collection."
    ),
) -> None:
    """List Zotero collections, to set `zotero.inbox_collection`."""
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        typer.secho(f"configuration error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from exc

    configured = config.zotero.inbox_collection
    typer.echo(f"zotero.inbox_collection = {configured!r}")
    typer.echo("")

    try:
        with _zotero_client(config) as zotero:
            found = zotero.list_collections()
            by_key = {collection.key: collection for collection in found}
            rows = sorted(
                found, key=lambda collection: collection_path(collection, by_key).lower()
            )
            width = max(
                (len(collection_path(collection, by_key)) for collection in rows),
                default=0,
            )
            for collection in rows:
                path = collection_path(collection, by_key)
                line = f"  {path:<{width}}  {collection.key}"
                if counts:
                    items = list(zotero.iter_papers(collection_key=collection.key))
                    pending = sum(1 for item in items if _fingerprint(item) is None)
                    line += f"  papers={len(items):<4} unprocessed={pending}"
                if path == configured or collection.name == configured:
                    line += "   <- configured"
                typer.echo(line)
    except ZoteroError as exc:
        typer.secho(f"Zotero local API: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from exc


@app.command()
def run(
    config_path: Path = typer.Option(
        DEFAULT_CONFIG, "--config", "-c", help="Path to config.yaml."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show what would happen; write nothing."
    ),
    limit: int = typer.Option(0, "--limit", help="Stop after N papers (0 = no limit)."),
    no_move: bool = typer.Option(
        False, "--no-move", help="Classify but leave the papers in the inbox."
    ),
) -> None:
    """Classify the inbox and file each paper where its tags point.

    THIS WRITES TO ZOTERO BY DEFAULT — it is the one-shot "do the work" command,
    and it takes no --apply flag. Pass --dry-run to see the plan first.

    It is the equivalent of ``process --apply`` followed by
    ``route --apply --prune``, scoped to the configured inbox only.

    Papers that need a human — a missing abstract, or a review flag — stay in the
    inbox on purpose, so the inbox reads as the remaining work.
    """
    mutate = not dry_run
    if not mutate:
        typer.secho("[dry-run] no Zotero changes will be made", fg=typer.colors.YELLOW)

    try:
        config = load_config(config_path)
        load_environment()
        summary = _run(
            config,
            mutate=mutate,
            limit=limit or None,
            include_processed=False,
            collection=None,
            whole_library=False,
        )
        route_summary = None
        if mutate and not no_move:
            route_summary = _route(
                config,
                mutate=True,
                prune=True,
                limit=None,
                collection=None,
                whole_library=False,
            )
    except ConfigError as exc:
        typer.secho(f"configuration error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from exc
    except (ZoteroError, JevError) as exc:
        typer.secho(f"error: {exc}", fg=typer.colors.RED, err=True)
        if isinstance(exc, ZoteroNotFoundError):
            typer.secho(
                "    run `jevero collections` to list collection names and paths",
                fg=typer.colors.YELLOW,
                err=True,
            )
        raise typer.Exit(code=2) from exc

    typer.echo("")
    typer.echo(
        f"{summary.processed} processed, {summary.flagged} flagged, "
        f"{summary.skipped} skipped, {summary.failed} failed"
    )
    if summary.cost:
        typer.echo(f"classifier cost: ${summary.cost:.6f}")
    _render_vocabulary(config, summary)
    if route_summary is not None:
        typer.echo(
            f"Filed: {route_summary.routed} routed, {route_summary.skipped} unchanged"
        )
    if mutate:
        _report_inbox_remainder(config)
    if summary.unavailable:
        typer.secho(
            f"stopped before filing: the classifier is unreachable "
            f"({summary.unavailable})",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=2)
    if summary.failed or (route_summary is not None and route_summary.failed):
        raise typer.Exit(code=1)


def _report_inbox_remainder(config: Config) -> None:
    """Say what is still in the inbox, and why, after a run."""
    with _zotero_client(config) as zotero:
        key = zotero.find_collection_key(config.zotero.inbox_collection)
        items = list(zotero.iter_papers(collection_key=key))

    if not items:
        typer.secho("Inbox is now empty.", fg=typer.colors.GREEN)
        return

    unclassified = [item for item in items if _fingerprint(item) is None]
    need_metadata = [
        item for item in unclassified if REASON_MISSING_ABSTRACT in item.tags
    ]
    awaiting = [item for item in unclassified if item not in need_metadata]

    typer.echo(f"Inbox now holds {len(items)} papers:")
    if not config.collections.remove_from_inbox:
        kept = len(items) - len(unclassified)
        if kept:
            typer.secho(
                f"  {kept} classified but kept (remove_from_inbox is off)",
                fg=typer.colors.YELLOW,
            )
    if need_metadata:
        typer.secho(
            f"  {len(need_metadata)} need metadata (no abstract -> "
            "agent/review/missing-abstract)",
            fg=typer.colors.YELLOW,
        )
    if awaiting:
        typer.secho(
            f"  {len(awaiting)} awaiting review (add abstract? -> see their "
            "agent/review/* tags)",
            fg=typer.colors.YELLOW,
        )


@app.command()
def route(
    config_path: Path = typer.Option(
        DEFAULT_CONFIG, "--config", "-c", help="Path to config.yaml."
    ),
    apply: bool = typer.Option(
        False, "--apply", help="Write collection membership. Without it, nothing changes."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Explicitly inspect only. This is the default."
    ),
    prune: bool = typer.Option(
        False,
        "--prune",
        help="Also drop managed memberships the tags no longer point at.",
    ),
    limit: int = typer.Option(0, "--limit", help="Stop after N papers (0 = no limit)."),
    collection: str | None = typer.Option(
        None,
        "--collection",
        help="Route this collection (name or 'Parent/Child') instead of the inbox.",
    ),
    whole_library: bool = typer.Option(
        False, "--all", help="Route every top-level item in the library."
    ),
) -> None:
    """File papers into collections based on the tags they already have.

    By default only papers directly in the configured inbox are considered; use
    --collection or --all for a wider run, which asks for confirmation before
    writing. It never calls the classifier either: it only reads tags and writes
    collection membership, so re-running it after changing the collection
    settings is free.
    """
    if apply and dry_run:
        raise typer.BadParameter("--apply and --dry-run are mutually exclusive")
    if collection and whole_library:
        raise typer.BadParameter("--collection and --all are mutually exclusive")
    mutate = apply
    if not mutate:
        typer.secho("[dry-run] no Zotero changes will be made", fg=typer.colors.YELLOW)

    try:
        config = load_config(config_path)
        load_environment()
        summary = _route(
            config,
            mutate=mutate,
            prune=prune,
            limit=limit or None,
            collection=collection,
            whole_library=whole_library,
        )
    except ConfigError as exc:
        typer.secho(f"configuration error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from exc
    except (ZoteroError, JevError) as exc:
        typer.secho(f"error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from exc

    typer.echo("")
    typer.echo(f"{summary.routed} routed, {summary.skipped} unchanged, {summary.failed} failed")
    if summary.foreign:
        typer.secho(
            f"{len(summary.foreign)} managed-namespace tag(s) are neither "
            "configured nor retired, so they were not routed: "
            + ", ".join(sorted(summary.foreign)),
            fg=typer.colors.YELLOW,
        )
        typer.echo(
            "    add them to config.yaml, or retire them so the next "
            "`process --apply` removes them"
        )
    if summary.failed:
        raise typer.Exit(code=1)


@app.command()
def check(
    config_path: Path = typer.Option(
        DEFAULT_CONFIG, "--config", "-c", help="Path to config.yaml."
    ),
) -> None:
    """Validate configuration and report what the pipeline can reach."""
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        typer.secho(f"configuration error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from exc

    typer.echo(f"config: {config_path}")
    typer.echo(f"  model:    {config.model.provider} / {config.model.name}")
    typer.echo(
        f"  topics:   {len(config.topics)}  kinds: {len(config.kinds)}"
    )
    typer.echo(f"  coverage: {', '.join(config.coverage)}")
    retired = vocabularies(config)["retired"]
    typer.echo(f"  fingerprint: {fingerprint(config)}")
    if retired:
        typer.echo(f"  retired words: {', '.join(sorted(retired))}")
    typer.echo(
        "  title-only classification: "
        f"{'allowed' if config.classification.allow_title_only else 'disabled'}"
    )

    load_environment()
    _report_env("OPENROUTER_API_KEY", openrouter_api_key)
    if zotero_write_key():
        typer.secho(
            "  ZOTERO_LOCAL_WRITE_KEY: set (writes will not ask Zotero)", fg=typer.colors.GREEN
        )
    else:
        typer.secho(
            "  ZOTERO_LOCAL_WRITE_KEY: not stored yet (first write asks once)",
            fg=typer.colors.YELLOW,
        )
    if not os.environ.get("OPENROUTER_API_KEY"):
        typer.echo(
            f"    put it in {Path('.env').resolve()} "
            "(cp .env.example .env), or export it in the shell"
        )

    try:
        with _zotero_client(config) as zotero:
            collection_key = zotero.find_collection_key(config.zotero.inbox_collection)
            count = sum(1 for _ in zotero.iter_papers(collection_key=collection_key))
            version = zotero.zotero_version
            label = f"Zotero {version}" if version else "Zotero"
            typer.secho(
                f"  Zotero local API at {config.zotero.base_url}: ok ({label})",
                fg=typer.colors.GREEN,
            )
            typer.secho(
                f"  Inbox {config.zotero.inbox_collection!r}: {count} top-level items",
                fg=typer.colors.GREEN,
            )
            library = list(zotero.iter_papers(collection_key=None))
            stamp = fingerprint(config)
            judged = [item for item in library if _fingerprint(item) is not None]
            stale = sum(
                1
                for item in judged
                if _is_stale(item, stamp, legacy_prefixes=config.retired_prefixes)
            )
            legacy = [
                item
                for item in library
                if _fingerprint(item) is None
                and _is_stale(item, stamp, legacy_prefixes=config.retired_prefixes)
            ]
            if stale or legacy:
                typer.secho(
                    f"  out of date: {stale + len(legacy)} of {len(library)} papers "
                    "need judging with the current configuration; the next "
                    "`process` picks them up",
                    fg=typer.colors.YELLOW,
                )
            else:
                typer.secho(
                    f"  up to date: all {len(judged)} judged papers match the "
                    "current fingerprint",
                    fg=typer.colors.GREEN,
                )
            if zotero.server_id:
                typer.secho(
                    "  local writes: available (stored key reused, no dialog)"
                    if zotero_write_key()
                    else '  local writes: available (the first write asks for '
                    '"Always Allow" once)',
                    fg=typer.colors.GREEN,
                )
            else:
                typer.secho(
                    "  local writes: unavailable, this Zotero does not report a "
                    "Zotero-Server-ID (local writes need Zotero 10+)",
                    fg=typer.colors.YELLOW,
                )
    except ZoteroError as exc:
        typer.secho(f"  Zotero local API: {exc}", fg=typer.colors.RED)
        if isinstance(exc, ZoteroNotFoundError):
            typer.secho(
                "    run `jevero collections` to list collection names and paths",
                fg=typer.colors.YELLOW,
            )


def _resolve_scope(
    zotero: ZoteroClient,
    config: Config,
    *,
    collection: str | None,
    whole_library: bool,
) -> tuple[str | None, str]:
    """Which papers a run may touch, and how to describe that scope."""
    if whole_library:
        return None, "the whole library"
    if collection:
        return zotero.find_collection_key(collection), f"collection {collection!r}"
    return (
        zotero.find_collection_key(config.zotero.inbox_collection),
        f"inbox {config.zotero.inbox_collection!r}",
    )


def _fingerprint(item: ZoteroItem) -> str | None:
    """The judgement stamp recorded on an item, if it was judged at all."""
    return item.extra_values.get(EXTRA_FINGERPRINT)


def _is_stale(
    item: ZoteroItem, stamp: str, *, legacy_prefixes: Iterable[str] = ()
) -> bool:
    """True when a paper was judged before, but not with the current fingerprint.

    A paper with no judgement at all is not stale: whether to judge it is a
    question about scope. Evidence of an earlier judgement is a fingerprint, the
    current state tag, or any tag under a namespace that has since been renamed
    (``legacy_prefixes``) — a paper carrying those was judged by an older version,
    so its tags came from an older model, prompt, vocabulary, or threshold set.

    That last clause is what makes the first run after the fingerprint was
    introduced do something: those papers carry ``agent/processed`` and no stamp,
    and without it they would be neither up to date nor out of date.
    """
    current = _fingerprint(item)
    if current == stamp:
        return False
    if current is not None:
        return True
    prefixes = {prefix for prefix in legacy_prefixes if prefix}
    return any(tag.startswith(prefix) for tag in item.tags for prefix in prefixes)


def _candidates(
    zotero: ZoteroClient,
    *,
    scope_key: str | None,
    stamp: str,
    include_processed: bool,
    library_wide_staleness: bool,
    legacy_prefixes: Iterable[str] = (),
) -> list[ZoteroItem]:
    """Papers this run should judge.

    Three reasons to include one: it has never been processed (new work), its
    judgement is out of date, or ``--include-processed`` asked for a forced
    re-run. For an inbox-scoped run staleness is checked across the whole library,
    because a configuration change is not a property of the inbox — and routing
    is what moves papers out of the inbox in the first place.
    """
    stale = lambda item: _is_stale(item, stamp, legacy_prefixes=legacy_prefixes)
    found: dict[str, ZoteroItem] = {
        item.key: item for item in zotero.iter_papers(collection_key=scope_key)
    }
    if library_wide_staleness:
        for item in zotero.iter_papers(collection_key=None):
            if stale(item):
                found.setdefault(item.key, item)

    selected: list[ZoteroItem] = []
    for item in found.values():
        if include_processed or _fingerprint(item) is None or stale(item):
            selected.append(item)
    return selected


def _render_vocabulary(config: Config, summary: RunSummary) -> None:
    """Per-word usage for this run.

    There are two ways a word stops carrying information: it never fires, or it
    fires on nearly every paper. Neither is visible from the tag panel, and both
    are cheap to say out loud at the moment they appear.
    """
    if not summary.considered:
        return
    total = summary.considered
    typer.echo("")
    typer.echo(f"Vocabulary usage (of {total} papers judged this run)")
    dimensions = (
        (TAG_TOPIC_PREFIX, config.topics),
        (TAG_KIND_PREFIX, config.kinds),
    )
    for prefix, names in dimensions:
        for name in names:
            tag = prefix + name
            count = summary.applied.get(tag, 0)
            note = ""
            if count == 0:
                note = "   <- never applied"
            elif count * 2 >= total:
                note = "   <- on most papers; carries little information"
            typer.echo(f"  {tag:<28} {count:>3}/{total}{note}")
    retired = vocabularies(config)["retired"]
    if retired:
        typer.echo(
            "  retired (tags removed, never re-added): "
            + ", ".join(sorted(retired))
        )


def _confirm_wide_scope(label: str, count: int, *, default_scope: bool) -> None:
    """Make a run outside the inbox an explicit, warned-about decision.

    The inbox is the safe default. Anything wider changes papers the reader did
    not hand over for this run, so it names the scope, the number of papers and
    asks for a `y` before writing anything.
    """
    if default_scope:
        return
    typer.secho("", err=True)
    typer.secho(
        f"WARNING: this run will write to {count} papers in {label}, "
        "not just the inbox.",
        fg=typer.colors.RED,
        bold=True,
        err=True,
    )
    typer.secho(
        "Tags and collection membership are modified for every paper that needs "
        "a change. Check the dry-run output first.",
        fg=typer.colors.RED,
        err=True,
    )
    if not typer.confirm("Continue?", default=False):
        typer.secho("aborted; nothing was changed", err=True)
        raise typer.Exit(code=1)


def _run(
    config: Config,
    *,
    mutate: bool,
    limit: int | None,
    include_processed: bool,
    collection: str | None,
    whole_library: bool,
) -> RunSummary:
    summary = RunSummary()
    with _zotero_client(config) as zotero:
        # Check writability before demanding a classifier credential and before
        # spending anything: on Zotero < 10 there is nothing to write with.
        if mutate:
            if not zotero.supports_write:
                raise ZoteroWriteError(
                    "cannot write: this client has no write support. Run with "
                    "--dry-run to inspect, or restore the archived Web API "
                    "client (archive/README.md)."
                )
            zotero.ensure_writes_available()

        scope_key, scope_label = _resolve_scope(
            zotero, config, collection=collection, whole_library=whole_library
        )
        inbox_key = zotero.find_collection_key(config.zotero.inbox_collection)
        stamp = fingerprint(config)
        candidates = _candidates(
            zotero,
            scope_key=scope_key,
            stamp=stamp,
            include_processed=include_processed,
            # A stale paper is stale wherever it lives: routing moves papers out
            # of the inbox, and a vocabulary change must still reach them.
            library_wide_staleness=scope_key == inbox_key,
            legacy_prefixes=config.retired_prefixes,
        )
        summary.stale = sum(
            1
            for item in candidates
            if _is_stale(item, stamp, legacy_prefixes=config.retired_prefixes)
        )
        # `--limit` counts papers to process, not items to look at: a paper that
        # needs no work must not eat one of the slots.
        if limit is not None:
            candidates = candidates[:limit]
        typer.echo(f"Scope: {scope_label} via the Zotero local API")
        typer.echo(f"Candidates: {len(candidates)} papers")
        if summary.stale:
            typer.echo(
                f"  {summary.stale} of them were judged with another model, "
                "prompt, vocabulary, or threshold set"
            )
        if mutate:
            _confirm_wide_scope(
                scope_label,
                len(candidates),
                default_scope=scope_key is not None
                and scope_key == inbox_key
                and all(
                    inbox_key in (item.data.get("collections") or [])
                    for item in candidates
                ),
            )
        if mutate:
            typer.secho(
                'writes need a one-time Zotero confirmation; choose "Always '
                'Allow" when Zotero asks (a single-use key would mean one '
                "dialog per paper)",
                fg=typer.colors.YELLOW,
            )

        with _jev_client(config) as jev:
            for item in candidates:
                _process_one(
                    item,
                    config,
                    zotero=zotero,
                    jev=jev,
                    mutate=mutate,
                    summary=summary,
                    stamp=stamp,
                )
                if summary.unavailable is not None:
                    break
    return summary


def _route(
    config: Config,
    *,
    mutate: bool,
    prune: bool,
    limit: int | None,
    collection: str | None,
    whole_library: bool,
) -> RouteSummary:
    """File papers into the collections their tags point at."""
    summary = RouteSummary()
    settings = config.collections
    managed: set[str] = set()
    with _zotero_client(config) as zotero:
        if mutate:
            zotero.ensure_writes_available()
        if prune:
            managed = {
                key
                for path, key in zotero.collection_paths().items()
                if is_managed_path(path, config)
            }
        inbox_key = zotero.find_collection_key(config.zotero.inbox_collection)
        scope_key, scope_label = _resolve_scope(
            zotero, config, collection=collection, whole_library=whole_library
        )
        items = list(zotero.iter_papers(collection_key=scope_key))
        for item in items:
            summary.foreign.update(foreign_tags(item.tags, config))
        routable = [
            item
            for item in items
            if target_paths(item.tags, config, judged=_fingerprint(item) is not None)
        ]
        summary.skipped = len(items) - len(routable)
        if limit is not None:
            routable = routable[:limit]
        typer.echo(f"Scope: {scope_label} via the Zotero local API")
        typer.echo(f"Candidates: {len(routable)} papers with routable tags")
        if mutate:
            _confirm_wide_scope(
                scope_label,
                len(routable),
                default_scope=scope_key is not None and scope_key == inbox_key,
            )

        for item in routable:
            paths = target_paths(item.tags, config, judged=True)
            current = {str(key) for key in (item.data.get("collections") or [])}
            try:
                resolved = _resolve_targets(zotero, paths, mutate=mutate)
            except ZoteroError as exc:
                typer.secho(f"[{item.key}] routing failed: {exc}", fg=typer.colors.RED)
                summary.failed += 1
                continue

            add = {key for key in resolved.values() if key}
            missing = [path for path, key in resolved.items() if key is None]
            remove = set()
            if settings.remove_from_inbox and inbox_key not in add:
                remove.add(inbox_key)
            if prune:
                # Converge: managed collections the tags no longer point at are
                # dropped, so a re-run reflects the current tags exactly.
                remove |= (managed & current) - add

            if not missing and add <= current and not remove & current:
                summary.skipped += 1
                continue

            _render_route(
                item, resolved, remove, zotero=zotero, mutate=mutate
            )
            if mutate:
                try:
                    zotero.apply_membership(item, add=add, remove=remove)
                except ZoteroError as exc:
                    typer.secho(
                        f"[{item.key}] routing failed: {exc}", fg=typer.colors.RED
                    )
                    summary.failed += 1
                    continue
            summary.routed += 1

    return summary


def _resolve_targets(
    zotero: ZoteroClient, paths: list[str], *, mutate: bool
) -> dict[str, str | None]:
    """Map target paths to keys, creating them only when we are writing."""
    if mutate:
        return {path: zotero.ensure_collection_path(path) for path in paths}
    return {path: zotero.collection_key_for_path(path) for path in paths}


def _render_route(
    item: ZoteroItem,
    resolved: dict[str, str | None],
    remove: set[str],
    *,
    zotero: ZoteroClient,
    mutate: bool,
) -> None:
    title = str(item.data.get("title") or "").strip()
    typer.secho(f"\n[{item.key}] {title}", bold=True)
    for path, key in resolved.items():
        suffix = "" if key else "   (does not exist yet)"
        typer.secho(f"  + {path}{suffix}", fg=typer.colors.GREEN)
    by_key = {key: path for path, key in zotero.collection_paths().items()}
    for key in sorted(remove):
        typer.secho(f"  - {by_key.get(key, key)}", fg=typer.colors.RED)


def _process_one(
    item: ZoteroItem,
    config: Config,
    *,
    zotero: ZoteroClient,
    jev: JevClient,
    mutate: bool,
    summary: RunSummary,
    stamp: str,
) -> None:
    try:
        paper = item.to_paper()
    except ValidationError as exc:
        typer.secho(f"[{item.key}] unusable Zotero record: {exc}", fg=typer.colors.RED)
        summary.failed += 1
        return

    if not paper.has_abstract and not config.classification.allow_title_only:
        _apply_review_skip(paper, zotero=zotero, mutate=mutate)
        summary.skipped += 1
        return

    full_text, text_available = _paper_text(item, config, zotero=zotero)
    try:
        outcome = jev.classify(paper, config, full_text=full_text)
    except JevTransportError as exc:
        # Not this paper's fault: stop the run and leave every paper untouched,
        # rather than spraying agent/error over papers that were never judged.
        typer.secho(f"[{paper.zotero_key}] classifier unreachable: {exc}", fg=typer.colors.RED)
        summary.unavailable = str(exc)
        return
    except JevError as exc:
        typer.secho(f"[{paper.zotero_key}] classification failed: {exc}", fg=typer.colors.RED)
        _fail(item, zotero=zotero, mutate=mutate, reason=str(exc))
        summary.failed += 1
        return

    actions = plan(
        outcome.result, config, input_mode=paper.input_mode, fingerprint=stamp
    )
    _render(
        PaperPlan(
            item=item,
            paper=paper,
            actions=actions,
            outcome=outcome,
            text_sent=len(full_text),
            text_available=text_available,
        ),
        config,
    )
    if outcome.usage and outcome.usage.cost:
        summary.cost += outcome.usage.cost

    summary.considered += 1
    for tag in actions.add_tags:
        if tag.startswith(TAG_TOPIC_PREFIX) or tag.startswith(TAG_KIND_PREFIX):
            summary.applied[tag] += 1

    if _is_flagged(actions):
        summary.flagged += 1
    else:
        summary.processed += 1

    if mutate:
        _write(item, actions, zotero=zotero, summary=summary)


def _write(
    item: ZoteroItem, actions: PolicyActions, *, zotero: ZoteroClient, summary: RunSummary
) -> None:
    try:
        zotero.apply_actions(item, actions)
    except ZoteroError as exc:
        typer.secho(f"[{item.key}] could not write tags: {exc}", fg=typer.colors.RED)
        summary.failed += 1


def _fail(
    item: ZoteroItem, *, zotero: ZoteroClient, mutate: bool, reason: str
) -> None:
    """Record a failure on the paper, but only when that is safe.

    The stamp is cleared and the reason kept in ``extra``: the paper goes back to
    "not judged", so the next run retries it instead of trusting tags produced
    before the failure.
    """
    if not mutate or not zotero.supports_write:
        return
    try:
        zotero.apply_actions(item, error_actions(reason))
    except ZoteroError as exc:
        typer.secho(
            f"[{item.key}] could not record the failure in Zotero: {exc}",
            fg=typer.colors.RED,
        )


def _apply_review_skip(paper: PaperRecord, *, zotero: ZoteroClient, mutate: bool) -> None:
    typer.secho(
        f"[{paper.zotero_key}] {paper.title}\n"
        f"  skipped: no abstract; flagged for review "
        f"(set classification.allow_title_only to classify anyway)",
        fg=typer.colors.YELLOW,
    )
    if mutate:
        try:
            item = zotero.get_item(paper.zotero_key)
            zotero.apply_actions(item, review_actions())
        except ZoteroError as exc:
            typer.secho(
                f"[{paper.zotero_key}] could not flag for review: {exc}",
                fg=typer.colors.RED,
            )


def _paper_text(
    item: ZoteroItem, config: Config, *, zotero: ZoteroClient
) -> tuple[str, int]:
    """An excerpt of the paper's own text, and how much of it exists.

    The excerpt is where a paper usually says what framework it is built on, which
    its abstract often leaves out. Reading it can fail for reasons that say nothing
    about the paper — no PDF, or Zotero has not indexed it — so that yields no
    text and the judgement falls back to the abstract. A real read error is
    reported, because a broken library should not look like "no text".
    """
    settings = config.classification.full_text
    if not settings.enabled or settings.max_chars == 0:
        return "", 0
    try:
        text = zotero.full_text(item.key)
    except ZoteroError as exc:
        typer.secho(
            f"[{item.key}] could not read the paper's text: {exc}",
            fg=typer.colors.YELLOW,
        )
        return "", 0
    return text[: settings.max_chars], len(text)


def _is_flagged(actions: PolicyActions) -> bool:
    return any(tag.startswith(REVIEW_PREFIX) for tag in actions.add_tags)


def _render(plan_result: PaperPlan, config: Config) -> None:
    paper = plan_result.paper
    outcome = plan_result.outcome
    if outcome is None:
        return

    header = f"[{paper.zotero_key}] {paper.title}"
    typer.secho(f"\n{header}", bold=True)
    meta = " · ".join(
        part
        for part in (
            ", ".join(paper.authors[:3]) + (" et al." if len(paper.authors) > 3 else ""),
            str(paper.year) if paper.year else None,
            f"doi:{paper.doi}" if paper.doi else None,
            f"arXiv:{paper.arxiv_id}" if paper.arxiv_id else None,
        )
        if part
    )
    if meta:
        typer.echo(f"  {meta}")
    if paper.input_mode is not InputMode.FULL:
        typer.secho("  input: title-only", fg=typer.colors.YELLOW)
    if plan_result.text_sent:
        typer.echo(
            f"  input: abstract + {plan_result.text_sent:,} of "
            f"{plan_result.text_available:,} characters of the paper's text"
        )

    _render_scores(
        "Topics",
        outcome.result.topics,
        TAG_TOPIC_PREFIX,
        plan_result.actions,
    )
    _render_scores(
        "Kinds",
        outcome.result.kinds,
        TAG_KIND_PREFIX,
        plan_result.actions,
    )
    _render_coverage(outcome.result.coverage, plan_result.actions, config)

    typer.echo("Planned tags")
    for tag in sorted(plan_result.actions.add_tags):
        typer.secho(f"  + {tag}", fg=typer.colors.GREEN)
    # The plan removes every known name it did not ask for, so that the tag set
    # converges; show only the removals that are actually on this paper.
    current = set(plan_result.item.tags)
    removals = {tag for tag in plan_result.actions.remove_tags if tag in current}
    prefixes = plan_result.actions.remove_tag_prefixes
    if prefixes:
        removals |= {
            tag for tag in current if any(tag.startswith(p) for p in prefixes)
        }
    removals -= plan_result.actions.add_tags
    for tag in sorted(removals):
        typer.secho(f"  - {tag}", fg=typer.colors.RED)
    for key, value in sorted(plan_result.actions.extra.items()):
        previous = plan_result.item.extra_values.get(key)
        if value == previous:
            continue
        if value is None:
            shown = f"   (clears {EXTRA_FINGERPRINT if key == EXTRA_FINGERPRINT else key})"
            label = f"  extra {key} removed{shown}"
        else:
            note = f"   (was {previous})" if previous else ""
            label = f"  extra {key} = {value}{note}"
        typer.secho(label, fg=typer.colors.BLUE)


def _render_scores(
    title: str,
    scores: dict[str, float],
    prefix: str,
    actions: PolicyActions,
) -> None:
    typer.echo(f"\n{title}")
    for name, probability in sorted(scores.items(), key=lambda kv: (-kv[1], kv[0])):
        marker = "  APPLY" if prefix + name in actions.add_tags else ""
        typer.echo(f"  {name:<24} {probability:.2f}{marker}")


def _render_coverage(
    coverage: dict[str, float], actions: PolicyActions, config: Config
) -> None:
    typer.echo("\nCoverage")
    thresholds = config.thresholds
    limits = {
        "covered": thresholds.covered_apply,
        "missing-topic": thresholds.missing_topic_review,
        "irrelevant": thresholds.irrelevant,
    }
    for name, probability in sorted(coverage.items(), key=lambda kv: (-kv[1], kv[0])):
        boundary = limits.get(name)
        marker = f"  >= {boundary:.2f}" if boundary is not None and probability >= boundary else ""
        typer.echo(f"  {name:<24} {probability:.2f}{marker}")
    for tag in sorted(t for t in actions.add_tags if t.startswith(REVIEW_PREFIX)):
        typer.echo(f"  -> {tag}")


def _zotero_client(config: Config) -> ZoteroClient:
    return ZoteroClient(
        base_url=config.zotero.base_url,
        timeout_seconds=config.zotero.timeout_seconds,
        authorize_timeout_seconds=config.zotero.authorize_timeout_seconds,
        app_name=config.zotero.app_name,
        write_key=zotero_write_key(),
        on_write_key=_remember_write_key,
    )


def _remember_write_key(key: str) -> None:
    """Keep a freshly granted key in .env, so later runs ask Zotero nothing."""
    try:
        path = store_zotero_write_key(key)
    except OSError as exc:
        typer.secho(
            f"warning: could not store the Zotero write key ({exc}); the next run "
            "will ask Zotero again",
            fg=typer.colors.YELLOW,
            err=True,
        )
        return
    typer.secho(
        f"Zotero granted a write key; stored in {path} (delete that line, or use "
        "Settings -> Advanced -> Clear Write Authorizations, to revoke)",
        fg=typer.colors.YELLOW,
    )


def _jev_client(config: Config) -> JevClient:
    return JevClient(
        openrouter_api_key(),
        model=config.model.name,
        base_url=config.model.base_url,
        timeout_seconds=config.model.timeout_seconds,
    )


def _report_env(name: str, getter: Callable[[], str | None]) -> None:
    """Report whether a credential is present, never the value itself."""
    try:
        value = getter()
    except ConfigError:
        value = None
    if value:
        typer.secho(f"  {name}: set", fg=typer.colors.GREEN)
    else:
        typer.secho(f"  {name}: missing", fg=typer.colors.YELLOW)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
