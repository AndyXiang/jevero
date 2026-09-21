"""Loading and validation of ``config.yaml`` and the local environment.

Configuration errors are raised as ``ConfigError`` so the CLI can report them
without a traceback, and so no stage runs with a half-valid taxonomy.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .models import COVERAGE_STATES

DEFAULT_CONFIG_PATH = Path("config.yaml")
DEFAULT_ENV_PATH = Path(".env")

#: Taxonomy names become tag suffixes, so keep them tag-safe.
_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


class ConfigError(Exception):
    """Raised when configuration is missing, unreadable, or invalid."""


class MissingEnvironmentError(ConfigError):
    """Raised when a required secret is absent from the environment."""


class TaxonomyEntry(BaseModel):
    """One topic, with the description used for classification."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    description: str

    @field_validator("description")
    @classmethod
    def _require_description(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("description must not be empty")
        return value.strip()


class Thresholds(BaseModel):
    """Probability cut-offs. Every Zotero mutation must trace back to one."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    topic_apply: float = 0.85
    role_apply: float = 0.85

    review: float = 0.55
    missing_topic_review: float = 0.70
    irrelevant: float = 0.80
    #: How convincing ``coverage.covered`` must be for the taxonomy to count as
    #: adequate. Below it the result is reported for review instead of acted on.
    covered_apply: float = 0.70

    @field_validator("*")
    @classmethod
    def _validate_range(cls, value: float, info) -> float:
        if not 0.0 <= float(value) <= 1.0:
            raise ValueError(f"{info.field_name} must lie in [0, 1], got {value!r}")
        return float(value)

    @model_validator(mode="after")
    def _validate_ordering(self) -> Thresholds:
        for name in ("topic_apply", "role_apply"):
            if getattr(self, name) <= self.review:
                raise ValueError(
                    f"{name} ({getattr(self, name)}) must be greater than "
                    f"review ({self.review}); otherwise the review band is empty"
                )
        if self.missing_topic_review <= self.review:
            raise ValueError(
                "missing_topic_review must be greater than review; otherwise "
                "missing-topic and ambiguity cannot be distinguished"
            )
        if self.covered_apply <= self.review:
            raise ValueError(
                "covered_apply must be greater than review; otherwise the "
                "coverage judgement can never be convincing"
            )
        return self


class ModelConfig(BaseModel):
    """Where and how the classifier is reached."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: Literal["openrouter"] = "openrouter"
    name: str = "typesafe/jev-1.13"
    base_url: str = "https://openrouter.ai/api"
    timeout_seconds: float = Field(default=60.0, gt=0)


class ZoteroConfig(BaseModel):
    """Which Zotero library to read, and whether writes are possible."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    backend: Literal["web", "local"] = "web"
    library_type: Literal["user", "group"] = "user"
    library_id: str = ""
    inbox_collection: str = "00 Inbox"
    timeout_seconds: float = Field(default=30.0, gt=0)


class ClassificationConfig(BaseModel):
    """Behaviour that is not a threshold or a taxonomy description."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    allow_title_only: bool = False
    #: Prose description of what belongs in this library at all. It is the
    #: evidence behind the `irrelevant` coverage judgment, so it belongs in
    #: configuration rather than in a prompt string.
    scope: str = ""


class Config(BaseModel):
    """The validated contents of ``config.yaml``.

    Unknown keys are rejected rather than ignored: a typo, or a dimension that
    is no longer implemented, must not silently change nothing.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    model: ModelConfig = ModelConfig()
    zotero: ZoteroConfig = ZoteroConfig()
    classification: ClassificationConfig = ClassificationConfig()

    topics: dict[str, TaxonomyEntry]
    roles: dict[str, str] = Field(default_factory=dict)
    coverage: dict[str, str] = Field(default_factory=dict)
    thresholds: Thresholds = Thresholds()

    @field_validator("topics")
    @classmethod
    def _validate_named_entries(
        cls, value: dict[str, TaxonomyEntry], info
    ) -> dict[str, TaxonomyEntry]:
        if not value:
            raise ValueError(f"{info.field_name} must define at least one entry")
        for name in value:
            _validate_name(name, info.field_name)
        return value

    @field_validator("roles", "coverage", mode="before")
    @classmethod
    def _normalize_named(cls, value: object, info) -> dict[str, str]:
        """Accept a list of names, or a mapping of name -> description.

        Three shapes are accepted, because all three read well in YAML::

            roles: [core, method]
            roles:
              core: Central to the reader's work.
            roles:
              core:
                description: Central to the reader's work.

        Descriptions are classification guidance, so the mapping forms are
        preferred: a one-word label is rarely unambiguous.
        """
        if value is None:
            return {}

        pairs: list[tuple[object, object]] = []
        if isinstance(value, dict):
            pairs = list(value.items())
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, str):
                    pairs.append((item, ""))
                elif isinstance(item, dict):
                    pairs.extend(item.items())
                else:
                    raise ValueError(
                        f"{info.field_name} entries must be strings or "
                        f"single-entry name -> description mappings, "
                        f"got {type(item).__name__}"
                    )
        else:
            raise ValueError(
                f"{info.field_name} must be a list of names or a mapping of "
                f"name -> description, got {type(value).__name__}"
            )

        normalized: dict[str, str] = {}
        for name, description in pairs:
            if not isinstance(name, str):
                raise ValueError(
                    f"{info.field_name} entries must be strings or "
                    f"name -> description mappings"
                )
            _validate_name(name, info.field_name)
            normalized[name] = _coerce_description(
                description, field_name=info.field_name, name=name
            )
        return normalized

    @model_validator(mode="after")
    def _validate_coverage_states(self) -> Config:
        missing = [state for state in COVERAGE_STATES if state not in self.coverage]
        if missing:
            raise ValueError(
                "coverage must define the states "
                f"{', '.join(COVERAGE_STATES)}; missing {', '.join(missing)}"
            )
        return self


def _validate_name(name: str, field_name: str) -> None:
    if not _NAME_PATTERN.match(name):
        raise ValueError(
            f"{field_name} name {name!r} is not tag-safe; expected "
            f"{_NAME_PATTERN.pattern}"
        )


def _coerce_description(value: object, *, field_name: str, name: str) -> str:
    """Read a description from either a bare string or a ``description`` key."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict) and set(value) == {"description"}:
        description = value["description"]
        if isinstance(description, str):
            return description.strip()
    raise ValueError(
        f"{field_name}.{name} must be a description string or a mapping with "
        f"a 'description' key"
    )


def load_config(path: str | Path = DEFAULT_CONFIG_PATH) -> Config:
    """Read and validate ``config.yaml``.

    Raises:
        ConfigError: the file is missing, is not valid YAML, or does not match
            the expected schema.
    """
    config_path = Path(path)
    try:
        raw = config_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ConfigError(f"configuration file not found: {config_path}") from exc
    except OSError as exc:
        raise ConfigError(f"cannot read {config_path}: {exc}") from exc

    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{config_path} is not valid YAML: {exc}") from exc

    if data is None:
        raise ConfigError(f"{config_path} is empty")
    if not isinstance(data, dict):
        raise ConfigError(
            f"{config_path} must contain a mapping at the top level, "
            f"got {type(data).__name__}"
        )

    try:
        return Config.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(f"{config_path} is invalid:\n{exc}") from exc


def load_environment(env_file: str | Path | None = DEFAULT_ENV_PATH) -> None:
    """Load ``.env`` without overriding already-set variables."""
    path = Path(env_file) if env_file is not None else None
    if path is not None and not path.exists():
        return
    load_dotenv(dotenv_path=path, override=False)


def _optional_env(name: str) -> str | None:
    value = os.environ.get(name)
    return value.strip() if value and value.strip() else None


def _require_env(name: str) -> str:
    value = _optional_env(name)
    if value is None:
        raise MissingEnvironmentError(
            f"{name} is not set; copy .env.example to .env and fill it in"
        )
    return value


def openrouter_api_key() -> str:
    """OpenRouter key used by the Jev client."""
    return _require_env("OPENROUTER_API_KEY")


def zotero_api_key() -> str | None:
    """Zotero key. Required for the Web API, optional for local read-only use."""
    return _optional_env("ZOTERO_API_KEY")


def zotero_library_id(config: Config) -> str:
    """Library ID from the environment, falling back to ``config.yaml``."""
    return _optional_env("ZOTERO_LIBRARY_ID") or config.zotero.library_id
