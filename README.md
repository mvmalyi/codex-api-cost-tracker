# Codex Usage Tracker

A macOS CLI that estimates OpenAI API cost from local Codex JSONL rollout files,
with optional provider-reported usage. It never sends session data over the
network and never reads message content beyond identifying prompt events.

## Who is this for?

This tool is designed specifically for people who interact with OpenAI API using Codex through **managed, institutional, or enterprise proxy keys** (such as keys issued by universities or companies) with monthly or annual quotas but no direct access to the OpenAI API dashboard.

![screenshot](screenshot.png)

## Setup

```bash
uv tool install .
codex-usage
```

`uv tool install` keeps the installed CLI independent of the project folder. Re-run `uv tool install --force .` after changing the source code.

The default dashboard shows the current calendar month's main estimate against
`monthly_budget_usd`, with cache writes priced at the 30-minute rate. The Models
table shows the same local estimate per model. The optional 12-hour upper bound
and arithmetic midpoint are hidden by default; enable them with
`app.show_cost_range = true` or the global `--show-cost-range` option. Override
that setting with `--hide-cost-range`. The midpoint is a comparison value, not
a predicted bill. All three estimates remain available in JSON.

Configuration is read from `$XDG_CONFIG_HOME/codex-usage/pricing.toml`
(`~/.config/codex-usage/pricing.toml` by default) if it exists, otherwise from
`./pricing.toml`. An explicit `--config /path/to/pricing.toml` takes precedence.
For terminal use from any directory, copy the supplied `pricing.toml` to that
personal configuration path before customising it.

Both **As of** and **Updated** use `app.timezone`, including daylight-saving
changes at each timestamp. It defaults to `"local"`, which detects the machine's
timezone when the command starts (honouring an IANA `TZ` environment variable).
Set an IANA name such as `"Europe/London"` to fix the reporting timezone. This
also determines calendar-month boundaries. London shows GMT in winter and BST
in summer. Restart watch mode after changing the system timezone or configuration.
JSON timestamps retain their original offsets.

Useful commands:

```bash
codex-usage watch
codex-usage report --period month
codex-usage report --period all --model gpt-5.6-terra
codex-usage report --period month --json
codex-usage models
codex-usage doctor
codex-usage --show-parser-warnings report
codex-usage --show-cost-range
```

For development and testing:

```bash
uv sync --group dev --no-editable
uv run --no-sync pytest
```

## Optional provider cost endpoint

This feature is disabled in the supplied configuration. To enable it, edit the
`[cost_endpoint]` table in your personal pricing file:

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

Set the named environment variable in the shell that launches `codex-usage`.
The tool sends an HTTPS GET request with `Authorization: Bearer <key>`. The key
is read at request time and is never stored in the pricing file or included in
output. No session data is sent. Redirects are not followed.

Response field names support dotted object paths, such as `data.usage`. Optional
GET query parameters can be supplied as strings in a `[cost_endpoint.params]`
table. A typical response is:

```json
{"usage": 480.0128, "limit": 500.0, "currency": "USD", "asOf": "2026-11-22T00:00:00Z"}
```

Amounts must be non-negative and in USD. If a provider incorrectly labels its
USD figures as another currency, set `currency_override = "USD"` in
`[cost_endpoint]`. This changes the label only and performs no conversion.
Missing currency is treated as USD; any other currency is rejected unless
overridden. Missing `limit` uses `app.monthly_budget_usd`. Missing `asOf` is
shown as “not supplied”; when present it must include a timezone. Set a metadata
field name to an empty string to ignore that response field.

When enabled, the dashboard shows two budget bars: the current local estimate
on the left and provider-reported usage on the right. Panels stack only when
their amounts and timestamp cannot fit side by side. Each uses its own budget
or limit. The provider's **As of** timestamp is displayed because it may lag
behind local activity. Its usage and quota period come directly from the
provider; they are not reconciled with or added to the local calendar-month
estimate. There is no per-model provider breakdown.

Watch mode refreshes the endpoint in the background at the configured interval,
independently of local log updates. On failure it retains the last successful
response and timestamp with a warning. A missing key or failed request leaves
the local estimate available. One-off commands use the configured request timeout.
`doctor` checks the endpoint when enabled. All-time and model-filtered reports
show only local costs, since the endpoint describes account-wide usage.

Enabled monthly JSON reports add `cost_endpoint` with `data`, `error` and
`refreshing`. Data contains decimal-string `usage_usd` and optional `limit_usd`,
normalised `currency`, the provider's `as_of`, and the retrieval time `fetched_at`.
The existing local cost fields keep their meaning. Disabled configurations
make no endpoint requests and retain the existing JSON structure.

## A note on Codex logs

The CLI tool can parse archived chats but not the ones that have been completely deleted. For an accurate estimate, keep the chats from the current month on your local machine.

Both plain `.jsonl` and Zstandard-compressed `.jsonl.zst` logs are read from
the active and archived session directories, including subdirectories.
Compression or moving a log to the archive preserves its usage. If plain and
compressed copies coexist, the tracker counts one copy per session ID, choosing
the one with the most decoded data rather than the largest compressed file.

Watch mode reads plain logs incrementally and caches unchanged compressed logs.
Run `codex-usage doctor` to see plain/compressed file counts and read errors,
including damaged compressed logs. These file counts are also included in JSON
report diagnostics. Parser and deduplication warnings are hidden in the normal
dashboard, watch mode and reports by default; pass the global
`--show-parser-warnings` option before a subcommand to show them. Unpriced-model
warnings and fatal errors remain visible.

## Pricing and accounting

Edit `pricing.toml` to change budgets, paths, prices or model aliases. Prices
are USD per one million tokens and are static by design.

Every short/long context price band requires both `cache_write_input` (30-minute
writes) and `cache_write_input_12h` (12-hour writes). Existing custom configurations
must add the latter from their provider's rate card. For example:

```toml
[models."gpt-6.2-sol".short]
input = "2.00"
cached_input = "0.10"
cache_write_input = "2.50"
cache_write_input_12h = "5.00"
output = "10.00"
```

The bundled prices use [Standard API pricing](https://developers.openai.com/api/docs/pricing),
checked on 17 November 2026. The context band is selected separately for each
request; only the cache-write rate changes between scenarios. Input and output
prices are not doubled to produce the upper bound.

The API reports cache writes separately by lifetime, but the Codex logs inspected
here store only a combined count. The tracker therefore estimates a range; it
cannot recover the exact lifetime mix. These estimates cover recorded, priced
activity and do not account for deleted logs or different proxy billing.
All-time reports apply the configured static prices across recorded history;
they do not reconstruct historical invoices or the availability of cache options.

Requests are priced from `token_count.info.last_token_usage`. Repeated status
snapshots with unchanged cumulative usage are counted once;
`total_token_usage` is used only to identify those repeats, never as a charge.
Subagent requests are included at their own model's rates, alongside parent
requests. Input is split
into mutually exclusive uncached, cached and cache-write categories; reasoning
tokens are already included in output tokens and are not charged again.

The Models table counts user messages as **Prompts**, recognising both legacy
`user_message` events and newer `response_item` message records without counting
both copies twice. Injected instructions, goal continuation context and automated
subagent messages are excluded. Internal API
requests are intentionally hidden from the normal interface because agentic
workflows can make several model calls for one prompt; they remain available as
`diagnostics.api_requests` in JSON output and continue to drive token costs.

If a model has no configured rate, its activity and tokens remain visible but
its cost is excluded. The CLI marks the local estimate as partial, including
the optional range when shown; provider-reported usage is independent.

Reports support the `month` and `all` periods. JSON preserves `cost_usd` as the
default estimate and adds `cost_upper_usd` and `cost_mean_usd` to the report and
each model. Monetary values are decimal strings with full calculation precision.
The default `--json` output contains a single `month` object; activity statistics
and parser diagnostics remain available in JSON even when hidden visually.
