"""
Load OHLCV Parquet files into the Nautilus ParquetDataCatalog.

Usage:
    uv run python scripts/catalog_setup.py [PATH ...]
        [--symbol NAME] [--lot-size UNDERLYING=N ...] [--stamped-at-close]

PATH can be a .parquet file or a folder of them (default: data/historical/). Column names are matched
flexibly (e.g. timestamp/datetime, or separate date + time columns), timestamps
in any timezone are accepted (naive ones are treated as IST), and each symbol
becomes an instrument (see nautilus_angel.instruments.factory) plus its bars.

Re-running on the same file is safe: files already in the catalog are skipped.
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
from nautilus_angel.instruments import factory


IST = "Asia/Kolkata"

# Accepted spellings for each column we need (compared in lower case).
COLUMN_ALIASES = {
    "timestamp": ["timestamp", "datetime", "date_time", "ts", "time_stamp"],
    "symbol": ["symbol", "tradingsymbol", "trading_symbol", "ticker", "instrument"],
    "open": ["open", "o"],
    "high": ["high", "h"],
    "low": ["low", "l"],
    "close": ["close", "c", "ltp"],
    "volume": ["volume", "vol", "v", "qty"],
    "oi": ["oi", "open_interest", "openinterest"],
}

OHLC = ["open", "high", "low", "close"]

# NSE monthly futures trade for about 3 months. A "FUT" series reaching further back
# than this is a continuous front-month series under one name, not that contract.
MAX_FUTURE_LIFE = pd.Timedelta(days=100)


def main() -> None:
    args = parse_args()
    # A forced lot applies to every expiry of that underlying in this run.
    for underlying, size in args.lot_size.items():
        factory.LOT_SIZE_HISTORY[underlying] = [("1900-01-01", size)]

    catalog = ParquetDataCatalog(str(CATALOG_PATH))
    files = find_parquet_files(args.paths)
    print(f"Catalog: {CATALOG_PATH}")
    print(f"Files:   {len(files)}\n")
    if not files:
        raise SystemExit(f"No .parquet files found in {', '.join(map(str, args.paths))}")

    for path in files:
        load_file(path, catalog, args)

    print("Done.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Load OHLCV Parquet files into the catalog.")
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        default=[HISTORICAL_DATA_PATH],
        help=f"Parquet files or folders (default: {HISTORICAL_DATA_PATH})",
    )
    parser.add_argument(
        "--symbol",
        help="Symbol to use when a file has no symbol column (default: the file name)",
    )
    parser.add_argument(
        "--lot-size",
        action="append",
        default=[],
        type=parse_lot_size,
        metavar="UNDERLYING=N",
        help="Force one lot size for every expiry of an underlying, e.g. --lot-size BANKNIFTY=35 (repeatable)",
    )
    parser.add_argument(
        "--stamped-at-close",
        action="store_true",
        help="Timestamps already mark the bar's close (default: they mark its open)",
    )
    args = parser.parse_args()
    args.lot_size = dict(args.lot_size)
    return args


def parse_lot_size(text: str) -> tuple[str, int]:
    name, _, size = text.partition("=")
    if not name or not size.isdigit():
        raise argparse.ArgumentTypeError(f"expected UNDERLYING=N, got {text!r}")
    return name.upper(), int(size)


def find_parquet_files(paths: list[Path]) -> list[Path]:
    files = []
    for path in paths:
        if path.is_dir():
            files.extend(sorted(path.rglob("*.parquet")))
        elif path.suffix == ".parquet":
            files.append(path)
        else:
            raise SystemExit(f"Not a .parquet file or folder: {path}")
    return files


def load_file(path: Path, catalog: ParquetDataCatalog, args: argparse.Namespace) -> None:
    print(f"== {path.name}")
    df = read_bars(path, args.symbol)
    df, fixes = clean(df)
    minutes = infer_bar_minutes(df)

    # Nautilus stamps a bar when it closes; shift open-stamped bars by one bar length.
    if not args.stamped_at_close:
        df["timestamp"] += pd.Timedelta(minutes=minutes)

    kinds: Counter = Counter()
    skipped: list[str] = []
    warnings: list[str] = []
    bar_count = 0

    for symbol, group in df.groupby("symbol", sort=False):
        first_ns = group["timestamp"].iloc[0].value
        try:
            instrument = factory.make_instrument(symbol, activation_ns=first_ns)
        except ValueError as error:
            skipped.append(f"{symbol}: {error}")
            continue

        if isinstance(instrument, FuturesContract):
            expiry = pd.Timestamp(instrument.expiration_ns, tz="UTC")
            if group["timestamp"].iloc[0] < expiry - MAX_FUTURE_LIFE:
                skipped.append(
                    f"{symbol}: continuous front-month series from "
                    f"{group['timestamp'].iloc[0].date()}, not this contract"
                )
                continue

        if isinstance(instrument, (FuturesContract, OptionContract)):
            warning = check_lot_size(symbol, int(instrument.lot_size.as_double()), group)
            if warning:
                warnings.append(warning)

        bar_type = BarType.from_str(f"{instrument.id}-{minutes}-MINUTE-LAST-EXTERNAL")
        frame = group.set_index("timestamp")[OHLC + ["volume"]].astype("float64")
        bars = BarDataWrangler(bar_type, instrument).process(frame)

        try:
            catalog.write_data([instrument])
            catalog.write_data(bars)
        except ValueError as error:
            skipped.append(f"{symbol}: {error}")
            continue

        kinds[type(instrument).__name__] += 1
        bar_count += len(bars)

    print(f"   bar size: {minutes} minute(s), rows after cleaning: {len(df):,}")
    note = volume_unit_note(df)
    if note:
        print(f"   NOTE {note}")
    for name, count in fixes.items():
        if count:
            print(f"   fixed: {name}: {count:,}")
    print(f"   written: {dict(kinds)}, {bar_count:,} bars")
    for reason in skipped:
        print(f"   skipped {reason}")
    for warning in warnings:
        print(f"   WARNING {warning}")
    print()


def check_lot_size(symbol: str, lot_size: int, group: pd.DataFrame) -> str | None:
    """
    Open interest (in units) only ever changes by whole lots, so the greatest common
    divisor (GCD) of its changes is a multiple of the true lot. Warn when it isn't a
    multiple of the lot we chose. Files without OI fall back to volume.
    """
    if group["oi"].notna().any():
        source, steps = "OI changes", group["oi"].diff()
    else:
        source, steps = "volumes", group["volume"]
    step = gcd_of_nonzero(steps)
    if step and step % lot_size != 0:
        return f"{symbol}: lot size {lot_size}, but {source} move in steps of {step}"
    return None


def volume_unit_note(df: pd.DataFrame) -> str | None:
    """Say so when volume doesn't move in whole lots like OI does (i.e. isn't in units)."""
    oi_step = gcd_of_nonzero(df.groupby("symbol")["oi"].diff())
    volume_step = gcd_of_nonzero(df["volume"])
    if oi_step and volume_step and volume_step % oi_step != 0:
        return (
            f"volume moves in steps of {volume_step} while OI moves in steps of {oi_step}: "
            "volume is probably not in units (stored as given)"
        )
    return None


def gcd_of_nonzero(values: pd.Series) -> int | None:
    """GCD of the non-zero whole-number values (sign ignored); None if there are none."""
    numbers = [abs(int(value)) for value in values.dropna() if value != 0]
    return reduce(gcd, numbers) if numbers else None


def read_bars(path: Path, default_symbol: str | None) -> pd.DataFrame:
    """Read one file into columns: timestamp (UTC), symbol, open, high, low, close, volume."""
    df = pd.read_parquet(path)

    # A timestamp stored as the index becomes a normal column.
    if isinstance(df.index, pd.DatetimeIndex):
        df = df.reset_index()

    df.columns = [str(column).strip().lower() for column in df.columns]
    df = df.rename(columns=alias_map(df.columns))

    if "timestamp" not in df.columns and {"date", "time"} <= set(df.columns):
        df["timestamp"] = pd.to_datetime(df["date"].astype(str) + " " + df["time"].astype(str))
    elif "timestamp" not in df.columns and "date" in df.columns:
        df["timestamp"] = df["date"]

    missing = [name for name in ["timestamp", *OHLC] if name not in df.columns]
    if missing:
        raise SystemExit(f"{path.name}: missing columns {missing}; found {list(df.columns)}")

    if "symbol" not in df.columns:
        df["symbol"] = default_symbol or path.stem
    if "volume" not in df.columns:
        df["volume"] = 0
    if "oi" not in df.columns:
        df["oi"] = float("nan")  # optional: only used to check lot sizes

    df["timestamp"] = to_utc(df["timestamp"])
    for name in [*OHLC, "volume", "oi"]:
        df[name] = pd.to_numeric(df[name], errors="coerce")
    df["symbol"] = df["symbol"].astype(str).str.strip()

    return df[["timestamp", "symbol", *OHLC, "volume", "oi"]]


def alias_map(columns: list[str]) -> dict[str, str]:
    """Map each column found in the file to our standard name (first alias wins)."""
    mapping = {}
    for standard, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            if alias in columns and standard not in columns:
                mapping[alias] = standard
                break
    return mapping


def to_utc(values: pd.Series) -> pd.Series:
    """Parse timestamps; naive ones are treated as IST. Returns UTC."""
    if pd.api.types.is_numeric_dtype(values):
        raise SystemExit("Numeric timestamps are not supported yet: store them as datetimes")
    timestamps = pd.to_datetime(values)
    if timestamps.dt.tz is None:
        timestamps = timestamps.dt.tz_localize(IST)
    return timestamps.dt.tz_convert("UTC")


def clean(df: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, int]]:
    """Drop or repair bad rows. Returns the cleaned frame and counts of each fix."""
    fixes = {}

    bad_values = df[[*OHLC, "volume"]].isna().any(axis=1) | (df[OHLC] <= 0).any(axis=1)
    fixes["rows with missing or non-positive prices dropped"] = int(bad_values.sum())
    df = df[~bad_values].copy()

    fixes["negative volumes set to 0"] = int((df["volume"] < 0).sum())
    df["volume"] = df["volume"].clip(lower=0)

    true_high = df[OHLC].max(axis=1)
    true_low = df[OHLC].min(axis=1)
    fixes["high/low widened to contain open/close"] = int(
        ((df["high"] != true_high) | (df["low"] != true_low)).sum()
    )
    df["high"] = true_high
    df["low"] = true_low

    before = len(df)
    df = df.drop_duplicates(["symbol", "timestamp"], keep="last")
    fixes["duplicate rows dropped"] = before - len(df)

    df = df.sort_values(["symbol", "timestamp"], kind="stable").reset_index(drop=True)
    return df, fixes


def infer_bar_minutes(df: pd.DataFrame) -> int:
    """The bar size is the smallest gap between two bars of the same symbol."""
    gaps = df.groupby("symbol")["timestamp"].diff().dropna()
    gaps = gaps[gaps > pd.Timedelta(0)]
    if gaps.empty:
        raise SystemExit("Cannot infer the bar size: every symbol has a single bar")
    minutes = gaps.min() / pd.Timedelta(minutes=1)
    if minutes != int(minutes):
        raise SystemExit(f"Bar size {gaps.min()} is not a whole number of minutes")
    return int(minutes)


if __name__ == "__main__":
    main()
