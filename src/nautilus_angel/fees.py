"""
Angel One trading charges for NSE / NFO, as a Nautilus fee model.

Nautilus calls `get_commission` once per fill. We charge:
    brokerage         once per ORDER (on its first fill), as Angel One does
    statutory charges on every fill's turnover (quantity x price):
        STT, exchange transaction charges, SEBI fee, stamp duty, and GST (18%)
        on brokerage + exchange + SEBI.

Rates change over time (e.g. STT on F&O rose on 2024-10-01 and 2026-04-01), so each
segment has a schedule of (start date, rates), and the rates in force on the order's
date are used, the same idea as LOT_SIZE_HISTORY in instruments/factory.py.

Source: angelone.in/exchange-transaction-charges (checked 2026-10-24), plus the
Finance Act STT changes and NSE's 2024-10-01 transaction-charge revision.
"""

from dataclasses import dataclass
from dataclasses import replace

import pandas as pd

from nautilus_trader.backtest.config import FeeModelConfig
from nautilus_trader.backtest.models import FeeModel
from nautilus_trader.model.currencies import INR
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.instruments import Equity
from nautilus_trader.model.instruments import FuturesContract
from nautilus_trader.model.instruments import Instrument
from nautilus_trader.model.instruments import OptionContract
from nautilus_trader.model.objects import Money
from nautilus_trader.model.objects import Price
from nautilus_trader.model.objects import Quantity
from nautilus_trader.model.orders import Order


IST = "Asia/Kolkata"

# Angel One product types for equity orders (F&O has no such split for charges).
INTRADAY = "INTRADAY"
DELIVERY = "DELIVERY"


@dataclass(frozen=True)
class Rates:
    """One segment's charges. Percentages are written as fractions: 0.1% -> 0.001."""

    brokerage_flat: float  # rupees per order
    brokerage_pct: float  # of order value; 0 = flat only
    brokerage_min: float  # rupees per order (applies with brokerage_pct)
    stt_buy: float
    stt_sell: float
    exchange: float  # NSE transaction charge, both sides
    stamp_buy: float
    sebi: float = 10 / 1e7  # Rs 10 per crore, both sides
    gst: float = 0.18  # on brokerage + exchange + SEBI


# Equity: brokerage is the lower of Rs 20 or 0.1% of order value, minimum Rs 5.
EQUITY_INTRADAY = Rates(
    brokerage_flat=20.0, brokerage_pct=0.001, brokerage_min=5.0,
    stt_buy=0.0, stt_sell=0.00025, exchange=0.0000322, stamp_buy=0.00003,
)
EQUITY_DELIVERY = Rates(
    brokerage_flat=20.0, brokerage_pct=0.001, brokerage_min=5.0,
    stt_buy=0.001, stt_sell=0.001, exchange=0.0000322, stamp_buy=0.00015,
)
# F&O: flat Rs 20 per order. Option charges are on the premium turnover.
FUTURES = Rates(
    brokerage_flat=20.0, brokerage_pct=0.0, brokerage_min=0.0,
    stt_buy=0.0, stt_sell=0.0001, exchange=0.0000188, stamp_buy=0.00002,
)
OPTIONS = Rates(
    brokerage_flat=20.0, brokerage_pct=0.0, brokerage_min=0.0,
    stt_buy=0.0, stt_sell=0.0005, exchange=0.000495, stamp_buy=0.00003,
)

# (first date, rates) per segment; the latest entry on or before the order date wins.
# The earliest entry also covers older dates (rates before then are not modelled).
RATE_SCHEDULE: dict[str, list[tuple[str, Rates]]] = {
    "EQUITY_INTRADAY": [
        ("2015-01-01", EQUITY_INTRADAY),
        ("2024-10-01", replace(EQUITY_INTRADAY, exchange=0.000030699)),
    ],
    "EQUITY_DELIVERY": [
        ("2015-01-01", EQUITY_DELIVERY),
        ("2024-10-01", replace(EQUITY_DELIVERY, exchange=0.000030699)),
    ],
    "FUTURES": [
        ("2015-01-01", FUTURES),
        ("2023-04-01", replace(FUTURES, stt_sell=0.000125)),
        ("2024-10-01", replace(FUTURES, stt_sell=0.0002, exchange=0.000018299)),
        ("2026-04-01", replace(FUTURES, stt_sell=0.0005, exchange=0.000018299)),
    ],
    "OPTIONS": [
        ("2015-01-01", replace(OPTIONS, stt_sell=0.00017)),
        ("2016-06-01", OPTIONS),
        ("2023-04-01", replace(OPTIONS, stt_sell=0.000625)),
        ("2024-10-01", replace(OPTIONS, stt_sell=0.001, exchange=0.000355299)),
        ("2026-04-01", replace(OPTIONS, stt_sell=0.0015, exchange=0.000355299)),
    ],
}


class AngelOneFeeModelConfig(FeeModelConfig, frozen=True):
    """
    `equity_product` is the default for equity orders; an order can override it
    with a tag: order_factory.market(..., tags=["DELIVERY"]).
    """

    equity_product: str = INTRADAY


class AngelOneFeeModel(FeeModel):
    """Angel One NSE/NFO charges for equities, futures and options."""

    def __init__(self, config: AngelOneFeeModelConfig | None = None) -> None:
        config = config or AngelOneFeeModelConfig()
        if config.equity_product not in (INTRADAY, DELIVERY):
            raise ValueError(f"equity_product must be {INTRADAY} or {DELIVERY}")
        self.equity_product = config.equity_product

    def get_commission(
        self,
        order: Order,
        fill_qty: Quantity,
        fill_px: Price,
        instrument: Instrument,
    ) -> Money:
        rates = rates_for(segment_of(order, instrument, self.equity_product), order.ts_init)
        multiplier = instrument.multiplier.as_double()
        turnover = fill_qty.as_double() * fill_px.as_double() * multiplier

        # Brokerage once per order: Nautilus has not yet added this fill to filled_qty.
        brokerage = 0.0
        if order.filled_qty == 0:
            order_value = order.quantity.as_double() * fill_px.as_double() * multiplier
            brokerage = brokerage_for(rates, order_value)

        is_buy = order.side == OrderSide.BUY
        stt = turnover * (rates.stt_buy if is_buy else rates.stt_sell)
        exchange = turnover * rates.exchange
        sebi = turnover * rates.sebi
        stamp = turnover * rates.stamp_buy if is_buy else 0.0
        gst = (brokerage + exchange + sebi) * rates.gst

        total = brokerage + stt + exchange + sebi + stamp + gst
        return Money(round(total, 2), INR)


def segment_of(order: Order, instrument: Instrument, equity_product: str) -> str:
    if isinstance(instrument, OptionContract):
        return "OPTIONS"
    if isinstance(instrument, FuturesContract):
        return "FUTURES"
    if isinstance(instrument, Equity):
        tags = order.tags or []
        product = DELIVERY if DELIVERY in tags else INTRADAY if INTRADAY in tags else equity_product
        return f"EQUITY_{product}"
    raise ValueError(f"No Angel One charges defined for {type(instrument).__name__}")


def rates_for(segment: str, ts_ns: int) -> Rates:
    """The rates in force for `segment` on the IST date of `ts_ns`."""
    date = pd.Timestamp(ts_ns, tz="UTC").tz_convert(IST).strftime("%Y-%m-%d")
    schedule = RATE_SCHEDULE[segment]
    rates = schedule[0][1]
    for start_date, entry in schedule:
        if start_date <= date:
            rates = entry
    return rates


def brokerage_for(rates: Rates, order_value: float) -> float:
    if rates.brokerage_pct == 0:
        return rates.brokerage_flat
    percentage = order_value * rates.brokerage_pct
    return max(rates.brokerage_min, min(rates.brokerage_flat, percentage))
