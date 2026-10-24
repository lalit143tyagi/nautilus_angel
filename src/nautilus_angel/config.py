"""
Central configuration for backtests.

Holds only settings shared by every symbol. Instruments and bar types are
always passed in by the caller, so nothing here is tied to a specific symbol.
"""

from pathlib import Path

from nautilus_trader.config import BacktestDataConfig
from nautilus_trader.config import BacktestEngineConfig
from nautilus_trader.config import BacktestVenueConfig
from nautilus_trader.config import ImportableFeeModelConfig
from nautilus_trader.backtest.config import MarginModelConfig
from nautilus_trader.config import LoggingConfig
from nautilus_trader.model.data import Bar


# --- Paths -------------------------------------------------------------------

# This file is src/nautilus_angel/config.py, so the project root is 2 folders up.
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# The ParquetDataCatalog lives here (historical now, recorded live data later).
CATALOG_PATH = PROJECT_ROOT / "catalog"

# Drop historical OHLCV Parquet files here; scripts/catalog_setup.py reads it by default.
HISTORICAL_DATA_PATH = PROJECT_ROOT / "data" / "historical"


# --- Account / venue defaults ------------------------------------------------

# One Nautilus venue per Angel One exchange segment.
NSE = "NSE"  # equities and indices
NFO = "NFO"  # futures and options

DEFAULT_VENUE = NSE
BASE_CURRENCY = "INR"
STARTING_BALANCE = "1000000 INR"


def venue_config(
    venue: str = DEFAULT_VENUE,
    equity_product: str = "INTRADAY",
    margin_rates: dict[str, float] | None = None,
) -> BacktestVenueConfig:
    """
    Return the simulated exchange (venue) and the account we trade it with,
    charging Angel One fees (nautilus_angel.fees) and NSE-style margin
    (nautilus_angel.margin). `margin_rates` overrides margin.DEFAULTS.
    """
    return BacktestVenueConfig(
        name=venue,
        oms_type="NETTING",
        account_type="MARGIN",
        default_leverage=1.0,
        base_currency=BASE_CURRENCY,
        starting_balances=[STARTING_BALANCE],
        fee_model=ImportableFeeModelConfig(
            fee_model_path="nautilus_angel.fees:AngelOneFeeModel",
            config_path="nautilus_angel.fees:AngelOneFeeModelConfig",
            config={"equity_product": equity_product},
        ),
        margin_model=MarginModelConfig(
            model_type="nautilus_angel.margin:NseMarginModel",
            config=margin_rates or {},
        ),
    )


def data_config(bar_types: list[str]) -> BacktestDataConfig:
    """Return a config telling the backtest which bars to read from the catalog."""
    return BacktestDataConfig(
        catalog_path=str(CATALOG_PATH),
        # Must be the Bar class, not the "module:Bar" string: with the string,
        # Nautilus 1.231 ignores `bar_types` and loads every bar in the catalog.
        data_cls=Bar,
        bar_types=bar_types,
    )


def engine_config(log_level: str = "INFO") -> BacktestEngineConfig:
    """Return the backtest engine (kernel) config. Strategies are added later."""
    return BacktestEngineConfig(
        trader_id="BACKTESTER-001",
        logging=LoggingConfig(log_level=log_level),
        strategies=[],
    )
