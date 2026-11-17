from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from io import StringIO
from pathlib import Path

import pytest

from rich.console import Console

from codex_usage.analytics import ModelTotals, Period, TokenTotals, UsageReport
from codex_usage.domain import ParseIssue
from codex_usage.parser import ScanResult
from codex_usage.render import ScenarioPanels, budget_panel, dashboard_renderable, model_table, token_table


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
    assert "Default — 30m writes" in text
    assert "Upper bound — 12h writes" in text
    assert "Mean — midpoint" in text
    assert "Codex Usage Tracker" not in text
    assert "Current calendar month" not in text
    assert "Mean is the midpoint" not in text
    assert "Activity" not in text
    assert "Trailing" not in text
    assert "parser/deduplication" not in text
    assert all(len(line) <= width for line in text.splitlines())
    title_lines = [
        i for i, line in enumerate(text.splitlines())
        if any(title in line for title in ("Default —", "Upper bound —", "Mean —"))
    ]
    assert len(title_lines) == (3 if width < 104 else 1)
    if state == "partial":
        assert text.count("Partial estimate") == 3
        assert "Unpriced models: mystery" in text
    elif state == "empty":
        assert "No usage" in text
    else:
        assert "$1.23" in text and "$2.47" in text and "$1.85" in text


def test_each_budget_panel_uses_its_own_cost_and_clamps_remaining() -> None:
    report = report_fixture()
    report.budget_usd = Decimal("2")
    stream = StringIO()
    console = Console(file=stream, width=80)
    console.print(budget_panel(report, "Default", report.cost_usd))
    console.print(budget_panel(report, "Upper", report.cost_upper_usd))
    console.print(budget_panel(report, "Mean", report.cost_mean_usd))
    text = stream.getvalue()
    assert "61.5%" in text and "123.5%" in text and "92.5%" in text
    assert "Remaining: $0.77" in text and "Remaining: $0.00" in text and "Remaining: $0.15" in text


def test_zero_budget_has_no_division_error() -> None:
    report = report_fixture()
    report.budget_usd = Decimal("0")
    stream = StringIO()
    Console(file=stream, width=80).print(budget_panel(report, "Default", report.cost_usd))
    assert "Remaining: $0.00" in stream.getvalue()


@pytest.mark.parametrize("width,title_rows,panel_rows", [(103, 3, 21), (104, 1, 7), (125, 1, 7)])
def test_scenario_panels_only_stack_when_contents_no_longer_fit(width, title_rows, panel_rows) -> None:
    stream = StringIO()
    Console(file=stream, width=width).print(ScenarioPanels(report_fixture()))
    lines = stream.getvalue().splitlines()
    assert sum("╭" in line for line in lines) == title_rows
    assert len(lines) == panel_rows
    assert all(len(line) <= width for line in lines)


@pytest.mark.parametrize("width,title_rows,panel_rows", [(125, 3, 21), (160, 1, 7)])
def test_large_amounts_require_more_room_without_wrapping(width, title_rows, panel_rows) -> None:
    report = report_fixture()
    report.budget_usd = Decimal("1000000000")
    report.cost_usd = Decimal("500000000")
    report.cost_upper_usd = Decimal("900000000")
    stream = StringIO()
    Console(file=stream, width=width).print(ScenarioPanels(report))
    text = stream.getvalue()
    lines = text.splitlines()
    assert sum("╭" in line for line in lines) == title_rows
    assert len(lines) == panel_rows
    assert all(len(line) <= width for line in lines)
    for cost in ("$500,000,000.00", "$900,000,000.00", "$700,000,000.00"):
        assert f"{cost} / $1,000,000,000.00" in text
