# AGENTS.md

## Project Overview

This repository implements a minimal, auditable literature-classification pipeline for Zotero.

The MVP flow is:

```text
Zotero Inbox
    ↓
read title, abstract, metadata, and a bounded excerpt of the paper's text
    ↓
Jev via OpenRouter
    ↓
structured semantic decisions
    ↓
deterministic Python policy
    ↓
write tags / processing status back to Zotero
```

The system is intentionally narrow. It is not an autonomous research agent and should not become one without an explicit design change.

---

## Core Design Principles

### 1. Jev classifies; Python decides

Jev may estimate semantic probabilities such as:

- topic membership,
- project relevance (deferred, see "Current Scope" below),
- what kind of paper it is,
- taxonomy coverage.

Jev must not directly decide what to write into Zotero.

All persistent actions must pass through deterministic Python policy code.

Example:

```python
if result.topics["nrqcd"] >= config.thresholds.apply_floor:
    actions.add_tag("topic/nrqcd")
```

Do not ask Jev to return instructions such as:

```text
Move this paper into collection X and add tags A, B, C.
```

Instead, ask Jev for structured semantic judgments and derive actions in code.

---

### 2. Do not mutate the Zotero SQLite database

Use a supported Zotero API surface only.

For the MVP, prefer a small Zotero client module that can:

- list candidate items,
- read item metadata,
- read tags,
- write tags,
- optionally add/remove collection membership later.

Never modify Zotero's SQLite files directly.

---

### 3. Keep the MVP small

The initial version must NOT include:

- PDF parsing or OCR in this project (Zotero indexes attachments itself, so the
  paper's own text is read through the API — see "Which evidence is sent"),
- DeepSeek or any second LLM,
- RAG,
- embeddings,
- vector databases,
- SQLite state tracking,
- FastAPI,
- web UI,
- Docker,
- Celery,
- LangChain,
- agent frameworks,
- autonomous taxonomy creation,
- automatic Obsidian integration.

Add these only after the basic Jev classifier has been validated on real papers.

---

### 4. Prefer tags before collection mutation

The first working version should write namespaced tags such as:

```text
topic/nrqcd
topic/quarkonium
kind/theory
review/taxonomy-gap
```

State is not a tag either: whether a paper was judged, and whether the last attempt
failed, live in Zotero's `extra` field (see "Processing State").

Do not automatically move papers between Zotero collections in the first iteration.

Collection automation may be added later once classification quality is established.

---

### 5. `other` is not a permanent topic

Do not create or write:

```text
topic/other
```

Taxonomy coverage should be modeled separately from topic membership.

Preferred coverage states:

```text
covered
missing-topic
irrelevant
```

Interpretation:

- `covered`: the current taxonomy is adequate;
- `missing-topic`: the paper is in scope, but the list leaves out its area of physics or the framework it is built on;
- `irrelevant`: the paper is outside the intended literature scope.

If `missing-topic` is sufficiently probable, add:

```text
review/taxonomy-gap
```

Taxonomy expansion is a human decision.

Never let Jev invent and persist new topic names automatically.

---

## Current Scope: `projects` is deferred

The `projects` dimension (per-project usefulness) is **not implemented**. It was removed from the code, from `ClassificationResult`, and from `config.yaml`, and it will be added back later under its own design.

- `config.yaml` must not contain a `projects:` key; unknown keys are rejected rather than ignored.
- The dimensions that exist today are `topics`, `kinds`, and `coverage`.
- The design for re-adding projects lives in `docs/projects-design.md`. Do not re-implement it from memory, and do not add it back without an explicit design decision.

The **Zotero Web API is also deferred**. Zotero is read through the desktop local API on `127.0.0.1:23119` only; the Web API client is archived in `archive/zotero_web_api.py`, with restore instructions in `archive/README.md`.

- Reads need no credentials and no network. The local API must be enabled in Zotero's preferences, otherwise every request returns `403`.
- **Writes need Zotero 10 or later.** They use a local API key granted at runtime (`POST /api/local/authorize`) and require `Zotero-Server-ID` on every write. "Always Allow" is mandatory: a single-use key would mean one dialog per paper (such a key is refused). The granted key is stored in `.env` as `ZOTERO_LOCAL_WRITE_KEY` so later runs need no dialog; a key Zotero rejects with `401` is replaced once. Never log the key. Call `ensure_writes_available()` before doing work that assumes writes, so an older Zotero fails fast instead of per paper, and never claim a write succeeded without a 2xx response.
- Do not put a `ZOTERO_API_KEY` / `ZOTERO_LIBRARY_ID` path back into active code without restoring the archived client deliberately.

`jevero route` projects existing tags onto collection membership: `topic/<name>` -> `<topics_parent>/<name>`, `kind/<name>` -> `<kinds_parent>/<name>`, and every `review*` paper into one review queue. It never calls the classifier, so it is free to re-run; it merges membership instead of replacing it, so only the inbox is ever removed from; and it creates missing collections. See `docs/collections-design.md`.

---

## Expected Repository Structure

Keep the first implementation approximately as follows:

```text
jevero/
├── AGENTS.md
├── README.md
├── pyproject.toml
├── .env.example
├── config.yaml
└── src/
    └── jevero/
        ├── __init__.py
        ├── main.py
        ├── config.py
        ├── models.py
        ├── zotero.py
        ├── jev.py
        ├── http.py
        └── policy.py
```

Optional tests:

```text
tests/
├── test_policy.py
├── test_config.py
└── fixtures/
```

Do not introduce additional architectural layers without a concrete need.

---

## Language and Runtime

Use:

- Python 3.13+ where practical,
- `httpx` for HTTP,
- `pydantic` for structured models and validation,
- `PyYAML` for taxonomy/configuration,
- `python-dotenv` for local environment loading,
- `typer` for CLI ergonomics if a CLI framework is useful.

Prefer synchronous code for the MVP.

Do not introduce `asyncio` unless profiling or throughput requirements justify it.

---

## Configuration

Taxonomy, descriptions, and thresholds belong in `config.yaml`, not in Python source.

Example shape:

```yaml
topics:
  energy-correlator:
    description: >
      Energy correlators, energy-energy correlations,
      energy-flow observables, and related angular correlations.

  nrqcd:
    description: >
      NRQCD factorization, matching, long-distance matrix elements,
      color-singlet and color-octet mechanisms.

  pnrqcd:
    description: >
      Potential NRQCD and quarkonium bound-state effective theory.

  scet:
    description: >
      Soft-collinear effective theory, jet factorization,
      endpoint factorization, and related resummation.

  quarkonium:
    description: >
      Production, decay, spectroscopy, or structure of heavy quarkonium.

kinds:
  - theory
  - method
  - overview
  - phenomenology
  - experiment
  - reference

coverage:
  - covered
  - missing-topic
  - irrelevant

thresholds:
  # Membership: is this word part of what the paper is? Then focus: how many of
  # the qualifying words to keep, by rank. Two numbers per dimension, because one
  # number doing both jobs ends up sitting where the judgements are densest.
  apply_floor: 0.60
  # Guaranteed band: at or above this a topic is applied whatever the cap says.
  topic_guaranteed: 0.95
  # Kinds are stricter than the shared floor: they hedge more, so 0.60 let a tag
  # appear and vanish between two runs of the same configuration.
  kind_floor: 0.70
  topic_max: 3
  kind_max: 2
  review: 0.55
  missing_topic_review: 0.50   # calibrated: real gaps 0.63/0.71/0.84 vs highest non-gap 0.33
  irrelevant: 0.80
  covered_apply: 0.70

# Words dropped from the vocabulary. Their tags are removed on the next run and
# never added back, so retiring a word cleans up after itself. This is also what
# separates a retired name from a name the reader typed by hand: only known names
# are ever removed or routed. A word may not be both live and retired.
retired_topics:
  - perturbative-qcd
retired_kinds:
  - core
  - review

# Whole namespaces that were renamed or replaced; every tag under them is cleared
# once.
retired_prefixes:
  - "role/"
  - "agent/"
  - "jevero/"
```

The judgement fingerprint is not a tag: it is recorded in each item's `extra`
field as `jevero-fingerprint: <digest>`, so machine bookkeeping never clutters the
tag panel.

Descriptions should be meaningful enough for semantic classification.

Avoid ambiguous one-word definitions when possible.

---

## Data Model

Normalize Zotero data before sending it to Jev.

A minimal internal paper model should contain:

```python
class PaperRecord(BaseModel):
    zotero_key: str
    title: str
    abstract: str | None = None
    authors: list[str] = []
    year: int | None = None
    doi: str | None = None
    arxiv_id: str | None = None
```

Do not make downstream modules depend directly on raw Zotero response objects.

A classification result should be explicit and validated.

For example:

```python
class ClassificationResult(BaseModel):
    topics: dict[str, float]
    kinds: dict[str, float]
    coverage: dict[str, float]
```

All probabilities must be validated to lie in `[0, 1]`.

Reject malformed model output instead of guessing.

---

## Jev Integration

Use OpenRouter as the initial model gateway.

Keep OpenRouter-specific HTTP code inside `jev.py` or a very small client module.

Do not spread model API calls across the codebase.

The Jev input should use:

- original title,
- original abstract,
- small amounts of basic metadata when useful,
- the configured taxonomy and descriptions,
- a bounded excerpt of the paper's own text (see "Which evidence is sent").

### Which evidence is sent

A paper states the framework it is built on in its introduction, and usually not in
its abstract. Judging from the abstract alone therefore cannot see the framework
topics at all: two papers whose text names NRQCD seven and twenty-nine times scored
that topic 0.50 and 0.54 from the abstract alone, and 0.86-0.92 once the first few
thousand characters were added.

So `classification.full_text` sends an excerpt of the paper's **own** text:

- **Source: Zotero's index.** Zotero indexes PDF attachments itself, so this is one
  read of an attachment's `fulltext` endpoint. There is no PDF parser here and no
  second model in the loop; the text goes to Jev as it is.
- **Bounded, head-first.** `max_chars` (default 10000) characters from the start,
  because the introduction is where a paper says what it is built on. This matters:
  the median paper in this library is 53k characters, the longest 590k, and the
  model's context is 32k tokens. Measured: 8k, 10k and 20k characters give the same
  judgements.
- **The abstract stays.** It is clean metadata, and for 5 of 43 papers the indexed
  text does not begin with it.
- **Losing the text is never a failure.** No PDF, or nothing indexed yet, means the
  judgement falls back to the abstract. A real read error is reported instead, so a
  broken library cannot pass for "no text".
- **The budget is part of the fingerprint**, so changing it re-judges the library
  instead of leaving tags produced from a different amount of evidence.

The classifier output should contain probabilities only, not prose explanations, unless a short diagnostic explanation is explicitly enabled for debugging.

Prefer one classification request per paper that evaluates all configured topics/kinds/coverage states together.

---

## Policy Layer

`policy.py` is the only layer allowed to translate classification probabilities into Zotero mutations.

It should be:

- deterministic,
- easy to unit test,
- independent of HTTP clients,
- independent of OpenRouter,
- independent of Zotero response formats.

Recommended actions:

```python
class PolicyActions(BaseModel):
    add_tags: set[str] = set()
    remove_tags: set[str] = set()
```

Initial policy examples:

```text
topic probability >= apply_floor (strongest first, at most topic_max)
    → add topic/<name>

kind probability >= kind_floor (strongest first, at most kind_max)
    → add kind/<name>

a configured or retired topic/kind name the plan did not ask for
    → remove that tag

coverage.missing-topic >= missing_topic_review
    → add review/taxonomy-gap

coverage.irrelevant >= irrelevant
    → mark processed without adding a topic

coverage.covered < covered_apply
    → add review/coverage

topics undecided (nothing >= apply_floor, best >= review)
    → add review/ambiguous

successful processing
    → set extra jevero-fingerprint, clear jevero-error

processing failure
    → clear jevero-fingerprint, set extra jevero-error
```

A flagged paper always carries at least one reason tag, so every paper in the review
queue says why it is there: `review/ambiguous`, `review/coverage`,
`review/taxonomy-gap`, or `review/missing-abstract`. Reasons are tags because a human
reads and filters on them; everything else about the pipeline is bookkeeping and lives
in `extra`.

Avoid hidden policy inside prompts.

---

## Processing State

State is split by audience. Everything a reader would browse or filter on is a tag;
everything only the tool needs is a line in Zotero's free-text `extra` field, because a
cryptic tag on 29 papers is noise in the very panel the reader uses.

Tags, both machine-owned and reconciled on every run:

```text
topic/<name>            # what the paper is about
kind/<name>             # what sort of paper it is
review/ambiguous        # no topic cleared the floor, but one was plausible
review/coverage         # covered is low: what is this paper?
review/taxonomy-gap     # in scope, but no configured topic fits
review/missing-abstract # too little text to judge
```

The `review/*` reasons are the human work queue: `jevero route` files every paper
carrying one into the single `04 Review` collection, and the tag says why.

`extra` carries the bookkeeping:

```text
jevero-fingerprint: <digest>   # present => this paper was judged, with that setup
jevero-error: <why>            # present => the last attempt failed; cleared on success
```

The fingerprint is a digest over the model, the prompt text, the taxonomy descriptions,
and the thresholds. That is what makes staleness a query instead of a bookkeeping file:
a paper whose stamp differs from the current one was judged by an older configuration,
and the next `process` re-judges it without being asked. A paper with *no* stamp that
carries an old state tag was judged before stamps existed, so it counts as out of date
too — otherwise the first run after such a change would silently do nothing. A failure
clears the stamp and records why, so the paper goes back to "not judged" and is retried
rather than frozen.

Ownership decides what may be deleted:

- `topic/*` and `kind/*` are **machine-owned**. Every plan states what these namespaces
  should contain, so a name the plan does not ask for is removed and a re-run converges
  on the current judgement instead of accumulating the union of every run.
- Only *known* names are ever removed: the configured vocabulary, plus anything in `retired_topics` / `retired_kinds`. A `topic/...` tag the reader typed by hand is neither, so it survives untouched, and `route` reports it instead of turning it into a collection.
- Everything outside those namespaces belongs to the reader.

Do not add a local database yet. The vocabulary is versioned by the fingerprint, the
tags plus `extra` are the state, and the retired lists are the only memory of what used
to exist.

Candidate selection takes a paper when it has never been judged, when its fingerprint is
out of date (anywhere in the library: routing is what moves papers out of the inbox), or
when `--include-processed` asks for a forced re-run.

Reclassification support may later be exposed through a CLI command such as:

```bash
jevero reclassify --tag review/taxonomy-gap
```

---

## CLI Behavior

The CLI should be safe by default.

Prefer:

```bash
jevero process --dry-run
```

for inspection.

Require an explicit flag for mutations:

```bash
jevero process --apply
```

Both `process` and `route` default to the configured inbox. `--collection NAME`
and `--all` widen the scope; combined with `--apply` they print the scope, the
number of papers to be written, and block on a `y`/`N` confirmation. A dry-run
asks nothing, so a wide scope can always be inspected first.

Dry-run must not mutate Zotero.

### Output is the result, not the reasoning

The default output is what changed, one paper per line: its title, the tags it gained
and lost, and where it was filed. A re-run that moves nothing prints no paper at all,
because listing 43 identical papers buries the one line that matters.

```text
[ABCD1234] Example Paper Title
    +topic/quarkonium +topic/nrqcd +kind/theory
[EFGH5678] Another Example Paper
    +review/ambiguous
[EFGH5678] → 04 Review (new)

31 processed, 1 flagged, 12 unchanged, 0 skipped, 0 failed
Filed: 31 routed, 0 unchanged
classifier cost: $0.008600
Review: 3 papers need a human → 04 Review
```

- Tags and collections get a line each rather than sharing one: the tag line is the
  judgement, the collection line is where it landed, and one crowded line of both is
  read by nobody. Tag changes are green, removals red, and a `review/*` gain yellow.
- The summary carries the counts, the cost, and whether anything is waiting for a
  human. It deliberately omits vocabulary usage and the inbox census: those describe
  the library, not this run, and `jevero status` reports them on demand.
- Nothing was changed for a paper whose plan matches its current tags, so "unchanged"
  is the whole report for it.

`--verbose` / `-v` prints the reasoning behind each line — every probability, the
coverage split, the engine fingerprint, which evidence was sent and how much of it, and
the planned `extra` writes:

```text
[ABCD1234] Example Paper Title
  A. Author, B. Author et al. · 2024 · arXiv:2401.00001
  input: abstract + 10,000 of 53,111 characters of the paper's text

Topics
  quarkonium              0.97
  nrqcd                   0.91
  energy-correlator       0.88

Kinds
  theory                  0.86

Coverage
  covered                 0.96
  missing-topic           0.03
  irrelevant              0.01
Planned tags
  + topic/quarkonium
  + topic/nrqcd
  + topic/energy-correlator
  + kind/theory
  extra jevero-fingerprint = 9f3c1a77
  extra jevero-error removed
```

### `jevero status`

`status` reads Zotero and calls nothing, so it is free and instant. It answers the
questions the tag system is judged by: how much of the library is judged and how much
is out of date, what is waiting for a human and why, which words sit on no paper or on
nearly every one, and what is wrong (retired words still in use, tags outside the
vocabulary, papers with no topic, empty managed collections). A word that never gets
applied and a word that lands on most of the library are both called out, because
neither carries information.

---

## Error Handling

Do not silently swallow failures.

At minimum distinguish:

- Zotero read errors,
- Zotero write errors,
- OpenRouter transport errors,
- model/API errors,
- malformed Jev output,
- missing abstract,
- configuration errors.

Distinguish a failure of the *paper* from a failure of the *environment*:

- `JevResponseError` (malformed answer) is this paper's problem: clear its stamp, record the failure in `jevero-error`, and carry on.
- `JevTransportError` (unreachable endpoint) is nobody's paper: abort the run, write nothing, and retry later. The client retries transient transport failures and 429/5xx itself before giving up, honouring `Retry-After`.

A paper that fails processing should remain recoverable.

Prefer recording a failure only when it is safe to do so and when Zotero itself is reachable.

Log enough context to identify the Zotero item key and stage that failed.

Never log API keys.

---

## Missing Abstracts

A missing abstract is not equivalent to an irrelevant paper.

For the first version:

- if an abstract exists, classify normally;
- if no abstract exists, either skip and mark for review or allow title-only classification behind an explicit configuration option.

Both paths carry `review/missing-abstract`, so thin evidence is always visible in the review queue. Do not silently downgrade title-only results to normal confidence.

---

## Taxonomy Evolution

Taxonomy changes are expected.

When papers repeatedly receive `review/taxonomy-gap`, the human maintainer may decide to add a new topic to `config.yaml`.

Do not automatically create taxonomy entries.

A useful future workflow is:

```text
classify
→ collect review/taxonomy-gap
→ human reviews recurring unknown themes
→ edit config.yaml
→ reclassify review queue
```

Keep taxonomy definitions human-readable and version-control friendly.

### Describe a topic positively

State what a topic covers, including the borderline sub-cases that belong to it. Do
**not** add exclusion clauses such as "not for papers about X, which belong to Y".

That was tried on the real library and it backfired measurably: the model split its
probability across the two neighbours and cleared neither, so papers that had been
correctly tagged became untagged. Examples from one run: a generalized-detector paper
fell from `energy-correlator` 0.95 to 0.75 with `jet` 0.13 (tagged → `ambiguous`),
`scet` on an energy-correlator paper fell from 0.72 to 0.13, and two dihadron
fragmentation papers lost `fragmentation` after a "not TMD fragmentation" clause was
added. Rewriting the same descriptions as positive enumerations restored every one of
those judgements without touching the threshold.

One topic per axis. A topic that the model applies to every paper, or to none, carries
no information: on this library `perturbative-qcd`, `collider-phenomenology`,
`experiment`, `amplitudes`, and `loop-integrals` were applied to 0 of 29 papers (they
duplicated `kinds`, or were implied by a more specific topic), so they were deleted.
`kinds` already carries the activity axis (`theory` / `method` / `phenomenology` /
`experiment`); topics should not restate it.

A topic counts as live only if it is applied to something, or is expected to be applied
as soon as the matching literature arrives. `jevero status` therefore reports vocabulary
usage (`applied/total`, with "never applied" and "on most papers" called out), so a word
that stopped carrying information is visible immediately instead of being discovered
months later.

Removing a word is a deliberate two-step decision: delete it from the vocabulary, and
list it under `retired_topics` / `retired_kinds` so the next run removes its tags. A
word removed from the vocabulary but *not* retired keeps its tags forever and is
reported as foreign — a name the tool no longer knows about is indistinguishable from
one the reader typed by hand.

---

## Testing

Prioritize tests for deterministic behavior.

At minimum test:

1. threshold boundaries;
2. `missing-topic` handling;
3. `irrelevant` handling;
4. ambiguous classifications;
5. preservation of unrelated existing Zotero tags;
6. dry-run performs no mutations;
7. malformed Jev output is rejected.

Mock network calls in unit tests.

Do not require live OpenRouter or Zotero access for policy tests.

A small manually labeled validation set of approximately 30–50 real papers should be used before enabling automatic writes broadly.

---

## Coding Style

Prefer:

- small functions,
- explicit models,
- clear names,
- type hints,
- pure functions in policy code,
- dependency injection for HTTP clients where helpful,
- standard library solutions when sufficient.

Avoid:

- unnecessary metaprogramming,
- large inheritance hierarchies,
- premature plugin systems,
- generic agent abstractions,
- opaque prompt-building frameworks.

The code should remain understandable to a new contributor in one sitting.

---

## Security

Secrets belong in environment variables.

Expected variables:

```text
OPENROUTER_API_KEY
```

`ZOTERO_API_KEY` and `ZOTERO_LIBRARY_ID` belonged to the archived Web API client and are no longer read by any active code.

Provide `.env.example`, never commit `.env`.

Do not print secrets in tracebacks or debug logs.

When possible, request only the Zotero permissions required by the program.

---

## Change Discipline

When changing classification behavior:

1. change taxonomy or thresholds in config when possible;
2. keep prompt changes localized to `jev.py`;
3. keep action changes localized to `policy.py`;
4. update or add policy tests;
5. run a dry-run on the validation set;
6. inspect false positives before enabling `--apply`.

Do not combine large taxonomy changes, prompt changes, and policy changes in one unreviewed patch.

---

## Success Criterion for the MVP

The MVP is successful if it can reliably:

1. find unprocessed Zotero papers;
2. read their title and abstract;
3. obtain structured Jev probabilities;
4. convert them into deterministic tags;
5. flag taxonomy gaps for review;
6. show a clear dry-run;
7. write approved tags back to Zotero;
8. avoid damaging existing library organization.

Everything else is secondary.
