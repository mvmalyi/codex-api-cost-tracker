from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from io import StringIO
from pathlib import Path
from dataclasses import replace
from zoneinfo import ZoneInfo

import pytest

from rich.console import Console

from codex_usage.analytics import ModelTotals, Period, TokenTotals, UsageReport, build_report
from codex_usage.cli import _report_dict
from codex_usage.domain import ParseIssue
from codex_usage.endpoint import EndpointCost, EndpointState
from codex_usage.parser import ScanResult
from codex_usage.render import budget_panel, dashboard_renderable, model_table, report_renderable, token_table


def report_fixture() -> UsageReport:
    tokens = TokenTotals(
        input=1_500_000,
        uncached_input=100_000,
        cached_input=1_300_000,
        cache_write_input=100_000,
        output=1_240_000,
        reasoning_output=373_300,
    )
    model = ModelTotals(
        model="model-a",
        resolved_model="model-a",
        prompts=3,
        api_requests=20,
        sessions={"session-1"},
        cost_usd=Decimal("1.23"),
        cost_upper_usd=Decimal("2.47"),
    )
    return UsageReport(
        period=Period.month,
        start=datetime(2026, 7, 1, tzinfo=timezone.utc),
        end=datetime(2026, 7, 29, tzinfo=timezone.utc),
        sessions=1,
        prompts=3,
        api_requests=20,
        tokens=tokens,
        cost_usd=Decimal("1.23"),
        cost_upper_usd=Decimal("2.47"),
        budget_usd=Decimal("500"),
        models={"model-a": model},
        unknown_models=set(),
        parse_issue_count=0,
        discovered_files=1,
        duplicate_files=0,
    )


def test_models_table_only_shows_user_facing_columns() -> None:
    table = model_table(report_fixture())
    assert [column.header for column in table.columns] == [
        "Model", "Sessions", "Prompts", "Estimate",
    ]
    table = model_table(report_fixture(), show_cost_range=True)
    assert [column.header for column in table.columns] == [
        "Model", "Sessions", "Prompts", "Default", "Upper bound", "Mean",
    ]


def test_reasoning_is_nested_under_output_without_footnote() -> None:
    table = token_table(report_fixture())
    assert [column.header for column in table.columns] == [
        "Input",
        "Uncached",
        "Cached",
        "Cache writes",
        "Output",
    ]
    assert table.caption is None
    console = Console(record=True, width=120)
    console.print(table)
    rendered = console.export_text()
    assert "1.24M" in rendered
    assert "373.3K reasoning" in rendered
    assert "Reasoning¹" not in rendered


@pytest.mark.parametrize("width", [80, 103, 104, 110, 125, 126, 160])
@pytest.mark.parametrize("state", ["populated", "empty", "partial"])
def test_dashboard_layout_is_readable_at_different_widths(width, state) -> None:
    report = report_fixture()
    if state == "empty":
        report.models.clear()
        report.cost_usd = report.cost_upper_usd = Decimal("0")
    elif state == "partial":
        report.unknown_models.add("mystery")
        report.models["model-a"].priced = False
    scan = ScanResult([], [ParseIssue(Path("bad.jsonl"), 1, "Invalid JSON")], 1, 0)
    stream = StringIO()
    Console(file=stream, width=width).print(dashboard_renderable(report, scan))
    text = stream.getvalue()
    assert "Estimate from logs" in text
    assert "Upper bound" not in text
    assert "Midpoint:" not in text
    assert "Codex Usage Tracker" not in text
    assert "Current calendar month" not in text
    assert "Mean is the midpoint" not in text
    assert "Activity" not in text
    assert "Trailing" not in text
    assert "parser/deduplication" not in text
    assert all(len(line) <= width for line in text.splitlines())
    assert text.count("Estimate from logs") == 1
    assert sum("░" in line or "█" in line for line in text.splitlines()) == 1
    if state == "partial":
        assert text.count("Partial estimate") == 1
        assert "Unpriced models: mystery" in text
    elif state == "empty":
        assert "No usage" in text
    else:
        assert "$1.23" in text
        assert "$2.47" not in text and "$1.85" not in text


def test_budget_panel_uses_main_cost_for_budget_and_secondary_costs_as_amounts() -> None:
    report = report_fixture()
    report.budget_usd = Decimal("2")
    stream = StringIO()
    console = Console(file=stream, width=80)
    console.print(budget_panel(report, show_cost_range=True))
    text = stream.getvalue()
    assert "$1.23 / $2.00  (61.5%)" in text
    assert "Remaining: $0.77" in text
    assert "Upper bound (12h writes): $2.47" in text and "Midpoint: $1.85" in text
    assert text.count("%") == text.count("Remaining:") == 1


def test_main_estimate_over_budget_clamps_remaining_and_progress() -> None:
    report = report_fixture()
    report.budget_usd = Decimal("1")
    stream = StringIO()
    Console(file=stream, width=80).print(budget_panel(report))
    text = stream.getvalue()
    assert "123.0%" in text
    assert "Remaining: $0.00" in text
    assert "░" not in text


def test_zero_budget_has_no_division_error() -> None:
    report = report_fixture()
    report.budget_usd = Decimal("0")
    stream = StringIO()
    Console(file=stream, width=80).print(budget_panel(report))
    assert "Remaining: $0.00" in stream.getvalue()


@pytest.mark.parametrize("width", [40, 60, 80, 103, 104, 125, 160])
def test_single_budget_panel_keeps_one_bar_and_complete_amounts_at_different_widths(width) -> None:
    stream = StringIO()
    Console(file=stream, width=width).print(budget_panel(report_fixture(), show_cost_range=True))
    lines = stream.getvalue().splitlines()
    assert sum("╭" in line for line in lines) == 1
    bar_lines = [line for line in lines if "░" in line or "█" in line]
    assert len(bar_lines) == 1
    assert bar_lines[0].count("█") + bar_lines[0].count("░") == width - 6
    assert "Upper bound (12h writes): $2.47" in stream.getvalue()
    assert "Midpoint: $1.85" in stream.getvalue()
    assert all(len(line) <= width for line in lines)


@pytest.mark.parametrize("width", [80, 125, 160])
def test_large_amounts_fit_in_the_single_panel_without_wrapping(width) -> None:
    report = report_fixture()
    report.budget_usd = Decimal("1000000000")
    report.cost_usd = Decimal("500000000")
    report.cost_upper_usd = Decimal("900000000")
    stream = StringIO()
    Console(file=stream, width=width).print(budget_panel(report, show_cost_range=True))
    text = stream.getvalue()
    lines = text.splitlines()
    assert sum("╭" in line for line in lines) == 1
    assert all(len(line) <= width for line in lines)
    assert "$500,000,000.00 / $1,000,000,000.00" in text
    assert "Upper bound (12h writes): $900,000,000.00" in text
    assert "Midpoint: $700,000,000.00" in text


def test_all_time_report_keeps_secondary_amounts_without_a_budget_bar() -> None:
    report = report_fixture()
    report.start = None
    report.budget_usd = None
    stream = StringIO()
    Console(file=stream, width=80).print(report_renderable(report, ScanResult([], [], 0, 0), show_cost_range=True))
    text = stream.getvalue()
    assert "All recorded usage" in text
    assert "Upper bound (12h writes): $2.47" in text and "Midpoint: $1.85" in text
    assert "Remaining:" not in text and "░" not in text and "█" not in text


def endpoint_fixture() -> EndpointState:
    return EndpointState(cost=EndpointCost(
        Decimal("480.0128"), Decimal("500"),
        datetime(2026, 7, 28, tzinfo=timezone.utc), datetime(2026, 7, 29, tzinfo=timezone.utc),
    ))


@pytest.mark.parametrize("width", [40, 60, 76, 80, 100, 110, 160])
def test_provider_panels_fit_and_only_stack_when_content_requires_it(width) -> None:
    stream = StringIO()
    report = report_fixture()
    Console(file=stream, width=width).print(dashboard_renderable(
        report, ScanResult([], [], 0, 0), endpoint=endpoint_fixture(),
    ))
    text = stream.getvalue()
    lines = text.splitlines()
    top_borders = [line for line in lines if "╭" in line]
    assert len(top_borders) == (1 if width >= 76 else 2)
    assert sum(line.count("╭") for line in top_borders) == 2
    assert sum(line.count("█") > 0 or line.count("░") > 0 for line in lines) == (1 if width >= 76 else 2)
    assert all(len(line) <= width for line in lines)
    assert "$1.23 / $500.00  (0.2%)" in text
    assert "$480.01 / $500.00  (96.0%)" in text
    assert "As of" not in text
    assert "Remaining: $19.99" in text
    assert "Upper bound" not in text and "Midpoint" not in text


def test_large_amounts_stack_based_on_content_width() -> None:
    report = report_fixture()
    report.cost_usd = Decimal("500000000")
    report.budget_usd = Decimal("1000000000")
    stream = StringIO()
    Console(file=stream, width=80).print(dashboard_renderable(
        report, ScanResult([], [], 0, 0), endpoint=endpoint_fixture(),
    ))
    text = stream.getvalue()
    assert len([line for line in text.splitlines() if "╭" in line]) == 2
    assert "$500,000,000.00 / $1,000,000,000.00  (50.0%)" in text


@pytest.mark.parametrize("cached", [False, True])
def test_endpoint_failure_keeps_local_estimate_visible(cached) -> None:
    state = EndpointState(cost=endpoint_fixture().cost if cached else None, error="Could not reach endpoint.")
    stream = StringIO()
    Console(file=stream, width=110).print(dashboard_renderable(
        report_fixture(), ScanResult([], [], 0, 0), endpoint=state,
    ))
    text = stream.getvalue()
    assert "Estimate from logs" in text and "$1.23" in text
    assert "Could not reach endpoint." in text
    assert ("Last successful response" in text) is cached
    assert ("$480.01" in text) is cached


def test_provider_limit_is_independent_and_missing_limit_uses_local_budget() -> None:
    from dataclasses import replace
    report = report_fixture()
    report.budget_usd = Decimal("900")
    for limit, expected in [(Decimal("600"), "$480.01 / $600.00  (80.0%)"),
                            (None, "$480.01 / $900.00  (53.3%)"),
                            (Decimal("0"), "$480.01 / $0.00  (0.0%)")]:
        stream = StringIO()
        state = EndpointState(cost=replace(endpoint_fixture().cost, limit_usd=limit))
        Console(file=stream, width=110).print(dashboard_renderable(report, ScanResult([], [], 0, 0), endpoint=state))
        assert expected in stream.getvalue()


@pytest.mark.parametrize("zone,as_of,now,expected_updated", [
    ("Europe/London", "2026-11-22T00:00:00Z", "2026-11-22T12:00:00Z",
     "2026-11-22 12:00:00 GMT"),
    ("Europe/London", "2026-07-28T00:00:00Z", "2026-07-29T12:00:00Z",
     "2026-07-29 13:00:00 BST"),
    ("Europe/London", "2026-03-29T00:30:00Z", "2026-03-29T01:30:00Z",
     "2026-03-29 02:30:00 BST"),
    ("America/New_York", "2026-11-22T00:00:00Z", "2026-11-22T12:00:00Z",
     "2026-11-22 07:00:00 EST"),
    ("Asia/Kolkata", "2026-11-22T00:00:00Z", "2026-11-22T12:00:00Z",
     "2026-11-22 17:30:00 IST"),
])
def test_provider_timestamp_is_hidden_and_local_updated_preserves_json_offsets(
    settings_factory, zone, as_of, now, expected_updated,
) -> None:
    settings, _ = settings_factory()
    settings = replace(settings, timezone=ZoneInfo(zone))
    as_of = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
    now = datetime.fromisoformat(now.replace("Z", "+00:00"))
    scan = ScanResult([], [], 0, 0)
    endpoint = EndpointState(cost=replace(endpoint_fixture().cost, as_of=as_of))
    for period in (Period.month, Period.all):
        report = build_report(scan, settings, period, now)
        stream = StringIO()
        Console(file=stream, width=110).print(dashboard_renderable(report, scan, endpoint=endpoint))
        text = stream.getvalue()
        assert "As of" not in text
        assert f"Updated {expected_updated}" in text
        payload = _report_dict(report, endpoint)
        assert payload["end"] == now.isoformat()
        assert payload["cost_endpoint"]["data"]["as_of"] == as_of.isoformat()
