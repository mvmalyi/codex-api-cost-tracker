from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from http.client import HTTPException
import json
import os
from threading import Lock, Thread
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .config import CostEndpointSettings


class EndpointError(ValueError):
    """An actionable endpoint error containing no credentials or response body."""


@dataclass(frozen=True)
class EndpointCost:
    usage_usd: Decimal
    limit_usd: Decimal | None
    as_of: datetime | None
    fetched_at: datetime

    def to_dict(self) -> dict[str, str | None]:
        return {
            "usage_usd": str(self.usage_usd),
            "limit_usd": str(self.limit_usd) if self.limit_usd is not None else None,
            "currency": "USD",
            "as_of": self.as_of.isoformat() if self.as_of else None,
            "fetched_at": self.fetched_at.isoformat(),
        }


@dataclass(frozen=True)
class EndpointState:
    cost: EndpointCost | None = None
    error: str | None = None
    refreshing: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "data": self.cost.to_dict() if self.cost else None,
            "error": self.error,
            "refreshing": self.refreshing,
        }


def _field(payload: object, path: str) -> object:
    value = payload
    for name in path.split("."):
        if not isinstance(value, dict) or name not in value:
            return None
        value = value[name]
    return value


def _money(value: object, name: str) -> Decimal:
    try:
        if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
            raise ValueError
        result = Decimal(value)
        if not result.is_finite() or result < 0:
            raise ValueError
    except (InvalidOperation, ValueError):
        raise EndpointError(f"The endpoint's {name} field must be a finite, non-negative amount.") from None
    return result


def parse_cost(payload: object, settings: CostEndpointSettings) -> EndpointCost:
    if not isinstance(payload, dict):
        raise EndpointError("The endpoint must return a JSON object.")
    usage = _money(_field(payload, settings.usage_field), "usage")
    limit_raw = _field(payload, settings.limit_field) if settings.limit_field else None
    limit = _money(limit_raw, "limit") if limit_raw is not None else None
    currency = settings.currency_override or _field(payload, settings.currency_field)
    if currency is not None and currency != "USD":
        raise EndpointError("The endpoint currency must be USD; set currency_override only if the label is incorrect.")
    as_of = None
    as_of_raw = _field(payload, settings.as_of_field) if settings.as_of_field else None
    if as_of_raw is not None:
        try:
            if not isinstance(as_of_raw, str):
                raise ValueError
            as_of = datetime.fromisoformat(as_of_raw.replace("Z", "+00:00"))
            if as_of.tzinfo is None:
                raise ValueError
        except ValueError:
            raise EndpointError("The endpoint's as-of field must be an ISO 8601 timestamp with a timezone.") from None
    return EndpointCost(usage, limit, as_of, datetime.now(timezone.utc))


class _NoRedirect(HTTPRedirectHandler):
    # Never forward a bearer credential to a redirected URL.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch_cost(settings: CostEndpointSettings) -> EndpointCost:
    if not settings.enabled:
        raise EndpointError("The cost endpoint is disabled.")
    key = os.environ.get(settings.api_key_env, "").strip()
    if not key:
        raise EndpointError(f"Set the {settings.api_key_env} environment variable to use the cost endpoint.")
    url = settings.url
    if settings.params:
        url += ("&" if "?" in url else "?") + urlencode(settings.params)
    try:
        request = Request(url, headers={"Authorization": f"Bearer {key}", "Accept": "application/json"})
        with build_opener(_NoRedirect()).open(request, timeout=settings.timeout_seconds) as response:
            body = response.read(1_048_577)
        if len(body) > 1_048_576:
            raise EndpointError("The endpoint response exceeds 1 MiB.")
        payload = json.loads(body, parse_float=Decimal)
    except EndpointError:
        raise
    except HTTPError as exc:
        code = exc.code
        exc.close()
        raise EndpointError(f"Cost endpoint returned HTTP {code}; check its URL and credentials.") from None
    except (URLError, TimeoutError, OSError, HTTPException):
        raise EndpointError("Could not reach the cost endpoint; check the connection and endpoint settings.") from None
    except (ValueError, UnicodeError, RecursionError):
        raise EndpointError("The endpoint did not return valid JSON, or its request settings are invalid.") from None
    return parse_cost(payload, settings)


def endpoint_state(settings: CostEndpointSettings) -> EndpointState | None:
    if not settings.enabled:
        return None
    try:
        return EndpointState(cost=fetch_cost(settings))
    except EndpointError as exc:
        return EndpointState(error=str(exc))


class EndpointPoller:
    """Fetch in a daemon thread so slow providers never stop local watch updates."""

    def __init__(self, settings: CostEndpointSettings):
        self.settings = settings
        self._state = EndpointState()
        self._lock = Lock()
        self._next_refresh = 0.0

    def snapshot(self) -> EndpointState | None:
        if not self.settings.enabled:
            return None
        with self._lock:
            if not self._state.refreshing and time.monotonic() >= self._next_refresh:
                self._state = EndpointState(self._state.cost, self._state.error, refreshing=True)
                Thread(target=self._refresh, daemon=True).start()
            return self._state

    def _refresh(self) -> None:
        state = endpoint_state(self.settings)
        with self._lock:
            self._state = EndpointState(state.cost or self._state.cost, state.error)
            self._next_refresh = time.monotonic() + self.settings.refresh_interval_seconds
