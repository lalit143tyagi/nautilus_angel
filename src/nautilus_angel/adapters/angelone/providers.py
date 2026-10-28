"""
Angel One instrument provider: builds Nautilus instruments for NIFTY / BANKNIFTY and
their NFO options and futures.

    historical  provider.add_from_symbol("BANKNIFTY28JUL2658000CE")
                lot size, tick and expiry time come from nautilus_angel.constants
    current     provider.load_all()  downloads Angel One's instrument master;
                lot size, tick and token come from Angel One.
                provider.load_all({"underlyings": ["NIFTY"]}) loads only NIFTY.

Both go through build_instrument(spec), so every instrument is built the same way.

Instrument ids: options/futures "<trading symbol>.NFO"; indices "<underlying>.NFO"
(e.g. BANKNIFTY.NFO, with the spelled-out name "Nifty Bank" as the raw symbol).
The index is on NFO, not NSE, because Nautilus settles an option at expiry against
"<underlying>.<option's venue>". The exchange Angel One files each instrument under
("NSE" for the index, "NFO" for options/futures) is kept in instrument.info["exchange"],
with Angel One's token in instrument.info["token"] when known.
"""

import json
from datetime import datetime

from nautilus_trader.common.providers import InstrumentProvider
from nautilus_trader.config import InstrumentProviderConfig
from nautilus_trader.core import nautilus_pyo3
from nautilus_trader.model.currencies import INR
from nautilus_trader.model.enums import AssetClass
from nautilus_trader.model.enums import OptionKind
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.identifiers import Symbol
from nautilus_trader.model.identifiers import Venue
from nautilus_trader.model.instruments import FuturesContract
from nautilus_trader.model.instruments import IndexInstrument
from nautilus_trader.model.instruments import Instrument
from nautilus_trader.model.instruments import OptionContract
from nautilus_trader.model.objects import Price
from nautilus_trader.model.objects import Quantity

from nautilus_angel import constants
from nautilus_angel.adapters.angelone.constants import DOWNLOAD_TIMEOUT_SECS
from nautilus_angel.adapters.angelone.constants import INSTRUMENT_MASTER_URL
from nautilus_angel.adapters.angelone.parsing import FUTURE
from nautilus_angel.adapters.angelone.parsing import INDEX
from nautilus_angel.adapters.angelone.parsing import OPTION
from nautilus_angel.adapters.angelone.parsing import ContractSpec
from nautilus_angel.adapters.angelone.parsing import parse_master_record
from nautilus_angel.adapters.angelone.parsing import parse_symbol


# Every NSE price has at most 2 decimals (ticks 0.05, 0.10, 0.20, 0.01).
PRICE_PRECISION = 2


class AngelOneInstrumentProvider(InstrumentProvider):
    """Provides NSE/NFO instruments, from trading symbols or from Angel One's instrument master."""

    def __init__(self, config: InstrumentProviderConfig | None = None) -> None:
        super().__init__(config=config)

    def add_from_symbol(self, symbol: str, activation_ns: int = 0) -> Instrument:
        """
        Build the instrument for a historical trading symbol, add it to the provider and
        return it. `activation_ns`: when the contract started trading, if known (UNIX ns).
        """
        instrument = build_instrument(parse_symbol(symbol), activation_ns)
        self.add(instrument)
        return instrument

    async def load_all_async(self, filters: dict | None = None) -> None:
        """
        Download Angel One's instrument master and add today's NIFTY / BANKNIFTY index,
        options and futures. `filters` (or the provider config's filters) may hold
        {"underlyings": ["NIFTY", ...]} to load only some underlyings.
        """
        filters = filters if filters is not None else self._filters
        wanted = set((filters or {}).get("underlyings", constants.UNDERLYINGS))

        records = await self._download_instrument_master()
        added = 0
        for record in records:
            spec = parse_master_record(record)
            if spec is not None and spec.underlying in wanted:
                self.add(build_instrument(spec))
                added += 1
        self._log.info(f"Loaded {added:,} instruments from {len(records):,} instrument master records")

    async def _download_instrument_master(self) -> list[dict]:
        client = nautilus_pyo3.HttpClient(timeout_secs=DOWNLOAD_TIMEOUT_SECS)
        response = await client.get(INSTRUMENT_MASTER_URL)
        if response.status != 200:
            raise RuntimeError(f"Instrument master download failed: HTTP {response.status}")
        return json.loads(response.body)


def build_instrument(spec: ContractSpec, activation_ns: int = 0) -> Instrument:
    """The Nautilus instrument for `spec`. Missing lot size / tick come from constants."""
    if spec.kind == INDEX:
        return _build_index(spec)
    if spec.kind == OPTION:
        return _build_option(spec, activation_ns)
    if spec.kind == FUTURE:
        return _build_future(spec, activation_ns)
    raise ValueError(f"Unknown contract kind {spec.kind!r}")


def _build_index(spec: ContractSpec) -> IndexInstrument:
    tick = _price(constants.INDEX_TICK)
    return IndexInstrument(
        # On the derivatives venue, so its options and futures can settle against it.
        instrument_id=InstrumentId(Symbol(spec.underlying), Venue(constants.NFO)),
        raw_symbol=Symbol(spec.symbol),
        currency=INR,
        price_precision=PRICE_PRECISION,
        size_precision=0,
        price_increment=tick,
        size_increment=Quantity.from_int(1),
        ts_event=0,
        ts_init=0,
        info=_info(spec),
    )


def _build_option(spec: ContractSpec, activation_ns: int) -> OptionContract:
    tick = spec.tick_size if spec.tick_size is not None else constants.OPTION_TICK
    return OptionContract(
        instrument_id=InstrumentId(Symbol(spec.symbol), Venue(constants.NFO)),
        raw_symbol=Symbol(spec.symbol),
        asset_class=AssetClass.INDEX,
        currency=INR,
        price_precision=PRICE_PRECISION,
        price_increment=_price(tick),
        multiplier=Quantity.from_int(1),
        lot_size=Quantity.from_int(_lot_size(spec)),
        underlying=spec.underlying,
        option_kind=OptionKind.CALL if spec.option_type == "CE" else OptionKind.PUT,
        strike_price=_price(spec.strike),
        activation_ns=activation_ns,
        expiration_ns=_expiration_ns(spec),
        ts_event=0,
        ts_init=0,
        info=_info(spec),
    )


def _build_future(spec: ContractSpec, activation_ns: int) -> FuturesContract:
    if spec.tick_size is not None:
        tick = spec.tick_size
    else:
        tick = constants.futures_tick(spec.underlying, spec.expiry)
    return FuturesContract(
        instrument_id=InstrumentId(Symbol(spec.symbol), Venue(constants.NFO)),
        raw_symbol=Symbol(spec.symbol),
        asset_class=AssetClass.INDEX,
        currency=INR,
        price_precision=PRICE_PRECISION,
        price_increment=_price(tick),
        multiplier=Quantity.from_int(1),
        lot_size=Quantity.from_int(_lot_size(spec)),
        underlying=spec.underlying,
        activation_ns=activation_ns,
        expiration_ns=_expiration_ns(spec),
        ts_event=0,
        ts_init=0,
        info=_info(spec),
    )


def _lot_size(spec: ContractSpec) -> int:
    """Angel One's lot size when known (live), else the one in force for this expiry."""
    if spec.lot_size is not None:
        return spec.lot_size
    return constants.lot_size(spec.underlying, spec.expiry)


def _expiration_ns(spec: ContractSpec) -> int:
    """Contracts expire at the NFO close on their expiry date (15:30, or 15:40 from 2026-08-03)."""
    close = constants.session(constants.NFO, spec.expiry).close
    expires_at = datetime.combine(spec.expiry, close, tzinfo=constants.IST)
    return int(expires_at.timestamp()) * 1_000_000_000


def _price(value) -> Price:
    """A Price with exactly PRICE_PRECISION decimals, from a str or Decimal."""
    return Price.from_str(f"{float(value):.{PRICE_PRECISION}f}")


def _info(spec: ContractSpec) -> dict:
    """Extra data stored with the instrument: Angel One's exchange, and token when known."""
    info = {"exchange": spec.exchange}
    if spec.token:
        info["token"] = spec.token
    return info
