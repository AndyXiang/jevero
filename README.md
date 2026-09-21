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
  name: typesafe/jev
```

Use the current Jev model identifier supported by OpenRouter when implementing the client.

Keep all provider-specific code isolated so that switching to the direct TypeSafe API later does not affect policy or Zotero code.

---

## Zotero configuration

Expected environment variables:

```bash
ZOTERO_API_KEY=...
ZOTERO_LIBRARY_ID=...
```

Example `.env.example`:

```bash
OPENROUTER_API_KEY=

ZOTERO_API_KEY=
ZOTERO_LIBRARY_ID=
```

Do not commit `.env`.

The initial implementation may use either the Zotero Web API or a supported local API path, but the storage layer should be isolated behind `zotero.py`.

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

A useful starting rule is:

```text
p >= 0.85
    automatically apply

0.55 <= p < 0.85
    ambiguous; consider agent/review

p < 0.55
    ignore
```

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
