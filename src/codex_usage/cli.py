from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import time
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.live import Live
from rich.table import Table

from .analytics import Period, UsageReport, build_report
from .config import ConfigError, Settings, load_settings
from .parser import SessionScanner
from .render import dashboard_renderable, report_renderable


app = typer.Typer(
    name="codex-usage",
    help="Track local Codex token usage and estimated API costs.",
    no_args_is_help=False,
    add_completion=False,
)
console = Console()


def _settings(path: Path) -> Settings:
    try:
        return load_settings(path)
    except ConfigError as exc:
        console.print(f"[bold red]Configuration error:[/] {exc}")
        raise typer.Exit(2) from exc


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _report_dict(report: UsageReport) -> dict[str, Any]:
    return {
        "period": report.period.value,
        "start": report.start.isoformat() if report.start else None,
        "end": report.end.isoformat(),
        "sessions": report.sessions,
        "prompts": report.prompts,
        "cost_usd": str(report.cost_usd),
        "cost_upper_usd": str(report.cost_upper_usd),
        "cost_mean_usd": str(report.cost_mean_usd),
        "budget_usd": str(report.budget_usd) if report.budget_usd is not None else None,
        "partial_cost": report.partial_cost,
        "unknown_models": sorted(report.unknown_models),
        "tokens": {
            "input": report.tokens.input,
            "uncached_input": report.tokens.uncached_input,
            "cached_input": report.tokens.cached_input,
            "cache_write_input": report.tokens.cache_write_input,
            "output": report.tokens.output,
            "reasoning_output": report.tokens.reasoning_output,
        },
        "models": {
            name: {
                "resolved_model": row.resolved_model,
                "sessions": len(row.sessions),
                "prompts": row.prompts,
                "input_tokens": row.tokens.input,
                "output_tokens": row.tokens.output,
                "cost_usd": str(row.cost_usd),
                "cost_upper_usd": str(row.cost_upper_usd),
                "cost_mean_usd": str(row.cost_mean_usd),
                "priced": row.priced,
            }
            for name, row in sorted(report.models.items())
        },
        "diagnostics": {
            "api_requests": report.api_requests,
            "long_context_api_requests": sum(
                row.long_context_api_requests for row in report.models.values()
            ),
            "discovered_files": report.discovered_files,
            "plain_files": report.plain_files,
            "compressed_files": report.compressed_files,
            "duplicate_files": report.duplicate_files,
            "parse_issue_count": report.parse_issue_count,
        },
    }


def _dashboard(
    settings: Settings, json_output: bool = False, *, show_parser_warnings: bool = False
) -> None:
    scan = SessionScanner(settings).scan()
    now = _now()
    month = build_report(scan, settings, Period.month, now)
    if json_output:
        console.print_json(json.dumps({"month": _report_dict(month)}))
    else:
        console.print(dashboard_renderable(month, scan, show_parser_warnings=show_parser_warnings))


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    config: Annotated[
        Path,
        typer.Option("--config", "-c", help="Path to the editable pricing TOML file."),
    ] = Path("pricing.toml"),
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit machine-readable JSON for the default dashboard."),
    ] = False,
    show_parser_warnings: Annotated[
        bool,
        typer.Option("--show-parser-warnings", help="Show parser/deduplication warnings in normal output."),
    ] = False,
) -> None:
    """Show current-month default, upper-bound and mean cost estimates."""
    settings = _settings(config)
    ctx.obj = settings
    ctx.meta["show_parser_warnings"] = show_parser_warnings
    if ctx.invoked_subcommand is None:
        _dashboard(settings, json_output, show_parser_warnings=show_parser_warnings)


@app.command()
def report(
    ctx: typer.Context,
    period: Annotated[Period, typer.Option(help="Reporting window: month or all.")] = Period.month,
    model: Annotated[str | None, typer.Option(help="Only include one exact observed model name.")] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Emit machine-readable JSON.")] = False,
) -> None:
    """Show a report for one period, optionally filtered by model."""
    settings: Settings = ctx.obj
    scan = SessionScanner(settings).scan()
    result = build_report(scan, settings, period, _now(), model=model)
    if json_output:
        console.print_json(json.dumps(_report_dict(result)))
    else:
        console.print(
            report_renderable(result, scan, show_parser_warnings=ctx.meta["show_parser_warnings"])
        )


@app.command()
def watch(
    ctx: typer.Context,
    interval: Annotated[
        float | None,
        typer.Option(min=0.2, help="Refresh interval in seconds; defaults to pricing.toml."),
    ] = None,
) -> None:
    """Continuously refresh the local dashboard using incremental file reads."""
    settings: Settings = ctx.obj
    refresh_interval = interval or settings.watch_interval_seconds
    scanner = SessionScanner(settings)
    try:
        scan = scanner.scan()
        now = _now()
        renderable = dashboard_renderable(
            build_report(scan, settings, Period.month, now),
            scan,
            show_parser_warnings=ctx.meta["show_parser_warnings"],
        )
        with Live(renderable, console=console, refresh_per_second=4, screen=False) as live:
            while True:
                time.sleep(refresh_interval)
                scan = scanner.scan()
                now = _now()
                live.update(
                    dashboard_renderable(
                        build_report(scan, settings, Period.month, now),
                        scan,
                        show_parser_warnings=ctx.meta["show_parser_warnings"],
                    ),
                    refresh=True,
                )
    except KeyboardInterrupt:
        console.print("\n[dim]Watch stopped.[/]")


@app.command("models")
def models_command(ctx: typer.Context) -> None:
    """List observed models and whether each has a configured price."""
    settings: Settings = ctx.obj
    scan = SessionScanner(settings).scan()
    report_all = build_report(scan, settings, Period.all, _now())
    table = Table(title="Observed and configured models", header_style="bold magenta")
    table.add_column("Observed model")
    table.add_column("Pricing model")
    table.add_column("Prompts", justify="right")
    table.add_column("Status")
    observed = set(report_all.models)
    for name in sorted(observed | set(settings.models)):
        row = report_all.models.get(name)
        resolved = settings.resolve_model(name)
        priced = resolved in settings.models
        status = "[green]priced[/]" if priced else "[yellow]missing price[/]"
        if name not in observed:
            status = "[dim]configured only[/]"
        table.add_row(name, resolved, f"{row.prompts:,}" if row else "0", status)
    console.print(table)


@app.command()
def doctor(ctx: typer.Context) -> None:
    """Validate configuration, source paths and rollout parsing."""
    settings: Settings = ctx.obj
    checks = Table(title="Codex usage tracker diagnostics")
    checks.add_column("Check")
    checks.add_column("Status")
    checks.add_column("Details")
    checks.add_row("Configuration", "[green]OK[/]", str(settings.config_path))
    missing_paths = 0
    for path in settings.source_paths:
        exists = path.is_dir()
        missing_paths += int(not exists)
        checks.add_row(
            "Session source",
            "[green]OK[/]" if exists else "[red]MISSING[/]",
            str(path),
        )
    scan = SessionScanner(settings).scan()
    checks.add_row(
        "Rollout discovery",
        "[green]OK[/]",
        f"{scan.discovered_files} file(s): {scan.plain_files} plain, {scan.compressed_files} compressed",
    )
    if scan.duplicate_files:
        checks.add_row("Deduplication", "[yellow]WARNING[/]", f"{scan.duplicate_files} duplicate(s)")
    else:
        checks.add_row("Deduplication", "[green]OK[/]", "No duplicate session IDs")
    if scan.issues:
        checks.add_row("Parsing", "[yellow]WARNING[/]", f"{len(scan.issues)} issue(s)")
    else:
        checks.add_row("Parsing", "[green]OK[/]", "No malformed records")
    report_all = build_report(scan, settings, Period.all, _now())
    if report_all.unknown_models:
        checks.add_row(
            "Model pricing",
            "[yellow]PARTIAL[/]",
            ", ".join(sorted(report_all.unknown_models)),
        )
    else:
        checks.add_row("Model pricing", "[green]OK[/]", "Every observed model is priced")
    console.print(checks)
    if scan.issues:
        issue_table = Table(title="Parser and deduplication warnings", show_lines=False)
        issue_table.add_column("File")
        issue_table.add_column("Line", justify="right")
        issue_table.add_column("Message")
        for issue in scan.issues[:20]:
            issue_table.add_row(str(issue.path), str(issue.line_number or "—"), issue.message)
        console.print(issue_table)
        if len(scan.issues) > 20:
            console.print(f"[dim]Showing 20 of {len(scan.issues)} warnings.[/]")
    if missing_paths == len(settings.source_paths):
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
