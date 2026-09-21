"""Jev classifier access, through OpenRouter's Decisions endpoint.

Jev is a *decision* model, not a chat model. It reads a state and answers typed
questions, returning probabilities rather than prose. One request evaluates
every configured topic, role, and coverage state for a single paper.

Verified transport contract (OpenRouter, ``/api/alpha/decisions``)::

    POST {base_url}/alpha/decisions
    Authorization: Bearer $OPENROUTER_API_KEY

    {
      "model": "typesafe/jev-1.13",
      "state": "<string | object | array>",
      "questions": {
        "<key>": {"type": "noul", "instructions": "...",
                  "criteria": {"true": "...", "false": "..."}}
      }
    }

    {
      "id": "...", "model": "typesafe/jev-1.13-20260917", "provider": "TypeSafe",
      "answers": {"<key>": {"type": "noul", "noul": 0.96}},
      "usage": {"input_tokens": 476, "output_tokens": 70, "cost": 0.00002}
    }

Question types are ``noul`` (a yes/no probability), ``choice`` (one of several
options) and ``score`` (a position on an ordered scale). Independent membership
judgments are modelled as one ``noul`` question per taxonomy entry, because a
paper may legitimately belong to several topics at once.

All provider-specific code stays in this module: switching to the TypeSafe API
directly must not touch ``policy.py`` or ``zotero.py``.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx
from pydantic import ValidationError

from .config import Config
from .http import create_client
from .models import ClassificationResult, PaperRecord, Usage

DECISIONS_PATH = "/alpha/decisions"
ANSWER_TYPE_NOUL = "noul"

#: A dropped TLS connection or a transient 5xx should not cost a paper.
RETRY_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 1.5

#: Coverage is judged on aspects, not just on the subject: a topic list that
#: names the paper's subject but none of its methods is not full coverage. Kept
#: per state so each question asks one thing; unknown states fall back to a
#: generic phrasing.
_COVERAGE_INSTRUCTIONS = {
    "covered": (
        "Are both the paper's subject matter and the approach it is built on "
        "represented by the configured topics? Answer yes even if a specific "
        "technique, observable, or formalism variant it uses is not separately "
        "named, as long as its approach fits inside a listed topic."
    ),
    "missing-topic": (
        "Is this paper built on a whole approach - an entire method, framework, "
        "or subfield - that no configured topic has any place for? Answer yes "
        "when browsing the configured topics would leave this paper's approach "
        "with nowhere to go. Answer no when the approach fits inside a topic that "
        "is already listed, even if the paper's particular technique, observable, "
        "or formalism variant is not named."
    ),
    "irrelevant": "Is this paper outside the intended literature scope?",
}

TOPIC_KIND = "topic"
ROLE_KIND = "role"
COVERAGE_KIND = "coverage"

_INTENDED_SCOPE_FALLBACK = (
    "the reader's research literature on perturbative QCD, effective field "
    "theories, heavy-flavor and quarkonium physics, collider phenomenology, "
    "and related computational methods"
)


class JevError(Exception):
    """Base class for classifier failures."""


class JevTransportError(JevError):
    """The request could not be completed (network, timeout, non-2xx response)."""


class JevResponseError(JevError):
    """The response arrived but could not be trusted."""


@dataclass(frozen=True)
class JevOutcome:
    """A validated classification plus the accounting metadata from the call."""

    result: ClassificationResult
    model: str | None = None
    provider: str | None = None
    request_id: str | None = None
    usage: Usage | None = None


def question_key(kind: str, name: str) -> str:
    """Answer key for one question, mirroring the tag namespace."""
    return f"{kind}/{name}"


def build_state(paper: PaperRecord, config: Config) -> dict[str, Any]:
    """Everything the classifier is allowed to see.

    Original metadata plus the taxonomy. No PDF text, no summaries, no
    instructions about what to write into Zotero.
    """
    return {
        "task": (
            "Judge this paper for a personal Zotero literature library. Answer "
            "each question independently; a paper may belong to several topics "
            "at once. Report what the paper appears to be, not what should be "
            "done with it."
        ),
        "paper": {
            "title": paper.title,
            "abstract": paper.abstract,
            "authors": paper.authors,
            "year": paper.year,
            "doi": paper.doi,
            "arxiv_id": paper.arxiv_id,
        },
        "intended_scope": config.classification.scope or _INTENDED_SCOPE_FALLBACK,
        "taxonomy": {
            "topics": {name: entry.description for name, entry in config.topics.items()},
            "roles": dict(config.roles),
        },
        "coverage_states": dict(config.coverage),
        "note": (
            "Coverage is not a topic. Judge it on the paper's subject matter and "
            "on the approach it is built on - not on every detail: a specific "
            "technique or observable that sits inside a listed topic is not a "
            "gap. 'missing-topic' means the paper is built on a whole approach "
            "that no topic has a place for, so the taxonomy is incomplete; "
            "'irrelevant' means the paper is outside the intended scope."
        ),
    }


def build_questions(config: Config) -> dict[str, dict[str, Any]]:
    """One question per configured topic, role, and coverage state."""
    questions: dict[str, dict[str, Any]] = {}

    for name, entry in config.topics.items():
        questions[question_key(TOPIC_KIND, name)] = _noul(
            instructions=(
                f"Does this paper's subject matter fall within the topic "
                f"'{name}'?"
            ),
            true_description=entry.description,
            false_description=(
                f"The paper does not substantively address the topic '{name}'."
            ),
        )

    for name, description in config.roles.items():
        questions[question_key(ROLE_KIND, name)] = _noul(
            instructions=f"Does the role '{name}' describe this paper?",
            true_description=description
            or f"The paper plays the role '{name}' in a literature workflow.",
            false_description=f"The paper does not play the role '{name}'.",
        )

    for name, description in config.coverage.items():
        questions[question_key(COVERAGE_KIND, name)] = _noul(
            instructions=_COVERAGE_INSTRUCTIONS.get(
                name,
                f"Does the coverage state '{name}' describe how well the "
                f"configured topics cover this paper?",
            ),
            true_description=description
            or f"The coverage state '{name}' applies to this paper.",
            false_description=f"The coverage state '{name}' does not apply.",
        )

    return questions


def parse_answers(answers: Any, config: Config) -> ClassificationResult:
    """Validate raw answers into a ``ClassificationResult``.

    Malformed output is rejected rather than guessed at: a missing answer, a
    non-numeric probability, or a value outside ``[0, 1]`` raises
    ``JevResponseError``.
    """
    if not isinstance(answers, Mapping):
        raise JevResponseError(
            f"expected an 'answers' object, got {type(answers).__name__}"
        )

    try:
        return ClassificationResult(
            topics={
                name: _probability(answers, question_key(TOPIC_KIND, name))
                for name in config.topics
            },
            roles={
                name: _probability(answers, question_key(ROLE_KIND, name))
                for name in config.roles
            },
            coverage={
                name: _probability(answers, question_key(COVERAGE_KIND, name))
                for name in config.coverage
            },
        )
    except ValidationError as exc:
        raise JevResponseError(f"invalid classification probabilities: {exc}") from exc


class JevClient:
    """Synchronous OpenRouter client for the Jev decision model."""

    def __init__(
        self,
        api_key: str,
        *,
        model: str,
        base_url: str = "https://openrouter.ai/api",
        timeout_seconds: float = 60.0,
        retry_attempts: int = RETRY_ATTEMPTS,
        retry_backoff_seconds: float = RETRY_BACKOFF_SECONDS,
        http_client: httpx.Client | None = None,
    ) -> None:
        if not api_key:
            raise JevError("an OpenRouter API key is required")
        self._api_key = api_key
        self._model = model
        self._endpoint = base_url.rstrip("/") + DECISIONS_PATH
        self._timeout = timeout_seconds
        self._retry_attempts = max(1, retry_attempts)
        self._retry_backoff = retry_backoff_seconds
        self._http = http_client or create_client(timeout_seconds)
        self._owns_client = http_client is None

    def __enter__(self) -> JevClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_client:
            self._http.close()

    def classify(self, paper: PaperRecord, config: Config) -> JevOutcome:
        """Classify one paper in a single Decisions request."""
        state = build_state(paper, config)
        questions = build_questions(config)
        payload = self._request(
            state, questions, session_id=f"jevero:{paper.zotero_key}"
        )
        result = parse_answers(payload.get("answers", {}), config)
        return JevOutcome(
            result=result,
            model=payload.get("model"),
            provider=payload.get("provider"),
            request_id=payload.get("id"),
            usage=_usage(payload.get("usage")),
        )

    def _request(
        self, state: dict[str, Any], questions: dict[str, dict[str, Any]], *, session_id: str
    ) -> dict[str, Any]:
        body = {
            "model": self._model,
            "state": state,
            "questions": questions,
            "session_id": session_id,
        }
        response = self._post(body)
        if response.status_code >= 400:
            raise JevTransportError(
                f"Jev request failed with HTTP {response.status_code}: "
                f"{_snippet(response)}"
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise JevResponseError("Jev response was not valid JSON") from exc
        if not isinstance(payload, dict):
            raise JevResponseError(
                f"Jev response must be a JSON object, got {type(payload).__name__}"
            )
        return payload


    def _post(self, body: dict[str, Any]) -> httpx.Response:
        """POST the request, retrying transport blips and transient statuses.

        A dropped connection, a timeout, a 429 or a 5xx says nothing about the
        paper, so those are retried with backoff; anything else is returned as
        it is. If every attempt fails to get a response at all, this raises
        ``JevTransportError``, which the caller reads as "the classifier is
        unavailable" rather than "this paper is bad".
        """
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        last_error = "no attempt was made"
        for attempt in range(1, self._retry_attempts + 1):
            retry_after: float | None = None
            try:
                response = self._http.post(self._endpoint, json=body, headers=headers)
            except httpx.TransportError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            else:
                if not _is_transient(response.status_code):
                    return response
                last_error = f"HTTP {response.status_code}: {_snippet(response)}"
                if attempt == self._retry_attempts:
                    return response
                retry_after = _retry_after_seconds(response)

            if attempt < self._retry_attempts:
                delay = (
                    retry_after
                    if retry_after is not None
                    else self._retry_backoff * 2 ** (attempt - 1)
                )
                time.sleep(delay)

        raise JevTransportError(
            f"could not reach the Jev endpoint {self._endpoint} after "
            f"{self._retry_attempts} attempts ({last_error})"
        )


def _is_transient(status_code: int) -> bool:
    """Worth retrying: rate limiting, or the gateway/provider failing."""
    return status_code == 429 or status_code >= 500


def _retry_after_seconds(response: httpx.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None


def _noul(*, instructions: str, true_description: str, false_description: str) -> dict[str, Any]:
    return {
        "type": ANSWER_TYPE_NOUL,
        "instructions": instructions,
        "criteria": {"true": true_description, "false": false_description},
    }


def _probability(answers: Mapping[str, Any], key: str) -> float:
    answer = answers.get(key)
    if answer is None:
        raise JevResponseError(f"Jev did not answer {key!r}")
    if not isinstance(answer, Mapping):
        raise JevResponseError(
            f"answer for {key!r} must be an object, got {type(answer).__name__}"
        )
    if answer.get("type") != ANSWER_TYPE_NOUL:
        raise JevResponseError(
            f"answer for {key!r} has type {answer.get('type')!r}, "
            f"expected {ANSWER_TYPE_NOUL!r}"
        )
    value = answer.get(ANSWER_TYPE_NOUL)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise JevResponseError(
            f"answer for {key!r} must be a number, got {value!r}"
        )
    probability = float(value)
    if not 0.0 <= probability <= 1.0:
        raise JevResponseError(
            f"answer for {key!r} must lie in [0, 1], got {probability!r}"
        )
    return probability


def _usage(raw: Any) -> Usage | None:
    if not isinstance(raw, Mapping):
        return None
    cost = raw.get("cost")
    return Usage(
        input_tokens=_as_int(raw.get("input_tokens")),
        output_tokens=_as_int(raw.get("output_tokens")),
        cost=float(cost) if isinstance(cost, int | float) else None,
    )


def _as_int(value: Any) -> int | None:
    return int(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def _snippet(response: httpx.Response, limit: int = 300) -> str:
    """Short, secret-free description of a failed response."""
    text = response.text or ""
    text = " ".join(text.split())
    return text[:limit]
