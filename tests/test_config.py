from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from codex_usage.analytics import request_cost
from codex_usage.config import ConfigError, default_config_path, load_settings
from codex_usage.domain import TokenUsage


def test_legacy_config_requires_explicit_12h_price(settings_factory) -> None:
    _, config = settings_factory()
    config.write_text(config.read_text().replace('cache_write_input_12h = "40"\n', ""))
    with pytest.raises(ConfigError, match="cache_write_input_12h.*12-hour rate"):
        load_settings(config)


@pytest.mark.parametrize("field", ["cache_write_input", "cache_write_input_12h"])
@pytest.mark.parametrize("value", ["-1", "NaN", "sNaN", "Infinity", "-Infinity", "bad"])
def test_invalid_write_prices_are_rejected(settings_factory, field, value) -> None:
    _, config = settings_factory()
    original = '20' if field == 'cache_write_input' else '40'
    config.write_text(config.read_text().replace(f'{field} = "{original}"', f'{field} = "{value}"'))
    with pytest.raises(ConfigError, match=field):
        load_settings(config)


def test_upper_write_price_cannot_be_below_default(settings_factory) -> None:
    _, config = settings_factory()
    config.write_text(config.read_text().replace('cache_write_input_12h = "40"', 'cache_write_input_12h = "19"'))
    with pytest.raises(ConfigError, match="cannot be below"):
        load_settings(config)


def test_proxy_write_prices_need_not_use_public_multipliers(settings_factory) -> None:
    _, config = settings_factory()
    config.write_text(config.read_text().replace('cache_write_input_12h = "40"', 'cache_write_input_12h = "21"'))
    assert load_settings(config).models["model-a"].short.cache_write_input_12h == Decimal("21")


# Standard API rate card checked on 17 November 2026, USD per million tokens.
@pytest.mark.parametrize("model,short_12h,long_12h", [
    ("gpt-6.2-sol", "5", "10"),
    ("gpt-6.2-astra", "20", "40"),
    ("gpt-6.1-sol", "5", "10"),
    ("gpt-6-sol", "5", "10"),
    ("gpt-6-astra", "25", "50"),
    ("gpt-6.1-terra", "2.5", "5"),
    ("gpt-6-luna", ".25", ".5"),
    ("gpt-5.6-sol", "10", "20"),
    ("gpt-5.6-terra", "5", "10"),
    ("gpt-5.6-luna", ".5", "1"),
])
def test_shipped_write_rates_match_published_prices(model, short_12h, long_12h) -> None:
    settings = load_settings(Path(__file__).parents[1] / "pricing.toml")
    price = settings.models[model]
    for band, expected in [(price.short, short_12h), (price.long, long_12h)]:
        assert band is not None
        assert band.cache_write_input_12h == Decimal(expected)
        assert band.cache_write_input == band.input * Decimal("1.25")
        assert band.cache_write_input_12h == band.input * Decimal("2.5")


@pytest.mark.parametrize("model", ["gpt-6.2-sol", "gpt-6.2-astra"])
@pytest.mark.parametrize("count,is_long", [(272000, False), (272001, True)])
def test_context_boundary_applies_to_both_scenarios(model, count, is_long) -> None:
    settings = load_settings(Path(__file__).parents[1] / "pricing.toml")
    for upper in [False, True]:
        _, actual_long = request_cost(TokenUsage(count, 0, 0, 0, 0), settings.models[model], upper_bound=upper)
        assert actual_long is is_long


def test_sol_cached_input_prices_are_corrected() -> None:
    price = load_settings(Path(__file__).parents[1] / "pricing.toml").models["gpt-6.1-sol"]
    assert price.short.cached_input == Decimal(".10")
    assert price.long.cached_input == Decimal(".20")


def test_optional_features_default_to_disabled(settings_factory) -> None:
    settings, _ = settings_factory()
    assert not settings.cost_endpoint.enabled
    assert not settings.show_cost_range
    shipped = load_settings(Path(__file__).parents[1] / "pricing.toml")
    assert not shipped.cost_endpoint.enabled
    assert not shipped.show_cost_range


@pytest.mark.parametrize("setting", ['', 'timezone = "local"\n'])
def test_local_timezone_uses_system_zone_rules_not_a_fixed_offset(settings_factory, monkeypatch, setting) -> None:
    _, config = settings_factory()
    config.write_text(config.read_text().replace('timezone = "Europe/London"\n', setting))
    monkeypatch.setenv("TZ", "America/New_York")
    zone = load_settings(config).timezone
    winter = datetime(2026, 1, 15, 12, tzinfo=timezone.utc).astimezone(zone)
    summer = datetime(2026, 7, 15, 12, tzinfo=timezone.utc).astimezone(zone)
    assert (winter.hour, winter.tzname()) == (7, "EST")
    assert (summer.hour, summer.tzname()) == (8, "EDT")


def test_explicit_timezone_overrides_system_timezone(settings_factory, monkeypatch) -> None:
    monkeypatch.setenv("TZ", "Asia/Tokyo")
    settings, _ = settings_factory()
    assert str(settings.timezone) == "Europe/London"


@pytest.mark.parametrize("value", ['"Unknown/Nowhere"', '""', '"/Europe/London"', '123'])
def test_invalid_timezone_is_a_configuration_error(settings_factory, value) -> None:
    _, config = settings_factory()
    config.write_text(config.read_text().replace('timezone = "Europe/London"', f'timezone = {value}'))
    with pytest.raises(ConfigError, match="Unknown timezone"):
        load_settings(config)


def test_personal_config_is_preferred_and_explicit_config_wins(settings_factory, tmp_path, monkeypatch) -> None:
    _, config = settings_factory()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.chdir(tmp_path)
    assert default_config_path() == Path("pricing.toml")
    personal = tmp_path / "config" / "codex-usage" / "pricing.toml"
    personal.parent.mkdir(parents=True)
    personal.write_text(config.read_text().replace('monthly_budget_usd = "500"', 'monthly_budget_usd = "600"'))
    assert default_config_path() == personal
    assert load_settings().monthly_budget_usd == Decimal("600")
    assert load_settings(config).monthly_budget_usd == Decimal("500")


@pytest.mark.parametrize("setting", [
    'enabled = "yes"',
    'enabled = true\nurl = "http://example.com/costs"\napi_key_env = "TEST_KEY"',
    'enabled = true\nurl = "https://user:pass@example.com/costs"\napi_key_env = "TEST_KEY"',
    'enabled = true\nurl = "https://example.com/costs#fragment"\napi_key_env = "TEST_KEY"',
    'enabled = true\nurl = "https://example.com/costs"\napi_key_env = ""',
    'enabled = true\nurl = "https://example.com/costs"\napi_key_env = "TEST_KEY"\nusage_field = ""',
    'currency_override = "GBP"',
    'timeout_seconds = 0',
    'timeout_seconds = nan',
    'refresh_interval_seconds = -1',
    'refresh_interval_seconds = inf',
    'params = { account = 42 }',
])
def test_invalid_endpoint_settings_are_rejected(settings_factory, setting) -> None:
    _, config = settings_factory()
    config.write_text(config.read_text() + "\n[cost_endpoint]\n" + setting)
    with pytest.raises(ConfigError, match="cost_endpoint"):
        load_settings(config)


def test_configurable_endpoint_fields_and_parameters(settings_factory) -> None:
    _, config = settings_factory()
    config.write_text(config.read_text() + '''
[cost_endpoint]
enabled = true
url = "https://example.com/costs"
api_key_env = "TEST_KEY"
usage_field = "data.spent"
limit_field = "quota.total"
as_of_field = "updated"
currency_override = "USD"
timeout_seconds = 10
refresh_interval_seconds = 120
[cost_endpoint.params]
account = "research"
''')
    endpoint = load_settings(config).cost_endpoint
    assert endpoint.enabled
    assert endpoint.usage_field == "data.spent"
    assert endpoint.params == (("account", "research"),)
    assert endpoint.refresh_interval_seconds == 120
