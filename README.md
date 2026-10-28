# nautilus-angel

NSE / NFO trading on [Nautilus Trader](https://nautilustrader.io) with Angel One SmartAPI:
backtesting first, then paper trading, then live trading.

## Setup

```bash
uv sync
```

Creates `.venv/` with Python 3.12 and the pinned Nautilus Trader version.

## Layout

| Path | Purpose |
|---|---|
| `data/historical/` | Your historical Parquet files: one expiry per file, 1-minute bars (git-ignored) |
| `catalog/` | Nautilus `ParquetDataCatalog` built from them (generated, git-ignored) |
| `src/nautilus_angel/` | The project's Python package |
