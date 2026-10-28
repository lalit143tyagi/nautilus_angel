"""
Project settings and NSE market facts, in one place.

Facts that change over time (lot sizes, market hours) are *schedules*: tuples of
(first date, value), oldest first. Read them through the lookup functions at the
bottom (lot_size, futures_tick, session, closing_auction), never by indexing them directly.
One-off trading days (Diwali Muhurat sessions) are in SPECIAL_SESSIONS.
"""

from dataclasses import dataclass
from datetime import date
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo


# --- Folders -------------------------------------------------------------------

# This file is src/nautilus_angel/constants.py, so the project root is 2 folders up.
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Input: your historical Parquet files, one expiry per file.
HISTORICAL_DATA_PATH = PROJECT_ROOT / "data" / "historical"

# Output: the Nautilus ParquetDataCatalog (instruments + bars).
CATALOG_PATH = PROJECT_ROOT / "catalog"


# --- Time ----------------------------------------------------------------------

# Indian Standard Time. Nautilus stores every timestamp in UTC; we convert at the edges.
IST = ZoneInfo("Asia/Kolkata")


# --- Venues --------------------------------------------------------------------

# One Nautilus venue per Angel One exchange segment (Angel One's "exch_seg").
NSE = "NSE"  # cash market: equities and indices
NFO = "NFO"  # equity derivatives: index and stock futures and options

CURRENCY = "INR"

# Minimum price steps, as text so they convert exactly (0.05 is not exact as a float).
# Index futures ticks change over time: see Underlying.futures_ticks below.
OPTION_TICK = "0.05"  # NFO index options
INDEX_TICK = "0.01"  # index values


# --- Market sessions -----------------------------------------------------------


@dataclass(frozen=True)
class Session:
    """Continuous-trading hours of one trading day, in IST."""

    open: time
    close: time


# NFO: the close moved from 15:30 to 15:40 on 2026-08-03, to align with the new
# cash-market Closing Auction Session (NSE circular; visible in the data: the last
# 1-minute bar opens at 15:30 up to 2026-07-31 and at 15:39 from 2026-08-03).
NFO_SESSIONS: tuple[tuple[date, Session], ...] = (
    (date(2015, 1, 1), Session(open=time(9, 15), close=time(15, 30))),
    (date(2026, 8, 3), Session(open=time(9, 15), close=time(15, 40))),
)

# NSE cash market: continuous trading is unchanged at 09:15-15:30.
NSE_SESSIONS: tuple[tuple[date, Session], ...] = (
    (date(2015, 1, 1), Session(open=time(9, 15), close=time(15, 30))),
)

# One-off sessions that replace the normal hours on both NSE and NFO that day:
# Diwali "Muhurat" trading (one short session, usually in the evening) and the
# 2021-02-24 extended session after NSE's trading outage. All appear in the data.
SPECIAL_SESSIONS: dict[date, Session] = {
    date(2015, 11, 11): Session(open=time(17, 45), close=time(18, 45)),
    date(2016, 10, 30): Session(open=time(18, 30), close=time(19, 30)),
    date(2017, 10, 19): Session(open=time(18, 30), close=time(19, 30)),
    date(2018, 11, 7): Session(open=time(17, 30), close=time(18, 30)),
    date(2019, 10, 27): Session(open=time(18, 15), close=time(19, 15)),
    date(2020, 11, 14): Session(open=time(18, 15), close=time(19, 15)),
    date(2021, 2, 24): Session(open=time(9, 15), close=time(17, 0)),
    date(2021, 11, 4): Session(open=time(18, 15), close=time(19, 15)),
    date(2022, 10, 24): Session(open=time(18, 15), close=time(19, 15)),
    date(2023, 11, 12): Session(open=time(18, 15), close=time(19, 15)),
    date(2024, 11, 1): Session(open=time(18, 0), close=time(19, 0)),
    date(2025, 10, 21): Session(open=time(13, 45), close=time(14, 45)),
}

# Closing Auction Session (cash market, phase 1: only stocks that have derivatives).
# It sets those stocks' official closing price. None = no closing auction.
CLOSING_AUCTIONS: tuple[tuple[date, Session | None], ...] = (
    (date(2015, 1, 1), None),
    (date(2026, 8, 3), Session(open=time(15, 15), close=time(15, 35))),
)


# --- Underlyings ---------------------------------------------------------------


@dataclass(frozen=True)
class Underlying:
    """An index whose futures and options we trade."""

    symbol: str  # as in option symbols and Angel One's "name", e.g. "BANKNIFTY"
    index_name: str  # as in the data files and Angel One's index "symbol", e.g. "Nifty Bank"
    strike_step: int  # gap between neighbouring listed strikes
    lot_sizes: tuple[tuple[date, int], ...]  # by contract EXPIRY date: a contract keeps its listing lot
    # Index futures tick, by date. From 2025-04-15 NSE links it to the index level
    # (<15,000: 0.05; 15,000-30,000: 0.10; >30,000: 0.20); the value here is the band
    # this index has traded in since then.
    futures_ticks: tuple[tuple[date, str], ...]


NIFTY = Underlying(
    symbol="NIFTY",
    index_name="Nifty 50",
    strike_step=50,
    lot_sizes=(
        (date(2015, 1, 1), 75),
        (date(2021, 7, 29), 50),
        (date(2024, 5, 2), 25),
        (date(2025, 1, 2), 75),
        (date(2026, 1, 6), 65),
    ),
    futures_ticks=(
        (date(2015, 1, 1), "0.05"),
        (date(2025, 4, 15), "0.10"),
    ),
)

BANKNIFTY = Underlying(
    symbol="BANKNIFTY",
    index_name="Nifty Bank",
    strike_step=100,
    lot_sizes=(
        (date(2015, 1, 1), 25),
        (date(2015, 8, 28), 30),
        (date(2016, 4, 29), 40),
        (date(2018, 10, 26), 20),
        (date(2020, 7, 30), 25),
        (date(2023, 7, 27), 15),
        (date(2024, 11, 20), 30),
        (date(2025, 7, 1), 35),
        (date(2026, 1, 1), 30),
    ),
    futures_ticks=(
        (date(2015, 1, 1), "0.05"),
        (date(2025, 4, 15), "0.20"),
    ),
)

# Look up by symbol: UNDERLYINGS["BANKNIFTY"].
UNDERLYINGS = {underlying.symbol: underlying for underlying in (NIFTY, BANKNIFTY)}


# --- Lookups -------------------------------------------------------------------


def in_force[T](schedule: tuple[tuple[date, T], ...], on: date) -> T:
    """The value of a (first date, value) schedule on the date `on`."""
    value_now = None
    found = False
    for first_date, value in schedule:
        if first_date <= on:
            value_now = value
            found = True
    if not found:
        raise ValueError(f"{on} is before this schedule starts ({schedule[0][0]})")
    return value_now


def lot_size(underlying: str, expiry: date) -> int:
    """Units per lot for a contract of `underlying` that expires on `expiry`."""
    if underlying not in UNDERLYINGS:
        raise ValueError(f"Unknown underlying {underlying!r}; known: {sorted(UNDERLYINGS)}")
    return in_force(UNDERLYINGS[underlying].lot_sizes, expiry)


def futures_tick(underlying: str, on: date) -> str:
    """Minimum price step of `underlying`'s index futures on the date `on`."""
    if underlying not in UNDERLYINGS:
        raise ValueError(f"Unknown underlying {underlying!r}; known: {sorted(UNDERLYINGS)}")
    return in_force(UNDERLYINGS[underlying].futures_ticks, on)


def session(venue: str, on: date) -> Session:
    """Continuous-trading hours of `venue` (NSE or NFO) on the date `on`."""
    schedules = {NSE: NSE_SESSIONS, NFO: NFO_SESSIONS}
    if venue not in schedules:
        raise ValueError(f"Unknown venue {venue!r}; known: {sorted(schedules)}")
    if on in SPECIAL_SESSIONS:
        return SPECIAL_SESSIONS[on]
    return in_force(schedules[venue], on)


def closing_auction(on: date) -> Session | None:
    """The cash market's Closing Auction Session on the date `on`, or None if there was none."""
    return in_force(CLOSING_AUCTIONS, on)
