"""
Angel One SmartAPI facts (broker-specific; NSE market facts live in nautilus_angel.constants).
"""

# Angel One's instrument master: every instrument tradable today, as one JSON list
# (about 37 MB, refreshed daily). Public: no login needed.
INSTRUMENT_MASTER_URL = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"

# Seconds to wait for the instrument master download before giving up.
DOWNLOAD_TIMEOUT_SECS = 60

# Angel One brokerage on NFO futures and options: a flat Rs 20 per executed order
# (angelone.in/exchange-transaction-charges, checked 2026-10-24). Charged once per
# order however many fills it takes; also on orders that expire or are exercised.
BROKERAGE_PER_ORDER = 20.0
