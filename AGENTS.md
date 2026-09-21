# AGENTS.md

## Project Overview

This repository implements a minimal, auditable literature-classification pipeline for Zotero.

The MVP flow is:

```text
Zotero Inbox
    ↓
read title + abstract + basic metadata
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
- project relevance,
- paper role,
- taxonomy coverage.

Jev must not directly decide what to write into Zotero.

All persistent actions must pass through deterministic Python policy code.

Example:

```python
if result.topics["nrqcd"] >= config.thresholds.topic:
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

- PDF full-text parsing,
- OCR,
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
project/qec
status/processed
status/need-review
status/error
```

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
- `missing-topic`: the paper appears relevant, but the taxonomy is missing an appropriate topic;
- `irrelevant`: the paper is outside the intended literature scope.

If `missing-topic` is sufficiently probable, add:

```text
status/need-review
```

Taxonomy expansion is a human decision.

Never let Jev invent and persist new topic names automatically.

---

## Expected Repository Structure

Keep the first implementation approximately as follows:

```text
zotero-jev/
├── AGENTS.md
├── README.md
├── pyproject.toml
├── .env.example
├── config.yaml
└── src/
    ├── __init__.py
    ├── main.py
    ├── config.py
    ├── models.py
    ├── zotero.py
    ├── jev.py
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

projects:
  qec:
    description: >
      Work relevant to quarkonium energy correlators, including
      perturbative and nonperturbative factorization, NRQCD/pNRQCD,
      energy-flow observables, and related phenomenology.

roles:
  - core
  - method
  - review
  - phenomenology
  - experiment
  - reference

coverage:
  - covered
  - missing-topic
  - irrelevant

thresholds:
  topic_apply: 0.85
  project_apply: 0.85
  role_apply: 0.85
  review: 0.55
  missing_topic_review: 0.70
  irrelevant: 0.80
```

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
    projects: dict[str, float]
    roles: dict[str, float]
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
- the configured taxonomy and descriptions.

The MVP should not send full PDF text.

The classifier output should contain probabilities only, not prose explanations, unless a short diagnostic explanation is explicitly enabled for debugging.

Prefer one classification request per paper that evaluates all configured topics/projects/roles/coverage states together.

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
topic probability >= topic_apply
    → add topic/<name>

project probability >= project_apply
    → add project/<name>

role probability >= role_apply
    → add role/<name>

coverage.missing-topic >= missing_topic_review
    → add ai/topic-review

coverage.irrelevant >= irrelevant
    → mark processed without adding a topic

ambiguous classifications
    → add ai/review

successful processing
    → add ai/processed

processing failure
    → add ai/error
```

Avoid hidden policy inside prompts.

---

## Processing State

For the MVP, use Zotero tags as state markers.

Supported states:

```text
ai/processed
ai/review
ai/topic-review
ai/error
```

Do not add a local database yet.

Candidate selection should ignore already processed papers unless the user explicitly requests reclassification.

Reclassification support may later be exposed through a CLI command such as:

```bash
zotero-jev reclassify --tag ai/topic-review
```

---

## CLI Behavior

The CLI should be safe by default.

Prefer:

```bash
zotero-jev process --dry-run
```

for inspection.

Require an explicit flag for mutations:

```bash
zotero-jev process --apply
```

A useful dry-run output is:

```text
[ABCD1234] Example Paper Title

Topics
  quarkonium              0.97
  nrqcd                   0.91
  energy-correlator       0.88

Projects
  qec                     0.94

Roles
  core                    0.86

Coverage
  covered                 0.96
  missing-topic           0.03
  irrelevant              0.01

Planned actions
  + topic/quarkonium
  + topic/nrqcd
  + topic/energy-correlator
  + project/qec
  + role/core
  + ai/processed
```

Dry-run must not mutate Zotero.

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

A paper that fails processing should remain recoverable.

Prefer adding `ai/error` only when it is safe to do so and when Zotero itself is reachable.

Log enough context to identify the Zotero item key and stage that failed.

Never log API keys.

---

## Missing Abstracts

A missing abstract is not equivalent to an irrelevant paper.

For the first version:

- if an abstract exists, classify normally;
- if no abstract exists, either skip and mark for review or allow title-only classification behind an explicit configuration option.

Do not silently downgrade title-only results to normal confidence.

---

## Taxonomy Evolution

Taxonomy changes are expected.

When papers repeatedly receive `ai/topic-review`, the human maintainer may decide to add a new topic to `config.yaml`.

Do not automatically create taxonomy entries.

A useful future workflow is:

```text
classify
→ collect ai/topic-review
→ human reviews recurring unknown themes
→ edit config.yaml
→ reclassify review queue
```

Keep taxonomy definitions human-readable and version-control friendly.

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

Expected variables may include:

```text
OPENROUTER_API_KEY
ZOTERO_API_KEY
ZOTERO_LIBRARY_ID
```

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
