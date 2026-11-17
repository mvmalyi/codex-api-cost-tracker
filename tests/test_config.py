from decimal import Decimal
from pathlib import Path

import pytest

from codex_usage.analytics import request_cost
from codex_usage.config import ConfigError, load_settings
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
