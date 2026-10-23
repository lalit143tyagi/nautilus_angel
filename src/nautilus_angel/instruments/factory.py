"""
Build Nautilus instrument definitions from Angel One style trading symbols.

    BANKNIFTY26MAY2643000PE  -> OptionContract  BANKNIFTY26MAY2643000PE.NFO
    BANKNIFTY26MAY26FUT      -> FuturesContract BANKNIFTY26MAY26FUT.NFO
    Nifty Bank               -> IndexInstrument BANKNIFTY.NSE
    SBIN-EQ                  -> Equity          SBIN-EQ.NSE

Quantities are in units (shares), the same as Angel One's order quantity, so
`multiplier` is 1 and `lot_size` is the exchange lot (orders must be a multiple of it).
"""

import re

import pandas as pd

from nautilus_trader.model.currencies import INR
from nautilus_trader.model.enums import AssetClass
from nautilus_trader.model.enums import OptionKind
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.identifiers import Symbol
from nautilus_trader.model.identifiers import Venue
from nautilus_trader.model.instruments import Equity
from nautilus_trader.model.instruments import FuturesContract
from nautilus_trader.model.instruments import IndexInstrument
from nautilus_trader.model.instruments import Instrument
from nautilus_trader.model.instruments import OptionContract
from nautilus_trader.model.objects import Price
from nautilus_trader.model.objects import Quantity

from nautilus_angel.config import NFO
from nautilus_angel.config import NSE


IST = "Asia/Kolkata"

# Contracts expire at the NSE close on their expiry date.
EXPIRY_TIME = "15:30"

# Exchange lot sizes (units per lot) over time, as (first expiry date, lot size).
# A contract keeps the lot it was listed with, so the lot is chosen by EXPIRY date:
# the latest entry starting on or before it. Source: the user's NSE history table,
# plus Bank Nifty's 35 period (NSE circulars, Apr and Oct 2025).
LOT_SIZE_HISTORY: dict[str, list[tuple[str, int]]] = {
    "NIFTY": [
        ("2015-01-01", 75),
        ("2021-07-29", 50),
        ("2024-05-02", 25),
        ("2025-01-02", 75),
        ("2026-01-06", 65),
    ],
    "BANKNIFTY": [
        ("2015-01-01", 25),
        ("2015-08-28", 30),
        ("2016-04-29", 40),
        ("2018-10-26", 20),
        ("2020-07-30", 25),
        ("2023-07-27", 15),
        ("2024-11-20", 30),
        ("2025-07-01", 35),
        ("2026-01-01", 30),
    ],
}

# Index names as Angel One / the data files spell them -> our clean symbol.
INDEX_NAMES = {
    "Nifty 50": "NIFTY",
    "Nifty Bank": "BANKNIFTY",
}

# Derivatives on these underlyings are index derivatives; anything else is a stock.
INDEX_UNDERLYINGS = set(INDEX_NAMES.values())

# NAME + DD + MMM + YY + STRIKE + CE/PE, e.g. BANKNIFTY 26 MAY 26 43000 PE
OPTION_PATTERN = re.compile(
    r"^(?P<underlying>[A-Z0-9&-]+?)(?P<expiry>\d{2}[A-Z]{3}\d{2})"
    r"(?P<strike>\d+(?:\.\d+)?)(?P<kind>CE|PE)$"
)

# NAME + DD + MMM + YY + FUT, e.g. BANKNIFTY 26 MAY 26 FUT
FUTURE_PATTERN = re.compile(
    r"^(?P<underlying>[A-Z0-9&-]+?)(?P<expiry>\d{2}[A-Z]{3}\d{2})FUT$"
)

# NSE F&O and equity tick size is 0.05; index values move in 0.01.
TRADED_TICK = Price.from_str("0.05")
INDEX_TICK = Price.from_str("0.01")


def make_instrument(
    symbol: str,
    lot_size: int | None = None,
    activation_ns: int = 0,
) -> Instrument:
    """
    Return the Nautilus instrument for an Angel One trading symbol.

    `lot_size` overrides LOT_SIZE_HISTORY (needed for stock F&O).
    `activation_ns` is when the contract started trading, if known (UNIX nanoseconds).
    """
    if symbol in INDEX_NAMES:
        return _make_index(symbol)

    match = OPTION_PATTERN.match(symbol)
    if match:
        return _make_option(symbol, match, lot_size, activation_ns)

    match = FUTURE_PATTERN.match(symbol)
    if match:
        return _make_future(symbol, match, lot_size, activation_ns)

    return _make_equity(symbol)


def _make_index(raw_name: str) -> IndexInstrument:
    clean = INDEX_NAMES[raw_name]
    return IndexInstrument(
        instrument_id=InstrumentId(Symbol(clean), Venue(NSE)),
        raw_symbol=Symbol(raw_name),
        currency=INR,
        price_precision=INDEX_TICK.precision,
        size_precision=0,
        price_increment=INDEX_TICK,
        size_increment=Quantity.from_int(1),
        ts_event=0,
        ts_init=0,
    )


def _make_option(
    symbol: str,
    match: re.Match,
    lot_size: int | None,
    activation_ns: int,
) -> OptionContract:
    underlying = match["underlying"]
    expiry_ns = _expiry_ns(match["expiry"])
    lots = _lot_size(underlying, expiry_ns, lot_size)
    return OptionContract(
        instrument_id=InstrumentId(Symbol(symbol), Venue(NFO)),
        raw_symbol=Symbol(symbol),
        asset_class=_asset_class(underlying),
        currency=INR,
        price_precision=TRADED_TICK.precision,
        price_increment=TRADED_TICK,
        multiplier=Quantity.from_int(1),
        lot_size=Quantity.from_int(lots),
        underlying=underlying,
        option_kind=OptionKind.CALL if match["kind"] == "CE" else OptionKind.PUT,
        strike_price=Price(float(match["strike"]), TRADED_TICK.precision),
        activation_ns=activation_ns,
        expiration_ns=expiry_ns,
        ts_event=0,
        ts_init=0,
    )


def _make_future(
    symbol: str,
    match: re.Match,
    lot_size: int | None,
    activation_ns: int,
) -> FuturesContract:
    underlying = match["underlying"]
    expiry_ns = _expiry_ns(match["expiry"])
    lots = _lot_size(underlying, expiry_ns, lot_size)
    return FuturesContract(
        instrument_id=InstrumentId(Symbol(symbol), Venue(NFO)),
        raw_symbol=Symbol(symbol),
        asset_class=_asset_class(underlying),
        currency=INR,
        price_precision=TRADED_TICK.precision,
        price_increment=TRADED_TICK,
        multiplier=Quantity.from_int(1),
        lot_size=Quantity.from_int(lots),
        underlying=underlying,
        activation_ns=activation_ns,
        expiration_ns=expiry_ns,
        ts_event=0,
        ts_init=0,
    )


def _make_equity(symbol: str) -> Equity:
    return Equity(
        instrument_id=InstrumentId(Symbol(symbol), Venue(NSE)),
        raw_symbol=Symbol(symbol),
        currency=INR,
        price_precision=TRADED_TICK.precision,
        price_increment=TRADED_TICK,
        lot_size=Quantity.from_int(1),
        ts_event=0,
        ts_init=0,
    )


def _lot_size(underlying: str, expiry_ns: int, override: int | None) -> int:
    """The lot in force for a contract of `underlying` expiring at `expiry_ns`."""
    if override is not None:
        return override
    if underlying not in LOT_SIZE_HISTORY:
        raise ValueError(
            f"No lot size known for {underlying!r}: add it to LOT_SIZE_HISTORY or pass lot_size="
        )
    expiry_date = pd.Timestamp(expiry_ns, tz=IST).strftime("%Y-%m-%d")
    lots = None
    for start_date, size in LOT_SIZE_HISTORY[underlying]:
        if start_date <= expiry_date:
            lots = size
    if lots is None:
        raise ValueError(f"No lot size known for {underlying!r} expiring {expiry_date}")
    return lots


def _asset_class(underlying: str) -> AssetClass:
    return AssetClass.INDEX if underlying in INDEX_UNDERLYINGS else AssetClass.EQUITY


def _expiry_ns(ddmmmyy: str) -> int:
    """'26MAY26' -> UNIX nanoseconds of 26 May 2026 15:30 IST."""
    expiry = pd.to_datetime(f"{ddmmmyy} {EXPIRY_TIME}", format="%d%b%y %H:%M")
    return expiry.tz_localize(IST).value
