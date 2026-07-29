from __future__ import annotations

from pathlib import Path

import pytest

from codex_usage.config import load_settings


@pytest.fixture
def settings_factory(tmp_path: Path):
    def factory(*, aliases: dict[str, str] | None = None):
        active = tmp_path / "sessions"
        archived = tmp_path / "archived"
        active.mkdir(exist_ok=True)
        archived.mkdir(exist_ok=True)
        config = tmp_path / "pricing.toml"
        alias_lines = "\n".join(f'"{key}" = "{value}"' for key, value in (aliases or {}).items())
        config.write_text(
            f'''[app]
timezone = "Europe/London"
monthly_budget_usd = "500"
rolling_30d_budget_usd = "500"
watch_interval_seconds = 1
active_sessions_path = "{active}"
archived_sessions_path = "{archived}"

[models."model-a"]
long_context_threshold_tokens = 100
[models."model-a".short]
input = "10"
cached_input = "2"
cache_write_input = "20"
output = "40"
[models."model-a".long]
input = "20"
cached_input = "4"
cache_write_input = "40"
output = "80"

[models."model-b".short]
input = "1"
cached_input = "0.1"
cache_write_input = "1.25"
output = "5"

[aliases]
{alias_lines}
''',
            encoding="utf-8",
        )
        return load_settings(config), config

    return factory

