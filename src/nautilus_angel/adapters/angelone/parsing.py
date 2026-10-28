"""
Parse Angel One instrument identities into plain fields (ContractSpec).

Two sources, one result, so the instrument provider builds every instrument the same way:
    trading symbols (historical data)  BANKNIFTY28JUL2658000CE, BANKNIFTY28JUL26FUT, Nifty Bank
    instrument master records (live)   {"symbol": "BANKNIFTY27OCT2647500CE", "expiry": "27OCT2026",
                                        "strike": "4750000.000000", "lotsize": "30", ...}

A symbol carries only the contract's identity; the instrument master also carries the
lot size, tick size and Angel One's token. Nothing here creates Nautilus objects.
"""

import re
from dataclasses import dataclass
from datetime import date
from datetime import datetime
from decimal import Decimal

from nautilus_angel.constants import NFO
from nautilus_angel.constants import NSE
from nautilus_angel.constants import UNDERLYINGS


OPTION = "OPTION"
FUTURE = "FUTURE"
INDEX = "INDEX"


@dataclass(frozen=True)
class ContractSpec:
    """Everything needed to build one Nautilus instrument."""

    kind: str  # OPTION, FUTURE or INDEX
    symbol: str  # Angel One trading symbol, e.g. "BANKNIFTY28JUL2658000CE" or "Nifty Bank"
    venue: str  # NSE or NFO
    underlying: str  # e.g. "BANKNIFTY"
    expiry: date | None = None  # options and futures
    strike: Decimal | None = None  # options
    option_type: str | None = None  # options: "CE" or "PE"
    lot_size: int | None = None  # from the instrument master only
    tick_size: Decimal | None = None  # from the instrument master only
    token: str | None = None  # Angel One's instrument id, needed for live data and orders


# UNDERLYING + DDMMMYY expiry + STRIKE + CE/PE, e.g. BANKNIFTY | 28JUL26 | 58000 | CE
OPTION_SYMBOL = re.compile(
    r"^(?P<underlying>[A-Z]+?)(?P<expiry>\d{2}[A-Z]{3}\d{2})(?P<strike>\d+(?:\.\d+)?)(?P<option_type>CE|PE)$"
)

# UNDERLYING + DDMMMYY expiry + FUT, e.g. BANKNIFTY | 28JUL26 | FUT
FUTURE_SYMBOL = re.compile(r"^(?P<underlying>[A-Z]+?)(?P<expiry>\d{2}[A-Z]{3}\d{2})FUT$")

# Index names as Angel One and the data files spell them, e.g. "Nifty Bank" -> "BANKNIFTY".
INDEX_NAMES = {underlying.index_name: underlying.symbol for underlying in UNDERLYINGS.values()}


def parse_symbol(symbol: str) -> ContractSpec:
    """Parse an Angel One trading symbol (as in the historical data files)."""
    if symbol in INDEX_NAMES:
        return ContractSpec(kind=INDEX, symbol=symbol, venue=NSE, underlying=INDEX_NAMES[symbol])

    match = OPTION_SYMBOL.match(symbol)
    if match:
        return ContractSpec(
            kind=OPTION,
            symbol=symbol,
            venue=NFO,
            underlying=_known_underlying(match["underlying"], symbol),
            expiry=_parse_date(match["expiry"], "%d%b%y"),
            strike=Decimal(match["strike"]),
            option_type=match["option_type"],
        )

    match = FUTURE_SYMBOL.match(symbol)
    if match:
        return ContractSpec(
            kind=FUTURE,
            symbol=symbol,
            venue=NFO,
            underlying=_known_underlying(match["underlying"], symbol),
            expiry=_parse_date(match["expiry"], "%d%b%y"),
        )

    raise ValueError(f"Not an index, index option or index future symbol: {symbol!r}")


def parse_master_record(record: dict) -> ContractSpec | None:
    """
    Parse one record of Angel One's instrument master (OpenAPIScripMaster.json).
    Returns None for anything this project doesn't trade (other exchanges, stocks,
    other underlyings), so the caller can simply skip it.
    """
    segment, kind, name = record["exch_seg"], record["instrumenttype"], record["name"]
    if name not in UNDERLYINGS:
        return None

    if segment == NSE and kind == "AMXIDX" and record["symbol"] in INDEX_NAMES:
        spec = parse_symbol(record["symbol"])
        return _with_master_fields(spec, record, tick_size=None)

    if segment == NFO and kind in ("OPTIDX", "FUTIDX"):
        spec = parse_symbol(record["symbol"])
        # The master's own fields must agree with what the symbol says.
        expiry = _parse_date(record["expiry"], "%d%b%Y")
        if spec.expiry != expiry:
            raise ValueError(f"{record['symbol']}: expiry field {expiry} != symbol's {spec.expiry}")
        if spec.kind == OPTION and Decimal(record["strike"]) / 100 != spec.strike:
            raise ValueError(f"{record['symbol']}: strike field {record['strike']} != symbol's {spec.strike}")
        # tick_size is in paise: "5.000000" -> 0.05 rupees.
        return _with_master_fields(spec, record, tick_size=Decimal(record["tick_size"]) / 100)

    return None


def _with_master_fields(spec: ContractSpec, record: dict, tick_size: Decimal | None) -> ContractSpec:
    """Copy of `spec` with the lot size, tick size and token from an instrument master record."""
    return ContractSpec(
        kind=spec.kind,
        symbol=spec.symbol,
        venue=spec.venue,
        underlying=spec.underlying,
        expiry=spec.expiry,
        strike=spec.strike,
        option_type=spec.option_type,
        lot_size=int(record["lotsize"]),
        tick_size=tick_size,
        token=record["token"],
    )


def _known_underlying(underlying: str, symbol: str) -> str:
    if underlying not in UNDERLYINGS:
        raise ValueError(f"{symbol!r}: unknown underlying {underlying!r}; known: {sorted(UNDERLYINGS)}")
    return underlying


def _parse_date(text: str, pattern: str) -> date:
    """'28JUL26' with '%d%b%y', or '28JUL2026' with '%d%b%Y' -> date(2026, 7, 28)."""
    return datetime.strptime(text, pattern).date()
