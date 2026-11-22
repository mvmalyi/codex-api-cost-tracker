from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone as utc_timezone, tzinfo
from decimal import Decimal, InvalidOperation
from pathlib import Path
import math
import os
import tomllib
from urllib.parse import urlsplit
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
class CostEndpointSettings:
    enabled: bool = False
    url: str = ""
    api_key_env: str = ""
    usage_field: str = "usage"
    limit_field: str = "limit"
    as_of_field: str = "asOf"
    currency_field: str = "currency"
    currency_override: str = ""
    timeout_seconds: float = 30
    refresh_interval_seconds: float = 60
    params: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class Settings:
    timezone: tzinfo
    monthly_budget_usd: Decimal
    watch_interval_seconds: float
    active_sessions_path: Path
    archived_sessions_path: Path
    models: dict[str, ModelPrice]
    aliases: dict[str, str]
    config_path: Path
    show_cost_range: bool = False
    cost_endpoint: CostEndpointSettings = CostEndpointSettings()

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


def default_config_path() -> Path:
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", "~/.config")).expanduser()
    personal = config_home / "codex-usage" / "pricing.toml"
    return personal if personal.is_file() else Path("pricing.toml")


def _local_timezone() -> tzinfo:
    name = os.environ.get("TZ", "").removeprefix(":")
    if name:
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            pass
    # Reading the system zone retains historical and daylight-saving rules,
    # unlike datetime.astimezone().tzinfo, which is only the current UTC offset.
    try:
        with Path("/etc/localtime").open("rb") as handle:
            return ZoneInfo.from_file(handle)
    except (OSError, ValueError):
        return datetime.now().astimezone().tzinfo or utc_timezone.utc


def _timezone(value: object) -> tzinfo:
    if value == "local":
        return _local_timezone()
    try:
        if not isinstance(value, str):
            raise ValueError
        return ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError(f"Unknown timezone: {value}; use local or an IANA name such as Europe/London") from exc


def _boolean(value: object, field: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{field} must be true or false")
    return value


def _positive_seconds(value: object, field: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ConfigError(f"{field} must be a positive number") from exc
    if isinstance(value, bool) or not math.isfinite(result) or result <= 0:
        raise ConfigError(f"{field} must be a finite positive number")
    return result


def _cost_endpoint(data: object) -> CostEndpointSettings:
    if not isinstance(data, dict):
        raise ConfigError("[cost_endpoint] must be a TOML table")
    enabled = _boolean(data.get("enabled", False), "cost_endpoint.enabled")
    strings = {}
    for name in ("url", "api_key_env", "usage_field", "limit_field", "as_of_field",
                 "currency_field", "currency_override"):
        default = getattr(CostEndpointSettings(), name)
        value = data.get(name, default)
        if not isinstance(value, str):
            raise ConfigError(f"cost_endpoint.{name} must be a string")
        strings[name] = value.strip()
    if enabled:
        try:
            url = urlsplit(strings["url"])
            valid_url = (url.scheme == "https" and bool(url.hostname)
                         and not url.username and not url.password and not url.fragment)
        except ValueError:
            valid_url = False
        if not valid_url:
            raise ConfigError("cost_endpoint.url must be an HTTPS URL without credentials or a fragment")
        if not strings["api_key_env"].isidentifier():
            raise ConfigError("cost_endpoint.api_key_env must name an environment variable")
        if not strings["usage_field"]:
            raise ConfigError("cost_endpoint.usage_field cannot be empty")
    if strings["currency_override"] not in ("", "USD"):
        raise ConfigError("cost_endpoint.currency_override must be USD; currency conversion is not supported")
    params = data.get("params", {})
    if not isinstance(params, dict) or any(not isinstance(value, str) for value in params.values()):
        raise ConfigError("cost_endpoint.params must be a table of string query parameters")
    return CostEndpointSettings(
        enabled=enabled,
        **strings,
        timeout_seconds=_positive_seconds(data.get("timeout_seconds", 30), "cost_endpoint.timeout_seconds"),
        refresh_interval_seconds=_positive_seconds(
            data.get("refresh_interval_seconds", 60), "cost_endpoint.refresh_interval_seconds"
        ),
        params=tuple(params.items()),
    )


def load_settings(path: Path | str | None = None) -> Settings:
    if path is None:
        path = default_config_path()
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
    timezone = _timezone(app.get("timezone", "local"))

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
        show_cost_range=_boolean(app.get("show_cost_range", False), "app.show_cost_range"),
        cost_endpoint=_cost_endpoint(raw.get("cost_endpoint", {})),
    )

