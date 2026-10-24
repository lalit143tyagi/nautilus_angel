"""
Approximate NSE / NFO margin requirements, as a Nautilus margin model.

Real F&O margin is SPAN + exposure (ELM), recalculated daily by NSE Clearing from risk
parameter files. Those files aren't in our data, so we approximate with a percentage of
the contract's UNDERLYING value:

    futures            futures_margin   x price  x quantity
    short options      option_margin    x strike x quantity  (strike stands in for the
                                                              underlying, which the model
                                                              can't see; close for ATM)
    long options       the premium      x quantity           (a buyer pays it in full)
    equities           equity_margin    x price  x quantity  (intraday about 20%;
                                                              1.0 for delivery)

When live, Angel One's RMS margin API gives the exact figure; this is for backtests.
"""

from decimal import Decimal

from nautilus_trader.accounting.margin_models import MarginModel
from nautilus_trader.backtest.config import MarginModelConfig
from nautilus_trader.model.enums import PositionSide
from nautilus_trader.model.instruments import Equity
from nautilus_trader.model.instruments import FuturesContract
from nautilus_trader.model.instruments import Instrument
from nautilus_trader.model.instruments import OptionContract
from nautilus_trader.model.objects import Money
from nautilus_trader.model.objects import Price
from nautilus_trader.model.objects import Quantity


DEFAULTS = {
    "futures_margin": 0.12,  # index futures SPAN + ELM, typically about 10-12%
    "option_margin": 0.12,  # short index options, near the money
    "equity_margin": 0.20,  # intraday (about 5x); set 1.0 to backtest delivery trades
}


class NseMarginModel(MarginModel):
    """
    Use it through the venue config:
        MarginModelConfig(model_type="nautilus_angel.margin:NseMarginModel",
                          config={"futures_margin": 0.12, ...})
    Any rate left out keeps its value from DEFAULTS.
    """

    def __init__(self, config: MarginModelConfig | None = None) -> None:
        rates = {**DEFAULTS, **(config.config if config else {})}
        unknown = set(rates) - set(DEFAULTS)
        if unknown:
            raise ValueError(f"Unknown margin settings: {sorted(unknown)}")
        self.rates = {name: Decimal(str(value)) for name, value in rates.items()}

    def calculate_margin_init(
        self,
        instrument: Instrument,
        quantity: Quantity,
        price: Price,
        leverage: Decimal,
        use_quote_for_inverse: bool = False,
    ) -> Money:
        """Margin blocked by an open order. The side is unknown here, so options use the larger (seller's) figure."""
        if isinstance(instrument, OptionContract):
            return max(
                self._short_option(instrument, quantity),
                self._premium(instrument, quantity, price),
            )
        return self._linear(instrument, quantity, price)

    def calculate_margin_maint(
        self,
        instrument: Instrument,
        side: PositionSide,
        quantity: Quantity,
        price: Price,
        leverage: Decimal,
        use_quote_for_inverse: bool = False,
    ) -> Money:
        """Margin blocked by an open position."""
        if isinstance(instrument, OptionContract):
            if side == PositionSide.SHORT:
                return self._short_option(instrument, quantity)
            return self._premium(instrument, quantity, price)
        return self._linear(instrument, quantity, price)

    def _linear(self, instrument: Instrument, quantity: Quantity, price: Price) -> Money:
        """Futures and equities: a percentage of the position's value."""
        if isinstance(instrument, FuturesContract):
            rate = self.rates["futures_margin"]
        elif isinstance(instrument, Equity):
            rate = self.rates["equity_margin"]
        else:
            raise ValueError(f"No NSE margin rule for {type(instrument).__name__}")
        notional = instrument.notional_value(quantity, price).as_decimal()
        return Money(notional * rate, instrument.quote_currency)

    def _short_option(self, instrument: OptionContract, quantity: Quantity) -> Money:
        underlying_value = (
            instrument.strike_price.as_decimal()
            * quantity.as_decimal()
            * instrument.multiplier.as_decimal()
        )
        return Money(underlying_value * self.rates["option_margin"], instrument.quote_currency)

    def _premium(self, instrument: OptionContract, quantity: Quantity, price: Price) -> Money:
        return instrument.notional_value(quantity, price)
