"""
security_rms.py
Risk management, daily loss lock, news filter, ATR spike shield,
cooldown, and persistent state saving/loading.
"""

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import config

logger = logging.getLogger(__name__)

# Fallback values if missing in config.py
STATE_FILE_PATH = getattr(config, "STATE_FILE_PATH", "daily_state.json")
MAX_CONSECUTIVE_LOSSES = getattr(config, "MAX_CONSECUTIVE_LOSSES", 3)
COOLDOWN_SECONDS = getattr(config, "COOLDOWN_SECONDS", 900)
NEWS_BLACKOUT_MINUTES = getattr(config, "NEWS_BLACKOUT_MINUTES", 30)

# -----------------------------------------------------------------------------
# Persistent state file helper
# -----------------------------------------------------------------------------
def _read_full_state() -> Dict[int, Dict[str, Any]]:
    """Read the entire state JSON file. Returns dict of user_id -> state."""
    if not os.path.exists(STATE_FILE_PATH):
        return {}
    try:
        with open(STATE_FILE_PATH, 'r') as f:
            data = json.load(f)
        return {int(k): v for k, v in data.items()}
    except Exception as e:
        logger.error(f"Error reading state file: {e}")
        return {}

def _write_full_state(all_state: Dict[int, Dict[str, Any]]) -> None:
    """Write the entire state dict to the JSON file safely."""
    dir_path = os.path.dirname(STATE_FILE_PATH)
    if dir_path:
        os.makedirs(dir_path, exist_ok=True)
    try:
        with open(STATE_FILE_PATH, 'w') as f:
            json.dump(all_state, f, indent=2, default=str)
    except Exception as e:
        logger.error(f"Error writing state file: {e}")

def load_daily_state(user_id: int) -> Dict[str, Any]:
    """Return the persisted state for a user, or a default state if none exists."""
    all_state = _read_full_state()
    state = all_state.get(user_id)
    if state is not None:
        state = _reset_if_new_day(state)
        return state
    return {
        "daily_loss": 0.0,
        "locked": False,
        "lock_time": None,
        "consecutive_losses": 0,
        "cooldown_until": None,
        "atr_ema": {},
        "last_update_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "capital": None,
        "risk_percent": None,
    }

def save_daily_state(user_id: int, state: Dict[str, Any]) -> None:
    """Persist a user's state to the JSON file."""
    all_state = _read_full_state()
    all_state[user_id] = state
    _write_full_state(all_state)

def update_daily_state(user_id: int, pnl: float, session: Dict[str, Any]) -> Dict[str, Any]:
    """Update the user's daily state after a trade."""
    state = session.get("daily_state")
    if state is None:
        state = load_daily_state(user_id)
        session["daily_state"] = state

    if state.get("capital") is None:
        state["capital"] = session.get("capital")
    if state.get("risk_percent") is None:
        state["risk_percent"] = session.get("risk_percent")

    state = _reset_if_new_day(state)

    # Accumulate loss (only negative PnL is tracked)
    if pnl < 0:
        state["daily_loss"] += pnl
        state["consecutive_losses"] += 1
    else:
        state["consecutive_losses"] = 0

    state["daily_loss"] = min(0.0, float(state["daily_loss"]))

    capital = state.get("capital")
    risk_pct = state.get("risk_percent")
    if capital and risk_pct:
        loss_limit = capital * (risk_pct / 100.0)
        if abs(state["daily_loss"]) >= loss_limit:
            state["locked"] = True
            state["lock_time"] = datetime.now(timezone.utc).isoformat()
            logger.warning(f"User {user_id} daily loss limit hit. Locking until midnight.")

    if state["consecutive_losses"] >= MAX_CONSECUTIVE_LOSSES:
        state["cooldown_until"] = time.time() + COOLDOWN_SECONDS
        logger.info(f"Cooldown activated for {user_id} for {COOLDOWN_SECONDS} seconds.")

    save_daily_state(user_id, state)
    return state

def is_trading_allowed(
    session: Dict[str, Any],
    current_atr: Optional[float] = None,
    symbol: Optional[str] = None
) -> bool:
    """Check if trading is allowed considering locks, cooldown, news, and ATR spikes."""
    state = session.get("daily_state")
    if state is None:
        return True

    # 1. Daily lock
    if state.get("locked", False):
        state = _reset_if_new_day(state)
        if state.get("locked", False):
            logger.debug("Trading blocked: daily lock active.")
            return False

    # 2. Cooldown
    cooldown_until = state.get("cooldown_until")
    if cooldown_until and time.time() < cooldown_until:
        remaining = int(cooldown_until - time.time())
        logger.debug(f"Trading blocked: cooldown active for {remaining}s.")
        return False

    # 3. High-impact news blackout
    if _is_news_blackout():
        logger.debug("Trading blocked: news blackout window.")
        return False

    # 4. ATR spike
    if current_atr is not None and symbol is not None:
        if _check_atr_spike(symbol, current_atr, state):
            logger.debug("Trading blocked: ATR spike detected.")
            return False

    return True

def _reset_if_new_day(state: Dict[str, Any]) -> Dict[str, Any]:
    """Reset state if UTC date has rolled over."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    last_date = state.get("last_update_date")
    if last_date != today:
        logger.info("New trading day detected. Resetting daily state.")
        state["daily_loss"] = 0.0
        state["locked"] = False
        state["lock_time"] = None
        state["consecutive_losses"] = 0
        state["cooldown_until"] = None
        state["last_update_date"] = today
    return state

# -----------------------------------------------------------------------------
# News blackout filter
# -----------------------------------------------------------------------------
_HIGH_IMPACT_NEWS = [
    (datetime(2026, 8, 7, 13, 30, tzinfo=timezone.utc), "Non-Farm Payrolls"),
    (datetime(2026, 8, 12, 14, 0, tzinfo=timezone.utc), "FOMC Minutes"),
]

def _is_news_blackout() -> bool:
    """Return True if current time is within high-impact news blackout window."""
    now = datetime.now(timezone.utc)
    for event_time, desc in _HIGH_IMPACT_NEWS:
        start = event_time.timestamp() - (NEWS_BLACKOUT_MINUTES * 60)
        end = event_time.timestamp() + (NEWS_BLACKOUT_MINUTES * 60)
        if start <= now.timestamp() <= end:
            logger.info(f"News blackout active for event: {desc}")
            return True
    return False

# -----------------------------------------------------------------------------
# ATR spike shield
# -----------------------------------------------------------------------------
_ATR_EMA_ALPHA = 0.05
_ATR_SPIKE_MULTIPLIER = 3.0

def _check_atr_spike(symbol: str, current_atr: float, state: Dict[str, Any]) -> bool:
    """Detect unusual volatility spikes (>3x long-term average ATR)."""
    atr_ema_dict = state.setdefault("atr_ema", {})
    ema = atr_ema_dict.get(symbol)
    if ema is None:
        atr_ema_dict[symbol] = current_atr
        return False

    new_ema = _ATR_EMA_ALPHA * current_atr + (1 - _ATR_EMA_ALPHA) * ema
    atr_ema_dict[symbol] = new_ema

    if current_atr > new_ema * _ATR_SPIKE_MULTIPLIER:
        return True
    return False
