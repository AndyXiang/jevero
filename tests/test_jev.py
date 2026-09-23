"""Classifier tests. Network calls are mocked; no live OpenRouter access."""

from __future__ import annotations

import json

import httpx
import pytest

from jevero.config import Config
from jevero.jev import (
    JevClient,
    JevError,
    JevResponseError,
    JevTransportError,
    build_questions,
    build_state,
    parse_answers,
    question_key,
)
from jevero.models import PaperRecord


@pytest.fixture
def paper() -> PaperRecord:
    return PaperRecord(
        zotero_key="ABCD2345",
        title="Energy Correlators in Heavy Quarkonium Production",
        abstract="We study energy correlators for quarkonium production.",
        authors=["Ada Lovelace", "Emmy Noether"],
        year=2019,
        doi="10.1000/example.doi",
        arxiv_id="1905.01234",
    )


def complete_answers(config: Config, value: float = 0.9) -> dict:
    answers = {}
    for kind, names in (
        ("topic", config.topics),
        ("kind", config.kinds),
        ("coverage", config.coverage),
    ):
        for name in names:
            answers[question_key(kind, name)] = {"type": "noul", "noul": value}
    return answers


def make_client(handler, *, attempts: int = 3) -> JevClient:
    """A client with real retry behaviour but no sleeping between attempts."""
    return JevClient(
        "test-key",
        model="typesafe/jev-1.13",
        base_url="https://openrouter.ai/api",
        retry_attempts=attempts,
        retry_backoff_seconds=0.0,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def test_questions_cover_every_configured_entry(config: Config):
    questions = build_questions(config)

    expected = {
        question_key("topic", "quarkonium"),
        question_key("topic", "nrqcd"),
        question_key("kind", "core"),
        question_key("kind", "method"),
        question_key("coverage", "covered"),
        question_key("coverage", "missing-topic"),
        question_key("coverage", "irrelevant"),
    }
    assert set(questions) == expected
    assert all(q["type"] == "noul" for q in questions.values())
    # Descriptions from config.yaml become the classification criteria.
    assert "quarkonium" in questions[question_key("topic", "quarkonium")]["criteria"]["true"]


def test_state_carries_metadata_and_taxonomy_but_no_tag_instructions(
    config: Config, paper: PaperRecord
):
    state = build_state(paper, config)
    serialized = json.dumps(state)

    assert state["paper"]["title"] == paper.title
    assert state["paper"]["abstract"] == paper.abstract
    assert set(state["taxonomy"]["topics"]) == set(config.topics)
    assert state["intended_scope"] == config.classification.scope
    # The classifier is told what things are, never what to write.
    assert "topic/" not in serialized
    assert "jevero/processed" not in serialized


def test_parse_answers_returns_probabilities(config: Config):
    result = parse_answers(complete_answers(config, 0.9), config)

    assert result.topics == {"quarkonium": 0.9, "nrqcd": 0.9}
    assert result.coverage["missing-topic"] == 0.9


def test_missing_answer_is_rejected(config: Config):
    answers = complete_answers(config)
    del answers[question_key("topic", "nrqcd")]

    with pytest.raises(JevResponseError, match="did not answer"):
        parse_answers(answers, config)


def test_wrong_answer_type_is_rejected(config: Config):
    answers = complete_answers(config)
    answers[question_key("kind", "core")] = {"type": "choice", "choice": "core"}

    with pytest.raises(JevResponseError, match="expected 'noul'"):
        parse_answers(answers, config)


def test_non_numeric_probability_is_rejected(config: Config):
    answers = complete_answers(config)
    answers[question_key("kind", "core")] = {"type": "noul", "noul": "high"}

    with pytest.raises(JevResponseError, match="must be a number"):
        parse_answers(answers, config)


@pytest.mark.parametrize("bad", [-0.01, 1.01, 42])
def test_out_of_range_probability_is_rejected(config: Config, bad: float):
    answers = complete_answers(config)
    answers[question_key("topic", "nrqcd")] = {"type": "noul", "noul": bad}

    with pytest.raises(JevResponseError, match="must lie in"):
        parse_answers(answers, config)


def test_non_mapping_answers_are_rejected(config: Config):
    with pytest.raises(JevResponseError, match="expected an 'answers' object"):
        parse_answers(["nope"], config)


def test_classify_sends_one_request_for_every_dimension(config: Config, paper: PaperRecord):
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("Authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": "gen-dec-1",
                "model": "typesafe/jev-1.13-20260917",
                "provider": "TypeSafe",
                "answers": complete_answers(config, 0.88),
                "usage": {"input_tokens": 476, "output_tokens": 70, "cost": 0.00002},
            },
        )

    with make_client(handler) as client:
        outcome = client.classify(paper, config)

    assert seen["url"] == "https://openrouter.ai/api/alpha/decisions"
    assert seen["auth"] == "Bearer test-key"
    assert seen["body"]["model"] == "typesafe/jev-1.13"
    assert seen["body"]["session_id"] == "jevero:ABCD2345"
    assert set(seen["body"]["questions"]) == set(build_questions(config))

    assert outcome.result.topics["nrqcd"] == 0.88
    assert outcome.model == "typesafe/jev-1.13-20260917"
    assert outcome.provider == "TypeSafe"
    assert outcome.request_id == "gen-dec-1"
    assert outcome.usage is not None and outcome.usage.cost == 0.00002


def test_malformed_response_is_rejected(config: Config, paper: PaperRecord):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"answers": {}})

    with make_client(handler) as client:
        with pytest.raises(JevResponseError):
            client.classify(paper, config)


def test_non_json_response_is_rejected(config: Config, paper: PaperRecord):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>gateway</html>")

    with make_client(handler) as client:
        with pytest.raises(JevResponseError, match="not valid JSON"):
            client.classify(paper, config)


def test_http_error_becomes_a_transport_error(config: Config, paper: PaperRecord):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(503, text="upstream unavailable")

    with make_client(handler) as client:
        with pytest.raises(JevTransportError, match="HTTP 503"):
            client.classify(paper, config)

    assert len(calls) == 3  # a 5xx is transient, so it is retried


def test_a_transient_transport_failure_is_retried(config: Config, paper: PaperRecord):
    """The real outage here was SSL EOF on the first attempt(s)."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) < 3:
            raise httpx.ConnectError("ssl eof", request=request)
        return httpx.Response(200, json={
            "id": "gen-1", "model": "m", "provider": "p",
            "answers": complete_answers(config, 0.9),
        })

    with make_client(handler) as client:
        outcome = client.classify(paper, config)

    assert len(calls) == 3
    assert outcome.result.topics["nrqcd"] == 0.9


def test_a_transient_http_error_is_retried(config: Config, paper: PaperRecord):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, text="slow down")
        return httpx.Response(200, json={
            "id": "gen-1", "model": "m", "provider": "p",
            "answers": complete_answers(config, 0.9),
        })

    with make_client(handler) as client:
        outcome = client.classify(paper, config)

    assert len(calls) == 2
    assert outcome.result.topics["nrqcd"] == 0.9


def test_a_client_error_is_not_retried(config: Config, paper: PaperRecord):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(401, text="invalid api key")

    with make_client(handler) as client:
        with pytest.raises(JevTransportError, match="HTTP 401"):
            client.classify(paper, config)

    assert len(calls) == 1


def test_network_failure_becomes_a_transport_error(config: Config, paper: PaperRecord):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host", request=request)

    with make_client(handler) as client:
        with pytest.raises(JevTransportError, match="could not reach"):
            client.classify(paper, config)


def test_api_key_is_required():
    with pytest.raises(JevError, match="API key"):
        JevClient("", model="typesafe/jev-1.13")


def test_the_gap_question_asks_about_the_area_and_the_framework(config: Config):
    """The pair has to be symmetric, or a whole missing subfield reads as covered.

    The gap question used to ask only whether the paper was *built on* an approach
    the list had no place for. A paper whose framework matched a listed topic while
    its entire area of physics did not was therefore answered "not a gap" --
    measured at 0.18 for a heavy-ion paper against a 0.25 gate. Asking about the
    area *or* the framework, and answering "no" only when both are covered,
    separates real gaps (0.63-0.84) from the rest (<=0.33).
    """
    questions = build_questions(config)

    missing = questions[question_key("coverage", "missing-topic")]["instructions"]
    covered = questions[question_key("coverage", "covered")]["instructions"]

    assert "area of physics" in missing
    assert "framework" in missing
    assert "both covered" in missing
    assert "approach" in covered and "subject matter" in covered
    assert "outside the intended literature scope" in questions[
        question_key("coverage", "irrelevant")
    ]["instructions"]


def test_the_task_asks_for_a_selective_topic_choice(config: Config):
    from jevero.jev import build_state

    state = build_state(
        PaperRecord(zotero_key="K", title="T", abstract="A"), config
    )
    task = state["task"]

    assert "two or three" in task
    assert "specific" in task


def test_topic_questions_ask_about_central_topics(config: Config):
    questions = build_questions(config)
    instructions = questions[question_key("topic", "nrqcd")]["instructions"]

    assert "central topics" in instructions
    assert "neighbouring" in instructions


def test_fingerprint_covers_everything_that_changes_a_judgement(config: Config):
    """Staleness is derived from this digest, so it has to be complete.

    A field that changes the judgement or the tags but not the digest would let a
    paper keep tags from a superseded configuration.
    """
    from jevero.config import TaxonomyEntry
    from jevero.jev import fingerprint

    base = fingerprint(config)
    assert fingerprint(config) == base  # deterministic

    other_model = config.model_copy(
        update={"model": config.model.model_copy(update={"name": "typesafe/jev-9.99"})}
    )
    other_floor = config.model_copy(
        update={"thresholds": config.thresholds.model_copy(update={"apply_floor": 0.5})}
    )
    other_topics = config.model_copy(
        update={
            "topics": {
                **config.topics,
                "extra": TaxonomyEntry(description="A brand new topic."),
            }
        }
    )

    assert fingerprint(other_model) != base
    assert fingerprint(other_floor) != base
    assert fingerprint(other_topics) != base
