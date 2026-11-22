from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from http.client import IncompleteRead
from io import BytesIO
import json
from threading import Event, Thread
from urllib.error import HTTPError, URLError

import pytest

from codex_usage.config import CostEndpointSettings
from codex_usage.endpoint import (
    EndpointError, EndpointPoller, EndpointState, _NoRedirect, endpoint_state, fetch_cost, parse_cost,
)


SETTINGS = CostEndpointSettings(
    enabled=True, url="https://example.com/costs", api_key_env="TEST_COST_KEY", currency_override="USD",
)
PAYLOAD = {"usage": Decimal("480.0128"), "limit": Decimal("500"), "currency": "GBP",
           "asOf": "2026-11-22T00:00:00Z"}


def test_reported_amount_is_exact_and_currency_override_does_not_convert() -> None:
    cost = parse_cost(PAYLOAD, SETTINGS)
    assert cost.usage_usd == Decimal("480.0128")
    assert cost.limit_usd == Decimal("500")
    assert cost.as_of == datetime(2026, 11, 22, tzinfo=timezone.utc)
    assert cost.to_dict()["currency"] == "USD"
    assert cost.to_dict()["usage_usd"] == "480.0128"
    with pytest.raises(EndpointError, match="currency"):
        parse_cost(PAYLOAD, replace(SETTINGS, currency_override=""))


def test_nested_fields_and_optional_metadata() -> None:
    cost = parse_cost({"data": {"spent": "10.1234567890123456789"}},
                      replace(SETTINGS, usage_field="data.spent"))
    assert cost.usage_usd == Decimal("10.1234567890123456789")
    assert cost.limit_usd is cost.as_of is None
    cost = parse_cost({"spent": "0", "quota": {"max": "0"}, "time": "2026-11-21T23:00:00-01:00"},
                      replace(SETTINGS, usage_field="spent", limit_field="quota.max", as_of_field="time"))
    assert cost.limit_usd == 0
    assert cost.as_of == datetime(2026, 11, 22, tzinfo=timezone.utc)


@pytest.mark.parametrize("value", [None, True, "NaN", "sNaN", "Infinity", "-1", {}, []])
@pytest.mark.parametrize("field", ["usage", "limit"])
def test_invalid_amounts_are_rejected(field, value) -> None:
    if field == "limit" and value is None:
        assert parse_cost({**PAYLOAD, field: value}, SETTINGS).limit_usd is None
    else:
        with pytest.raises(EndpointError, match=field):
            parse_cost({**PAYLOAD, field: value}, SETTINGS)


@pytest.mark.parametrize("value", ["yesterday", 42, "2026-11-22T00:00:00"])
def test_invalid_as_of_is_rejected(value) -> None:
    with pytest.raises(EndpointError, match="as-of"):
        parse_cost({**PAYLOAD, "asOf": value}, SETTINGS)


def test_disabled_or_missing_key_never_makes_a_request(monkeypatch) -> None:
    def unexpected(*args):
        pytest.fail("Endpoint should not have been contacted")
    monkeypatch.setattr("codex_usage.endpoint.build_opener", unexpected)
    assert endpoint_state(CostEndpointSettings()) is None
    assert EndpointPoller(CostEndpointSettings()).snapshot() is None
    monkeypatch.delenv("TEST_COST_KEY", raising=False)
    assert "TEST_COST_KEY" in endpoint_state(SETTINGS).error


def test_bearer_auth_query_parameters_and_decimal_json(monkeypatch) -> None:
    monkeypatch.setenv("TEST_COST_KEY", "test-secret")
    requests = []

    class Opener:
        def open(self, request, timeout):
            requests.append((request, timeout))
            return BytesIO(b'{"usage": 480.012812345678901, "currency": "GBP"}')

    monkeypatch.setattr("codex_usage.endpoint.build_opener", lambda *args: Opener())
    cost = fetch_cost(replace(SETTINGS, url="https://example.com/costs?period=month",
                              params=(("account", "a & b"),), timeout_seconds=7))
    request, timeout = requests[0]
    assert request.full_url == "https://example.com/costs?period=month&account=a+%26+b"
    assert request.get_header("Authorization") == "Bearer test-secret"
    assert request.get_method() == "GET"
    assert timeout == 7
    assert cost.usage_usd == Decimal("480.012812345678901")


@pytest.mark.parametrize("failure", ["http", "connection", "timeout", "invalid", "large", "truncated"])
def test_failures_are_nonfatal_and_never_echo_secrets(monkeypatch, failure) -> None:
    monkeypatch.setenv("TEST_COST_KEY", "test-secret")

    class Opener:
        def open(self, *args, **kwargs):
            if failure == "http":
                raise HTTPError("https://example.com/?secret=test-secret", 401, "test-secret", {}, BytesIO())
            if failure == "connection":
                raise URLError("test-secret")
            if failure == "timeout":
                raise TimeoutError("test-secret")
            if failure == "truncated":
                raise IncompleteRead(b"test-secret")
            return BytesIO(b"test-secret" if failure == "invalid" else b"x" * 1_048_577)

    monkeypatch.setattr("codex_usage.endpoint.build_opener", lambda *args: Opener())
    state = endpoint_state(SETTINGS)
    assert state.cost is None and state.error
    assert "test-secret" not in json.dumps(state.to_dict())
    if failure == "http":
        assert "HTTP 401" in state.error
    if failure == "large":
        assert "1 MiB" in state.error


def test_redirects_are_not_followed_with_credentials() -> None:
    assert _NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.example") is None


def test_watch_poller_is_nonblocking_throttled_and_retains_last_success(monkeypatch) -> None:
    started, release = Event(), Event()
    calls = []
    now = [100.0]
    good = parse_cost(PAYLOAD, SETTINGS)
    workers = []

    def worker(**kwargs):
        thread = Thread(**kwargs)
        workers.append(thread)
        return thread

    def fetch(settings):
        calls.append(settings)
        started.set()
        assert release.wait(2)
        return EndpointState(cost=good) if len(calls) == 1 else EndpointState(error="HTTP 503")

    monkeypatch.setattr("codex_usage.endpoint.endpoint_state", fetch)
    monkeypatch.setattr("codex_usage.endpoint.Thread", worker)
    monkeypatch.setattr("codex_usage.endpoint.time.monotonic", lambda: now[0])
    poller = EndpointPoller(SETTINGS)
    initial = poller.snapshot()
    assert initial.refreshing and initial.cost is None
    assert started.wait(2)
    assert poller.snapshot().refreshing  # Does not wait for the blocked request.
    assert len(calls) == 1
    release.set()
    workers[0].join(2)
    assert not workers[0].is_alive()
    assert poller.snapshot().cost == good
    assert len(calls) == 1
    now[0] += 61
    started.clear()
    poller.snapshot()
    assert started.wait(2)
    workers[1].join(2)
    assert not workers[1].is_alive()
    state = poller.snapshot()
    assert state.cost == good and state.error == "HTTP 503" and not state.refreshing
    assert len(calls) == 2
