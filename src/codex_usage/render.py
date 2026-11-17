from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from rich.console import Console, ConsoleOptions, Group, RenderableType, RenderResult
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .analytics import UsageReport
from .parser import ScanResult


def format_tokens(value: int) -> str:
    if value >= 1_000_000_000:
        return f"{value / 1_000_000_000:.2f}B"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}K"
    return f"{value:,}"


def format_money(value: Decimal) -> str:
    return f"${value:,.2f}"


def budget_style(ratio: Decimal) -> str:
    if ratio >= Decimal("0.90"):
        return "bold red"
    if ratio >= Decimal("0.70"):
        return "yellow"
    return "green"


def _progress_bar(ratio: Decimal, width: int = 28) -> Text:
    bounded = max(Decimal("0"), min(Decimal("1"), ratio))
    filled = int(bounded * width)
    style = budget_style(ratio)
    bar = Text()
    bar.append("█" * filled, style=style)
    bar.append("░" * (width - filled), style="dim")
    return bar


def budget_panel(report: UsageReport, title: str, cost: Decimal) -> Panel:
    text = Text()
    style = "#ffb87e"
    if report.budget_usd is None:
        text.append(format_money(cost), style=f"bold {style}")
    else:
        ratio = cost / report.budget_usd if report.budget_usd else Decimal("0")
        remaining = max(Decimal("0"), report.budget_usd - cost)
        style = budget_style(ratio)
        text.append(format_money(cost), style=style)
        text.append(f" / {format_money(report.budget_usd)}  ({ratio:.1%})\n")
        text.append_text(_progress_bar(ratio))
        text.append(f"\nRemaining: {format_money(remaining)}")
    if report.partial_cost:
        text.append("\nPartial estimate", style="bold yellow")
    return Panel(text, title=title, border_style=style, padding=(1, 2))


@dataclass(frozen=True)
class ScenarioPanels:
    report: UsageReport

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        panels = [
            budget_panel(self.report, "Default — 30m writes", self.report.cost_usd),
            budget_panel(self.report, "Upper bound — 12h writes", self.report.cost_upper_usd),
            budget_panel(self.report, "Mean — midpoint", self.report.cost_mean_usd),
        ]
        panel_width = max(console.measure(panel, options=options).maximum for panel in panels)
        # The grid collapses its horizontal padding to one space between panels.
        required_width = len(panels) * panel_width + len(panels) - 1
        if options.max_width < required_width:
            yield Group(*panels)
        else:
            table = Table.grid(expand=True, padding=(0, 1))
            for _ in panels:
                table.add_column(width=panel_width, ratio=1)
            table.add_row(*panels)
            yield table


def token_table(report: UsageReport) -> Table:
    table = Table(title="Token usage", expand=True, header_style="bold #ffb87e")
    table.add_column("Input", justify="right")
    table.add_column("Uncached", justify="right")
    table.add_column("Cached", justify="right")
    table.add_column("Cache writes", justify="right")
    table.add_column("Output", justify="right")
    tokens = report.tokens
    output = Text(format_tokens(tokens.output), justify="right")
    output.append("\n")
    output.append(f"{format_tokens(tokens.reasoning_output)} reasoning", style="dim italic")
    table.add_row(
        format_tokens(tokens.input),
        format_tokens(tokens.uncached_input),
        format_tokens(tokens.cached_input),
        format_tokens(tokens.cache_write_input),
        output,
    )
    return table


def model_table(report: UsageReport) -> Table:
    table = Table(title="Models", expand=True, header_style="bold magenta")
    table.add_column("Model", overflow="fold")
    table.add_column("Sessions", justify="right", no_wrap=True)
    table.add_column("Prompts", justify="right", no_wrap=True)
    table.add_column("Default", justify="right", no_wrap=True)
    table.add_column("Upper bound", justify="right", no_wrap=True)
    table.add_column("Mean", justify="right", no_wrap=True)
    for model, row in sorted(report.models.items(), key=lambda item: item[1].cost_usd, reverse=True):
        label = model
        if row.resolved_model != model:
            label += f" → {row.resolved_model}"
        costs = [
            format_money(value) if row.priced else "[yellow]unpriced[/]"
            for value in (row.cost_usd, row.cost_upper_usd, row.cost_mean_usd)
        ]
        table.add_row(
            label,
            f"{len(row.sessions):,}",
            f"{row.prompts:,}",
            *costs,
        )
    if not report.models:
        table.add_row("[dim]No usage in this period[/]", "—", "—", "—", "—", "—")
    return table


def warning_panel(
    report: UsageReport, scan: ScanResult, *, show_parser_warnings: bool = False
) -> Panel | None:
    lines: list[str] = []
    if report.unknown_models:
        lines.append("Unpriced models: " + ", ".join(sorted(report.unknown_models)))
    if show_parser_warnings and scan.issues:
        lines.append(f"{len(scan.issues)} parser/deduplication warning(s); run [bold]codex-usage doctor[/].")
    if not lines:
        return None
    return Panel("\n".join(lines), title="Warnings", border_style="yellow")


def report_renderable(
    report: UsageReport, scan: ScanResult, *, show_parser_warnings: bool = False
) -> Group:
    pieces: list[RenderableType] = [
        ScenarioPanels(report),
        token_table(report),
        model_table(report),
    ]
    if report.start is None:
        pieces.insert(0, Text("All recorded usage", style="bold", justify="center"))
    warnings = warning_panel(report, scan, show_parser_warnings=show_parser_warnings)
    if warnings:
        pieces.append(warnings)
    return Group(*pieces)


def dashboard_renderable(
    month: UsageReport, scan: ScanResult, *, show_parser_warnings: bool = False
) -> Group:
    pieces: list[RenderableType] = [
        report_renderable(month, scan, show_parser_warnings=show_parser_warnings),
    ]
    updated = month.end.astimezone(month.start.tzinfo if month.start else None)
    pieces.append(Text(f"Updated {updated.strftime('%Y-%m-%d %H:%M:%S %Z')}", style="dim"))
    return Group(*pieces)
