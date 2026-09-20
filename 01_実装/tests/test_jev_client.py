import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
import requests

from jev_client import JevClient, JevError

CRITERIA = {"h1": "Horse number 1 wins the race", "h2": "Horse number 2 wins the race"}


class FakeResponse:
    def __init__(self, status=200, body=None, text=""):
        self.status_code = status
        self._body = body
        self.text = text

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class FakeSession:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def post(self, url, json, headers, timeout):
        self.requests.append((url, json, headers, timeout))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


OK = FakeResponse(
    body={
        "model": "jev-1.13.0",
        "answers": {"winner": {"type": "choice", "choice": "h2", "confidence": 0.93, "probabilities": {"h1": 0.1, "h2": 0.9}}},
        "usage": {"input_tokens": 400, "output_tokens": 5},
    }
)


def client(session, attempts=3):
    return JevClient("key-123", session=session, max_attempts=attempts, sleep=lambda s: None)


def test_request_shape_and_parsing():
    session = FakeSession(OK)
    answer = client(session).ask_choice({"runners": []}, "winner", "pick one", CRITERIA)
    assert (answer.choice, answer.confidence, answer.probabilities) == ("h2", 0.93, {"h1": 0.1, "h2": 0.9})

    url, payload, headers, _ = session.requests[0]
    assert url == "https://api.typesafe.ai/v1/systemone"
    assert headers["Authorization"] == "Bearer key-123"
    assert payload["model"] == "jev-latest"
    assert payload["state"] == {"runners": []}
    assert payload["questions"] == {"winner": {"type": "choice", "instructions": "pick one", "criteria": CRITERIA}}


def test_retries_on_overload_then_succeeds():
    session = FakeSession(FakeResponse(529), requests.ConnectionError("boom"), OK)
    assert client(session).ask_choice({}, "winner", "x", CRITERIA).choice == "h2"
    assert len(session.requests) == 3


def test_gives_up_after_max_attempts():
    session = FakeSession(FakeResponse(429), FakeResponse(429))
    with pytest.raises(JevError, match="HTTP 429"):
        client(session, attempts=2).ask_choice({}, "winner", "x", CRITERIA)


def test_auth_error_is_not_retried():
    session = FakeSession(FakeResponse(401), OK)
    with pytest.raises(JevError, match="認証"):
        client(session).ask_choice({}, "winner", "x", CRITERIA)
    assert len(session.requests) == 1


def test_validation_error_is_not_retried():
    session = FakeSession(FakeResponse(422, text="bad criteria"), OK)
    with pytest.raises(JevError, match="422"):
        client(session).ask_choice({}, "winner", "x", CRITERIA)
    assert len(session.requests) == 1


def test_malformed_and_unknown_choice_rejected():
    with pytest.raises(JevError):
        client(FakeSession(FakeResponse(body={"answers": {}}))).ask_choice({}, "winner", "x", CRITERIA)
    bad = FakeResponse(body={"answers": {"winner": {"choice": "h9", "confidence": 0.9, "probabilities": {}}}})
    with pytest.raises(JevError, match="criteria"):
        client(FakeSession(bad)).ask_choice({}, "winner", "x", CRITERIA)
