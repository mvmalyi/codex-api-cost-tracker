from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


UNKNOWN_MODEL = "<unknown>"


@dataclass(frozen=True)
class TokenUsage:
    input: int
    cached_input: int
    cache_write_input: int
    output: int
    reasoning_output: int

    @property
    def uncached_input(self) -> int:
        return max(0, self.input - self.cached_input - self.cache_write_input)

    @property
    def total(self) -> int:
        return self.input + self.output


@dataclass(frozen=True)
class UsageEvent:
    timestamp: datetime
    session_id: str
    model: str
    tokens: TokenUsage


@dataclass(frozen=True)
class PromptEvent:
    timestamp: datetime
    session_id: str
    model: str


@dataclass(frozen=True)
class ParseIssue:
    path: Path
    line_number: int
    message: str


@dataclass
class ParsedSession:
    path: Path
    session_id: str
    requests: list[UsageEvent] = field(default_factory=list)
    prompts: list[PromptEvent] = field(default_factory=list)
    issues: list[ParseIssue] = field(default_factory=list)
    observed_models: set[str] = field(default_factory=set)
    current_model: str = UNKNOWN_MODEL
    byte_offset: int = 0
    line_number: int = 0
    inode: int = 0
    file_size: int = 0
    mtime_ns: int = 0
    is_subagent: bool = False
    automated_turn: bool = False
    prompt_turn: str = "initial"
    prompt_source_counts: dict[tuple[str, str], int] = field(default_factory=dict)
    prompt_indexes: dict[str, list[int]] = field(default_factory=dict)
    pending_prompt_indexes: list[int] = field(default_factory=list)
