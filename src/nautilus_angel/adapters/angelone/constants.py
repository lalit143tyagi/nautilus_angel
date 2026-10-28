"""
Angel One SmartAPI facts (broker-specific; NSE market facts live in nautilus_angel.constants).
"""

# Angel One's instrument master: every instrument tradable today, as one JSON list
# (about 37 MB, refreshed daily). Public: no login needed.
INSTRUMENT_MASTER_URL = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"

# Seconds to wait for the instrument master download before giving up.
DOWNLOAD_TIMEOUT_SECS = 60
