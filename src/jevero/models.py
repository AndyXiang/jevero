"""Normalized internal data models.

Downstream modules (``policy``, ``main``) depend only on these models, never on
raw Zotero response objects or raw Jev responses. Both of those are normalized
at the edge, in ``zotero.py`` and ``jev.py``.

Keeping the models here also keeps ``policy.py`` free of HTTP concerns.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator

#: Coverage states, kept separate from topic membership.
#: There is deliberately no ``other`` topic.
COVERAGE_COVERED = "covered"
COVERAGE_MISSING_TOPIC = "missing-topic"
COVERAGE_IRRELEVANT = "irrelevant"

COVERAGE_STATES: tuple[str, ...] = (
    COVERAGE_COVERED,
    COVERAGE_MISSING_TOPIC,
    COVERAGE_IRRELEVANT,
)


class InputMode(str, Enum):
    """How much text the classification was based on."""

    FULL = "full"
    TITLE_ONLY = "title-only"


class PaperRecord(BaseModel):
    """A normalized Zotero item, ready to send to the classifier."""

    model_config = ConfigDict(frozen=True)

    zotero_key: str
    title: str
    abstract: str | None = None
    authors: list[str] = Field(default_factory=list)
    year: int | None = None
    doi: str | None = None
    arxiv_id: str | None = None

    @field_validator("zotero_key", "title")
    @classmethod
    def _require_non_empty(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("must not be empty")
        return value

    @property
    def has_abstract(self) -> bool:
        return bool(self.abstract and self.abstract.strip())

    @property
    def input_mode(self) -> InputMode:
        """Title-only input is never silently treated as full-confidence input."""
        return InputMode.FULL if self.has_abstract else InputMode.TITLE_ONLY


class ClassificationResult(BaseModel):
    """Structured semantic judgments returned by the classifier.

    Every mapping holds probabilities in ``[0, 1]``. Keys are taxonomy names
    from ``config.yaml``; membership is per-key and independent, so a paper may
    belong to several topics at once.

    A ``projects`` dimension (per-project usefulness) is designed but not
    implemented yet; see ``docs/projects-design.md``.

    The model deliberately has no ``decision`` or ``actions`` field: the
    classifier never decides what to write into Zotero.
    """

    topics: dict[str, float]
    kinds: dict[str, float]
    coverage: dict[str, float]

    @field_validator("topics", "kinds", "coverage")
    @classmethod
    def _validate_probabilities(cls, value: dict[str, float], info) -> dict[str, float]:
        if not value:
            raise ValueError(f"{info.field_name} must not be empty")
        out: dict[str, float] = {}
        for name, probability in value.items():
            if not name or not str(name).strip():
                raise ValueError(f"{info.field_name} contains an empty name")
            if not isinstance(probability, int | float) or isinstance(probability, bool):
                raise ValueError(
                    f"{info.field_name}[{name!r}] must be a number, "
                    f"got {type(probability).__name__}"
                )
            if not 0.0 <= float(probability) <= 1.0:
                raise ValueError(
                    f"{info.field_name}[{name!r}] must lie in [0, 1], "
                    f"got {probability!r}"
                )
            out[str(name)] = float(probability)
        return out


class PolicyActions(BaseModel):
    """The only thing that may be written back to Zotero.

    Produced exclusively by ``policy.py``. An LLM never constructs this.
    """

    add_tags: set[str] = Field(default_factory=set)
    remove_tags: set[str] = Field(default_factory=set)
    #: Tag subtrees the plan owns as a whole, every tag under them being removed
    #: unless it is re-added. Used for namespaces that were renamed: the
    #: superseded names cannot be enumerated one by one.
    remove_tag_prefixes: set[str] = Field(default_factory=set)
    #: Lines to set in the item's Zotero ``extra`` field, by key; ``None`` removes
    #: that line. This is where the tool keeps what a reader would never browse by
    #: — the judgement stamp and the last failure — so the tag panel stays a list
    #: of things a reader would actually search for.
    extra: dict[str, str | None] = Field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        return (
            not self.add_tags
            and not self.remove_tags
            and not self.remove_tag_prefixes
            and not self.extra
        )

    def merge(self, other: PolicyActions) -> PolicyActions:
        """Combine two action sets; additive tags win over removals."""
        add = (self.add_tags | other.add_tags) - other.remove_tags
        remove = (self.remove_tags | other.remove_tags) - add
        return PolicyActions(
            add_tags=add,
            remove_tags=remove,
            remove_tag_prefixes=self.remove_tag_prefixes | other.remove_tag_prefixes,
            extra=self.extra | other.extra,
        )


class Usage(BaseModel):
    """Token/cost accounting for one classifier request."""

    input_tokens: int | None = None
    output_tokens: int | None = None
    cost: float | None = None
