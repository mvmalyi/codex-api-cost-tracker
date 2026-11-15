# Codex Usage Tracker

A local-only macOS CLI that estimates OpenAI API cost from Codex JSONL rollout files. It never sends session data over the network and never reads message content beyond identifying prompt events.

## Who is this for?

This tool is designed specifically for people who interact with OpenAI API using Codex through **managed, institutional, or enterprise proxy keys** (such as keys issued by universities or companies) with monthly or annual quotas but no direct access to the OpenAI API dashboard.

![screenshot](screenshot.png)

## Setup

```bash
uv tool install .
codex-usage
```

`uv tool install` keeps the installed CLI independent of the project folder. Re-run `uv tool install --force .` after changing the source code.

The default dashboard compares the current calendar month and trailing 30 days
with the two budgets in `pricing.toml`.

Useful commands:

```bash
codex-usage watch
codex-usage report --period 30d
codex-usage report --period all --model gpt-5.6-terra
codex-usage report --period month --json
codex-usage models
codex-usage doctor
```

For development and testing:

```bash
uv sync --group dev --no-editable
uv run --no-sync pytest
```

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
report diagnostics.

## Pricing and accounting

Edit `pricing.toml` to change budgets, paths, prices or model aliases. Prices
are USD per one million tokens and are static by design.

Each `token_count.info.last_token_usage` record is one model request. The
cumulative `total_token_usage` field is intentionally ignored. Input is split
into mutually exclusive uncached, cached and cache-write categories; reasoning
tokens are already included in output tokens and are not charged again.

The visual dashboard counts `user_message` events as **Prompts**. Internal API
requests are intentionally hidden from the normal interface because agentic
workflows can make several model calls for one prompt; they remain available as
`diagnostics.api_requests` in JSON output and continue to drive token costs.

If a model has no configured rate, its activity and tokens remain visible but
its cost is excluded. The CLI marks the resulting monetary total as partial.
