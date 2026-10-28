"""
Run a backtest on the catalog with Nautilus's high-level API (BacktestNode).

Usage:
    edit the SETTINGS block below, then:
    uv run python scripts/run_backtest.py

This first version runs Nautilus's own example strategy (EMACross: buy when a fast
EMA crosses above a slow one, sell when it crosses below) on one instrument, to prove
the whole pipeline: catalog -> venue -> engine -> strategy -> reports.
"""

import pandas as pd

from nautilus_trader.backtest.node import BacktestNode
from nautilus_trader.config import BacktestDataConfig
from nautilus_trader.config import BacktestEngineConfig
from nautilus_trader.config import BacktestRunConfig
from nautilus_trader.config import BacktestVenueConfig
from nautilus_trader.config import ImportableFeeModelConfig
from nautilus_trader.config import ImportableStrategyConfig
from nautilus_trader.config import LoggingConfig
from nautilus_trader.model.data import Bar
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.persistence.catalog import ParquetDataCatalog

from nautilus_angel import constants


# --- SETTINGS ----------------------------------------------------------------

INSTRUMENT_ID = "BANKNIFTY28JUL2657500CE.NFO"
START = "2026-07-01"  # first IST date (inclusive)
END = "2026-07-28"  # last IST date (inclusive); this contract's expiry day
LOTS = 1
STARTING_BALANCE = "1000000 INR"
FAST_EMA = 10  # bars
SLOW_EMA = 20  # bars

# -----------------------------------------------------------------------------


def main() -> None:
    instrument_id = InstrumentId.from_str(INSTRUMENT_ID)
    catalog = ParquetDataCatalog(str(constants.CATALOG_PATH))
    found = catalog.instruments(instrument_ids=[INSTRUMENT_ID])
    if not found:
        raise SystemExit(f"{INSTRUMENT_ID} is not in the catalog; run scripts/build_catalog.py")
    instrument = found[0]
    units = LOTS * int(instrument.lot_size.as_double())
    bar_type = f"{INSTRUMENT_ID}-1-MINUTE-LAST-EXTERNAL"
    # Options and futures also load their index (e.g. BANKNIFTY.NFO): Nautilus settles an
    # option still open at expiry against the index's last price.
    bar_types = [bar_type]
    if hasattr(instrument, "underlying"):
        bar_types.append(f"{instrument.underlying}.{instrument_id.venue}-1-MINUTE-LAST-EXTERNAL")

    # The simulated exchange and our account on it.
    venue = BacktestVenueConfig(
        name=instrument_id.venue.value,
        oms_type="NETTING",  # one position per instrument, like Angel One
        account_type="MARGIN",  # allows going short
        base_currency=constants.CURRENCY,
        starting_balances=[STARTING_BALANCE],
        # Angel One brokerage + NFO statutory and exchange charges on every fill.
        fee_model=ImportableFeeModelConfig(
            fee_model_path="nautilus_angel.adapters.angelone.fees:AngelOneFeeModel",
            config_path="nautilus_angel.adapters.angelone.fees:AngelOneFeeModelConfig",
            config={},
        ),
    )

    # What to load from the catalog.
    data = BacktestDataConfig(
        catalog_path=str(constants.CATALOG_PATH),
        # The Bar class itself: given the text "nautilus_trader.model.data:Bar",
        # Nautilus 1.231 ignores `bar_types` and loads every bar in the catalog.
        data_cls=Bar,
        bar_types=bar_types,
        start_time=ist_midnight_ns(START),
        end_time=ist_midnight_ns(END, next_day=True),
    )

    # The engine and the strategy it runs (by import path, built by Nautilus).
    engine = BacktestEngineConfig(
        trader_id="BACKTESTER-001",
        logging=LoggingConfig(log_level="WARNING"),
        strategies=[
            ImportableStrategyConfig(
                strategy_path="nautilus_trader.examples.strategies.ema_cross:EMACross",
                config_path="nautilus_trader.examples.strategies.ema_cross:EMACrossConfig",
                config={
                    "instrument_id": INSTRUMENT_ID,
                    "bar_type": bar_type,
                    "trade_size": str(units),
                    "fast_ema_period": FAST_EMA,
                    "slow_ema_period": SLOW_EMA,
                },
            ),
        ],
    )

    run = BacktestRunConfig(
        engine=engine,
        venues=[venue],
        data=[data],
        dispose_on_completion=False,  # keep the engine so its reports can be read
    )
    node = BacktestNode(configs=[run])
    results = node.run()
    if not results:
        raise SystemExit("Backtest failed: see the error above")

    report(node.get_engine(run.id), results[0], units)
    node.dispose()


def ist_midnight_ns(day: str, next_day: bool = False) -> int:
    """'2026-07-01' -> UNIX nanoseconds of that IST midnight (the next one if next_day)."""
    midnight = pd.Timestamp(day, tz=constants.IST)
    if next_day:
        midnight += pd.Timedelta(days=1)
    return midnight.value


def report(engine, result, units: int) -> None:
    """Print what the backtest did, from Nautilus's own result and reports."""
    positions = engine.trader.generate_positions_report()
    fills = engine.trader.generate_fills_report()
    pnl = result.stats_pnls.get(constants.CURRENCY, {})
    charges = fills["commission"].astype(str).str.split().str[0].astype(float).sum() if not fills.empty else 0.0
    net = pnl.get("PnL (total)", 0.0)  # Nautilus's P&L is after commissions

    print(f"\n=== {INSTRUMENT_ID}, {START} to {END}, {units} units per trade ===")
    print(f"Bars processed: {result.iterations:,}")
    print(f"Orders:         {result.total_orders}")
    print(f"Positions:      {result.total_positions}")
    print(f"Gross P&L:      Rs {net + charges:,.2f}")
    print(f"Charges:        Rs {charges:,.2f}")
    print(f"Net P&L:        Rs {net:,.2f}")
    print(f"Win rate:       {pnl.get('Win Rate', 0.0):.1%}")
    if not positions.empty:
        shown = positions[["entry", "peak_qty", "avg_px_open", "avg_px_close", "realized_pnl", "ts_opened", "ts_closed"]]
        print("\nFirst trades:")
        print(shown.head(5).to_string(index=False))


if __name__ == "__main__":
    main()
