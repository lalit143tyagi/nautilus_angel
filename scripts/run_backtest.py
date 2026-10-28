"""
Run a backtest on the catalog with Nautilus's high-level API (BacktestNode).

Usage:
    edit the SETTINGS block below, then:
    uv run python scripts/run_backtest.py

Runs the OTM strangle seller (nautilus_angel.strategies.strangle_seller) on one expiry
of NIFTY or BANKNIFTY. Loads the index's 1-minute bars (for the scan, and for settling
any option still open at expiry) and the 1-minute bars of every option of that expiry
whose strike is a multiple of the strategy's strike step.
"""

from datetime import date

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
from nautilus_trader.model.instruments import OptionContract
from nautilus_trader.persistence.catalog import ParquetDataCatalog

from nautilus_angel import constants


# --- SETTINGS ----------------------------------------------------------------

UNDERLYING = "BANKNIFTY"
EXPIRY = "2026-07-28"
START = "2026-07-01"  # first IST date (inclusive)
END = "2026-07-28"  # last IST date (inclusive)
STARTING_BALANCE = "1000000 INR"
STRATEGY = {
    "strike_step": 500,  # only multiples of 500
    "min_otm": 500,  # points out of the money, at least
    "strangle_count": 5,
    "bar_minutes": 1,  # combined-premium timeframe: 1, 5, 15, 45, 75 ...
    "body_min_pct": 0.05,
    "body_max_pct": 0.10,
    "volume_period": 20,
    "trailing_stop_pct": 0.10,
    "trade_lots": 1,  # per leg
    "max_open": 1,  # most strangles short at once; 0 = no limit
    "exit_minutes_before_close": 10,  # on the expiry day
}

# -----------------------------------------------------------------------------


def main() -> None:
    catalog = ParquetDataCatalog(str(constants.CATALOG_PATH))
    option_ids = chain_ids(catalog)
    if not option_ids:
        raise SystemExit(f"No {UNDERLYING} options expiring {EXPIRY} in the catalog")
    index_bar_type = f"{UNDERLYING}.{constants.NFO}-1-MINUTE-LAST-EXTERNAL"

    # The simulated exchange and our account on it.
    venue = BacktestVenueConfig(
        name=constants.NFO,
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

    # What to load from the catalog. data_cls is the Bar class itself: given the text
    # "nautilus_trader.model.data:Bar", Nautilus 1.231 ignores `bar_types` and loads
    # every bar in the catalog.
    start, end = ist_midnight_ns(START), ist_midnight_ns(END, next_day=True)
    data = [
        BacktestDataConfig(
            catalog_path=str(constants.CATALOG_PATH),
            data_cls=Bar,
            bar_types=[index_bar_type],
            start_time=start,
            end_time=end,
        ),
        BacktestDataConfig(
            catalog_path=str(constants.CATALOG_PATH),
            data_cls=Bar,
            bar_types=[f"{option_id}-1-MINUTE-LAST-EXTERNAL" for option_id in option_ids],
            start_time=start,
            end_time=end,
        ),
    ]

    # The engine and the strategy it runs (by import path, built by Nautilus).
    engine = BacktestEngineConfig(
        trader_id="BACKTESTER-001",
        logging=LoggingConfig(log_level="WARNING"),
        strategies=[
            ImportableStrategyConfig(
                strategy_path="nautilus_angel.strategies.strangle_seller:StrangleSeller",
                config_path="nautilus_angel.strategies.strangle_seller:StrangleSellerConfig",
                config={"underlying": UNDERLYING, "expiry": EXPIRY, **STRATEGY},
            ),
        ],
    )

    run = BacktestRunConfig(
        engine=engine,
        venues=[venue],
        data=data,
        dispose_on_completion=False,  # keep the engine so its reports can be read
    )
    node = BacktestNode(configs=[run])
    results = node.run()
    if not results:
        raise SystemExit("Backtest failed: see the error above")

    report(node.get_engine(run.id), len(option_ids))
    node.dispose()


def chain_ids(catalog: ParquetDataCatalog) -> list[str]:
    """Ids of the underlying's options expiring on EXPIRY, strikes multiple of strike_step."""
    expiry = date.fromisoformat(EXPIRY)
    ids = []
    for instrument in catalog.instruments():
        if not isinstance(instrument, OptionContract) or instrument.underlying != UNDERLYING:
            continue
        expires = pd.Timestamp(instrument.expiration_ns, tz="UTC").tz_convert(constants.IST).date()
        strike = instrument.strike_price.as_double()
        if expires == expiry and strike % STRATEGY["strike_step"] == 0:
            ids.append(str(instrument.id))
    return ids


def ist_midnight_ns(day: str, next_day: bool = False) -> int:
    """'2026-07-01' -> UNIX nanoseconds of that IST midnight (the next one if next_day)."""
    midnight = pd.Timestamp(day, tz=constants.IST)
    if next_day:
        midnight += pd.Timedelta(days=1)
    return midnight.value


def report(engine, option_count: int) -> None:
    """Print what the backtest did, from Nautilus's own reports."""
    orders = engine.trader.generate_orders_report()
    fills = engine.trader.generate_fills_report()
    positions = engine.trader.generate_positions_report()

    charges = money(fills["commission"]).sum() if not fills.empty else 0.0
    net = money(positions["realized_pnl"]).sum() if not positions.empty else 0.0  # after charges
    entries = int((orders["side"] == "SELL").sum()) // 2 if not orders.empty else 0

    print(
        f"\n=== Strangle seller: {UNDERLYING} {EXPIRY}, {START} to {END}, "
        f"{STRATEGY['bar_minutes']}-minute bars ({option_count} options loaded) ==="
    )
    print(f"Strangles sold: {entries}")
    print(f"Gross P&L:      Rs {net + charges:>12,.2f}")
    print(f"Charges:        Rs {charges:>12,.2f}")
    print(f"Net P&L:        Rs {net:>12,.2f}")
    print(f"Max drawdown:   Rs {closed_trade_drawdown(positions):>12,.2f}  (on closed-trade P&L)")


def closed_trade_drawdown(positions: pd.DataFrame) -> float:
    """
    Worst fall of the running total of realized P&L, by closing time. The account balance
    can't be used: Nautilus credits an option's premium to the balance when it is sold
    and debits it when bought back, so the balance swings by whole premiums.
    """
    if positions.empty:
        return 0.0
    closed = positions.assign(pnl=money(positions["realized_pnl"]))
    equity = closed.groupby("ts_closed")["pnl"].sum().sort_index().cumsum()
    return min(0.0, (equity - equity.cummax().clip(lower=0)).min())


def money(values: pd.Series) -> pd.Series:
    """Nautilus reports money as text like '-170.90 INR'; return the numbers."""
    return values.astype(str).str.split().str[0].astype(float)


if __name__ == "__main__":
    main()
