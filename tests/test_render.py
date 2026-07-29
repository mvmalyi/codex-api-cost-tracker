from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from rich.console import Console

from codex_usage.analytics import ModelTotals, Period, TokenTotals, UsageReport
from codex_usage.render import model_table, token_table


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
    )
    return UsageReport(
        period=Period.month,
        start=None,
        end=datetime(2026, 7, 29, tzinfo=timezone.utc),
        sessions=1,
        prompts=3,
        api_requests=20,
        tokens=tokens,
        cost_usd=Decimal("1.23"),
        budget_usd=Decimal("500"),
        models={"model-a": model},
        unknown_models=set(),
        parse_issue_count=0,
        discovered_files=1,
        duplicate_files=0,
    )


def test_models_table_only_shows_user_facing_columns() -> None:
    table = model_table(report_fixture())
    assert [column.header for column in table.columns] == ["Model", "Sessions", "Prompts", "Cost"]


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
