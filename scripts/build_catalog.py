"""
Build the Nautilus ParquetDataCatalog from your historical Parquet files.

Usage:
    uv run python scripts/build_catalog.py [PATH ...]     (default: data/historical/)

Each input file holds 1-minute bars, one row per symbol per minute, with the columns
    timestamp, symbol, open, high, low, close, volume, oi     (extra columns are ignored)
where `timestamp` is the minute the bar OPENED. Index files hold one index ("Nifty Bank").

For every symbol this writes its instrument (AngelOneInstrumentProvider) and its bars.
Re-running is safe: catalog files that already exist are skipped. After changing how
instruments are built (e.g. a lot size in constants.py), delete catalog/ and rebuild.
"""

import argparse
from collections import Counter
from functools import reduce
from math import gcd
from pathlib import Path

import pandas as pd

from nautilus_trader.model.data import BarType
from nautilus_trader.model.instruments import FuturesContract
from nautilus_trader.model.instruments import IndexInstrument
from nautilus_trader.model.instruments import Instrument
from nautilus_trader.persistence.catalog import ParquetDataCatalog
from nautilus_trader.persistence.wranglers import BarDataWrangler

from nautilus_angel import constants
from nautilus_angel.adapters.angelone.providers import AngelOneInstrumentProvider


COLUMNS = ["timestamp", "symbol", "open", "high", "low", "close", "volume", "oi"]
PRICES = ["open", "high", "low", "close"]

# The files stamp a bar when it opens; Nautilus stamps it when it closes.
BAR_LENGTH = pd.Timedelta(minutes=1)

# NSE monthly futures trade for about 3 months. A "FUT" series starting further back
# than this before its expiry is a continuous front-month series under one name.
MAX_FUTURE_LIFE = pd.Timedelta(days=100)


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the Nautilus catalog from Parquet files.")
    parser.add_argument("paths", nargs="*", type=Path, default=[constants.HISTORICAL_DATA_PATH])
    args = parser.parse_args()

    files = []
    for path in args.paths:
        files.extend(sorted(path.glob("*.parquet")) if path.is_dir() else [path])
    if not files:
        raise SystemExit(f"No .parquet files in {', '.join(map(str, args.paths))}")

    catalog = ParquetDataCatalog(str(constants.CATALOG_PATH))
    provider = AngelOneInstrumentProvider()
    print(f"Catalog: {constants.CATALOG_PATH}\nFiles:   {len(files)}\n")

    total: Counter = Counter()
    for path in files:
        total += load_file(path, catalog, provider)
    print(f"Done: {sum(total.values()):,} instruments written: {dict(total)}")


def load_file(path: Path, catalog: ParquetDataCatalog, provider: AngelOneInstrumentProvider) -> Counter:
    """Write one file's instruments and bars; return how many instruments of each kind."""
    print(f"== {path.name}")
    df, dropped = read_bars(path)

    written: Counter = Counter()
    bar_count = 0
    notes: list[str] = []
    for symbol, bars in df.groupby("symbol", sort=False):
        instrument = provider.add_from_symbol(symbol, activation_ns=bars["timestamp"].iloc[0].value)

        if isinstance(instrument, FuturesContract):
            expiry = pd.Timestamp(instrument.expiration_ns, tz="UTC")
            if bars["timestamp"].iloc[0] < expiry - MAX_FUTURE_LIFE:
                notes.append(f"skipped {symbol}: continuous front-month series, not this contract")
                continue

        if not isinstance(instrument, IndexInstrument):
            note = check_lot_size(instrument, bars["oi"])
            if note:
                notes.append(note)

        write_bars(catalog, instrument, bars)
        written[type(instrument).__name__] += 1
        bar_count += len(bars)

    print(f"   written: {dict(written)}, {bar_count:,} bars")
    for reason, count in dropped.items():
        if count:
            print(f"   dropped {count:,} bars: {reason}")
    note = volume_unit_note(df)
    if note:
        print(f"   NOTE {note}")
    for note in notes:
        print(f"   {note}")
    print()
    return written


def read_bars(path: Path) -> tuple[pd.DataFrame, dict[str, int]]:
    """Read one file, stamp bars at their close (UTC), and drop unusable rows."""
    df = pd.read_parquet(path, columns=COLUMNS)
    dropped = {}

    timestamps = pd.to_datetime(df["timestamp"])
    if timestamps.dt.tz is None:
        timestamps = timestamps.dt.tz_localize(constants.IST)
    df["timestamp"] = timestamps.dt.tz_convert("UTC") + BAR_LENGTH

    bad_price = (df[PRICES] <= 0).any(axis=1) | df[PRICES].isna().any(axis=1)
    dropped["missing or non-positive prices"] = int(bad_price.sum())
    df = df[~bad_price].copy()

    outside = ~in_session(df)
    dropped["outside the trading session (glitches, pre-open prints)"] = int(outside.sum())
    df = df[~outside].copy()

    # Repair rather than drop: Nautilus rejects a bar whose high/low don't contain open/close.
    df["volume"] = df["volume"].clip(lower=0)
    df["high"] = df[PRICES].max(axis=1)
    df["low"] = df[PRICES].min(axis=1)

    before = len(df)
    df = df.drop_duplicates(["symbol", "timestamp"], keep="last")
    dropped["duplicate (symbol, minute) rows"] = before - len(df)

    # The catalog needs each instrument's bars in time order.
    df = df.sort_values(["symbol", "timestamp"], kind="stable").reset_index(drop=True)
    return df, dropped


def in_session(df: pd.DataFrame) -> pd.Series:
    """True for bars that close inside their venue's session that day (open < close <= close)."""
    local = df["timestamp"].dt.tz_convert(constants.IST)
    days = local.dt.date
    is_index = df["symbol"].isin([u.index_name for u in constants.UNDERLYINGS.values()])
    venue = is_index.map({True: constants.NSE, False: constants.NFO})

    # Look each (venue, day) up once, not once per bar.
    pairs = pd.DataFrame({"venue": venue, "day": days}).drop_duplicates()
    hours = {
        (row.venue, row.day): constants.session(row.venue, row.day)
        for row in pairs.itertuples(index=False)
    }
    opens = [hours[(v, d)].open for v, d in zip(venue, days)]
    closes = [hours[(v, d)].close for v, d in zip(venue, days)]
    bar_time = local.dt.time
    return pd.Series(
        [o < t <= c for o, t, c in zip(opens, bar_time, closes)],
        index=df.index,
    )


def check_lot_size(instrument: Instrument, oi: pd.Series) -> str | None:
    """
    Open interest (in units) only changes by whole lots, so the greatest common divisor
    of its changes must be a multiple of the lot size. Warn when it isn't.
    """
    step = gcd_of_nonzero(oi.diff())
    lot = int(instrument.lot_size.as_double())
    if step and step % lot != 0:
        return f"WARNING {instrument.id}: lot size {lot}, but OI changes in steps of {step}"
    return None


def volume_unit_note(df: pd.DataFrame) -> str | None:
    """Note when volume doesn't move in whole lots the way OI does (i.e. isn't in units)."""
    oi_step = gcd_of_nonzero(df.groupby("symbol")["oi"].diff())
    volume_step = gcd_of_nonzero(df["volume"])
    if oi_step and volume_step and volume_step % oi_step != 0:
        return (
            f"volume moves in steps of {volume_step} while OI moves in steps of {oi_step}: "
            "volume is probably not in units (stored as given)"
        )
    return None


def gcd_of_nonzero(values: pd.Series) -> int | None:
    """Greatest common divisor of the non-zero whole numbers in `values`; None if there are none."""
    numbers = [abs(int(value)) for value in values.dropna() if value != 0]
    return reduce(gcd, numbers) if numbers else None


def write_bars(catalog: ParquetDataCatalog, instrument: Instrument, bars: pd.DataFrame) -> None:
    """Write `instrument` and its 1-minute bars to the catalog."""
    bar_type = BarType.from_str(f"{instrument.id}-1-MINUTE-LAST-EXTERNAL")
    frame = bars.set_index("timestamp")[[*PRICES, "volume"]].astype("float64")
    nautilus_bars = BarDataWrangler(bar_type, instrument).process(frame)
    catalog.write_data([instrument])
    catalog.write_data(nautilus_bars)


if __name__ == "__main__":
    main()
