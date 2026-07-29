from __future__ import annotations

from decimal import Decimal

from rich.columns import Columns
from rich.console import Group, RenderableType
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


def budget_panel(report: UsageReport, title: str) -> Panel:
    if report.budget_usd is None:
        return Panel(format_money(report.cost_usd), title=title, border_style="#ffb87e")
    ratio = report.cost_usd / report.budget_usd if report.budget_usd else Decimal("0")
    remaining = max(Decimal("0"), report.budget_usd - report.cost_usd)
    text = Text()
    qualifier = "at least " if report.partial_cost else ""
    text.append(f"{qualifier}{format_money(report.cost_usd)}", style=budget_style(ratio))
    text.append(f" / {format_money(report.budget_usd)}  ({ratio:.1%})\n")
    text.append_text(_progress_bar(ratio))
    text.append(f"\nRemaining: {format_money(remaining)}")
    if report.partial_cost:
        text.append("  • partial estimate", style="bold yellow")
    return Panel(text, title=title, border_style=budget_style(ratio), padding=(1, 2))


def metrics_panel(report: UsageReport) -> Panel:
    table = Table.grid(expand=True)
    table.add_column(justify="center")
    table.add_column(justify="center")
    table.add_row(
        f"[bold]{report.sessions:,}[/]\n[dim]sessions[/]",
        f"[bold]{report.prompts:,}[/]\n[dim]prompts[/]",
    )
    return Panel(table, title="Activity", border_style="blue")


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
    table.add_column("Model")
    table.add_column("Sessions", justify="right")
    table.add_column("Prompts", justify="right")
    table.add_column("Cost", justify="right")
    for model, row in sorted(report.models.items(), key=lambda item: item[1].cost_usd, reverse=True):
        label = model
        if row.resolved_model != model:
            label += f" → {row.resolved_model}"
        cost = format_money(row.cost_usd) if row.priced else "[yellow]unpriced[/]"
        table.add_row(
            label,
            f"{len(row.sessions):,}",
            f"{row.prompts:,}",
            cost,
        )
    if not report.models:
        table.add_row("[dim]No usage in this period[/]", "—", "—", "—")
    return table


def warning_panel(report: UsageReport, scan: ScanResult) -> Panel | None:
    lines: list[str] = []
    if report.unknown_models:
        lines.append("Unpriced models: " + ", ".join(sorted(report.unknown_models)))
    if scan.issues:
        lines.append(f"{len(scan.issues)} parser/deduplication warning(s); run [bold]codex-usage doctor[/].")
    if not lines:
        return None
    return Panel("\n".join(lines), title="Warnings", border_style="yellow")


def report_renderable(report: UsageReport, scan: ScanResult) -> Group:
    labels = {
        "month": "Current calendar month",
        "30d": "Trailing 30 days",
        "all": "All recorded usage",
    }
    pieces: list[RenderableType] = [
        budget_panel(report, labels[report.period.value]),
        metrics_panel(report),
        token_table(report),
        model_table(report),
    ]
    warnings = warning_panel(report, scan)
    if warnings:
        pieces.append(warnings)
    return Group(*pieces)


def dashboard_renderable(month: UsageReport, rolling: UsageReport, scan: ScanResult) -> Group:
    pieces: list[RenderableType] = [
        Text("Codex Usage Tracker", style="bold", justify="center"),
        Columns(
            [
                budget_panel(month, "Current calendar month"),
                budget_panel(rolling, "Trailing 30 days"),
            ],
            equal=True,
            expand=True,
        ),
        metrics_panel(month),
        token_table(month),
        model_table(month),
    ]
    warnings = warning_panel(month, scan)
    if warnings:
        pieces.append(warnings)
    pieces.append(Text(f"Updated {month.end.astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')}", style="dim"))
    return Group(*pieces)
