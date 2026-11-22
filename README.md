# Codex Usage Tracker

A macOS CLI that estimates OpenAI API spend from your local Codex session logs, with optional side-by-side provider-reported usage. It never sends session data over the network and never reads message content beyond identifying prompt events.

![screenshot](screenshot.png)

## Who is this for?

Built for anyone using Codex through **managed, institutional, or enterprise API proxies** (such as university or company keys) with monthly or annual quotas and no direct access to the OpenAI billing dashboard—especially when you want a per-model and token-level breakdown to see where spend can be optimised.

> **Using the University of Edinburgh's ELM API on macOS?** If you just want a lightweight, minimal utility sitting permanently in your menu bar to glance at your current spend and remaining limit, check out **[ELM API Tracker](https://github.com/mvmalyi/elm-api-usage-tracker)**.

## Quick Start

```bash
uv tool install .
codex-usage
```

`uv tool install` installs `codex-usage` globally, independent of the repo folder (re-run `uv tool install --force .` after modifying source files).

### Common Commands

```bash
codex-usage                                   # Current month's dashboard
codex-usage watch                             # Live-updating dashboard
codex-usage report --period month             # Monthly report
codex-usage report --period all --model gpt-6.1-sol
codex-usage report --period month --json      # Machine-readable JSON report
codex-usage models                            # List configured models & rates
codex-usage doctor                            # Check log files, errors & endpoint
codex-usage --show-cost-range                 # Include 12h cache-write upper bound & midpoint
codex-usage --show-parser-warnings report     # Surface parser/deduplication warnings
```

## Configuration

`codex-usage` loads `pricing.toml` in this order:
1. `--config /path/to/pricing.toml`
2. `$XDG_CONFIG_HOME/codex-usage/pricing.toml` (`~/.config/codex-usage/pricing.toml` by default)
3. `./pricing.toml` in the current directory

To use custom settings from any folder, copy the bundled `pricing.toml` to `~/.config/codex-usage/pricing.toml` and edit it there.

- **Budget & rates:** Set `monthly_budget_usd`, custom model aliases, and per-million-token USD rates (bundled rates reflect OpenAI Standard API pricing as of 17 November 2026).
- **Timezone:** `app.timezone` defaults to `"local"` (respecting `TZ`) and controls display timestamps and calendar-month boundaries. Set an IANA name like `"Europe/London"` to fix it.
- **Cost range:** By default, cache writes are priced at the 30-minute rate. Set `app.show_cost_range = true` (or pass `--show-cost-range` / `--hide-cost-range`) to also show the 12-hour upper bound and midpoint.

## Optional Provider Cost Endpoint

Disabled by default. To display **Provider reported usage** side by side with **Estimate from logs**, enable `[cost_endpoint]` in your `pricing.toml`:

```toml
[cost_endpoint]
enabled = true
url = "https://your-provider.example/api/v1/costs"
api_key_env = "PROVIDER_API_KEY"
usage_field = "usage"
limit_field = "limit"
as_of_field = "asOf"
currency_field = "currency"
timeout_seconds = 30
refresh_interval_seconds = 60
```

- Sends an HTTPS `GET` with `Authorization: Bearer <key>` read from the configured environment variable at request time (never saved in config or logs; redirects are not followed; no session data is sent).
- Supports dotted JSON paths (e.g. `data.usage`) and optional query strings via `[cost_endpoint.params]`.
- If a provider reports USD amounts mislabelled as another currency, set `currency_override = "USD"` (relabels without converting). Missing `limit` falls back to `app.monthly_budget_usd`.
- In `watch` mode, the endpoint refreshes in the background independently of local logs and retains the last successful value if a request fails. All-time and model-filtered reports always show local estimates only.

## How Logs & Accounting Work

- **Keep current-month chats locally:** Both active and archived sessions—including plain `.jsonl` and Zstandard-compressed `.jsonl.zst` logs—are parsed and deduplicated by session ID, but permanently deleted chats cannot be recovered.
- **Accurate token accounting:** Input tokens are split into mutually exclusive uncached, cached, and cache-write buckets; reasoning tokens are already counted inside output tokens and aren't double-charged; subagent calls are priced at their own model's rates.
- **Unpriced models & diagnostics:** Activity for models without configured rates is still shown, while the total is marked partial. Run `codex-usage doctor` to inspect log counts, corrupted files, or endpoint health.

<details>
<summary>Pricing configuration & accounting details</summary>

- Every short/long context price band in `pricing.toml` requires both `cache_write_input` (30-minute writes) and `cache_write_input_12h` (12-hour writes):
  ```toml
  [models."gpt-6.2-sol".short]
  input = "2.00"
  cached_input = "0.10"
  cache_write_input = "2.50"
  cache_write_input_12h = "5.00"
  output = "10.00"
  ```
- Because Codex logs record a combined cache-write count rather than separating 30-minute and 12-hour lifetimes, `--show-cost-range` provides the 30m baseline, 12h upper bound, and midpoint (all three are always included in `--json` as `cost_usd`, `cost_upper_usd`, and `cost_mean_usd`).
- Repeated `token_count` snapshots with unchanged cumulative totals are deduplicated automatically.
- The **Prompts** column counts user messages (excluding injected instructions and automated subagent messages); underlying API call counts are available in `--json` under `diagnostics.api_requests`.
- When `[cost_endpoint]` is enabled, monthly `--json` output includes a `cost_endpoint` object with `data` (`usage_usd`, `limit_usd`, `currency`, `as_of`, `fetched_at`), `error`, and `refreshing`.

</details>

## Development

```bash
uv sync --group dev --no-editable
uv run --no-sync pytest
```

## Related

- **[ELM API Tracker](https://github.com/mvmalyi/elm-api-usage-tracker)** — A lightweight, minimal macOS menu bar utility for University of Edinburgh ELM API users who want their spend and remaining limit always visible in the menu bar.
