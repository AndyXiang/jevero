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
topic / role / taxonomy-coverage probabilities   (projects deferred)
    ↓
deterministic Python policy
    ↓
Zotero tags
```

The first version intentionally does **not** summarize papers, parse full PDFs, use a second LLM, or act as an autonomous research agent.

Its job is classification and routing.

> **Current implementation status.** The classifiable dimensions today are
> `topics`, `roles`, and `coverage`. The `projects` dimension is designed but
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
   - paper role,
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
  "roles": {
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
if result.topics["nrqcd"] >= 0.85:
    add_tag("topic/nrqcd")

if result.coverage["missing-topic"] >= 0.70:
    add_tag("agent/review/taxonomy-gap")
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

The paper appears relevant to the literature workflow, but none of the current topic definitions fit well.

The paper should be marked:

```text
agent/review/taxonomy-gap
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
topic/energy-correlator
topic/nrqcd
topic/pnrqcd
topic/scet
topic/quarkonium
topic/heavy-flavor
topic/fragmentation
topic/perturbative-qcd
topic/amplitudes
topic/loop-integrals
topic/collider-phenomenology
topic/experiment
```

### Project tags

Deferred: these tags are not written yet; see `docs/projects-design.md`.

```text
project/qec
project/jpsi-ccbar
project/general-hep
```

### Role tags

```text
role/core
role/theory
role/method
role/review
role/phenomenology
role/experiment
role/reference
```

### Processing tags

A paper gets exactly one **state** tag. A paper awaiting a human additionally
gets one or more **reason** tags, so "show me everything waiting on a human" is
a single tag query while each reason still forms its own work queue.

```text
# state
agent/processed
agent/review
agent/error

# reasons, always attached together with agent/review
agent/review/ambiguous        # a judgement landed in the review band
agent/review/coverage         # covered is low: what is this paper?
agent/review/taxonomy-gap     # in scope, but no configured topic fits
agent/review/missing-abstract # too little text to judge
```

Each reason maps to a different next action:

| reason | what a human should do |
| --- | --- |
| `ambiguous` | calibrate a threshold, or rewrite a description in `config.yaml` |
| `coverage` | read the paper and decide what it is |
| `taxonomy-gap` | decide whether `config.yaml` needs a new topic |
| `missing-abstract` | supply metadata, or accept a title-only classification |

Priority may either be classified by Jev later or derived deterministically from project relevance and role:

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

  heavy-flavor:
    description: >
      Heavy-quark production, fragmentation, heavy-flavor hadrons,
      and related perturbative or nonperturbative physics.

  fragmentation:
    description: >
      Fragmentation functions, hadron production through fragmentation,
      evolution, and fragmentation factorization.

  perturbative-qcd:
    description: >
      Fixed-order and resummed perturbative QCD calculations,
      including higher-order corrections.

  amplitudes:
    description: >
      Scattering amplitudes, helicity methods, analytic structures,
      amplitude construction, and related formal methods.

  loop-integrals:
    description: >
      Loop integration, IBP reduction, differential equations,
      master integrals, and automated multi-loop computation.

  collider-phenomenology:
    description: >
      Collider predictions, observable-level phenomenology,
      event distributions, and comparison with collider data.

  experiment:
    description: >
      Experimental measurements, detector-level analyses,
      and direct presentation of collider data.

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
roles:
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
  topic_apply: 0.85
  # project_apply: 0.85  # deferred with the projects dimension
  role_apply: 0.85

  review: 0.55
  missing_topic_review: 0.70
  irrelevant: 0.80
  covered_apply: 0.70
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
agent/review/taxonomy-gap
  ↓
human review
```

If several reviewed papers repeatedly belong to the same missing category, add that topic manually to `config.yaml`.

For example:

```text
paper A ─┐
paper B ─┼─→ agent/review/taxonomy-gap ─→ recurring lattice-QCD theme
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
  roles_parent: "03 Roles"        # role/<name>  -> 03 Roles/<name>
  review_collection: "04 Review"  # every agent/review* -> one queue
  route_roles: true
  remove_from_inbox: false        # true keeps the inbox a real work queue
```

Missing collections are created. `collections` is a complete list on write, so
membership is merged rather than replaced: collections this tool knows nothing
about are never dropped. A paper in review is filed in its topic collection
*and* the review queue.

`--prune` makes routing converge: a paper is removed from the managed
collections (`topics_parent/*`, `roles_parent/*`, the review queue) that its
tags no longer point at. Anything outside those namespaces — your own folders —
is never touched. Without `--prune`, routing only ever adds.

### Reclassification converges

A re-run replaces the state instead of accumulating it. `agent/processed`,
`agent/review`, `agent/error` and the review reasons are one mutually exclusive
namespace: a plan also removes the state tags it does not ask for, so a paper
processed under an older rule loses the tags that rule produced. Running twice
changes nothing the second time.

`topic/*` and `role/*` stay add-only: you may have added them by hand, and the
model's probabilities drift by a few hundredths between runs, so removing them
would delete your intent and flap.

To make an already-tagged paper follow a new rule, include it explicitly:

```bash
jevero process --apply --include-processed --limit 1
jevero route --apply --prune
```

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
3. the key is kept **in memory only** — never written to `.env` or disk — and
   reused for the rest of the run;
4. every write echoes `Zotero-Server-ID` (otherwise `428 Precondition
   Required`) and carries `If-Unmodified-Since-Version`. A `412` means the item
   changed underneath and the run must be repeated; a `401` means the key was
   consumed or revoked, and jevero re-authorizes once and retries.

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

The processor should operate on papers that:

```text
are in the configured Inbox
AND
do not have agent/processed
```

The exact collection lookup mechanism can be configured later.

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

Topics
  energy-correlator       0.97  APPLY
  quarkonium              0.96  APPLY
  nrqcd                   0.91  APPLY
  scet                    0.42

Roles
  core                    0.87  APPLY
  method                  0.61

Coverage
  covered                 0.96
  missing-topic           0.03
  irrelevant              0.01

Planned tags
  + topic/energy-correlator
  + topic/quarkonium
  + topic/nrqcd
  + role/core
  + agent/processed
```

Use dry-run on a representative validation set before enabling automatic writes.

---

## Review behavior

Classification should be conservative.

A useful starting rule is, per dimension (topics, roles):

```text
topic p >= 0.85
    automatically apply

no topic applied, and the best topic in [0.55, 0.85)
    the topics are undecided; add agent/review/ambiguous

no topic applied, and no topic >= 0.55
    ignore
```

Two deliberate restrictions keep this from firing on almost every paper:

- a dimension that already produced an applied tag counts as decided, so a
  second, weaker candidate in the band ("maybe also this") is not reported;
- **roles never gate review.** Roles are facets, so "no confident role" is a
  normal outcome — it still produces tags, it just does not decide whether a
  human is needed.

Coverage uses separate thresholds:

```text
missing-topic >= 0.70
    → agent/review/taxonomy-gap

irrelevant >= 0.80
    → process without assigning a topic

covered < 0.70
    → agent/review/coverage
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
    and roles.get("core", 0.0) >= 0.80
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

The MVP uses Zotero tags instead of a local database.

Supported state tags:

```text
agent/processed
agent/review
agent/error
```

A review state always carries at least one reason tag:

```text
agent/review/ambiguous
agent/review/coverage
agent/review/taxonomy-gap
agent/review/missing-abstract
```

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
roles
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
