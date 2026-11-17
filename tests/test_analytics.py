from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from codex_usage.analytics import Period, build_report, period_start, request_cost
from codex_usage.domain import ParsedSession, PromptEvent, TokenUsage, UsageEvent
from codex_usage.parser import ScanResult


def scan_with(*requests: UsageEvent, prompts: tuple[PromptEvent, ...] = ()) -> ScanResult:
    session = ParsedSession(path=Path("fixture.jsonl"), session_id="session-1")
    session.requests.extend(requests)
    session.prompts.extend(prompts)
    return ScanResult([session], [], 1, 0)


def usage_event(timestamp: datetime, model: str = "model-a", input_tokens: int = 100) -> UsageEvent:
    return UsageEvent(
        timestamp,
        "session-1",
        model,
        TokenUsage(input_tokens, 60, 30, 20, 5),
    )


def test_cost_categories_are_exclusive_and_reasoning_is_not_added(settings_factory) -> None:
    settings, _ = settings_factory()
    usage = TokenUsage(input=100, cached_input=60, cache_write_input=30, output=20, reasoning_output=5)
    cost, is_long = request_cost(usage, settings.models["model-a"])

    expected = Decimal(10 * 10 + 60 * 2 + 30 * 20 + 20 * 40) / Decimal(1_000_000)
    assert cost == expected
    assert cost == Decimal("0.00162")
    assert not is_long


def test_long_context_rate_uses_per_request_input(settings_factory) -> None:
    settings, _ = settings_factory()
    usage = TokenUsage(input=101, cached_input=60, cache_write_input=30, output=20, reasoning_output=5)
    cost, is_long = request_cost(usage, settings.models["model-a"])
    expected = Decimal(11 * 20 + 60 * 4 + 30 * 40 + 20 * 80) / Decimal(1_000_000)
    assert cost == expected
    assert is_long


def test_month_boundary_uses_london_timezone_and_handles_dst(settings_factory) -> None:
    settings, _ = settings_factory()
    now = datetime(2026, 7, 15, 12, tzinfo=timezone.utc)
    start = period_start(Period.month, now, settings)
    assert start is not None
    assert start.isoformat() == "2026-07-01T00:00:00+01:00"

    included = usage_event(datetime(2026, 6, 30, 23, 0, tzinfo=timezone.utc))
    excluded = usage_event(datetime(2026, 6, 30, 22, 59, 59, tzinfo=timezone.utc))
    report = build_report(scan_with(included, excluded), settings, Period.month, now)
    assert report.api_requests == 1


def test_all_period_includes_old_usage_but_excludes_future_usage(settings_factory) -> None:
    settings, _ = settings_factory()
    now = datetime(2026, 7, 29, 12, tzinfo=timezone.utc)
    old = usage_event(datetime(2025, 6, 29, 12, tzinfo=timezone.utc))
    future = usage_event(datetime(2026, 7, 30, 12, tzinfo=timezone.utc))
    report = build_report(scan_with(old, future), settings, Period.all, now)
    assert report.api_requests == 1
    assert report.start is None
    assert report.budget_usd is None


def test_unknown_model_is_counted_but_cost_is_partial(settings_factory) -> None:
    settings, _ = settings_factory()
    now = datetime(2026, 7, 29, 12, tzinfo=timezone.utc)
    event = usage_event(now, model="mystery")
    report = build_report(scan_with(event), settings, Period.all, now)
    assert report.api_requests == 1
    assert report.tokens.input == 100
    assert report.cost_usd == 0
    assert report.cost_upper_usd == report.cost_mean_usd == 0
    assert report.partial_cost
    assert report.unknown_models == {"mystery"}


def test_alias_resolves_pricing(settings_factory) -> None:
    settings, _ = settings_factory(aliases={"internal": "model-a"})
    now = datetime(2026, 7, 29, 12, tzinfo=timezone.utc)
    report = build_report(scan_with(usage_event(now, model="internal")), settings, Period.all, now)
    assert not report.partial_cost
    assert report.models["internal"].resolved_model == "model-a"
    assert report.cost_usd == Decimal("0.00162")
    assert report.cost_upper_usd == Decimal("0.00222")
    assert report.cost_mean_usd == Decimal("0.00192")


def test_model_filter_applies_to_requests_and_prompts(settings_factory) -> None:
    settings, _ = settings_factory()
    now = datetime(2026, 7, 29, 12, tzinfo=timezone.utc)
    prompts = (PromptEvent(now, "session-1", "model-b"), PromptEvent(now, "session-1", "model-a"))
    report = build_report(
        scan_with(usage_event(now, "model-a"), usage_event(now, "model-b"), prompts=prompts),
        settings,
        Period.all,
        now,
        model="model-b",
    )
    assert report.api_requests == 1
    assert report.prompts == 1
    assert set(report.models) == {"model-b"}


def test_prompts_are_aggregated_by_model_independently_of_api_requests(settings_factory) -> None:
    settings, _ = settings_factory()
    now = datetime(2026, 7, 29, 12, tzinfo=timezone.utc)
    prompts = (
        PromptEvent(now, "session-1", "model-a"),
        PromptEvent(now, "session-1", "model-a"),
        PromptEvent(now, "session-1", "model-b"),
    )
    report = build_report(
        scan_with(usage_event(now, "model-a"), usage_event(now, "model-a"), prompts=prompts),
        settings,
        Period.all,
        now,
    )

    assert report.prompts == 3
    assert report.api_requests == 2
    assert report.models["model-a"].prompts == 2
    assert report.models["model-b"].prompts == 1
    assert sum(row.prompts for row in report.models.values()) == report.prompts


def test_upper_changes_only_write_price_and_midpoint_is_unrounded(settings_factory) -> None:
    settings, _ = settings_factory()
    now = datetime(2026, 7, 29, 12, tzinfo=timezone.utc)
    report = build_report(scan_with(usage_event(now)), settings, Period.month, now)
    assert report.cost_usd == Decimal("0.00162")
    assert report.cost_upper_usd == Decimal("0.00222")
    assert report.cost_mean_usd == Decimal("0.00192")
    assert report.cost_upper_usd - report.cost_usd == Decimal(30 * (40 - 20)) / 1_000_000


def test_scenarios_use_each_requests_context_band_and_sum_by_model(settings_factory) -> None:
    settings, _ = settings_factory()
    now = datetime(2026, 7, 29, 12, tzinfo=timezone.utc)
    events = [usage_event(now), usage_event(now, input_tokens=101), usage_event(now, "model-b")]
    report = build_report(scan_with(*events), settings, Period.all, now)
    row = report.models["model-a"]
    assert row.long_context_api_requests == 1
    assert row.cost_usd == Decimal("0.00162") + Decimal("0.00326")
    assert row.cost_upper_usd == Decimal("0.00222") + Decimal("0.00446")
    for field in ("cost_usd", "cost_upper_usd", "cost_mean_usd"):
        assert getattr(report, field) == sum(getattr(model, field) for model in report.models.values())


def test_zero_writes_gives_identical_scenarios(settings_factory) -> None:
    settings, _ = settings_factory()
    now = datetime(2026, 7, 29, 12, tzinfo=timezone.utc)
    event = UsageEvent(now, "session-1", "model-a", TokenUsage(100, 60, 0, 20, 5))
    report = build_report(scan_with(event), settings, Period.month, now)
    assert report.cost_usd == report.cost_upper_usd == report.cost_mean_usd == Decimal("0.00132")


def test_empty_report_has_zero_scenarios(settings_factory) -> None:
    settings, _ = settings_factory()
    now = datetime(2026, 7, 29, 12, tzinfo=timezone.utc)
    report = build_report(scan_with(), settings, Period.month, now)
    assert report.cost_usd == report.cost_upper_usd == report.cost_mean_usd == 0


def test_model_filter_applies_to_all_cost_scenarios(settings_factory) -> None:
    settings, _ = settings_factory()
    now = datetime(2026, 7, 29, 12, tzinfo=timezone.utc)
    report = build_report(
        scan_with(usage_event(now), usage_event(now, "model-b")),
        settings, Period.all, now, model="model-a",
    )
    assert report.cost_usd == Decimal("0.00162")
    assert report.cost_upper_usd == Decimal("0.00222")


def test_partial_scenarios_keep_priced_activity(settings_factory) -> None:
    settings, _ = settings_factory()
    now = datetime(2026, 7, 29, 12, tzinfo=timezone.utc)
    report = build_report(scan_with(usage_event(now), usage_event(now, "mystery")), settings, Period.all, now)
    assert report.partial_cost
    assert report.cost_usd == Decimal("0.00162")
    assert report.cost_upper_usd == Decimal("0.00222")
    assert report.cost_mean_usd == Decimal("0.00192")
