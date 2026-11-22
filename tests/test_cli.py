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
    assert "Upper bound" not in result.output
    assert "Midpoint" not in result.output
    assert "Mean" not in result.output
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
    assert result.output.count("Partial estimate") == 1
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
            assert "Main estimate — 30m writes" in rendered
            assert "Upper bound" not in rendered
            assert "Midpoint:" not in rendered
            assert "Activity" not in rendered
            assert "Trailing" not in rendered
            assert ("parser/deduplication" in rendered) is show_warnings


def test_cost_range_is_opt_in_and_can_override_config(settings_factory) -> None:
    _, config = settings_factory()
    result = runner.invoke(app, ["--config", str(config), "--show-cost-range"])
    assert result.exit_code == 0, result.output
    assert "Upper bound (12h writes):" in result.output and "Midpoint:" in result.output
    config.write_text(config.read_text().replace('[app]', '[app]\nshow_cost_range = true'))
    assert "Midpoint:" in runner.invoke(app, ["--config", str(config)]).output
    hidden = runner.invoke(app, ["--config", str(config), "--hide-cost-range"])
    assert hidden.exit_code == 0 and "Midpoint:" not in hidden.output


def test_endpoint_json_and_dashboard_integration(settings_factory, monkeypatch) -> None:
    from codex_usage.config import load_settings
    from codex_usage.endpoint import EndpointState, parse_cost
    _, config = settings_factory()
    config.write_text(config.read_text() + '''
[cost_endpoint]
enabled = true
url = "https://example.com/costs"
api_key_env = "TEST_COST_KEY"
currency_override = "USD"
''')
    cost = parse_cost({"usage": "480.0128", "limit": "500", "currency": "GBP",
                       "asOf": "2026-11-22T00:00:00Z"}, load_settings(config).cost_endpoint)
    calls = []

    def fetch(settings):
        calls.append(settings)
        return EndpointState(cost=cost)

    monkeypatch.setattr("codex_usage.cli.endpoint_state", fetch)
    for args in (["--json"], ["report", "--json"]):
        result = runner.invoke(app, ["--config", str(config), *args])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        row = payload.get("month", payload)
        assert row["cost_endpoint"]["data"]["usage_usd"] == "480.0128"
        assert row["cost_endpoint"]["data"]["currency"] == "USD"
        assert row["cost_usd"] == "0"
    normal = runner.invoke(app, ["--config", str(config)])
    assert normal.exit_code == 0, normal.output
    assert "Provider reported usage" in normal.output and "$480.01" in normal.output
    assert "As of:" in normal.output
    count = len(calls)
    for args in (["report", "--period", "all", "--json"], ["report", "--model", "model-a", "--json"]):
        result = runner.invoke(app, ["--config", str(config), *args])
        assert result.exit_code == 0, result.output
        assert "cost_endpoint" not in json.loads(result.output)
    assert len(calls) == count


def test_watch_refreshes_endpoint_and_local_costs_independently(settings_factory, monkeypatch) -> None:
    from codex_usage.endpoint import EndpointState
    _, config = settings_factory()
    snapshots = []
    polls = []

    class Poller:
        def __init__(self, settings):
            pass

        def snapshot(self):
            polls.append(True)
            return EndpointState(error="Provider temporarily unavailable")

    class FakeLive:
        def __init__(self, renderable, **kwargs):
            snapshots.append(renderable)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def update(self, renderable, **kwargs):
            snapshots.append(renderable)

    def sleep(_):
        if len(snapshots) >= 2:
            raise KeyboardInterrupt

    monkeypatch.setattr("codex_usage.cli.EndpointPoller", Poller)
    monkeypatch.setattr("codex_usage.cli.Live", FakeLive)
    monkeypatch.setattr("codex_usage.cli.time.sleep", sleep)
    result = runner.invoke(app, ["--config", str(config), "watch"])
    assert result.exit_code == 0, result.output
    assert len(polls) == len(snapshots) == 2
    for snapshot in snapshots:
        stream = StringIO()
        Console(file=stream, width=110).print(snapshot)
        assert "Main estimate" in stream.getvalue()
        assert "Provider temporarily unavailable" in stream.getvalue()
