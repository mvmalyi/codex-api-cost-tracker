from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
import tomllib
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class ConfigError(ValueError):
    """Raised when pricing.toml is invalid."""


@dataclass(frozen=True)
class PriceBand:
    input: Decimal
    cached_input: Decimal
    cache_write_input: Decimal
    cache_write_input_12h: Decimal
    output: Decimal


@dataclass(frozen=True)
class ModelPrice:
    short: PriceBand
    long: PriceBand | None
    long_context_threshold_tokens: int | None


@dataclass(frozen=True)
class Settings:
    timezone: ZoneInfo
    monthly_budget_usd: Decimal
    watch_interval_seconds: float
    active_sessions_path: Path
    archived_sessions_path: Path
    models: dict[str, ModelPrice]
    aliases: dict[str, str]
    config_path: Path

    @property
    def source_paths(self) -> tuple[Path, Path]:
        return self.active_sessions_path, self.archived_sessions_path

    def resolve_model(self, model: str) -> str:
        seen: set[str] = set()
        resolved = model
        while resolved in self.aliases and resolved not in seen:
            seen.add(resolved)
            resolved = self.aliases[resolved]
        return resolved


def _decimal(value: object, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ConfigError(f"{field} must be a decimal number") from exc
    if not result.is_finite():
        raise ConfigError(f"{field} must be finite")
    if result < 0:
        raise ConfigError(f"{field} cannot be negative")
    return result


def _band(data: object, field: str) -> PriceBand:
    if not isinstance(data, dict):
        raise ConfigError(f"{field} must be a TOML table")
    required = ("input", "cached_input", "cache_write_input", "cache_write_input_12h", "output")
    missing = [key for key in required if key not in data]
    if missing:
        raise ConfigError(
            f"{field} is missing: {', '.join(missing)}. "
            "Set cache_write_input to the 30-minute rate and cache_write_input_12h "
            "to your provider's 12-hour rate (USD per million tokens)."
        )
    band = PriceBand(**{key: _decimal(data[key], f"{field}.{key}") for key in required})
    if band.cache_write_input_12h < band.cache_write_input:
        raise ConfigError(f"{field}.cache_write_input_12h cannot be below cache_write_input")
    return band


def load_settings(path: Path | str = Path("pricing.toml")) -> Settings:
    config_path = Path(path).expanduser().resolve()
    try:
        with config_path.open("rb") as handle:
            raw = tomllib.load(handle)
    except FileNotFoundError as exc:
        raise ConfigError(f"Configuration file not found: {config_path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Invalid TOML in {config_path}: {exc}") from exc

    app = raw.get("app")
    if not isinstance(app, dict):
        raise ConfigError("Missing [app] configuration table")
    try:
        timezone = ZoneInfo(str(app.get("timezone", "Europe/London")))
    except ZoneInfoNotFoundError as exc:
        raise ConfigError(f"Unknown timezone: {app.get('timezone')}") from exc

    models: dict[str, ModelPrice] = {}
    model_tables = raw.get("models", {})
    if not isinstance(model_tables, dict):
        raise ConfigError("[models] must contain model tables")
    for name, data in model_tables.items():
        if not isinstance(data, dict):
            raise ConfigError(f"models.{name} must be a table")
        threshold = data.get("long_context_threshold_tokens")
        if threshold is not None:
            try:
                threshold = int(threshold)
            except (TypeError, ValueError) as exc:
                raise ConfigError(f"models.{name}.long_context_threshold_tokens must be an integer") from exc
            if threshold <= 0:
                raise ConfigError(f"models.{name}.long_context_threshold_tokens must be positive")
        long_band = _band(data["long"], f"models.{name}.long") if "long" in data else None
        if long_band is not None and threshold is None:
            raise ConfigError(f"models.{name} has long prices but no long-context threshold")
        models[name] = ModelPrice(
            short=_band(data.get("short"), f"models.{name}.short"),
            long=long_band,
            long_context_threshold_tokens=threshold,
        )

    aliases_raw = raw.get("aliases", {})
    if not isinstance(aliases_raw, dict):
        raise ConfigError("[aliases] must be a table")
    aliases = {str(key): str(value) for key, value in aliases_raw.items()}

    interval = float(app.get("watch_interval_seconds", 5.0))
    if interval <= 0:
        raise ConfigError("app.watch_interval_seconds must be positive")

    return Settings(
        timezone=timezone,
        monthly_budget_usd=_decimal(app.get("monthly_budget_usd", "500"), "app.monthly_budget_usd"),
        watch_interval_seconds=interval,
        active_sessions_path=Path(str(app.get("active_sessions_path", "~/.codex/sessions"))).expanduser(),
        archived_sessions_path=Path(
            str(app.get("archived_sessions_path", "~/.codex/archived_sessions"))
        ).expanduser(),
        models=models,
        aliases=aliases,
        config_path=config_path,
    )

