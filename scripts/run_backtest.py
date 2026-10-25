"""
Backtest a strategy on the catalog, using Nautilus's high-level API (BacktestNode).

Usage:
    edit the SETTINGS block below, then:
    uv run python scripts/run_backtest.py

The catalog holds 1-minute bars only; other timeframes are built from them while the
backtest runs, using a composite bar type "<target>-INTERNAL@<source>-EXTERNAL".
"""

from datetime import datetime

import pandas as pd

from nautilus_trader.backtest.config import MarginModelConfig
from nautilus_trader.backtest.node import BacktestNode
from nautilus_trader.config import BacktestDataConfig
from nautilus_trader.config import BacktestEngineConfig
from nautilus_trader.config import BacktestRunConfig
from nautilus_trader.config import BacktestVenueConfig
from nautilus_trader.config import DataEngineConfig
from nautilus_trader.config import ImportableFeeModelConfig
from nautilus_trader.config import ImportableStrategyConfig
from nautilus_trader.config import LoggingConfig
from nautilus_trader.model.data import Bar
from nautilus_trader.model.enums import BarAggregation
from nautilus_trader.model.identifiers import InstrumentId

from nautilus_angel.config import CATALOG_PATH
from nautilus_angel.config import NFO
from nautilus_angel.config import NSE
from nautilus_angel.config import REPORTS_PATH


# --- SETTINGS ----------------------------------------------------------------

INSTRUMENT_ID = "BANKNIFTY26MAY2654000PE.NFO"
BAR_MINUTES = 15  # 1, or minutes dividing 60 (5, 15, 30), or whole hours (60, 120)
START = None  # first IST date to trade, e.g. "2026-04-01"; None = from the first bar
END = None  # last IST date to trade (inclusive); None = to the last bar
STARTING_BALANCE = "1000000 INR"
STRATEGY_SETTINGS = {
    "period": 20,  # bars, so 20 x BAR_MINUTES of history
    "k_multiplier": 2.0,
    "trade_lots": 1,
    "allow_short": True,
    "square_off_time": "15:20",  # IST; None = hold overnight
}

# -----------------------------------------------------------------------------

IST = "Asia/Kolkata"

# NSE opens at 09:15 IST = 03:45 UTC, 225 minutes after UTC midnight. Nautilus aligns
# time bars to UTC midnight, so shift them by the remainder to start at the open.
MARKET_OPEN_UTC_MINUTES = 3 * 60 + 45


def main() -> None:
    source_bar_type = f"{INSTRUMENT_ID}-1-MINUTE-LAST-EXTERNAL"
    if BAR_MINUTES == 1:
        bar_type = source_bar_type
    elif BAR_MINUTES % 60 == 0:
        bar_type = f"{INSTRUMENT_ID}-{BAR_MINUTES // 60}-HOUR-LAST-INTERNAL@1-MINUTE-EXTERNAL"
    else:
        bar_type = f"{INSTRUMENT_ID}-{BAR_MINUTES}-MINUTE-LAST-INTERNAL@1-MINUTE-EXTERNAL"

    # One simulated exchange + account per Angel One segment, charging Angel One fees.
    # Margin uses Nautilus's "standard" model with each instrument's margin_init/maint.
    venues = [
        BacktestVenueConfig(
            name=venue,
            oms_type="NETTING",
            account_type="MARGIN",
            base_currency="INR",
            starting_balances=[STARTING_BALANCE],
            fee_model=ImportableFeeModelConfig(
                fee_model_path="nautilus_angel.fees:AngelOneFeeModel",
                config_path="nautilus_angel.fees:AngelOneFeeModelConfig",
                config={},
            ),
            margin_model=MarginModelConfig(model_type="standard"),
        )
        for venue in (NSE, NFO)
    ]

    data = [
        BacktestDataConfig(
            catalog_path=str(CATALOG_PATH),
            # The Bar class itself: given the "module:Bar" string, Nautilus 1.231
            # ignores `bar_types` and loads every bar in the catalog.
            data_cls=Bar,
            bar_types=[source_bar_type],
            start_time=ist_date_ns(START),
            end_time=ist_date_ns(END, next_day=True),
        ),
    ]

    offset = pd.Timedelta(minutes=MARKET_OPEN_UTC_MINUTES % BAR_MINUTES)
    engine = BacktestEngineConfig(
        trader_id="BACKTESTER-001",
        logging=LoggingConfig(log_level="WARNING"),
        data_engine=DataEngineConfig(
            # NSE is closed nights and weekends: don't build flat bars for those hours.
            time_bars_build_with_no_updates=False,
            time_bars_origin_offset={BarAggregation.MINUTE: offset, BarAggregation.HOUR: offset},
        ),
        strategies=[
            ImportableStrategyConfig(
                strategy_path="nautilus_angel.strategies.keltner_strategy:KeltnerStrategy",
                config_path="nautilus_angel.strategies.keltner_strategy:KeltnerConfig",
                config={"instrument_id": INSTRUMENT_ID, "bar_type": bar_type, **STRATEGY_SETTINGS},
            ),
        ],
    )

    run_config = BacktestRunConfig(
        engine=engine,
        venues=venues,
        data=data,
        dispose_on_completion=False,  # keep the engine so we can read its reports
    )

    node = BacktestNode(configs=[run_config])
    if not node.run():
        raise SystemExit("Backtest failed: see the error above")

    report(node.get_engine(run_config.id), bar_type)
    node.dispose()


def ist_date_ns(date: str | None, next_day: bool = False) -> int | None:
    """'2026-04-01' -> UNIX nanoseconds of that IST midnight (or the next, for an end date)."""
    if date is None:
        return None
    timestamp = pd.Timestamp(date, tz=IST)
    return (timestamp + pd.Timedelta(days=1) if next_day else timestamp).value


def report(engine, bar_type: str) -> None:
    """Print a summary and save Nautilus's standard reports as CSV."""
    venue = InstrumentId.from_str(INSTRUMENT_ID).venue
    positions = engine.trader.generate_positions_report()
    fills = engine.trader.generate_fills_report()
    account = engine.trader.generate_account_report(venue)

    balance = money(account["total"])
    charges = money(fills["commission"]).sum() if not fills.empty else 0.0
    net = balance.iloc[-1] - balance.iloc[0]

    print(f"\n=== {bar_type} ===")
    print(f"Trades:        {len(positions)}")
    if not positions.empty:
        print(f"Win rate:      {(money(positions['realized_pnl']) > 0).mean():.1%}")
    print(f"Gross P&L:     Rs {net + charges:>12,.2f}")
    print(f"Charges:       Rs {charges:>12,.2f}")
    print(f"Net P&L:       Rs {net:>12,.2f}  ({net / balance.iloc[0]:+.2%})")
    print(f"Max drawdown:  Rs {(balance - balance.cummax()).min():>12,.2f}")

    folder = REPORTS_PATH / f"{INSTRUMENT_ID}_{BAR_MINUTES}min_{datetime.now():%Y%m%d-%H%M%S}"
    folder.mkdir(parents=True, exist_ok=True)
    positions.to_csv(folder / "positions.csv")
    fills.to_csv(folder / "fills.csv")
    account.to_csv(folder / "account.csv")
    print(f"Reports:       {folder}")


def money(values: pd.Series) -> pd.Series:
    """Nautilus reports money as text like '-170.90 INR'; return the numbers."""
    return values.astype(str).str.split().str[0].astype(float)


if __name__ == "__main__":
    main()
