from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from rich.columns import Columns
from rich.console import Console, ConsoleOptions, Group, RenderableType, RenderResult
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .analytics import UsageReport
from .endpoint import EndpointState
from .parser import ScanResult


# Mid-tone accents stay legible on both light and dark terminal backgrounds.
ACCENT_PURPLE = "#9463c9"
ACCENT_ORANGE = "#b16e40"
BUDGET_RED = "#c65d65"
BUDGET_AMBER = "#9b7a32"
BUDGET_GREEN = "#438563"


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
        return BUDGET_RED
    if ratio >= Decimal("0.70"):
        return BUDGET_AMBER
    return BUDGET_GREEN


def _progress_bar(ratio: Decimal, width: int = 28) -> Text:
    bounded = max(Decimal("0"), min(Decimal("1"), ratio))
    filled = int(bounded * width)
    style = budget_style(ratio)
    bar = Text()
    bar.append("█" * filled, style=style)
    bar.append("░" * (width - filled), style="dim")
    return bar


@dataclass(frozen=True)
class _BudgetProgressBar:
    ratio: Decimal

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        yield _progress_bar(self.ratio, width=options.max_width)


def budget_panel(report: UsageReport, *, show_cost_range: bool = False) -> Panel:
    text = Text()
    pieces: list[RenderableType] = [text]
    style = ACCENT_ORANGE
    if report.budget_usd is None:
        text.append(format_money(report.cost_usd), style=f"bold {style}")
    else:
        ratio = report.cost_usd / report.budget_usd if report.budget_usd else Decimal("0")
        remaining = max(Decimal("0"), report.budget_usd - report.cost_usd)
        style = budget_style(ratio)
        text.append(format_money(report.cost_usd), style=f"bold {style}")
        text.append(f" / {format_money(report.budget_usd)}  ({ratio:.1%})")
        pieces.extend([
            _BudgetProgressBar(ratio),
            Text(f"Remaining: {format_money(remaining)}"),
        ])
    if show_cost_range:
        pieces.append(Columns([
            Text.assemble(("Upper bound (12h writes): ", "dim"), format_money(report.cost_upper_usd)),
            Text.assemble(("Midpoint: ", "dim"), format_money(report.cost_mean_usd)),
        ], padding=(0, 3)))
    if report.partial_cost:
        pieces.append(Text("Partial estimate", style="bold yellow"))
    return Panel(
        Group(*pieces), title="Main estimate — 30m writes", border_style=style, padding=(1, 2)
    )


def endpoint_panel(state: EndpointState, report: UsageReport) -> Panel:
    pieces: list[RenderableType] = []
    style = ACCENT_PURPLE
    cost = state.cost
    subtitle = None
    if cost is not None:
        text = Text.assemble((format_money(cost.usage_usd), f"bold {style}"))
        pieces.append(text)
        limit = cost.limit_usd if cost.limit_usd is not None else report.budget_usd
        if limit is not None:
            ratio = cost.usage_usd / limit if limit else Decimal("0")
            text.append(f" / {format_money(limit)}  ({ratio:.1%})", style="default")
            pieces.extend([
                _BudgetProgressBar(ratio),
                Text(f"Remaining: {format_money(max(Decimal('0'), limit - cost.usage_usd))}"),
            ])
        as_of = cost.as_of.astimezone(report.display_timezone) if cost.as_of else None
        stamp = as_of.strftime('%Y-%m-%d %H:%M %Z') if as_of else "not supplied"
        subtitle = Text(f"As of: {stamp}", style="default")
    elif state.refreshing:
        pieces.append(Text("Fetching reported usage…", style="dim"))
    if state.error:
        if cost:
            pieces.append(Text("Last successful response; refresh failed.", style="yellow"))
        pieces.append(Text(state.error, style="yellow"))
    return Panel(Group(*pieces), title="Provider reported usage", subtitle=subtitle,
                 border_style=style, padding=(1, 2))


@dataclass(frozen=True)
class _BudgetPanels:
    report: UsageReport
    endpoint: EndpointState
    show_cost_range: bool

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        local = budget_panel(self.report, show_cost_range=self.show_cost_range)
        remote = endpoint_panel(self.endpoint, self.report)
        # Keep the amount, limit and percentage intact in each half. The timestamp
        # also needs room; narrower terminals stack the panels without clipping.
        def amount_width(usage: Decimal, limit: Decimal | None) -> int:
            label = format_money(usage)
            if limit is not None:
                ratio = usage / limit if limit else Decimal("0")
                label += f" / {format_money(limit)}  ({ratio:.1%})"
            return len(label) + 6

        local_width = max(len("Main estimate — 30m writes") + 4,
                          amount_width(self.report.cost_usd, self.report.budget_usd))
        remote_width = len("Provider reported usage") + 4
        if self.endpoint.cost:
            cost = self.endpoint.cost
            limit = cost.limit_usd if cost.limit_usd is not None else self.report.budget_usd
            remote_width = max(remote_width, amount_width(cost.usage_usd, limit), remote.subtitle.cell_len + 4)
        if options.max_width < local_width + remote_width + 2:
            yield Group(local, remote)
        else:
            available = options.max_width - 2
            left_width = min(max(available // 2, local_width), available - remote_width)
            grid = Table.grid(expand=True, padding=(0, 2))
            grid.add_column(width=left_width)
            grid.add_column(width=available - left_width)
            grid.add_row(local, remote)
            yield grid


def token_table(report: UsageReport) -> Table:
    table = Table(title="Token usage", expand=True, header_style=f"bold {ACCENT_ORANGE}")
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


def model_table(report: UsageReport, *, show_cost_range: bool = False) -> Table:
    table = Table(title="Models", expand=True, header_style=f"bold {ACCENT_PURPLE}")
    table.add_column("Model", overflow="fold")
    table.add_column("Sessions", justify="right", no_wrap=True)
    table.add_column("Prompts", justify="right", no_wrap=True)
    table.add_column("Default" if show_cost_range else "Estimate", justify="right", no_wrap=True)
    if show_cost_range:
        table.add_column("Upper bound", justify="right", no_wrap=True)
        table.add_column("Mean", justify="right", no_wrap=True)
    for model, row in sorted(report.models.items(), key=lambda item: item[1].cost_usd, reverse=True):
        label = model
        if row.resolved_model != model:
            label += f" → {row.resolved_model}"
        costs = [
            format_money(value) if row.priced else "[yellow]unpriced[/]"
            for value in ((row.cost_usd, row.cost_upper_usd, row.cost_mean_usd)
                          if show_cost_range else (row.cost_usd,))
        ]
        table.add_row(
            label,
            f"{len(row.sessions):,}",
            f"{row.prompts:,}",
            *costs,
        )
    if not report.models:
        table.add_row("[dim]No usage in this period[/]", *(["—"] * (5 if show_cost_range else 3)))
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
    report: UsageReport, scan: ScanResult, *, show_parser_warnings: bool = False,
    show_cost_range: bool = False, endpoint: EndpointState | None = None,
) -> Group:
    pieces: list[RenderableType] = [
        (_BudgetPanels(report, endpoint, show_cost_range) if endpoint is not None
         else budget_panel(report, show_cost_range=show_cost_range)),
        token_table(report),
        model_table(report, show_cost_range=show_cost_range),
    ]
    if report.start is None:
        pieces.insert(0, Text("All recorded usage", style="bold", justify="center"))
    warnings = warning_panel(report, scan, show_parser_warnings=show_parser_warnings)
    if warnings:
        pieces.append(warnings)
    return Group(*pieces)


def dashboard_renderable(
    month: UsageReport, scan: ScanResult, *, show_parser_warnings: bool = False,
    show_cost_range: bool = False, endpoint: EndpointState | None = None,
) -> Group:
    pieces: list[RenderableType] = [
        report_renderable(month, scan, show_parser_warnings=show_parser_warnings,
                          show_cost_range=show_cost_range, endpoint=endpoint),
    ]
    updated = month.end.astimezone(month.display_timezone)
    pieces.append(Text(f"Updated {updated.strftime('%Y-%m-%d %H:%M:%S %Z')}", style="dim"))
    return Group(*pieces)
