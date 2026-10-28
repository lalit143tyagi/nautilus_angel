"""
Angel One's charges on NFO index options and futures, as a Nautilus fee model.

Nautilus calls get_commission once per fill and books the result as that fill's
commission. Per fill we charge:
    brokerage      BROKERAGE_PER_ORDER, on the order's first fill only (it is per order)
    STT            turnover x stt_sell, sells only
    exchange       turnover x exchange
    SEBI fee       turnover x SEBI_FEE
    stamp duty     turnover x stamp_buy, buys only
    GST            GST x (brokerage + exchange + SEBI fee)
where turnover = fill quantity x fill price x multiplier (for options: the premium), and
the rates are those in force on the order's date (nautilus_angel.constants).

Use it from a venue config:
    fee_model=ImportableFeeModelConfig(
        fee_model_path="nautilus_angel.adapters.angelone.fees:AngelOneFeeModel",
        config_path="nautilus_angel.adapters.angelone.fees:AngelOneFeeModelConfig",
        config={},
    )
"""

from nautilus_trader.backtest.config import FeeModelConfig
from nautilus_trader.backtest.models import FeeModel
from nautilus_trader.core.datetime import unix_nanos_to_dt
from nautilus_trader.model.currencies import INR
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.instruments import FuturesContract
from nautilus_trader.model.instruments import Instrument
from nautilus_trader.model.instruments import OptionContract
from nautilus_trader.model.objects import Money
from nautilus_trader.model.objects import Price
from nautilus_trader.model.objects import Quantity
from nautilus_trader.model.orders import Order

from nautilus_angel import constants
from nautilus_angel.adapters.angelone.constants import BROKERAGE_PER_ORDER


class AngelOneFeeModelConfig(FeeModelConfig, frozen=True):
    """No settings yet: the rates live in constants.py, brokerage in the adapter's constants."""


class AngelOneFeeModel(FeeModel):
    """Angel One brokerage plus NFO statutory and exchange charges."""

    def __init__(self, config: AngelOneFeeModelConfig | None = None) -> None:
        super().__init__()

    def get_commission(
        self,
        order: Order,
        fill_qty: Quantity,
        fill_px: Price,
        instrument: Instrument,
    ) -> Money:
        trade_date = unix_nanos_to_dt(order.ts_init).astimezone(constants.IST).date()
        if isinstance(instrument, OptionContract):
            rates = constants.option_charges(trade_date)
        elif isinstance(instrument, FuturesContract):
            rates = constants.futures_charges(trade_date)
        else:
            raise ValueError(f"No Angel One charges defined for {type(instrument).__name__}")

        turnover = fill_qty.as_double() * fill_px.as_double() * instrument.multiplier.as_double()
        is_buy = order.side == OrderSide.BUY

        # Nautilus has not yet added this fill to filled_qty, so 0 means "first fill".
        brokerage = BROKERAGE_PER_ORDER if order.filled_qty == 0 else 0.0
        stt = 0.0 if is_buy else turnover * rates.stt_sell
        exchange = turnover * rates.exchange
        sebi = turnover * constants.SEBI_FEE
        stamp = turnover * rates.stamp_buy if is_buy else 0.0
        gst = (brokerage + exchange + sebi) * constants.GST

        total = brokerage + stt + exchange + sebi + stamp + gst
        return Money(round(total, 2), INR)
