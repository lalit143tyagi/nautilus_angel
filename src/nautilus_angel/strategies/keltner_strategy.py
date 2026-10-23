"""
Keltner Channel breakout strategy.

    Middle band = EMA of the typical price (high + low + close) / 3
    Upper/lower = middle +/- k_multiplier * ATR

Rules (evaluated on each closed bar):
    flat:  close > upper -> buy;  close < lower -> sell short (if allow_short)
    long:  close < middle -> exit
    short: close > middle -> exit
    at or after square_off_time (IST): close any position, take no new entries
"""

from datetime import time
from zoneinfo import ZoneInfo

from nautilus_trader.config import StrategyConfig
from nautilus_trader.core.datetime import unix_nanos_to_dt
from nautilus_trader.indicators import KeltnerChannel
from nautilus_trader.model.data import Bar
from nautilus_trader.model.data import BarType
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.instruments import IndexInstrument
from nautilus_trader.trading.strategy import Strategy


IST = ZoneInfo("Asia/Kolkata")


class KeltnerConfig(StrategyConfig, frozen=True):
    """Settings for KeltnerStrategy. Frozen: cannot change once created."""

    instrument_id: InstrumentId
    bar_type: BarType
    period: int = 20
    k_multiplier: float = 2.0
    trade_lots: int = 1
    allow_short: bool = True
    square_off_time: str | None = "15:20"  # IST, "HH:MM"; None = hold overnight


class KeltnerStrategy(Strategy):
    """Trades breakouts of the Keltner Channel on one instrument."""

    def __init__(self, config: KeltnerConfig) -> None:
        super().__init__(config)
        self.channel = KeltnerChannel(config.period, config.k_multiplier)
        self.square_off = time.fromisoformat(config.square_off_time) if config.square_off_time else None
        self.instrument = None  # loaded from the cache in on_start

    def on_start(self) -> None:
        self.instrument = self.cache.instrument(self.config.instrument_id)
        if self.instrument is None:
            self.log.error(f"Instrument {self.config.instrument_id} not found in the cache")
            self.stop()
            return
        if isinstance(self.instrument, IndexInstrument):
            self.log.error(f"{self.config.instrument_id} is an index and cannot be traded")
            self.stop()
            return

        # The engine updates the indicator with each bar before calling on_bar.
        self.register_indicator_for_bars(self.config.bar_type, self.channel)
        self.subscribe_bars(self.config.bar_type)

    def on_bar(self, bar: Bar) -> None:
        if not self.channel.initialized:
            return  # still warming up: fewer than `period` bars seen

        instrument_id = self.config.instrument_id
        close = bar.close.as_double()

        if self._past_square_off(bar):
            if not self.portfolio.is_flat(instrument_id):
                self.close_all_positions(instrument_id)
            return

        if self.portfolio.is_net_long(instrument_id):
            if close < self.channel.middle:
                self.close_all_positions(instrument_id)
            return

        if self.portfolio.is_net_short(instrument_id):
            if close > self.channel.middle:
                self.close_all_positions(instrument_id)
            return

        if close > self.channel.upper:
            self._enter(OrderSide.BUY)
        elif self.config.allow_short and close < self.channel.lower:
            self._enter(OrderSide.SELL)

    def on_stop(self) -> None:
        self.cancel_all_orders(self.config.instrument_id)
        self.close_all_positions(self.config.instrument_id)

    def on_reset(self) -> None:
        self.channel.reset()

    def _enter(self, side: OrderSide) -> None:
        units = self.config.trade_lots * self.instrument.lot_size.as_double()
        order = self.order_factory.market(
            instrument_id=self.config.instrument_id,
            order_side=side,
            quantity=self.instrument.make_qty(units),
        )
        self.submit_order(order)

    def _past_square_off(self, bar: Bar) -> bool:
        if self.square_off is None:
            return False
        bar_close_ist = unix_nanos_to_dt(bar.ts_event).astimezone(IST)
        return bar_close_ist.time() >= self.square_off
