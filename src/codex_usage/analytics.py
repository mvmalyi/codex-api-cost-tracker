from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone, tzinfo
from decimal import Decimal
from enum import Enum
from typing import Iterable

from .config import ModelPrice, PriceBand, Settings
from .domain import PromptEvent, TokenUsage, UsageEvent
from .parser import ScanResult


MILLION = Decimal(1_000_000)


class Period(str, Enum):
    month = "month"
    all = "all"


@dataclass
class TokenTotals:
    input: int = 0
    uncached_input: int = 0
    cached_input: int = 0
    cache_write_input: int = 0
    output: int = 0
    reasoning_output: int = 0

    def add(self, usage: TokenUsage) -> None:
        self.input += usage.input
        self.uncached_input += usage.uncached_input
        self.cached_input += usage.cached_input
        self.cache_write_input += usage.cache_write_input
        self.output += usage.output
        self.reasoning_output += usage.reasoning_output


@dataclass
class ModelTotals:
    model: str
    resolved_model: str
    prompts: int = 0
    api_requests: int = 0
    sessions: set[str] = field(default_factory=set)
    tokens: TokenTotals = field(default_factory=TokenTotals)
    cost_usd: Decimal = Decimal("0")
    cost_upper_usd: Decimal = Decimal("0")
    priced: bool = True
    long_context_api_requests: int = 0

    @property
    def cost_mean_usd(self) -> Decimal:
        return (self.cost_usd + self.cost_upper_usd) / 2


@dataclass
class UsageReport:
    period: Period
    start: datetime | None
    end: datetime
    sessions: int
    prompts: int
    api_requests: int
    tokens: TokenTotals
    cost_usd: Decimal
    cost_upper_usd: Decimal
    budget_usd: Decimal | None
    models: dict[str, ModelTotals]
    unknown_models: set[str]
    parse_issue_count: int
    discovered_files: int
    duplicate_files: int
    plain_files: int = 0
    compressed_files: int = 0
    display_timezone: tzinfo = timezone.utc

    @property
    def partial_cost(self) -> bool:
        return bool(self.unknown_models)

    @property
    def cost_mean_usd(self) -> Decimal:
        return (self.cost_usd + self.cost_upper_usd) / 2


def period_start(period: Period, now: datetime, settings: Settings) -> datetime | None:
    local_now = now.astimezone(settings.timezone)
    if period == Period.month:
        return local_now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return None


def period_budget(period: Period, settings: Settings) -> Decimal | None:
    if period == Period.month:
        return settings.monthly_budget_usd
    return None


def _price_band(price: ModelPrice, usage: TokenUsage) -> tuple[PriceBand, bool]:
    is_long = bool(
        price.long is not None
        and price.long_context_threshold_tokens is not None
        and usage.input > price.long_context_threshold_tokens
    )
    return (price.long if is_long else price.short), is_long  # type: ignore[return-value]


def request_cost(
    usage: TokenUsage, price: ModelPrice, *, upper_bound: bool = False
) -> tuple[Decimal, bool]:
    """Price all recorded writes at 30 minutes (default) or 12 hours (upper)."""
    band, is_long = _price_band(price, usage)
    write_price = band.cache_write_input_12h if upper_bound else band.cache_write_input
    cost = (
        Decimal(usage.uncached_input) * band.input
        + Decimal(usage.cached_input) * band.cached_input
        + Decimal(usage.cache_write_input) * write_price
        + Decimal(usage.output) * band.output
    ) / MILLION
    return cost, is_long


def _in_window(timestamp: datetime, start: datetime | None, end: datetime) -> bool:
    return timestamp <= end and (start is None or timestamp >= start)


def _filtered_requests(
    requests: Iterable[UsageEvent], start: datetime | None, end: datetime, model: str | None
) -> list[UsageEvent]:
    return [
        item
        for item in requests
        if _in_window(item.timestamp, start, end) and (model is None or item.model == model)
    ]


def _filtered_prompts(
    prompts: Iterable[PromptEvent], start: datetime | None, end: datetime, model: str | None
) -> list[PromptEvent]:
    return [
        item
        for item in prompts
        if _in_window(item.timestamp, start, end) and (model is None or item.model == model)
    ]


def build_report(
    scan: ScanResult,
    settings: Settings,
    period: Period,
    now: datetime,
    model: str | None = None,
) -> UsageReport:
    start = period_start(period, now, settings)
    requests = _filtered_requests(scan.requests, start, now, model)
    prompts = _filtered_prompts(scan.prompts, start, now, model)
    totals = TokenTotals()
    models: dict[str, ModelTotals] = {}
    unknown: set[str] = set()
    cost = Decimal("0")
    cost_upper = Decimal("0")

    for request in requests:
        totals.add(request.tokens)
        resolved = settings.resolve_model(request.model)
        row = models.setdefault(
            request.model,
            ModelTotals(model=request.model, resolved_model=resolved),
        )
        row.api_requests += 1
        row.sessions.add(request.session_id)
        row.tokens.add(request.tokens)
        price = settings.models.get(resolved)
        if price is None:
            row.priced = False
            unknown.add(request.model)
            continue
        item_cost, is_long = request_cost(request.tokens, price)
        item_upper, _ = request_cost(request.tokens, price, upper_bound=True)
        row.cost_usd += item_cost
        row.cost_upper_usd += item_upper
        row.long_context_api_requests += int(is_long)
        cost += item_cost
        cost_upper += item_upper

    for prompt in prompts:
        resolved = settings.resolve_model(prompt.model)
        row = models.setdefault(
            prompt.model,
            ModelTotals(model=prompt.model, resolved_model=resolved),
        )
        row.prompts += 1
        row.sessions.add(prompt.session_id)

    return UsageReport(
        period=period,
        start=start,
        end=now,
        sessions=len({request.session_id for request in requests}),
        prompts=len(prompts),
        api_requests=len(requests),
        tokens=totals,
        cost_usd=cost,
        cost_upper_usd=cost_upper,
        budget_usd=period_budget(period, settings),
        models=models,
        unknown_models=unknown,
        parse_issue_count=len(scan.issues),
        discovered_files=scan.discovered_files,
        duplicate_files=scan.duplicate_files,
        plain_files=scan.plain_files,
        compressed_files=scan.compressed_files,
        display_timezone=settings.timezone,
    )
