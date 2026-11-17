from __future__ import annotations

import json
from decimal import Decimal
from io import StringIO

from rich.console import Console
from typer.testing import CliRunner

from codex_usage.cli import app
from codex_usage.parser import ZstdFile


runner = CliRunner()


def test_default_dashboard_and_doctor(settings_factory) -> None:
    _, config = settings_factory()
    result = runner.invoke(app, ["--config", str(config)])
    assert result.exit_code == 0, result.output
    assert "Codex Usage Tracker" not in result.output
    assert "Current calendar month" not in result.output
    assert "Mean is the midpoint" not in result.output
    assert "Upper bound" in result.output
    assert "Mean" in result.output
    assert "Activity" not in result.output
    assert "Trailing" not in result.output
    assert "Prompts" in result.output
    assert "API requests" not in result.output
    assert "user turns" not in result.output
    assert "Reasoning¹" not in result.output
    assert "reasoning" in result.output

    doctor = runner.invoke(app, ["--config", str(config), "doctor"])
    assert doctor.exit_code == 0, doctor.output
    assert "Codex usage tracker diagnostics" in doctor.output


def test_report_json_is_machine_readable(settings_factory) -> None:
    _, config = settings_factory()
    result = runner.invoke(app, ["--config", str(config), "report", "--period", "all", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["period"] == "all"
    assert payload["prompts"] == 0
    assert payload["diagnostics"]["api_requests"] == 0
    assert "requests" not in payload
    assert "user_turns" not in payload
    assert all("requests" not in model for model in payload["models"].values())
    assert payload["partial_cost"] is False
    assert payload["cost_usd"] == payload["cost_upper_usd"] == payload["cost_mean_usd"] == "0"


def test_report_json_exposes_prompts_per_model_without_request_fields(settings_factory) -> None:
    settings, config = settings_factory()
    session_id = "019fae69-69fc-7d51-b371-a2e868407c3b"
    path = settings.active_sessions_path / "2026" / "07" / "29" / f"rollout-{session_id}.jsonl"
    path.parent.mkdir(parents=True)
    usage = {
        "input_tokens": 100,
        "cached_input_tokens": 60,
        "cache_write_input_tokens": 30,
        "output_tokens": 20,
        "reasoning_output_tokens": 5,
    }
    records = [
        {"timestamp": "2026-07-29T10:00:00Z", "type": "session_meta", "payload": {"id": session_id}},
        {"timestamp": "2026-07-29T10:00:01Z", "type": "turn_context", "payload": {"model": "model-a"}},
        {"timestamp": "2026-07-29T10:00:02Z", "type": "event_msg", "payload": {"type": "user_message"}},
        {
            "timestamp": "2026-07-29T10:00:03Z",
            "type": "event_msg",
            "payload": {"type": "token_count", "info": {"last_token_usage": usage}},
        },
    ]
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")

    result = runner.invoke(app, ["--config", str(config), "report", "--period", "all", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["prompts"] == 1
    assert payload["models"]["model-a"]["prompts"] == 1
    assert payload["diagnostics"]["api_requests"] == 1
    assert not any("requests" in key for key in payload["models"]["model-a"])
    for row in (payload, payload["models"]["model-a"]):
        assert Decimal(row["cost_usd"]) == Decimal("0.00162")
        assert Decimal(row["cost_upper_usd"]) == Decimal("0.00222")
        assert Decimal(row["cost_mean_usd"]) == Decimal("0.00192")


def test_json_and_doctor_show_compressed_discovery(settings_factory) -> None:
    settings, config = settings_factory()
    with ZstdFile(settings.archived_sessions_path / "empty.jsonl.zst", "wb") as handle:
        handle.write(b"{}\n")
    (settings.active_sessions_path / "empty.jsonl").write_text("{}\n")

    result = runner.invoke(app, ["--config", str(config), "report", "--period", "all", "--json"])

    assert result.exit_code == 0, result.output
    diagnostics = json.loads(result.output)["diagnostics"]
    assert diagnostics["discovered_files"] == 2
    assert diagnostics["plain_files"] == 1
    assert diagnostics["compressed_files"] == 1
    doctor = runner.invoke(app, ["--config", str(config), "doctor"])
    assert doctor.exit_code == 0, doctor.output
    assert "1 plain, 1 compressed" in doctor.output


def test_doctor_displays_compressed_read_errors(settings_factory) -> None:
    settings, config = settings_factory()
    (settings.active_sessions_path / "damaged.jsonl.zst").write_bytes(b"not Zstandard")

    result = runner.invoke(app, ["--config", str(config), "doctor"])

    assert result.exit_code == 0, result.output
    assert "WARNING" in result.output
    assert "Could not read file" in result.output


def test_dashboard_json_only_contains_month(settings_factory) -> None:
    _, config = settings_factory()
    result = runner.invoke(app, ["--config", str(config), "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert list(payload) == ["month"]
    assert {"cost_usd", "cost_upper_usd", "cost_mean_usd"} <= payload["month"].keys()


def test_removed_period_is_rejected_and_absent_from_help(settings_factory) -> None:
    _, config = settings_factory()
    result = runner.invoke(app, ["--config", str(config), "report", "--period", "30d"])
    assert result.exit_code == 2
    for command in ([], ["report"]):
        help_result = runner.invoke(app, [*command, "--help"])
        assert help_result.exit_code == 0
        assert "30d" not in help_result.output
        assert "trailing" not in help_result.output.lower()


def test_parser_warnings_are_hidden_but_retained_for_diagnostics(settings_factory) -> None:
    settings, config = settings_factory()
    (settings.active_sessions_path / "bad.jsonl").write_text('{not-json}\n')
    meta = json.dumps({"type": "session_meta", "payload": {"id": "duplicate"}}) + "\n"
    (settings.active_sessions_path / "duplicate.jsonl").write_text(meta)
    (settings.archived_sessions_path / "duplicate.jsonl").write_text(meta)
    for command in ([], ["report", "--period", "all"]):
        normal = runner.invoke(app, ["--config", str(config), *command])
        assert normal.exit_code == 0, normal.output
        assert "Warnings" not in normal.output
        verbose = runner.invoke(app, ["--config", str(config), "--show-parser-warnings", *command])
        assert verbose.exit_code == 0, verbose.output
        assert "parser/deduplication" in verbose.output
    report = runner.invoke(app, ["--config", str(config), "--json"])
    diagnostics = json.loads(report.output)["month"]["diagnostics"]
    assert diagnostics["parse_issue_count"] == 2
    assert diagnostics["duplicate_files"] == 1
    doctor = runner.invoke(app, ["--config", str(config), "doctor"])
    assert "Invalid JSON" in doctor.output
    assert "Duplicate session" in doctor.output


def test_unpriced_model_warning_and_partial_scenarios_remain_visible(settings_factory) -> None:
    settings, config = settings_factory()
    records = [
        {"type": "turn_context", "payload": {"model": "mystery"}},
        {"timestamp": "2026-07-29T10:00:03Z", "type": "event_msg", "payload": {
            "type": "token_count", "info": {"last_token_usage": {"input_tokens": 100}},
        }},
    ]
    (settings.active_sessions_path / "unpriced.jsonl").write_text(
        "\n".join(map(json.dumps, records)) + "\n"
    )
    result = runner.invoke(app, ["--config", str(config), "report", "--period", "all"])
    assert result.exit_code == 0, result.output
    assert "Unpriced models: mystery" in result.output
    assert result.output.count("Partial estimate") == 3
    assert "Remaining" not in result.output


def test_missing_12h_price_is_actionable_cli_error(settings_factory) -> None:
    _, config = settings_factory()
    config.write_text(config.read_text().replace('cache_write_input_12h = "40"\n', ''))
    result = runner.invoke(app, ["--config", str(config)])
    assert result.exit_code == 2
    assert "cache_write_input_12h" in result.output
    assert "12-hour rate" in result.output


def test_watch_renders_and_refreshes_scenarios_with_warning_preference(settings_factory, monkeypatch) -> None:
    settings, config = settings_factory()
    (settings.active_sessions_path / "bad.jsonl").write_text('{not-json}\n')
    snapshots = []

    class FakeLive:
        def __init__(self, renderable, **kwargs):
            snapshots.append(renderable)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def update(self, renderable, **kwargs):
            snapshots.append(renderable)

    def sleep_once(_):
        if len(snapshots) >= 2:
            raise KeyboardInterrupt

    monkeypatch.setattr("codex_usage.cli.Live", FakeLive)
    monkeypatch.setattr("codex_usage.cli.time.sleep", sleep_once)
    for show_warnings in [False, True]:
        snapshots.clear()
        options = ["--show-parser-warnings"] if show_warnings else []
        result = runner.invoke(app, ["--config", str(config), *options, "watch"])
        assert result.exit_code == 0, result.output
        assert len(snapshots) == 2
        for snapshot in snapshots:
            stream = StringIO()
            Console(file=stream, width=160).print(snapshot)
            rendered = stream.getvalue()
            assert "Default — 30m writes" in rendered
            assert "Upper bound — 12h writes" in rendered
            assert "Mean — midpoint" in rendered
            assert "Activity" not in rendered
            assert "Trailing" not in rendered
            assert ("parser/deduplication" in rendered) is show_warnings
