# nautilus-angel

Algorithmic trading system built on [Nautilus Trader](https://nautilustrader.io),
with a custom Angel One SmartAPI integration (built incrementally).

## Setup

```bash
uv sync
```

This creates `.venv/` with Python 3.12 and installs the pinned dependencies.

## Layout

- `src/nautilus_angel/` — importable package (config, strategies, data, instruments, adapters)
- `scripts/` — runnable entry points (catalog setup, backtests)
- `catalog/` — generated Parquet data catalog (git-ignored)
- `tests/` — tests
