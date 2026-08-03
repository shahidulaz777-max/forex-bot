"""
config.py
Centralized configuration for Quotex Phantom Bot.
All constants, tokens, and default parameters.
"""

import os
from pathlib import Path

# -----------------------------------------------------------------------------
# Telegram & Access Control
# -----------------------------------------------------------------------------
TELEGRAM_BOT_TOKEN: str = "8926811082:AAH4T7FmcB2pcrwHuLA18TnPF3LV2mktaDc"

# List of Telegram user IDs allowed to interact with the bot.
# Example: [123456789, 987654321]
AUTHORIZED_USER_IDS: list[int] = []

# -----------------------------------------------------------------------------
# System Timing & Synchronisation
# -----------------------------------------------------------------------------
DEFAULT_SIGNAL_TIMEFRAME: str = "1m"          # Selected trading timeframe
DEFAULT_SR_ZONE_TIMEFRAME: str = "5m"         # Higher timeframe for S/R zones
NTP_SERVER: str = "pool.ntp.org"

# Pre-calculation and signal dispatch windows (seconds before candle close)
PRE_CALCULATION_SECONDS: int = 8
SIGNAL_DISPATCH_SECONDS: int = 5

# Candle close alignment tolerance (seconds)
CANDLE_BOUNDARY_TOLERANCE: float = 0.2

# -----------------------------------------------------------------------------
# Technical Indicator Default Parameters
# -----------------------------------------------------------------------------
# EMA periods
EMA_FAST: int = 50
EMA_SLOW: int = 200

# RSI
RSI_PERIOD: int = 14

# ATR
ATR_PERIOD: int = 14

# Bollinger Bands
BB_PERIOD: int = 20
BB_STDDEV: float = 2.0

# ADX thresholds
ADX_SIDEWAYS_LIMIT: int = 20          # ADX < 20  -> sideways / ranging
ADX_STRONG_TREND_MINIMUM: int = 25     # ADX > 25 -> strong trend

# Candle Pressure Index thresholds (buyer / seller dominance ratio)
PRESSURE_BULLISH_MIN_RATIO: float = 0.7   # at least 70% of range from low
PRESSURE_BEARISH_MIN_RATIO: float = 0.7   # at least 70% of range from high

# -----------------------------------------------------------------------------
# Risk Management Defaults & Pro Filters
# -----------------------------------------------------------------------------
MAX_CONSECUTIVE_LOSSES: int = 2            # trigger cool-down after 2 losses
COOLDOWN_MINUTES: int = 15                 # pause trading for 15 minutes
COOLDOWN_SECONDS: int = COOLDOWN_MINUTES * 60

# Dynamic ATR buffer zone multiplier (ATR * multiplier = buffer width)
ATR_BUFFER_MULTIPLIER: float = 0.2         # typical 3-5 pip buffer for most pairs

# Volatility Spike Shield (Multiplied against standard ATR to detect abnormal candles)
VOLATILITY_SPIKE_THRESHOLD: float = 2.5

# Reference pip values for Decimal Precision Scaling
DEFAULT_PIP_VALUE: float = 0.0001
JPY_PIP_VALUE: float = 0.01

# -----------------------------------------------------------------------------
# State Persistence (24-hour calendar day-lock)
# -----------------------------------------------------------------------------
STATE_DIR: str = "data"
STATE_FILE_PATH: str = os.path.join(STATE_DIR, "state_cache.json")

# Ensure state directory exists at module load time
Path(STATE_DIR).mkdir(parents=True, exist_ok=True)

# -----------------------------------------------------------------------------
# Additional OTC / Weekend / News Filter Constants
# -----------------------------------------------------------------------------
# Forex Factory Red Folder news - minutes before and after to block trading
NEWS_BLACKOUT_MINUTES: int = 15

# OTC market flag (can be overridden dynamically)
ALLOW_OTC_TRADING: bool = False

# -----------------------------------------------------------------------------
# Performance & Diagnostics
# -----------------------------------------------------------------------------
WATCHDOG_INTERVAL: int = 10
MAX_CLOCK_DRIFT: float = 1.0
