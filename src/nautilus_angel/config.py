"""
Project-wide constants: folder locations and venue names.

The Nautilus configs themselves (venue, data, engine, strategy) live in
scripts/run_backtest.py, following Nautilus's high-level backtest template.
"""

from pathlib import Path


# This file is src/nautilus_angel/config.py, so the project root is 2 folders up.
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Nautilus ParquetDataCatalog: instruments + 1-minute bars (historical now, live later).
CATALOG_PATH = PROJECT_ROOT / "catalog"

# Drop historical Parquet files here (one file per expiry); catalog_setup.py reads it.
HISTORICAL_DATA_PATH = PROJECT_ROOT / "data" / "historical"

# run_backtest.py saves each run's reports here.
REPORTS_PATH = PROJECT_ROOT / "reports"

# One Nautilus venue per Angel One exchange segment.
NSE = "NSE"  # equities and indices
NFO = "NFO"  # futures and options
