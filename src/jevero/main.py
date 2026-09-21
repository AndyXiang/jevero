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
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import typer
from pydantic import ValidationError

from .config import (
    Config,
    ConfigError,
    load_config,
    load_environment,
    openrouter_api_key,
)
from .jev import JevClient, JevError, JevOutcome
from .models import InputMode, PaperRecord, PolicyActions
from .routing import target_paths
from .policy import (
    STATE_ERROR,
    STATE_PROCESSED,
    STATE_REVIEW,
    TAG_ROLE_PREFIX,
    TAG_TOPIC_PREFIX,
    error_actions,
    plan,
    review_actions,
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


@dataclass
class RunSummary:
    processed: int = 0
    flagged: int = 0
    skipped: int = 0
    failed: int = 0
    cost: float = 0.0


@dataclass
class RouteSummary:
    routed: int = 0
    skipped: int = 0
    failed: int = 0


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
) -> None:
    """Classify papers in the configured Inbox collection."""
    if apply and dry_run:
        raise typer.BadParameter("--apply and --dry-run are mutually exclusive")
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
                    pending = sum(
                        1 for item in items if STATE_PROCESSED not in item.tags
                    )
                    line += f"  papers={len(items):<4} unprocessed={pending}"
                if path == configured or collection.name == configured:
                    line += "   <- configured"
                typer.echo(line)
    except ZoteroError as exc:
        typer.secho(f"Zotero local API: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from exc


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
    limit: int = typer.Option(0, "--limit", help="Stop after N papers (0 = no limit)."),
) -> None:
    """File papers into collections based on the tags they already have.

    Never calls the classifier: this only reads tags and writes collection
    membership, so re-running it after changing the collection settings is free.
    """
    if apply and dry_run:
        raise typer.BadParameter("--apply and --dry-run are mutually exclusive")
    mutate = apply
    if not mutate:
        typer.secho("[dry-run] no Zotero changes will be made", fg=typer.colors.YELLOW)

    try:
        config = load_config(config_path)
        summary = _route(config, mutate=mutate, limit=limit or None)
    except ConfigError as exc:
        typer.secho(f"configuration error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from exc
    except (ZoteroError, JevError) as exc:
        typer.secho(f"error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from exc

    typer.echo("")
    typer.echo(f"{summary.routed} routed, {summary.skipped} unchanged, {summary.failed} failed")
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
        f"  topics:   {len(config.topics)}  roles: {len(config.roles)}"
    )
    typer.echo(f"  coverage: {', '.join(config.coverage)}")
    typer.echo(
        "  title-only classification: "
        f"{'allowed' if config.classification.allow_title_only else 'disabled'}"
    )

    load_environment()
    _report_env("OPENROUTER_API_KEY", openrouter_api_key)
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
            if zotero.server_id:
                typer.secho(
                    "  local writes: available (--apply will ask for "
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


def _run(
    config: Config,
    *,
    mutate: bool,
    limit: int | None,
    include_processed: bool,
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

        collection_key = zotero.find_collection_key(config.zotero.inbox_collection)
        typer.echo(
            f"Inbox {config.zotero.inbox_collection!r} ({collection_key}) "
            f"via the Zotero local API"
        )
        if mutate:
            typer.secho(
                'writes need a one-time Zotero confirmation; choose "Always '
                'Allow" when Zotero asks (a single-use key would mean one '
                "dialog per paper)",
                fg=typer.colors.YELLOW,
            )

        with _jev_client(config) as jev:
            for item in zotero.iter_papers(collection_key=collection_key, limit=limit):
                if not include_processed and STATE_PROCESSED in item.tags:
                    continue
                _process_one(
                    item, config, zotero=zotero, jev=jev, mutate=mutate, summary=summary
                )
    return summary


def _route(config: Config, *, mutate: bool, limit: int | None) -> RouteSummary:
    """File each inbox paper into the collections its tags point at."""
    summary = RouteSummary()
    settings = config.collections
    with _zotero_client(config) as zotero:
        if mutate:
            zotero.ensure_writes_available()
        collection_key = zotero.find_collection_key(config.zotero.inbox_collection)
        typer.echo(
            f"Inbox {config.zotero.inbox_collection!r} ({collection_key}) "
            f"via the Zotero local API"
        )

        for item in zotero.iter_papers(collection_key=collection_key, limit=limit):
            paths = target_paths(item.tags, config)
            if not paths:
                summary.skipped += 1
                continue

            current = {str(key) for key in (item.data.get("collections") or [])}
            try:
                resolved = _resolve_targets(zotero, paths, mutate=mutate)
            except ZoteroError as exc:
                typer.secho(f"[{item.key}] routing failed: {exc}", fg=typer.colors.RED)
                summary.failed += 1
                continue

            add = {key for key in resolved.values() if key}
            missing = [path for path, key in resolved.items() if key is None]
            remove = (
                {collection_key}
                if settings.remove_from_inbox and collection_key not in add
                else set()
            )

            if not missing and add <= current and not remove & current:
                summary.skipped += 1
                continue

            _render_route(item, resolved, remove, mutate=mutate)
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
    mutate: bool,
) -> None:
    title = str(item.data.get("title") or "").strip()
    typer.secho(f"\n[{item.key}] {title}", bold=True)
    for path, key in resolved.items():
        suffix = "" if key else "   (does not exist yet)"
        typer.secho(f"  + {path}{suffix}", fg=typer.colors.GREEN)
    if remove:
        typer.secho("  - inbox", fg=typer.colors.RED)


def _process_one(
    item: ZoteroItem,
    config: Config,
    *,
    zotero: ZoteroClient,
    jev: JevClient,
    mutate: bool,
    summary: RunSummary,
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

    try:
        outcome = jev.classify(paper, config)
    except JevError as exc:
        typer.secho(f"[{paper.zotero_key}] classification failed: {exc}", fg=typer.colors.RED)
        _fail(item, zotero=zotero, mutate=mutate)
        summary.failed += 1
        return

    actions = plan(outcome.result, config, input_mode=paper.input_mode)
    _render(PaperPlan(item=item, paper=paper, actions=actions, outcome=outcome), config)
    if outcome.usage and outcome.usage.cost:
        summary.cost += outcome.usage.cost

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


def _fail(item: ZoteroItem, *, zotero: ZoteroClient, mutate: bool) -> None:
    """Mark a failed paper so it stays visible, but only when that is safe."""
    if not mutate or not zotero.supports_write:
        return
    try:
        zotero.apply_actions(item, error_actions())
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


def _is_flagged(actions: PolicyActions) -> bool:
    return bool({STATE_REVIEW, STATE_ERROR} & actions.add_tags)


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

    thresholds = config.thresholds
    _render_scores(
        "Topics",
        outcome.result.topics,
        TAG_TOPIC_PREFIX,
        thresholds.topic_apply,
        plan_result.actions,
    )
    _render_scores(
        "Roles",
        outcome.result.roles,
        TAG_ROLE_PREFIX,
        thresholds.role_apply,
        plan_result.actions,
    )
    _render_coverage(outcome.result.coverage, plan_result.actions, config)

    typer.echo("Planned tags")
    for tag in sorted(plan_result.actions.add_tags):
        typer.secho(f"  + {tag}", fg=typer.colors.GREEN)
    for tag in sorted(plan_result.actions.remove_tags):
        typer.secho(f"  - {tag}", fg=typer.colors.RED)


def _render_scores(
    title: str,
    scores: dict[str, float],
    prefix: str,
    apply_threshold: float,
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
    for tag in sorted(t for t in actions.add_tags if t.startswith("agent/")):
        typer.echo(f"  -> {tag}")


def _zotero_client(config: Config) -> ZoteroClient:
    return ZoteroClient(
        base_url=config.zotero.base_url,
        timeout_seconds=config.zotero.timeout_seconds,
        authorize_timeout_seconds=config.zotero.authorize_timeout_seconds,
        app_name=config.zotero.app_name,
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
