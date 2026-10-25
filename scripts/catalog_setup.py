"""
Load per-expiry Parquet files of 1-minute bars into the Nautilus ParquetDataCatalog.

Usage:
    uv run python scripts/catalog_setup.py [PATH ...]     (default: data/historical/)

Each file holds one expiry of NIFTY or BANKNIFTY: every strike's CE/PE (and maybe the
future) as rows of: timestamp, symbol, open, high, low, close, volume, oi.
Extra columns are ignored. Timestamps mark the bar's OPEN, in IST.

For each symbol this writes an instrument (nautilus_angel.instruments.factory) and its
bars. Re-running is safe: files already in the catalog are skipped.
"""

import argparse
from collections import Counter
from functools import reduce
from math import gcd
from pathlib import Path

import pandas as pd

from nautilus_trader.model.data import BarType
from nautilus_trader.model.instruments import FuturesContract
from nautilus_trader.model.instruments import OptionContract
from nautilus_trader.persistence.catalog import ParquetDataCatalog
from nautilus_trader.persistence.wranglers import BarDataWrangler

from nautilus_angel.config import CATALOG_PATH
from nautilus_angel.config import HISTORICAL_DATA_PATH
from nautilus_angel.instruments.factory import make_instrument


IST = "Asia/Kolkata"
COLUMNS = ["timestamp", "symbol", "open", "high", "low", "close", "volume", "oi"]
OHLCV = ["open", "high", "low", "close", "volume"]

# Nautilus stamps a bar when it closes; the files stamp it when it opens.
BAR_LENGTH = pd.Timedelta(minutes=1)

# NSE monthly futures trade for about 3 months. A "FUT" series reaching further back
# than this is a continuous front-month series under one name, not that contract.
MAX_FUTURE_LIFE = pd.Timedelta(days=100)


def main() -> None:
    parser = argparse.ArgumentParser(description="Load Parquet files into the catalog.")
    parser.add_argument("paths", nargs="*", type=Path, default=[HISTORICAL_DATA_PATH])
    args = parser.parse_args()

    files = [f for path in args.paths for f in (sorted(path.rglob("*.parquet")) if path.is_dir() else [path])]
    if not files:
        raise SystemExit(f"No .parquet files found in {', '.join(map(str, args.paths))}")

    catalog = ParquetDataCatalog(str(CATALOG_PATH))
    print(f"Catalog: {CATALOG_PATH}\nFiles:   {len(files)}\n")
    for path in files:
        load_file(path, catalog)
    print("Done.")


def load_file(path: Path, catalog: ParquetDataCatalog) -> None:
    print(f"== {path.name}")
    df = read_bars(path)

    written: Counter = Counter()
    notes: list[str] = []
    for symbol, group in df.groupby("symbol", sort=False):
        instrument = make_instrument(symbol, activation_ns=group["timestamp"].iloc[0].value)

        if isinstance(instrument, FuturesContract):
            expiry = pd.Timestamp(instrument.expiration_ns, tz="UTC")
            if group["timestamp"].iloc[0] < expiry - MAX_FUTURE_LIFE:
                notes.append(f"skipped {symbol}: continuous front-month series, not this contract")
                continue

        if isinstance(instrument, (FuturesContract, OptionContract)):
            note = check_lot_size(symbol, int(instrument.lot_size.as_double()), group["oi"])
            if note:
                notes.append(note)

        bar_type = BarType.from_str(f"{instrument.id}-1-MINUTE-LAST-EXTERNAL")
        frame = group.set_index("timestamp")[OHLCV].astype("float64")
        bars = BarDataWrangler(bar_type, instrument).process(frame)

        catalog.write_data([instrument])
        catalog.write_data(bars)
        written[type(instrument).__name__] += 1

    print(f"   written: {dict(written)}, {len(df):,} rows")
    for note in notes:
        print(f"   {note}")
    print()


def read_bars(path: Path) -> pd.DataFrame:
    """Read one file, clean it, and stamp each bar at its close (UTC)."""
    df = pd.read_parquet(path, columns=COLUMNS)

    timestamps = pd.to_datetime(df["timestamp"])
    if timestamps.dt.tz is None:
        timestamps = timestamps.dt.tz_localize(IST)
    df["timestamp"] = timestamps.dt.tz_convert("UTC") + BAR_LENGTH

    # Drop unusable rows; repair the rest so Nautilus accepts every bar.
    df = df[(df[["open", "high", "low", "close"]] > 0).all(axis=1)].copy()
    df["volume"] = df["volume"].clip(lower=0)
    df["high"] = df[["open", "high", "low", "close"]].max(axis=1)
    df["low"] = df[["open", "high", "low", "close"]].min(axis=1)

    # The catalog requires each instrument's bars in time order, without duplicates.
    df = df.drop_duplicates(["symbol", "timestamp"], keep="last")
    return df.sort_values(["symbol", "timestamp"], kind="stable").reset_index(drop=True)


def check_lot_size(symbol: str, lot_size: int, oi: pd.Series) -> str | None:
    """OI (in units) changes only by whole lots, so the GCD of its changes is a multiple of the lot."""
    changes = [abs(int(change)) for change in oi.diff().dropna() if change != 0]
    if not changes:
        return None
    step = reduce(gcd, changes)
    if step % lot_size != 0:
        return f"WARNING {symbol}: lot size {lot_size}, but OI changes in steps of {step}"
    return None


if __name__ == "__main__":
    main()
