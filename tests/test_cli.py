from __future__ import annotations

import json

from typer.testing import CliRunner

from codex_usage.cli import app
from codex_usage.parser import ZstdFile


runner = CliRunner()


def test_default_dashboard_and_doctor(settings_factory) -> None:
    _, config = settings_factory()
    result = runner.invoke(app, ["--config", str(config)])
    assert result.exit_code == 0, result.output
    assert "Codex Usage Tracker" in result.output
    assert "Current calendar month" in result.output
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
