# jevero

A minimal, auditable Zotero literature-classification tool powered by Jev.

The project is designed around one narrow workflow:

```text
Zotero Inbox
    ↓
title + abstract + metadata
    ↓
Jev
    ↓
topic / kind / taxonomy-coverage probabilities   (projects deferred)
    ↓
deterministic Python policy
    ↓
Zotero tags
```

The first version intentionally does **not** summarize papers, parse full PDFs, use a second LLM, or act as an autonomous research agent.

Its job is classification and routing.

> **Current implementation status.** The classifiable dimensions today are
> `topics`, `kinds`, and `coverage`. The `projects` dimension is designed but
> **not implemented**: it has been removed from the code and from `config.yaml`,
> and the design for adding it back lives in
> [`docs/projects-design.md`](docs/projects-design.md). `config.yaml` rejects a
> `projects:` key rather than ignoring it.

---

## Why this project exists

A Zotero library tends to accumulate papers much faster than they can be manually organized.

Traditional folder trees are also a poor fit for research literature because one paper may simultaneously belong to several conceptual categories:

```text
NRQCD
quarkonium
energy correlators
factorization
SCET
phenomenology
```

This project treats Zotero as the source-of-truth literature database and uses Jev as a semantic classifier.

The core principle is:

> **Jev decides what a paper appears to be; deterministic Python rules decide what to do about it.**

This separation keeps the system inspectable and prevents an LLM from directly reorganizing the library.

---

## MVP scope

The initial implementation should do only the following:

1. identify unprocessed papers in a Zotero Inbox;
2. read title, abstract, and basic metadata;
3. classify each paper with Jev through OpenRouter;
4. estimate:
   - topic membership,
   - paper kind,
   - taxonomy coverage;

   (`project relevance` is designed but deferred; see the status note above.)
5. convert probabilities into deterministic actions;
6. write namespaced tags back to Zotero;
7. flag ambiguous or taxonomy-missing papers for human review.

Not included in the MVP:

- PDF full-text parsing,
- OCR,
- DeepSeek or another LLM,
- automatic summaries,
- RAG,
- embeddings,
- vector databases,
- Obsidian integration,
- web UI,
- autonomous taxonomy creation,
- automatic Zotero collection restructuring.

---

## Design

### Semantic layer

Jev receives the original paper metadata:

```text
Title
Abstract
Authors
Year
```

plus the configured taxonomy.

It returns structured probability estimates such as:

```json
{
  "topics": {
    "energy-correlator": 0.94,
    "nrqcd": 0.88,
    "quarkonium": 0.97,
    "scet": 0.18
  },
  "kinds": {
    "core": 0.84,
    "method": 0.46
  },
  "coverage": {
    "covered": 0.96,
    "missing-topic": 0.03,
    "irrelevant": 0.01
  }
}
```

(`projects` is deferred, so this example no longer includes it.)

### Policy layer

Python applies explicit thresholds:

```python
# Membership floor first (is this topic part of the paper at all?), then the rank
# cap (which two or three to keep). One number cannot do both jobs.
if result.topics["nrqcd"] >= config.thresholds.apply_floor:
    add_tag("topic/nrqcd")

if result.coverage["missing-topic"] >= 0.50:
    add_tag("review/taxonomy-gap")
```

Jev never directly mutates Zotero.

---

## Why there is no `topic/other`

`other` mixes together two very different situations:

1. the current taxonomy is missing a useful category;
2. the paper is genuinely outside the intended scope.

Instead, taxonomy coverage is modeled separately:

```text
covered
missing-topic
irrelevant
```

### `covered`

The configured topics adequately describe the paper.

### `missing-topic`

The paper is in scope, but the topic list leaves out something central to it — the
area of physics it belongs to, or the framework it is built on. A reader browsing
the topics would look for such a paper and not find where it belongs, even if one
listed topic happens to match its observable.

This has to be asked about the **area or the framework**, not only about the
framework: a paper whose method matches a listed topic while its whole subfield does
not used to answer "not a gap" (measured 0.18 for a heavy-ion paper, against a 0.25
gate), so an entire missing subfield stayed invisible.

The paper should be marked:

```text
review/taxonomy-gap
```

and reviewed by a human.

### `irrelevant`

The paper is outside the intended scope and does not need a new topic.

This design allows the taxonomy to grow from real literature without letting the model create uncontrolled tags.

---

## Proposed tags

The first version should prefer tags over automatic collection changes.

### Topic tags

```text
# what the paper studies
topic/energy-correlator
topic/fragmentation
topic/tmd
topic/jet
topic/quarkonium
topic/heavy-flavor
topic/lattice-qcd
topic/chiral-dynamics

# which framework the paper is built on
topic/nrqcd
topic/pnrqcd
topic/scet
```

### Project tags

Deferred: these tags are not written yet; see `docs/projects-design.md`.

```text
project/qec
project/jpsi-ccbar
project/general-hep
```

### Kind tags

`kind/core` was retired: it asked whether a paper is central to the reader's
*current work*, which is a `projects` question (still deferred) rather than a
genre. See `retired_kinds` in `config.yaml`.

```text
kind/theory
kind/method
kind/overview
kind/phenomenology
kind/experiment
kind/reference
```

### Review tags

A paper awaiting a human carries one or more **reason** tags, so the single `04
Review` queue always says why a paper is in it.

```text
review/ambiguous        # a judgement landed in the review band
review/coverage         # covered is low: what is this paper?
review/taxonomy-gap     # in scope, but no configured topic fits
review/missing-abstract # too little text to judge
```

Reasons are tags because you read and filter on them. Everything else the tool
needs is bookkeeping and lives in Zotero's `extra` field instead, so the tag panel
stays a list of things you would actually search for:

```text
jevero-fingerprint: <digest>   # present => judged, with that model/prompt/vocabulary/thresholds
jevero-error: <why>            # present => the last attempt failed; cleared on success
```

Each reason maps to a different next action:

| reason | what a human should do |
| --- | --- |
| `ambiguous` | calibrate a threshold, or rewrite a description in `config.yaml` |
| `coverage` | read the paper and decide what it is |
| `taxonomy-gap` | decide whether `config.yaml` needs a new topic |
| `missing-abstract` | supply metadata, or accept a title-only classification |

Priority may either be classified by Jev later or derived deterministically from project relevance and kind:

```text
priority/read-now
priority/read-later
priority/archive
```

For the first iteration, deriving priority in Python is preferable.

---

## Suggested taxonomy

The taxonomy belongs in `config.yaml`.

Example:

```yaml
topics:
  # --- what the paper studies ---
  energy-correlator:
    description: >
      Energy-energy correlators, N-point energy correlators, energy-flow
      operators, and generalized detectors, together with the factorization,
      resummation, or measurement specific to those observables.

  fragmentation:
    description: >
      Fragmentation functions and hadron production through fragmentation:
      single- and dihadron fragmentation functions, their evolution and fits,
      fragmentation factorization, and the fragmentation contribution to
      quarkonium production.

  jet:
    description: >
      Jets and jet-like collider observables: jet production and jet algorithms,
      jet functions and jet substructure, grooming and tagging, and event-shape
      or thrust-like observables such as thrust, the C-parameter, and jet mass.

  quarkonium:
    description: >
      Production, decay, spectroscopy, or structure of heavy quarkonium.

  heavy-flavor:
    description: >
      Heavy-quark production, fragmentation, heavy-flavor hadrons, and related
      perturbative or nonperturbative physics.

  lattice-qcd:
    description: >
      Lattice gauge theory and lattice QCD: numerical simulation of QCD on a
      spacetime lattice, hadronic matrix elements and spectra, wave functions
      and structure of bound states, and other nonperturbative quantities
      obtained from the lattice.

  chiral-dynamics:
    description: >
      Spontaneous chiral symmetry breaking and its low-energy description:
      chiral Lagrangians and chiral perturbation theory, the pion and Goldstone
      sector, and the QCD vacuum and its condensates.

  # --- which framework or formalism the paper is built on ---
  tmd:
    description: >
      Transverse-momentum-dependent factorization: TMD parton distributions and
      TMD fragmentation functions, Collins-Soper evolution, rapidity
      divergences, and nonperturbative Sudakov resummation.

  nrqcd:
    description: >
      NRQCD factorization, matching, long-distance matrix elements,
      color-singlet and color-octet mechanisms.

  pnrqcd:
    description: >
      Potential NRQCD and quarkonium bound-state effective theory.

  scet:
    description: >
      Soft-collinear effective theory and the factorization or resummation built
      on it: jet, threshold, and endpoint factorization, rapidity
      renormalization, and soft functions.

# DEFERRED - not implemented; see docs/projects-design.md
#
# projects:
#   qec:
#     description: >
#       Papers useful for quarkonium energy-correlator research,
#       including perturbative and nonperturbative factorization,
#       energy-flow observables, NRQCD/pNRQCD, SCET where relevant,
#       quarkonium phenomenology, and experimental measurements.
#
#   jpsi-ccbar:
#     description: >
#       Papers useful for associated J/psi plus charm production,
#       color transfer, NRQCD production mechanisms,
#       phase-space dependence, and related phenomenology.
#
#   general-hep:
#     description: >
#       Papers of broader methodological relevance to perturbative QCD,
#       amplitudes, EFT, heavy-flavor physics, or computational HEP.
#
kinds:
  - core
  - theory
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
  # Membership floor, then the per-dimension rank cap. Calibrated into the empty
  # valley of the judgement distribution: measured over 319 topic judgements, only
  # 3 lie within +/-0.05 of 0.60, against 12 at the old 0.85 cut-off, while
  # run-to-run noise reaches 0.05. The tag sets at 0.60, 0.65 and 0.70 were
  # identical, which is the plateau a well-placed floor should have.
  apply_floor: 0.60
  # Guaranteed band: at or above this a topic is applied whatever the cap says, so
  # a paper with several headline topics cannot lose one to a counting rule.
  topic_guaranteed: 0.95
  # Kinds are stricter than the shared floor: "what sort of paper is this" hedges
  # more, so at 0.60 a kind tag could appear and vanish between two runs of the
  # same configuration. 0.70 measured 0/29 churn.
  kind_floor: 0.70
  topic_max: 3
  kind_max: 2

  review: 0.55
  missing_topic_review: 0.50
  irrelevant: 0.80
  covered_apply: 0.70

# Words dropped from the vocabulary: their tags are removed on the next run and
# never added back. A name that is neither configured nor retired is treated as
# yours, and is never removed or routed.
retired_topics:
  - perturbative-qcd
retired_kinds:
  - core

# Whole namespaces that were renamed; every tag under them is cleared once.
retired_prefixes:
  - "role/"
  - "agent/"
```

These values are starting points, not final truth.

They should be calibrated on a manually labeled validation set.

---

## Taxonomy evolution

A single `missing-topic` paper should **not** create a new topic.

Instead:

```text
paper
  ↓
coverage.missing-topic is high
  ↓
review/taxonomy-gap
  ↓
human review
```

If several reviewed papers repeatedly belong to the same missing category, add that topic manually to `config.yaml`.

For example:

```text
paper A ─┐
paper B ─┼─→ review/taxonomy-gap ─→ recurring lattice-QCD theme
paper C ─┘
```

Then add:

```yaml
lattice-qcd:
  description: >
    Lattice gauge theory and lattice QCD calculations,
    including hadronic matrix elements and other
    nonperturbative quantities obtained from lattice simulations.
```

and reclassify the review queue.

The taxonomy therefore grows from observed literature rather than from model-generated labels.

---

## Architecture

Suggested initial repository:

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

### `main.py`

Orchestrates the pipeline.

Responsibilities:

```text
find candidate papers
→ fetch metadata
→ call Jev
→ evaluate policy
→ print dry-run
→ optionally apply actions
```

### `zotero.py`

Contains all Zotero API interaction.

Expected operations:

```text
get candidate items
get item metadata
get existing tags
add/remove tags
```

Do not directly access the Zotero SQLite database.

### `jev.py`

Contains OpenRouter/Jev communication.

Responsibilities:

```text
construct classification request
send request
validate structured response
return ClassificationResult
```

### `policy.py`

Contains deterministic classification policy.

It should not perform HTTP requests.

Given:

```text
ClassificationResult + Config
```

it returns:

```text
PolicyActions
```

### `models.py`

Contains Pydantic models for normalized internal data.

### `config.py`

Loads and validates `config.yaml`.

---

## Python stack

The MVP can remain very small.

Recommended dependencies:

```text
Python 3.13+
httpx
pydantic
PyYAML
python-dotenv
typer
```

No asynchronous framework is required initially.

No database is required initially.

---

## OpenRouter

The MVP uses OpenRouter as the model gateway.

Environment variable:

```bash
OPENROUTER_API_KEY=...
```

Jev should be configurable rather than hard-coded:

```yaml
model:
  provider: openrouter
  name: typesafe/jev-1.13
```

Pin a concrete Jev version rather than an alias: the `~typesafe/jev-latest` alias
advertises no routable endpoint on OpenRouter, so a request against it can fail.
Check `GET https://openrouter.ai/api/v1/models/typesafe/jev-1.13/endpoints`
before bumping the version.

Keep all provider-specific code isolated so that switching to the direct TypeSafe API later does not affect policy or Zotero code.

---

## Zotero configuration

The tool reads Zotero through the **desktop local API** only
(`http://127.0.0.1:23119/api`). Reads need no credentials, no network, and are
not rate limited, so Zotero must be running with the local API enabled:

```text
Zotero -> Settings -> Advanced
    [x] Allow other applications on this computer to communicate with Zotero
```

If that checkbox is off, every request returns `403 Forbidden`; the client turns
that into an error naming the setting.

`zotero.inbox_collection` selects what gets classified. It is matched against
the collection **name**, or against a `Parent/Child` path when two collections
share a name. List them, with paper counts, using:

```bash
jevero collections
```

```text
  00 Read NOW!!!    KRQJSKLH  papers=1    unprocessed=1   <- configured
  02 Topics         VIW97AJZ  papers=0    unprocessed=0
  02 Topics/Physics YRM8M87H  papers=11   unprocessed=11
```

The only credential the tool needs is for the classifier:

```bash
OPENROUTER_API_KEY=
```

There is no `ZOTERO_API_KEY` or `ZOTERO_LIBRARY_ID`: the local API serves the
locally logged-in user as library `0`. The zotero.org Web API client is archived
in `archive/zotero_web_api.py` (see `archive/README.md`).

### Filing papers into collections

`jevero route` files each paper into collections based on the tags it already
has. It never calls the classifier, so re-running it after changing the settings
below costs nothing.

```bash
jevero route --dry-run               # show the plan (default)
jevero route --apply                 # create the collections and file the papers
jevero route --apply --prune         # also drop memberships the tags no longer imply
```

```yaml
collections:
  topics_parent: "02 Topics"      # topic/<name> -> 02 Topics/<name>
  kinds_parent: "03 Kinds"        # kind/<name>  -> 03 Kinds/<name>
  review_collection: "04 Review"  # every review* -> one queue
  route_kinds: true
  remove_from_inbox: false        # true keeps the inbox a real work queue
```

Missing collections are created. `collections` is a complete list on write, so
membership is merged rather than replaced: collections this tool knows nothing
about are never dropped. A paper in review is filed in its topic collection
*and* the review queue.

`--prune` makes routing converge: a paper is removed from the managed
collections (`topics_parent/*`, `kinds_parent/*`, the review queue) that its
tags no longer point at. Anything outside those namespaces — your own folders —
is never touched. Without `--prune`, routing only ever adds.

### Reclassification converges

A re-run replaces the judgement instead of accumulating it. `topic/*`, `kind/*`
and the `jevero/*` state are **machine-owned**: a plan states what these
namespaces should contain and removes the names it does not ask for, so a paper
processed under an older vocabulary or threshold set loses the tags that
produced. Running twice changes nothing the second time — measured on a 29-paper
library, zero papers change their tags.

Only names the tool knows are ever removed: the configured vocabulary plus
anything listed under `retired_topics` / `retired_kinds`. A `topic/...` tag you
typed yourself is neither, so it survives untouched (`route` reports it instead
of turning it into a collection).

Every judged paper records a stamp in Zotero's `extra` field:
`jevero-fingerprint: <digest>` — a digest of the model, the prompt text, the
taxonomy descriptions, and the thresholds that produced its tags. It is not a tag,
so nothing cryptic shows up in the tag panel. A paper whose stamp differs from the
current one was judged by an older configuration, and the next run re-judges it
without being asked.

```bash
jevero check                      # how many papers are out of date
jevero process --apply            # inbox + everything stale, then convergence
jevero route --apply --prune      # collections follow the tags
```

`--include-processed` still forces a re-run of papers that are already up to
date.

### Wider scopes need a confirmation

Both `process` and `route` operate on the configured inbox by default. Two flags
widen that:

```bash
jevero process --collection "02 Topics/Physics" --dry-run   # one collection
jevero route   --all --dry-run                              # the whole library
```

A dry-run never asks anything — it is how you inspect a wide scope first. When a
wide scope is combined with `--apply`, the run names the scope and how many
papers it will write to, and waits for a `y`:

```text
Scope: the whole library via the Zotero local API
Candidates: 20 papers with routable tags

WARNING: this run will write to 20 papers in the whole library, not just the inbox.
Tags and collection membership are modified for every paper that needs a change.
Continue? [y/N]:
```

Anything other than `y` aborts without writing. `--collection` and `--all`
refuse to run together.

### Writing tags: local API authorization

Writes need **Zotero 10 or later**. Zotero 9 and earlier expose the local API
read-only: they send no `Zotero-Server-ID` header and have no
`/api/local/authorize` endpoint, and `--apply` refuses before doing any work.

On Zotero 10+ the flow is:

1. the first write calls `POST /api/local/authorize` with the `Zotero-Server-ID`
   header. Zotero shows a confirmation dialog naming the application
   (`zotero.app_name`, default `jevero`);
2. **choose "Always Allow"**, which is required: a key granted with plain
   "Allow" is single-use, so it would mean one dialog per paper. jevero rejects
   a single-use key and tells you to re-run and pick "Always Allow";
3. the granted key is **stored in `.env`** as `ZOTERO_LOCAL_WRITE_KEY` (mode
   0600; `.env` is gitignored and already holds the OpenRouter key). Later runs
   read it and **never ask Zotero again** — no dialog, no request. A key that
   Zotero rejects with a `401` is replaced automatically, once;
4. every write echoes `Zotero-Server-ID` (otherwise `428 Precondition
   Required`) and carries `If-Unmodified-Since-Version`. A `412` means the item
   changed underneath and the run must be repeated; a `401` means the key was
   consumed or revoked, and jevero re-authorizes once and retries.

To revoke: delete the `ZOTERO_LOCAL_WRITE_KEY` line, and/or use Zotero ->
Settings -> Advanced -> "Clear Write Authorizations". Deleting only the line is
enough to stop jevero using it; Zotero forgets the authorization when you clear
it there.

Local object versions have **no relation** to Web API versions, so the two
backends must never share cached versions.

Do not commit `.env`.

Never mutate Zotero's SQLite database directly.

---

## Candidate papers

The intended workflow is to maintain a Zotero collection such as:

```text
00 Inbox
```

The processor operates on papers that:

```text
are in the configured Inbox
OR
carry no judgement stamp yet (`extra` has no `jevero-fingerprint`)
OR
carry a judgement stamp that is not the current one
   (their tags were produced by older model/prompt/vocabulary/thresholds, and
    they may live anywhere in the library, not just in the inbox)
```

`--include-processed` forces a re-run of papers that are already up to date.
That is why a vocabulary change needs no bookkeeping file: change `config.yaml`,
run `process`, and exactly the affected papers are re-judged.

A failed paper should remain visible and recoverable.

---

## Dry-run first

Mutation must not be the default behavior.

Recommended commands:

```bash
jevero process --dry-run
```

and:

```bash
jevero process --apply
```

Example dry-run:

```text
[ABCD1234] Energy Correlators in Heavy Quarkonium Production
  Ada Lovelace · 2019

Topics
  energy-correlator       0.97  APPLY
  quarkonium              0.96  APPLY
  nrqcd                   0.91  APPLY
  scet                    0.42

Kinds
  theory                  0.87  APPLY
  method                  0.61  APPLY

Coverage
  covered                 0.96  >= 0.70
  missing-topic           0.03
  irrelevant              0.01
Planned tags
  + topic/energy-correlator
  + topic/quarkonium
  + topic/nrqcd
  + kind/theory
  + kind/method
  extra jevero-fingerprint = 9f3c1a77
  extra jevero-error removed
```

The run then ends with a vocabulary usage summary, which is what makes a word
that never fires (or one that fires on almost every paper) visible immediately:

```text
Vocabulary usage (of 29 papers judged this run)
  topic/heavy-flavor              0/29   <- never applied
  topic/energy-correlator        11/29
  kind/theory                    16/29   <- on most papers; carries little information
```

Use dry-run on a representative validation set before enabling automatic writes.

---

## Review behavior

Classification should be conservative.

A useful starting rule is, per dimension (topics, kinds):

```text
topic p >= topic_guaranteed (0.95)
    always applied, whatever the cap says

word p >= its floor (topics 0.60, kinds 0.70)
    qualifies; keep the strongest topic_max (3) / kind_max (2), counting the
    guaranteed ones already selected

no word qualified, and the best topic >= review (0.55)
    the topics are undecided; add review/ambiguous

no word qualified, and no topic >= 0.55
    ignore
```

The floor sits in the empty valley between "not about this" and "this is a topic
of the paper", so run-to-run noise cannot flip a tag. The rank cap is what keeps
a paper at two or three topics, and it makes the result independent of how many
words the vocabulary happens to contain.

Coverage is judged on the paper's **approach**, not on every detail, and uses its
own gates:

```text
missing-topic >= 0.50
    the approach has no place in the taxonomy; add review/taxonomy-gap

covered < 0.70
    coverage is unclear; add review/coverage

irrelevant >= 0.80
    out of scope; process without a topic
```

The 0.25 gate is calibrated, not guessed: a paper known to be a gap (a
lattice-QCD paper with `lattice-qcd` removed from the taxonomy) scores 0.32,
while the highest-scoring non-gap scores 0.16. `covered` is the sensitive
signal — that same paper drops from 0.96 to 0.59 without its topic — so even a
missed gap still lands in review as `coverage`.

Two deliberate restrictions keep this from firing on almost every paper:

- a dimension that already produced an applied tag counts as decided, so a
  second, weaker candidate in the band ("maybe also this") is not reported;
- **kinds never gate review.** Kinds are facets, so "no confident kind" is a
  normal outcome — it still produces tags, it just does not decide whether a
  human is needed.

Coverage uses separate thresholds:

```text
missing-topic >= 0.70
    → review/taxonomy-gap

irrelevant >= 0.80
    → process without assigning a topic

covered < 0.70
    → review/coverage
```

`covered` is the taxonomy's own adequacy judgement: when it is not convincing,
and neither "out of scope" nor "missing topic" explains why, the paper is
reported rather than acted on. That holds even when a topic tag did apply, so a
confident topic cannot hide a taxonomy that fits poorly.

These thresholds should be tuned empirically.

---

## Priority

The first version does not need Jev to classify reading priority.

Priority can be derived from semantic judgments.

Example:

```python
if (
    projects.get("qec", 0.0) >= 0.90
    and kinds.get("core", 0.0) >= 0.80
):
    priority = "read-now"
elif max(projects.values(), default=0.0) >= 0.70:
    priority = "read-later"
else:
    priority = "archive"
```

This follows a useful separation:

```text
Jev
    → semantic facts

Python policy
    → personal workflow decisions
```

---

## Missing abstracts

Do not treat a missing abstract as irrelevant.

Default behavior should be one of:

```text
skip and flag for review
```

or, if explicitly enabled:

```text
perform title-only classification
```

Title-only results should be marked as lower-confidence input.

---

## Processing state

There is no local database. State lives in two places, split by audience:

```text
# tags: what you browse and filter on
topic/<name>
kind/<name>
review/ambiguous | review/coverage | review/taxonomy-gap | review/missing-abstract

# extra: what only the tool needs
jevero-fingerprint: <digest>   # present => judged, with that setup
jevero-error: <why>            # present => the last attempt failed
```

A paper is "judged" when it carries a stamp; a stamp that differs from the current
one means the judgement is out of date, and the next `process` re-judges it. A
failure clears the stamp, so the paper returns to "not judged" and is retried.

This keeps the first version simple.

A local SQLite state database may be introduced later if the project needs:

- classification history,
- pipeline versioning,
- cost accounting,
- model-version tracking,
- taxonomy migrations,
- audit trails.

---

## Installation

A likely development flow:

```bash
git clone <repository-url>
cd jevero

python -m venv .venv
source .venv/bin/activate

pip install -e .
```

Copy the environment template:

```bash
cp .env.example .env
```

Fill in:

```bash
OPENROUTER_API_KEY=...
ZOTERO_API_KEY=...
ZOTERO_LIBRARY_ID=...
```

Then edit:

```text
config.yaml
```

to define the local taxonomy and thresholds.

---

## Development workflow

Recommended order:

### 1. Implement configuration models

Load and validate:

```text
topics
kinds
coverage
thresholds
```

### 2. Implement policy tests

Before connecting any model, test probability-to-tag behavior with fake inputs.

### 3. Implement the Jev client

Feed controlled sample papers and inspect structured output.

### 4. Implement Zotero read-only access

List candidate papers and print normalized metadata.

### 5. Connect the pipeline in dry-run mode

Do not write anything yet.

### 6. Validate on real papers

Prepare approximately 30–50 papers whose categories are already understood.

Include:

- clearly relevant papers,
- borderline papers,
- papers requiring a missing topic,
- irrelevant papers.

Measure false positives and false negatives.

### 7. Enable `--apply`

Only after the dry-run behavior is satisfactory.

---

## Validation questions

Before considering the MVP reliable, answer:

1. Does Jev correctly identify obvious topic membership?
2. Does it separate `missing-topic` from `irrelevant`?
3. Does `project/qec` mean project relevance rather than merely containing quarkonium/QCD keywords?
4. Are false-positive automatic tags acceptably rare?
5. Are ambiguous cases consistently sent to review?
6. Does the pipeline preserve all unrelated existing Zotero tags?
7. Can every proposed Zotero mutation be explained by an explicit threshold?

If not, adjust taxonomy descriptions, prompt structure, or policy thresholds before adding more features.

---

## Future extensions

Only after the MVP is validated, possible additions include:

```text
PDF full-text extraction
        ↓
second LLM for structured reading notes
        ↓
more detailed Jev classification
        ↓
Zotero child notes
        ↓
Obsidian export
```

Other possible extensions:

- SQLite audit history,
- taxonomy versioning,
- cost tracking,
- scheduled Inbox processing,
- automatic Zotero collections,
- duplicate detection,
- citation-network analysis,
- related-paper discovery,
- native Zotero TypeScript plugin,
- separate backend service.

None of these are required for the initial classifier.

---

## Guiding principle

Keep the system decomposed into three responsibilities:

```text
Jev
    determines what the paper appears to be

Python policy
    determines what actions those probabilities imply

Zotero
    remains the persistent literature database
```

The first milestone is not “AI literature management.”

The first milestone is a small, reliable, inspectable literature router.
