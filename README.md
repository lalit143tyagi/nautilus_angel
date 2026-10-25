# nautilus-angel

Backtesting NSE / NFO strategies on [Nautilus Trader](https://nautilustrader.io), built
towards paper and live trading through Angel One SmartAPI.

## Setup

```bash
uv sync
```

## Workflow

1. Put your per-expiry Parquet files (1-minute bars) in `data/historical/`.
2. Build the catalog (instruments + bars):
   ```bash
   uv run python scripts/catalog_setup.py
   ```
3. Edit the SETTINGS block in `scripts/run_backtest.py`, then:
   ```bash
   uv run python scripts/run_backtest.py
   ```

## Layout

| Path | Purpose |
|---|---|
| `src/nautilus_angel/config.py` | Folder paths and venue names |
| `src/nautilus_angel/instruments/factory.py` | Angel One symbol -> Nautilus instrument (lot sizes, ticks, expiry, margin) |
| `src/nautilus_angel/fees.py` | Angel One charges as a Nautilus fee model |
| `src/nautilus_angel/strategies/` | Strategies |
| `scripts/catalog_setup.py` | Parquet files -> catalog |
| `scripts/run_backtest.py` | Backtest runner (Nautilus high-level template) |
| `data/historical/` | Your input files (git-ignored) |
| `catalog/`, `reports/` | Generated (git-ignored) |
