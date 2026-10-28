"""
Check the Nautilus catalog against the historical Parquet files it was built from.

Usage:
    uv run python scripts/verify_catalog.py [PATH ...]     (default: data/historical/)

Reads the catalog the way a backtest will (ParquetDataCatalog) and, independently of
build_catalog.py, recomputes from the raw files and constants.py what it should hold:
    instruments  lot size, tick, expiry time (NFO close on the expiry day), strike
    bars         count per instrument, and the first, a middle and the last bar's
                 timestamp (bar close) and open/high/low/close against the raw row
Prints one line per file and every mismatch; exits with an error if there is any.
"""

import argparse
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pandas as pd

from nautilus_trader.model.data import BarType
from nautilus_trader.model.instruments import FuturesContract
from nautilus_trader.model.instruments import IndexInstrument
from nautilus_trader.model.instruments import OptionContract
from nautilus_trader.persistence.catalog import ParquetDataCatalog

from nautilus_angel import constants


def main() -> None:
    parser = argparse.ArgumentParser(description="Check the catalog against the source files.")
    parser.add_argument("paths", nargs="*", type=Path, default=[constants.HISTORICAL_DATA_PATH])
    args = parser.parse_args()
    files = [f for p in args.paths for f in (sorted(p.glob("*.parquet")) if p.is_dir() else [p])]

    catalog = ParquetDataCatalog(str(constants.CATALOG_PATH))
    instruments = {instrument.raw_symbol.value: instrument for instrument in catalog.instruments()}
    print(f"Catalog: {constants.CATALOG_PATH} ({len(instruments):,} instruments)\n")

    problems = []
    for path in files:
        problems += check_file(path, catalog, instruments)

    if problems:
        print(f"\n{len(problems)} problem(s):")
        for problem in problems[:50]:
            print(f"  {problem}")
        raise SystemExit(1)
    print("\nAll checks passed.")


def check_file(path: Path, catalog: ParquetDataCatalog, instruments: dict) -> list[str]:
    raw = expected_bars(path)
    problems = []
    checked = bars_checked = 0

    for symbol, rows in raw.groupby("symbol", sort=False):
        instrument = instruments.get(symbol)
        if instrument is None:
            if not symbol.endswith("FUT"):  # continuous futures are skipped on purpose
                problems.append(f"{path.name}: {symbol} is not in the catalog")
            continue

        problems += check_instrument(instrument)
        bars = catalog.bars([f"{instrument.id}-1-MINUTE-LAST-EXTERNAL"])
        if len(bars) != len(rows):
            problems.append(f"{instrument.id}: {len(bars):,} bars in the catalog, {len(rows):,} expected")
            continue
        for i in sorted({0, len(rows) // 2, len(rows) - 1}):
            problems += compare_bar(instrument.id, bars[i], rows.iloc[i])
            bars_checked += 1
        checked += 1

    print(f"{path.name:26} instruments checked: {checked:4} | bars compared: {bars_checked:5} | problems: {len(problems)}")
    return problems


def expected_bars(path: Path) -> pd.DataFrame:
    """The raw rows that should be in the catalog: in session, positive prices, one per minute."""
    raw = pd.read_parquet(path, columns=["timestamp", "symbol", "open", "high", "low", "close"])
    raw["close_time"] = raw["timestamp"].dt.tz_convert(constants.IST) + pd.Timedelta(minutes=1)
    index_names = {u.index_name for u in constants.UNDERLYINGS.values()}

    def in_session(row) -> bool:
        venue = constants.NSE if row.symbol in index_names else constants.NFO
        hours = constants.session(venue, row.close_time.date())
        return hours.open < row.close_time.time() <= hours.close

    raw = raw[(raw[["open", "high", "low", "close"]] > 0).all(axis=1)]
    raw = raw[[in_session(row) for row in raw.itertuples(index=False)]]
    raw = raw.drop_duplicates(["symbol", "close_time"], keep="last")
    return raw.sort_values(["symbol", "close_time"], kind="stable").reset_index(drop=True)


def check_instrument(instrument) -> list[str]:
    """Lot size, tick and expiry time recomputed from the symbol and constants.py."""
    if isinstance(instrument, IndexInstrument):
        ok = str(instrument.price_increment) == constants.INDEX_TICK
        return [] if ok else [f"{instrument.id}: tick {instrument.price_increment}"]

    symbol = instrument.raw_symbol.value
    expiry = datetime.strptime(symbol[len(instrument.underlying):][:7], "%d%b%y").date()
    problems = []

    lot = constants.lot_size(instrument.underlying, expiry)
    if int(instrument.lot_size.as_double()) != lot:
        problems.append(f"{instrument.id}: lot {instrument.lot_size}, expected {lot}")

    if isinstance(instrument, OptionContract):
        tick = constants.OPTION_TICK
        strike = Decimal(symbol[len(instrument.underlying) + 7 : -2])
        if instrument.strike_price.as_decimal() != strike:
            problems.append(f"{instrument.id}: strike {instrument.strike_price}, expected {strike}")
    elif isinstance(instrument, FuturesContract):
        tick = constants.futures_tick(instrument.underlying, expiry)
    if Decimal(str(instrument.price_increment)) != Decimal(tick):
        problems.append(f"{instrument.id}: tick {instrument.price_increment}, expected {tick}")

    close = constants.session(constants.NFO, expiry).close
    expected_expiry = pd.Timestamp(datetime.combine(expiry, close), tz=constants.IST)
    actual_expiry = pd.Timestamp(instrument.expiration_ns, tz="UTC").tz_convert(constants.IST)
    if actual_expiry != expected_expiry:
        problems.append(f"{instrument.id}: expires {actual_expiry}, expected {expected_expiry}")
    return problems


def compare_bar(instrument_id, bar, row) -> list[str]:
    """One catalog bar against its raw row: close-stamped time and the four prices."""
    problems = []
    bar_time = pd.Timestamp(bar.ts_event, tz="UTC").tz_convert(constants.IST)
    if bar_time != row.close_time:
        problems.append(f"{instrument_id}: bar at {bar_time}, expected {row.close_time}")
    for field in ("open", "high", "low", "close"):
        expected = round(getattr(row, field), 2)
        actual = getattr(bar, field).as_double()
        # build_catalog widens high/low to contain open/close, so allow that direction only.
        widened = (field == "high" and actual >= expected) or (field == "low" and actual <= expected)
        if abs(actual - expected) > 1e-9 and not widened:
            problems.append(f"{instrument_id} {bar_time}: {field} {actual}, expected {expected}")
    return problems


if __name__ == "__main__":
    main()
