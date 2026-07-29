from __future__ import annotations

import json
from pathlib import Path

from codex_usage.parser import SessionScanner, parse_session


SESSION_ID = "019fae69-69fc-7d51-b371-a2e868407c3b"


def record(timestamp: str, record_type: str, payload: dict) -> str:
    return json.dumps({"timestamp": timestamp, "type": record_type, "payload": payload})


def token_record(timestamp: str, *, input_tokens: int = 100, model_total: int = 999999) -> str:
    last = {
        "input_tokens": input_tokens,
        "cached_input_tokens": 60,
        "cache_write_input_tokens": 30,
        "output_tokens": 20,
        "reasoning_output_tokens": 5,
        "total_tokens": input_tokens + 20,
    }
    return record(
        timestamp,
        "event_msg",
        {
            "type": "token_count",
            "info": {
                "last_token_usage": last,
                "total_token_usage": {**last, "input_tokens": model_total},
            },
        },
    )


def test_parser_uses_last_usage_and_tracks_model_changes(tmp_path: Path) -> None:
    path = tmp_path / f"rollout-2026-07-29T00-00-00-{SESSION_ID}.jsonl"
    lines = [
        record("2026-07-29T00:00:00Z", "session_meta", {"id": SESSION_ID}),
        record("2026-07-29T00:00:01Z", "turn_context", {"model": "model-a"}),
        record("2026-07-29T00:00:02Z", "event_msg", {"type": "user_message"}),
        token_record("2026-07-29T00:00:03Z"),
        record("2026-07-29T00:00:04Z", "turn_context", {"model": "model-b"}),
        token_record("2026-07-29T00:00:05Z"),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    session = parse_session(path)

    assert [item.model for item in session.requests] == ["model-a", "model-b"]
    assert len(session.requests) == 2
    assert sum(item.tokens.input for item in session.requests) == 200
    assert session.requests[0].tokens.uncached_input == 10
    assert len(session.prompts) == 1
    assert session.prompts[0].model == "model-a"


def test_incomplete_last_line_is_retried_incrementally(tmp_path: Path) -> None:
    path = tmp_path / f"rollout-2026-07-29T00-00-00-{SESSION_ID}.jsonl"
    prefix = record("2026-07-29T00:00:01Z", "turn_context", {"model": "model-a"}) + "\n"
    complete = token_record("2026-07-29T00:00:03Z") + "\n"
    partial = complete[:30]
    path.write_bytes((prefix + partial).encode())

    first = parse_session(path)
    assert not first.requests
    assert not first.issues
    retained_offset = first.byte_offset

    with path.open("ab") as handle:
        handle.write(complete[30:].encode())
    second = parse_session(path, first)

    assert second is first
    assert second.byte_offset > retained_offset
    assert len(second.requests) == 1
    assert second.requests[0].model == "model-a"


def test_malformed_complete_line_is_reported_and_skipped(tmp_path: Path) -> None:
    path = tmp_path / f"rollout-2026-07-29T00-00-00-{SESSION_ID}.jsonl"
    path.write_text("{not-json}\n" + token_record("2026-07-29T00:00:03Z") + "\n", encoding="utf-8")
    session = parse_session(path)
    assert len(session.issues) == 1
    assert len(session.requests) == 1


def test_active_and_archived_duplicates_choose_larger_copy(settings_factory) -> None:
    settings, _ = settings_factory()
    filename = f"rollout-2026-07-29T00-00-00-{SESSION_ID}.jsonl"
    small = settings.active_sessions_path / "2026" / "07" / "29" / filename
    small.parent.mkdir(parents=True)
    large = settings.archived_sessions_path / filename
    meta = record("2026-07-29T00:00:00Z", "session_meta", {"id": SESSION_ID})
    context = record("2026-07-29T00:00:01Z", "turn_context", {"model": "model-a"})
    small.write_text(meta + "\n", encoding="utf-8")
    large.write_text(meta + "\n" + context + "\n" + token_record("2026-07-29T00:00:03Z") + "\n")

    scan = SessionScanner(settings).scan()

    assert scan.discovered_files == 2
    assert scan.duplicate_files == 1
    assert len(scan.sessions) == 1
    assert len(scan.requests) == 1
