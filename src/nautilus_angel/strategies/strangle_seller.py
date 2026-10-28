"""
Sell out-of-the-money strangles on NIFTY / BANKNIFTY when their combined premium breaks
down, and buy them back on a trailing stop.

Every minute, from the index's last price:
    scan set   CE strikes: multiples of strike_step, at least min_otm above the index
               PE strikes: multiples of strike_step, at least min_otm below the index
               strangle k = (k-th CE strike up, k-th PE strike down), k = 0 .. strangle_count-1
    combined   for each tracked strangle, combined bar = CE bar + PE bar
               (open, high, low, close and volume added up), when both legs have that minute;
               then merged into bar_minutes bars, counted from that day's session open
               (e.g. 75 -> 5 bars a day; 45 -> 8 bars and a shorter last one). The pattern,
               the volume average and the trailing stop all use these bars.
    entry      flat, in the scan set, and a bearish breakdown candle:
                   close < open, close < the previous bar's low,
                   body (open - close) / open between body_min_pct and body_max_pct,
                   volume > the average volume of the previous volume_period bars
               -> queued; a timer 1 ns after the minute sells the queue highest combined
                  premium first (nearest the money) while fewer than max_open strangles
                  are short (max_open 0 = no limit). Same-minute bars arrive in no
                  meaningful order, so ranking has to wait for the whole minute; Nautilus
                  fires the timer before any bar of the next minute, so fills still use
                  the signal minute's prices.
    exit       combined close >= lowest close since entry x (1 + trailing_stop_pct) -> buy back
    expiry     on the expiry day, exit_minutes_before_close before the NFO close
               (constants.session): buy everything back and stop

Positions are held overnight. A strangle that has been scanned keeps building its chart
for the rest of the run, across days, like a chart on a trading screen.
"""

import math
from collections import deque
from datetime import date
from datetime import datetime
from datetime import timedelta

from nautilus_trader.common.events import TimeEvent
from nautilus_trader.config import StrategyConfig
from nautilus_trader.core.datetime import unix_nanos_to_dt
from nautilus_trader.model.data import Bar
from nautilus_trader.model.data import BarType
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.trading.strategy import Strategy

from nautilus_angel import constants


class StrangleSellerConfig(StrategyConfig, frozen=True):
    """Settings for StrangleSeller. Frozen: cannot change once created."""

    underlying: str  # "NIFTY" or "BANKNIFTY"
    expiry: str  # the options' expiry date, "YYYY-MM-DD"
    strike_step: int = 500  # only strikes that are multiples of this
    min_otm: int = 500  # points out of the money, at least
    strangle_count: int = 5  # strangles scanned at a time
    bar_minutes: int = 1  # timeframe of the combined-premium bars: 1, 5, 15, 45, 75 ...
    body_min_pct: float = 0.05
    body_max_pct: float = 0.10
    volume_period: int = 20  # bars in the volume average
    trailing_stop_pct: float = 0.10
    trade_lots: int = 1  # per leg
    max_open: int = 0  # most strangles short at once; 0 = no limit
    exit_minutes_before_close: int = 10  # on the expiry day


class StrangleState:
    """One strangle's chart history and position."""

    def __init__(self, ce: InstrumentId, pe: InstrumentId, volume_period: int) -> None:
        self.ce = ce
        self.pe = pe
        self.building = None  # (bar key, [open, high, low, close, volume]) still being merged
        self.prev = None  # previous combined bar: (open, high, low, close, volume)
        self.volumes = deque(maxlen=volume_period)
        self.short = False
        self.lowest_close = None  # lowest combined close since entry


class StrangleSeller(Strategy):
    """Scans OTM strangles and sells the ones whose combined premium breaks down."""

    def __init__(self, config: StrangleSellerConfig) -> None:
        super().__init__(config)
        if config.underlying not in constants.UNDERLYINGS:
            raise ValueError(f"Unknown underlying {config.underlying!r}")
        self.expiry = date.fromisoformat(config.expiry)
        # Option symbols name the expiry as DDMMMYY: 2026-07-28 -> "28JUL26".
        self.symbol_prefix = f"{config.underlying}{self.expiry.strftime('%d%b%y').upper()}"
        self.index_bar_type = BarType.from_str(f"{config.underlying}.{constants.NFO}-1-MINUTE-LAST-EXTERNAL")
        self.strangles: dict[tuple[int, int], StrangleState] = {}  # (CE strike, PE strike)
        self.scan_set: set[tuple[int, int]] = set()
        self.latest: dict[InstrumentId, Bar] = {}  # each leg's most recent bar
        self.queue: list[tuple[float, StrangleState]] = []  # (combined premium, strangle) signalled
        self.done = False  # set at the expiry-day exit

    def on_start(self) -> None:
        self.subscribe_bars(self.index_bar_type)

    def on_bar(self, bar: Bar) -> None:
        if self.done:
            return
        if bar.bar_type == self.index_bar_type:
            self._on_index_bar(bar)
        else:
            self._on_option_bar(bar)

    # --- Index bars: expiry-day exit and the scan set ----------------------------

    def _on_index_bar(self, bar: Bar) -> None:
        now = unix_nanos_to_dt(bar.ts_event).astimezone(constants.IST)
        if self._past_expiry_exit(now):
            for state in self.strangles.values():
                if state.short:
                    self._buy_back(state, "expiry-day exit")
            self.done = True
            return

        self.scan_set = self._scan_set(bar.close.as_double())
        for key in self.scan_set:
            if key not in self.strangles:
                self._track(key)

    def _past_expiry_exit(self, now: datetime) -> bool:
        if now.date() < self.expiry:
            return False
        close = constants.session(constants.NFO, now.date()).close
        exit_at = datetime.combine(now.date(), close) - timedelta(minutes=self.config.exit_minutes_before_close)
        return now.time() >= exit_at.time()

    def _scan_set(self, index_price: float) -> set[tuple[int, int]]:
        step, otm = self.config.strike_step, self.config.min_otm
        ce_first = math.ceil((index_price + otm) / step) * step
        pe_first = math.floor((index_price - otm) / step) * step
        return {(ce_first + k * step, pe_first - k * step) for k in range(self.config.strangle_count)}

    def _track(self, key: tuple[int, int]) -> None:
        ce, pe = (
            InstrumentId.from_str(f"{self.symbol_prefix}{strike}{kind}.{constants.NFO}")
            for strike, kind in zip(key, ("CE", "PE"))
        )
        if self.cache.instrument(ce) is None or self.cache.instrument(pe) is None:
            return  # strike not listed (or not loaded) for this expiry
        self.strangles[key] = StrangleState(ce, pe, self.config.volume_period)
        for leg in (ce, pe):
            self.subscribe_bars(self._bar_type(leg))

    # --- Option bars: combined premium -> entry or trailing stop -----------------

    def _on_option_bar(self, bar: Bar) -> None:
        now = unix_nanos_to_dt(bar.ts_event).astimezone(constants.IST)
        if self._past_expiry_exit(now):
            return  # the index bar for this minute does the exit
        leg = bar.bar_type.instrument_id
        self.latest[leg] = bar

        for key, state in self.strangles.items():
            if leg not in (state.ce, state.pe):
                continue
            ce, pe = self.latest.get(state.ce), self.latest.get(state.pe)
            if ce is None or pe is None or ce.ts_event != pe.ts_event:
                continue  # wait until both legs have a bar for this minute
            minute = tuple(
                getattr(ce, field).as_double() + getattr(pe, field).as_double()
                for field in ("open", "high", "low", "close", "volume")
            )
            for combined in self._merge_minute(state, now, minute):
                self._on_strangle_bar(key, state, combined)

    def _merge_minute(self, state: StrangleState, now: datetime, minute: tuple) -> list[tuple]:
        """
        Add one combined 1-minute bar, closing at `now`, to the strangle's bar_minutes bar.
        Return the bars this completes: usually none or one. A bar whose last minute never
        comes (one leg didn't trade then) is completed late, by the next bar's first minute.
        """
        size = self.config.bar_minutes
        if size == 1:
            return [minute]

        hours = constants.session(constants.NFO, now.date())
        day_open = datetime.combine(now.date(), hours.open, tzinfo=constants.IST)
        day_close = datetime.combine(now.date(), hours.close, tzinfo=constants.IST)
        minute_opened = int((now - day_open).total_seconds() // 60) - 1  # minutes after the open
        number = minute_opened // size  # which bar of the day this minute belongs to
        key = (now.date(), number)
        bar_end = min(day_open + timedelta(minutes=(number + 1) * size), day_close)

        completed = []
        if state.building is not None and state.building[0] != key:
            completed.append(tuple(state.building[1]))  # its last minute never came
            state.building = None

        open_, high, low, close, volume = minute
        if state.building is None:
            state.building = (key, [open_, high, low, close, volume])
        else:
            bar = state.building[1]
            bar[1] = max(bar[1], high)
            bar[2] = min(bar[2], low)
            bar[3] = close
            bar[4] += volume

        if now >= bar_end:
            completed.append(tuple(state.building[1]))
            state.building = None
        return completed

    def _on_strangle_bar(self, key: tuple[int, int], state: StrangleState, combined: tuple) -> None:
        open_, high, low, close, volume = combined

        if state.short:
            state.lowest_close = min(state.lowest_close, close)
            stop = state.lowest_close * (1 + self.config.trailing_stop_pct)
            if close >= stop:
                self._buy_back(state, f"trailing stop {stop:.2f}")
        elif key in self.scan_set and self._is_breakdown(state, open_, close, volume):
            if not self.queue:  # first signal this minute: sell the queue once the minute ends
                self.clock.set_time_alert_ns(
                    name=f"sell-queue-{self.clock.timestamp_ns()}",
                    alert_time_ns=self.clock.timestamp_ns() + 1,
                    callback=self._sell_queue,
                )
            self.queue.append((close, state))

        state.prev = combined
        state.volumes.append(volume)

    def _sell_queue(self, event: TimeEvent) -> None:
        """Sell this minute's signals, highest combined premium first, up to max_open."""
        queue, self.queue = self.queue, []
        now = unix_nanos_to_dt(event.ts_event).astimezone(constants.IST)
        if self.done or self._past_expiry_exit(now):
            return  # no new positions into the expiry-day exit
        for premium, state in sorted(queue, key=lambda item: item[0], reverse=True):
            if not state.short and self._can_open():
                self._sell(state, premium)

    def _can_open(self) -> bool:
        """True while fewer than max_open strangles are short (always, if max_open is 0)."""
        open_count = sum(state.short for state in self.strangles.values())
        return self.config.max_open == 0 or open_count < self.config.max_open

    def _is_breakdown(self, state: StrangleState, open_: float, close: float, volume: float) -> bool:
        if state.prev is None or len(state.volumes) < state.volumes.maxlen:
            return False  # not enough history yet
        body = (open_ - close) / open_
        average_volume = sum(state.volumes) / len(state.volumes)
        return (
            close < open_
            and close < state.prev[2]
            and self.config.body_min_pct <= body <= self.config.body_max_pct
            and volume > average_volume
        )

    # --- Orders -----------------------------------------------------------------
    # A leg can belong to two tracked strangles (NETTING keeps one position per
    # instrument), so each strangle buys back exactly what it sold.

    def _sell(self, state: StrangleState, premium: float) -> None:
        self.log.info(f"SELL {state.ce.symbol} + {state.pe.symbol} at combined {premium:.2f}")
        self._submit(state, OrderSide.SELL)
        state.short = True
        state.lowest_close = premium

    def _buy_back(self, state: StrangleState, reason: str) -> None:
        self.log.info(f"BUY {state.ce.symbol} + {state.pe.symbol}: {reason}")
        self._submit(state, OrderSide.BUY)
        state.short = False
        state.lowest_close = None

    def _submit(self, state: StrangleState, side: OrderSide) -> None:
        for leg in (state.ce, state.pe):
            instrument = self.cache.instrument(leg)
            units = self.config.trade_lots * instrument.lot_size.as_double()
            order = self.order_factory.market(
                instrument_id=leg,
                order_side=side,
                quantity=instrument.make_qty(units),
            )
            self.submit_order(order)

    def _bar_type(self, instrument_id: InstrumentId) -> BarType:
        return BarType.from_str(f"{instrument_id}-1-MINUTE-LAST-EXTERNAL")

    def on_stop(self) -> None:
        for state in self.strangles.values():
            for leg in (state.ce, state.pe):
                self.close_all_positions(leg)
