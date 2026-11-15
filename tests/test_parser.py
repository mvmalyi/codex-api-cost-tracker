from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from codex_usage.analytics import Period, build_report
from codex_usage.parser import SessionScanner, ZstdFile, parse_session


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


def session_bytes(*, requests: int = 1) -> bytes:
    lines = [
        record("2026-07-29T00:00:00Z", "session_meta", {"id": SESSION_ID}),
        record("2026-07-29T00:00:01Z", "turn_context", {"model": "model-a"}),
        record("2026-07-29T00:00:02Z", "event_msg", {"type": "user_message"}),
    ]
    lines.extend(token_record(f"2026-07-29T00:00:{index + 3:02d}Z") for index in range(requests))
    return ("\n".join(lines) + "\n").encode()


def write_compressed(path: Path, data: bytes) -> None:
    with ZstdFile(path, "wb") as handle:
        handle.write(data)


def test_compressed_and_plain_records_are_equivalent(tmp_path: Path) -> None:
    data = session_bytes(requests=2)
    plain = tmp_path / f"rollout-{SESSION_ID}.jsonl"
    compressed = plain.with_suffix(".jsonl.zst")
    plain.write_bytes(data)
    write_compressed(compressed, data)

    expected = parse_session(plain)
    actual = parse_session(compressed)

    assert actual.requests == expected.requests
    assert actual.prompts == expected.prompts
    assert actual.current_model == expected.current_model
    assert actual.byte_offset == expected.byte_offset == len(data)
    assert actual.issues == expected.issues == []


def test_compressed_session_without_metadata_keeps_fallback_id(tmp_path: Path) -> None:
    path = tmp_path / f"rollout-{SESSION_ID}.jsonl.zst"
    write_compressed(path, (token_record("2026-07-29T00:00:03Z") + "\n").encode())

    session = parse_session(path)

    assert session.session_id == SESSION_ID
    assert session.requests[0].session_id == SESSION_ID


def test_compression_and_archiving_preserve_watch_and_restart_totals(settings_factory) -> None:
    settings, _ = settings_factory()
    plain = settings.active_sessions_path / f"rollout-{SESSION_ID}.jsonl"
    plain.write_bytes(session_bytes())
    compressed = plain.with_suffix(".jsonl.zst")
    scanner = SessionScanner(settings)
    now = datetime(2026, 7, 30, tzinfo=timezone.utc)

    def totals(scanner):
        report = build_report(scanner.scan(), settings, Period.month, now)
        return report.tokens, report.cost_usd, report.sessions, report.prompts, report.api_requests

    expected = totals(scanner)
    assert expected[0].input == 100
    assert expected[1] > 0
    write_compressed(compressed, plain.read_bytes())
    assert totals(scanner) == expected  # Both representations coexist.
    assert scanner.scan().duplicate_files == 1
    plain.unlink()
    assert totals(scanner) == expected
    archived = settings.archived_sessions_path / "2026" / "07" / compressed.name
    archived.parent.mkdir(parents=True)
    compressed.rename(archived)
    assert totals(scanner) == expected
    assert totals(SessionScanner(settings)) == expected
    scan = scanner.scan()
    assert (scan.plain_files, scan.compressed_files) == (0, 1)


def test_duplicate_selection_uses_decoded_size(settings_factory) -> None:
    settings, _ = settings_factory()
    plain = settings.active_sessions_path / f"rollout-{SESSION_ID}.jsonl"
    compressed = settings.archived_sessions_path / (plain.name + ".zst")
    # The compressed copy contains more requests but occupies fewer disk bytes.
    plain.write_bytes(session_bytes())
    write_compressed(compressed, session_bytes(requests=2))
    assert compressed.stat().st_size < plain.stat().st_size

    scan = SessionScanner(settings).scan()

    assert len(scan.requests) == 2
    assert scan.sessions[0].path == compressed
    assert scan.duplicate_files == 1


def test_unchanged_compressed_files_are_not_reopened(settings_factory, monkeypatch) -> None:
    settings, _ = settings_factory()
    path = settings.active_sessions_path / f"rollout-{SESSION_ID}.jsonl.zst"
    write_compressed(path, session_bytes())
    scanner = SessionScanner(settings)
    first = scanner.scan()

    def unexpected_open(*args, **kwargs):
        raise AssertionError("Unchanged compressed file was decompressed again")

    monkeypatch.setattr("codex_usage.parser.ZstdFile", unexpected_open)
    second = scanner.scan()

    assert second.sessions[0] is first.sessions[0]
    assert second.requests == first.requests


def test_changed_compressed_file_is_reparsed_from_start(settings_factory) -> None:
    settings, _ = settings_factory()
    path = settings.active_sessions_path / f"rollout-{SESSION_ID}.jsonl.zst"
    write_compressed(path, session_bytes())
    scanner = SessionScanner(settings)
    first = scanner.scan()
    stat = path.stat()
    write_compressed(path, session_bytes(requests=2))
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))

    second = scanner.scan()

    assert len(first.requests) == 1
    assert len(second.requests) == 2
    assert second.sessions[0] is not first.sessions[0]


@pytest.mark.parametrize("damage", ["invalid_header", "truncated_frame"])
def test_damaged_compressed_file_is_reported(settings_factory, damage) -> None:
    settings, _ = settings_factory()
    damaged = settings.active_sessions_path / "damaged.jsonl.zst"
    if damage == "invalid_header":
        damaged.write_bytes(b"not a Zstandard frame")
    else:
        write_compressed(damaged, session_bytes())
        damaged.write_bytes(damaged.read_bytes()[:-1])
    valid = settings.archived_sessions_path / f"rollout-{SESSION_ID}.jsonl"
    valid.write_bytes(session_bytes())

    scan = SessionScanner(settings).scan()

    assert len(scan.requests) == 1  # Unaffected files are still included.
    assert len(scan.issues) == 1
    assert scan.issues[0].path == damaged
    assert "Could not read file" in scan.issues[0].message
    assert (scan.plain_files, scan.compressed_files) == (1, 1)


def test_concatenated_zstandard_frames_are_read(tmp_path: Path) -> None:
    path = tmp_path / f"rollout-{SESSION_ID}.jsonl.zst"
    write_compressed(path, session_bytes())
    with ZstdFile(path, "ab") as handle:
        handle.write((token_record("2026-07-29T00:00:04Z") + "\n").encode())

    assert len(parse_session(path).requests) == 2
