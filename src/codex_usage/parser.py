from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .config import Settings
from .domain import (
    UNKNOWN_MODEL,
    ParseIssue,
    ParsedSession,
    TokenUsage,
    UsageEvent,
    PromptEvent,
)


_SESSION_ID_RE = re.compile(
    r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\.jsonl$"
)


@dataclass(frozen=True)
class ScanResult:
    sessions: list[ParsedSession]
    issues: list[ParseIssue]
    discovered_files: int
    duplicate_files: int

    @property
    def requests(self) -> list[UsageEvent]:
        return [request for session in self.sessions for request in session.requests]

    @property
    def prompts(self) -> list[PromptEvent]:
        return [prompt for session in self.sessions for prompt in session.prompts]


def parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return timestamp if timestamp.tzinfo is not None else None


def fallback_session_id(path: Path) -> str:
    match = _SESSION_ID_RE.search(path.name)
    return match.group(1).lower() if match else str(path.resolve())


def _token_integer(data: dict[str, object], key: str) -> int | None:
    value = data.get(key, 0)
    if isinstance(value, bool):
        return None
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _replace_session_id(session: ParsedSession, session_id: str) -> None:
    if session.session_id == session_id:
        return
    session.session_id = session_id
    session.requests = [
        UsageEvent(item.timestamp, session_id, item.model, item.tokens) for item in session.requests
    ]
    session.prompts = [
        PromptEvent(item.timestamp, session_id, item.model) for item in session.prompts
    ]


def _process_record(session: ParsedSession, record: object, line_number: int) -> None:
    if not isinstance(record, dict):
        session.issues.append(ParseIssue(session.path, line_number, "JSON value is not an object"))
        return
    record_type = record.get("type")
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return

    if record_type == "session_meta":
        session_id = payload.get("id")
        if isinstance(session_id, str) and session_id:
            _replace_session_id(session, session_id)
        return

    if record_type == "turn_context":
        model = payload.get("model")
        if isinstance(model, str) and model:
            session.current_model = model
            session.observed_models.add(model)
        return

    timestamp = parse_timestamp(record.get("timestamp"))
    if record_type == "event_msg" and payload.get("type") == "user_message":
        if timestamp is None:
            session.issues.append(ParseIssue(session.path, line_number, "User turn has an invalid timestamp"))
            return
        session.prompts.append(
            PromptEvent(timestamp=timestamp, session_id=session.session_id, model=session.current_model)
        )
        return

    if record_type != "event_msg" or payload.get("type") != "token_count":
        return
    info = payload.get("info")
    if not isinstance(info, dict):
        return
    usage = info.get("last_token_usage")
    if usage is None:
        return
    if not isinstance(usage, dict):
        session.issues.append(ParseIssue(session.path, line_number, "last_token_usage is not an object"))
        return
    if timestamp is None:
        session.issues.append(ParseIssue(session.path, line_number, "Token count has an invalid timestamp"))
        return

    fields = {
        "input": _token_integer(usage, "input_tokens"),
        "cached_input": _token_integer(usage, "cached_input_tokens"),
        "cache_write_input": _token_integer(usage, "cache_write_input_tokens"),
        "output": _token_integer(usage, "output_tokens"),
        "reasoning_output": _token_integer(usage, "reasoning_output_tokens"),
    }
    invalid = [key for key, value in fields.items() if value is None]
    if invalid:
        session.issues.append(
            ParseIssue(session.path, line_number, f"Invalid token values: {', '.join(invalid)}")
        )
        return
    tokens = TokenUsage(**fields)  # type: ignore[arg-type]
    if tokens.cached_input + tokens.cache_write_input > tokens.input:
        session.issues.append(
            ParseIssue(session.path, line_number, "Cached plus cache-write input exceeds total input")
        )
    if tokens.reasoning_output > tokens.output:
        session.issues.append(
            ParseIssue(session.path, line_number, "Reasoning output exceeds total output")
        )
    session.observed_models.add(session.current_model)
    session.requests.append(
        UsageEvent(
            timestamp=timestamp,
            session_id=session.session_id,
            model=session.current_model,
            tokens=tokens,
        )
    )


def parse_session(path: Path, previous: ParsedSession | None = None) -> ParsedSession:
    stat = path.stat()
    can_append = (
        previous is not None
        and previous.inode == stat.st_ino
        and stat.st_size >= previous.byte_offset
    )
    if can_append:
        session = previous
        start_offset = previous.byte_offset
    else:
        session = ParsedSession(path=path, session_id=fallback_session_id(path))
        start_offset = 0

    if can_append and stat.st_size == session.file_size and stat.st_mtime_ns == session.mtime_ns:
        return session

    with path.open("rb") as handle:
        handle.seek(start_offset)
        while True:
            line_start = handle.tell()
            raw_line = handle.readline()
            if not raw_line:
                break
            next_line_number = session.line_number + 1
            has_newline = raw_line.endswith(b"\n")
            try:
                record = json.loads(raw_line)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                if not has_newline:
                    handle.seek(line_start)
                    break
                session.issues.append(
                    ParseIssue(path, next_line_number, f"Invalid JSON: {exc.msg if hasattr(exc, 'msg') else exc}")
                )
                session.line_number = next_line_number
                session.byte_offset = handle.tell()
                continue
            _process_record(session, record, next_line_number)
            session.line_number = next_line_number
            session.byte_offset = handle.tell()

    latest = path.stat()
    session.inode = latest.st_ino
    session.file_size = latest.st_size
    session.mtime_ns = latest.st_mtime_ns
    return session


class SessionScanner:
    """Scans rollout files and incrementally consumes appended JSONL data."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._cache: dict[Path, ParsedSession] = {}

    def discover(self) -> list[Path]:
        paths: set[Path] = set()
        active, archived = self.settings.source_paths
        if active.is_dir():
            paths.update(active.rglob("*.jsonl"))
        if archived.is_dir():
            paths.update(archived.glob("*.jsonl"))
        return sorted(paths)

    def scan(self) -> ScanResult:
        paths = self.discover()
        live_paths = set(paths)
        self._cache = {path: session for path, session in self._cache.items() if path in live_paths}
        issues: list[ParseIssue] = []
        parsed: list[ParsedSession] = []
        for path in paths:
            try:
                session = parse_session(path, self._cache.get(path))
            except (OSError, ValueError) as exc:
                issues.append(ParseIssue(path, 0, f"Could not read file: {exc}"))
                continue
            self._cache[path] = session
            parsed.append(session)

        by_id: dict[str, list[ParsedSession]] = {}
        for session in parsed:
            by_id.setdefault(session.session_id, []).append(session)
        selected: list[ParsedSession] = []
        duplicates = 0
        for session_id, copies in by_id.items():
            chosen = max(copies, key=lambda item: (item.file_size, item.mtime_ns))
            selected.append(chosen)
            for duplicate in copies:
                if duplicate is chosen:
                    continue
                duplicates += 1
                issues.append(
                    ParseIssue(duplicate.path, 0, f"Duplicate session {session_id}; using {chosen.path}")
                )
        for session in selected:
            issues.extend(session.issues)
        return ScanResult(
            sessions=sorted(selected, key=lambda item: str(item.path)),
            issues=issues,
            discovered_files=len(paths),
            duplicate_files=duplicates,
        )
