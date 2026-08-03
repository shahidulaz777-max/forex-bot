"""
strategy_engine.py
Multi-timeframe S/R zones, SBR/RBS, indicator confluence,
candle pressure, repainting shield, dynamic pip detection.
"""

import asyncio
import logging
import math
import random
import time
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

import config

logger = logging.getLogger(__name__)

# Timeframe to seconds map fallback
TIMEFRAME_TO_SECONDS_MAP = {
    "1m": 60,
    "2m": 120,
    "3m": 180,
    "5m": 300,
    "10m": 600,
}

# -----------------------------------------------------------------------------
# Dynamic pip scale detector
# -----------------------------------------------------------------------------
def detect_pip_value(symbol: str, sample_price: float) -> float:
    """
    Returns pip value based on price decimal places and symbol name.
    - Forex (non-JPY, non-crypto) with 5 decimals: pip = 0.0001
    - JPY pairs: pip = 0.01
    - Crypto: pip = 1.0 or 0.1
    """
    symbol_upper = symbol.upper()
    if "JPY" in symbol_upper:
        return 0.01
    if any(crypto in symbol_upper for crypto in ["BTC", "ETH", "XRP", "LTC", "BCH"]):
        return 1.0

    s = f"{sample_price:.10f}".rstrip('0')
    if '.' not in s:
        return 1.0
    
    parts = s.split('.')
    if len(parts) < 2:
        return 0.0001
        
    decimals = len(parts[1])
    if decimals >= 4:
        return 0.0001
    elif decimals in (2, 3):
        return 0.01
    else:
        return 0.0001

# -----------------------------------------------------------------------------
# Mock price data generator
# -----------------------------------------------------------------------------
class MockPriceFeed:
    """Generates realistic OHLCV candles using a random walk with mean reversion."""

    def __init__(self, start_price: float = 1.1000, volatility: float = 0.0002):
        self.last_price = start_price
        self.volatility = volatility

    def generate_candles(self, count: int, timeframe_seconds: int) -> List[Dict[str, Any]]:
        candles = []
        now = int(time.time())
        start_time = now - (now % timeframe_seconds) - count * timeframe_seconds
        price = self.last_price
        for i in range(count):
            open_time = start_time + i * timeframe_seconds
            trend = (self.last_price - price) * 0.001
            change = float(np.random.normal(trend, self.volatility))
            open_price = price
            close_price = price + change
            high_price = max(open_price, close_price) + abs(float(np.random.normal(0, self.volatility / 2)))
            low_price = min(open_price, close_price) - abs(float(np.random.normal(0, self.volatility / 2)))
            volume = random.randint(100, 1000)
            candles.append({
                'time': open_time,
                'open': round(open_price, 6),
                'high': round(high_price, 6),
                'low': round(low_price, 6),
                'close': round(close_price, 6),
                'volume': volume,
            })
            price = close_price
        self.last_price = price
        return candles

_mock_feed = MockPriceFeed()

async def fetch_candles(symbol: str, timeframe: str, count: int) -> List[Dict[str, Any]]:
    """Fetch OHLCV candles (Mock implementation - seamlessly replaceable with Broker API)."""
    tf_clean = timeframe.lower().strip()
    timeframe_seconds = TIMEFRAME_TO_SECONDS_MAP.get(tf_clean, 60)
    await asyncio.sleep(0.001)
    return _mock_feed.generate_candles(count, timeframe_seconds)

# -----------------------------------------------------------------------------
# Indicator calculations (Numpy Arrays)
# -----------------------------------------------------------------------------
def ema(data: np.ndarray, period: int) -> np.ndarray:
    """Exponential Moving Average."""
    alpha = 2 / (period + 1)
    result = np.zeros_like(data)
    result[0] = data[0]
    for i in range(1, len(data)):
        result[i] = alpha * data[i] + (1 - alpha) * result[i - 1]
    return result

def rsi(data: np.ndarray, period: int) -> np.ndarray:
    """Relative Strength Index with safe zero handling."""
    deltas = np.diff(data)
    gain = np.where(deltas > 0, deltas, 0)
    loss = np.where(deltas < 0, -deltas, 0)
    
    avg_gain = np.zeros_like(data)
    avg_loss = np.zeros_like(data)
    
    if len(data) <= period:
        return np.full_like(data, 50.0)

    avg_gain[period] = np.mean(gain[:period])
    avg_loss[period] = np.mean(loss[:period])
    
    for i in range(period + 1, len(data)):
        avg_gain[i] = (avg_gain[i - 1] * (period - 1) + gain[i - 1]) / period
        avg_loss[i] = (avg_loss[i - 1] * (period - 1) + loss[i - 1]) / period
        
    rs = avg_gain / np.where(avg_loss == 0, 1e-10, avg_loss)
    rsi_vals = 100 - (100 / (1 + rs))
    rsi_vals[:period] = 50.0  # Fill NaN space with neutral 50
    return rsi_vals

def atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int) -> np.ndarray:
    """Average True Range."""
    prev_close = np.roll(close, 1)
    prev_close[0] = close[0]
    tr = np.maximum(
        high - low,
        np.maximum(np.abs(high - prev_close), np.abs(low - prev_close))
    )
    atr_vals = np.zeros_like(tr)
    if len(tr) <= period:
        return np.full_like(tr, 0.0001)
        
    atr_vals[period - 1] = np.mean(tr[:period])
    for i in range(period, len(tr)):
        atr_vals[i] = (atr_vals[i - 1] * (period - 1) + tr[i]) / period
    atr_vals[:period - 1] = atr_vals[period - 1]
    return atr_vals

def bollinger_bands(close: np.ndarray, period: int, std_dev: float) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Upper, Middle, Lower Bollinger Bands."""
    sma = np.zeros_like(close)
    std = np.zeros_like(close)
    
    for i in range(len(close)):
        if i < period - 1:
            sma[i] = close[i]
            std[i] = 0.0
        else:
            window = close[i - period + 1:i + 1]
            sma[i] = np.mean(window)
            std[i] = np.std(window)
            
    upper = sma + std_dev * std
    lower = sma - std_dev * std
    return upper, sma, lower

def adx(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int) -> np.ndarray:
    """Average Directional Index."""
    prev_close = np.roll(close, 1)
    prev_close[0] = close[0]
    tr = np.maximum(high - low, np.maximum(np.abs(high - prev_close), np.abs(low - prev_close)))
    
    up_move = high - np.roll(high, 1)
    down_move = np.roll(low, 1) - low
    up_move[0] = 0
    down_move[0] = 0
    
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0)
    
    smoothed_tr = np.zeros_like(tr)
    smoothed_plus_dm = np.zeros_like(plus_dm)
    smoothed_minus_dm = np.zeros_like(minus_dm)
    
    if len(tr) < period * 2:
        return np.full_like(tr, 20.0)

    smoothed_tr[period - 1] = np.sum(tr[:period])
    smoothed_plus_dm[period - 1] = np.sum(plus_dm[:period])
    smoothed_minus_dm[period - 1] = np.sum(minus_dm[:period])
    
    for i in range(period, len(tr)):
        smoothed_tr[i] = smoothed_tr[i - 1] - (smoothed_tr[i - 1] / period) + tr[i]
        smoothed_plus_dm[i] = smoothed_plus_dm[i - 1] - (smoothed_plus_dm[i - 1] / period) + plus_dm[i]
        smoothed_minus_dm[i] = smoothed_minus_dm[i - 1] - (smoothed_minus_dm[i - 1] / period) + minus_dm[i]
        
    di_plus = 100 * (smoothed_plus_dm / np.where(smoothed_tr == 0, 1e-10, smoothed_tr))
    di_minus = 100 * (smoothed_minus_dm / np.where(smoothed_tr == 0, 1e-10, smoothed_tr))
    dx = 100 * np.abs(di_plus - di_minus) / np.where((di_plus + di_minus) == 0, 1e-10, (di_plus + di_minus))
    
    adx_vals = np.zeros_like(dx)
    for i in range(len(dx)):
        if i < 2 * period - 1:
            adx_vals[i] = 20.0
        else:
            adx_vals[i] = np.mean(dx[i - period + 1:i + 1])
            
    return adx_vals

# -----------------------------------------------------------------------------
# Support/Resistance zone identification
# -----------------------------------------------------------------------------
def find_swing_points(high: np.ndarray, low: np.ndarray, order: int = 2) -> Tuple[np.ndarray, np.ndarray]:
    """Identify swing highs and lows."""
    swing_highs = np.zeros_like(high, dtype=bool)
    swing_lows = np.zeros_like(low, dtype=bool)
    for i in range(order, len(high) - order):
        if all(high[i] >= high[i - order:i]) and all(high[i] >= high[i + 1:i + order + 1]):
            swing_highs[i] = True
        if all(low[i] <= low[i - order:i]) and all(low[i] <= low[i + 1:i + order + 1]):
            swing_lows[i] = True
    return swing_highs, swing_lows

def identify_sr_zones(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    atr_val: float,
    buffer_mult: float = getattr(config, "ATR_BUFFER_MULTIPLIER", 1.5),
    pip_value: float = 0.0001
) -> List[Dict[str, Any]]:
    """Returns list of support/resistance zones with dynamic buffer width."""
    swing_highs, swing_lows = find_swing_points(high, low, order=3)
    zones = []
    
    for i in np.where(swing_highs)[0]:
        zones.append({
            'price': float(high[i]),
            'type': 'resistance',
            'buffer': atr_val * buffer_mult,
        })
    for i in np.where(swing_lows)[0]:
        zones.append({
            'price': float(low[i]),
            'type': 'support',
            'buffer': atr_val * buffer_mult,
        })
        
    if not zones:
        return zones

    zones.sort(key=lambda z: z['price'])
    merged = []
    current = zones[0].copy()
    
    for z in zones[1:]:
        if z['type'] == current['type'] and abs(z['price'] - current['price']) <= current['buffer'] * 2:
            if current['type'] == 'resistance':
                current['price'] = max(current['price'], z['price'])
            else:
                current['price'] = min(current['price'], z['price'])
            current['buffer'] = max(current['buffer'], z['buffer'])
        else:
            merged.append(current)
            current = z.copy()
    merged.append(current)
    return merged

def candle_pressure(open_: float, high: float, low: float, close: float) -> Tuple[float, float]:
    """Returns bullish and bearish pressure ratios."""
    if high == low:
        return 0.5, 0.5
    bullish = (close - low) / (high - low)
    bearish = (high - close) / (high - low)
    return float(bullish), float(bearish)

# -----------------------------------------------------------------------------
# Main signal analysis function
# -----------------------------------------------------------------------------
async def analyze_signal(
    session: Dict[str, Any],
    pair: str,
    tf: str,
    candle_open_time: int
) -> Optional[Dict[str, Any]]:
    """Perform multi-timeframe analysis and return a trade signal or None."""
    data_current = await fetch_candles(pair, tf, count=100)
    if len(data_current) < 50:
        logger.error("Not enough data for current timeframe")
        return None

    data_higher_5m = await fetch_candles(pair, "5m", count=100)
    data_higher_10m = await fetch_candles(pair, "10m", count=100)

    arr_current = {
        'time': np.array([c['time'] for c in data_current]),
        'open': np.array([c['open'] for c in data_current]),
        'high': np.array([c['high'] for c in data_current]),
        'low': np.array([c['low'] for c in data_current]),
        'close': np.array([c['close'] for c in data_current]),
    }

    current_idx = np.where(arr_current['time'] == candle_open_time)[0]
    if len(current_idx) == 0:
        idx = len(arr_current['time']) - 1
    else:
        idx = current_idx[0]

    if idx < 10:
        logger.warning("Not enough closed candles before signal candle.")
        return None

    closed_high = arr_current['high'][:idx]
    closed_low = arr_current['low'][:idx]
    closed_close = arr_current['close'][:idx]

    current_candle = {
        'open': float(arr_current['open'][idx]),
        'high': float(arr_current['high'][idx]),
        'low': float(arr_current['low'][idx]),
        'close': float(arr_current['close'][idx]),
    }

    # 1. Indicators on CLOSED candles
    ema_fast_p = getattr(config, "EMA_FAST", 50)
    ema_slow_p = getattr(config, "EMA_SLOW", 200)
    rsi_p = getattr(config, "RSI_PERIOD", 14)
    atr_p = getattr(config, "ATR_PERIOD", 14)
    bb_p = getattr(config, "BB_PERIOD", 20)
    bb_std = getattr(config, "BB_STDDEV", 2.0)

    ema_50 = ema(closed_close, ema_fast_p)
    ema_200 = ema(closed_close, ema_slow_p)
    rsi_vals = rsi(closed_close, rsi_p)
    atr_vals = atr(closed_high, closed_low, closed_close, atr_p)
    upper_bb, middle_bb, lower_bb = bollinger_bands(closed_close, bb_p, bb_std)
    adx_vals = adx(closed_high, closed_low, closed_close, 14)

    last_ema_50 = float(ema_50[-1])
    last_ema_200 = float(ema_200[-1])
    last_rsi = float(rsi_vals[-1])
    last_atr = float(atr_vals[-1])
    last_upper_bb = float(upper_bb[-1])
    last_lower_bb = float(lower_bb[-1])
    last_adx = float(adx_vals[-1])

    # 2. Dynamic pip value
    sample_price = float(closed_close[-1])
    pip_value = detect_pip_value(pair, sample_price)

    # 3. Higher timeframe S/R zones
    arr_5m = {
        'high': np.array([c['high'] for c in data_higher_5m]),
        'low': np.array([c['low'] for c in data_higher_5m]),
        'close': np.array([c['close'] for c in data_higher_5m]),
    }
    atr_5m = atr(arr_5m['high'], arr_5m['low'], arr_5m['close'], atr_p)[-1] if len(arr_5m['close']) >= 50 else last_atr
    zones_5m = identify_sr_zones(arr_5m['high'], arr_5m['low'], arr_5m['close'], atr_5m, getattr(config, "ATR_BUFFER_MULTIPLIER", 1.5), pip_value)

    arr_10m = {
        'high': np.array([c['high'] for c in data_higher_10m]),
        'low': np.array([c['low'] for c in data_higher_10m]),
        'close': np.array([c['close'] for c in data_higher_10m]),
    }
    atr_10m = atr(arr_10m['high'], arr_10m['low'], arr_10m['close'], atr_p)[-1] if len(arr_10m['close']) >= 50 else last_atr
    zones_10m = identify_sr_zones(arr_10m['high'], arr_10m['low'], arr_10m['close'], atr_10m, getattr(config, "ATR_BUFFER_MULTIPLIER", 1.5), pip_value)

    combined_zones = zones_5m + zones_10m
    current_price = current_candle['close']
    relevant_zones = [z for z in combined_zones if abs(z['price'] - current_price) <= 5 * z['buffer']]

    # 4. Evaluate current candle pressure
    bullish_pressure, bearish_pressure = candle_pressure(
        current_candle['open'], current_candle['high'], current_candle['low'], current_candle['close']
    )

    touch_support = False
    touch_resistance = False
    for zone in relevant_zones:
        upper_bound = zone['price'] + zone['buffer'] / 2
        lower_bound = zone['price'] - zone['buffer'] / 2
        if zone['type'] == 'support' and current_candle['low'] <= upper_bound:
            touch_support = True
        elif zone['type'] == 'resistance' and current_candle['high'] >= lower_bound:
            touch_resistance = True

    # 5. Confluence Check
    direction = None
    entry_price = current_price

    trend_up = last_ema_50 >= last_ema_200
    trend_down = last_ema_50 < last_ema_200

    adx_limit = getattr(config, "ADX_SIDEWAYS_LIMIT", 20)
    adx_strong = getattr(config, "ADX_STRONG_TREND_MINIMUM", 25)

    if last_adx < adx_limit:
        return None

    rsi_ok_buy = 30 < last_rsi < 70
    rsi_ok_sell = 30 < last_rsi < 70

    bb_lower_touch = current_price <= last_lower_bb * 1.002
    bb_upper_touch = current_price >= last_upper_bb * 0.998

    bull_p_min = getattr(config, "PRESSURE_BULLISH_MIN_RATIO", 0.6)
    bear_p_min = getattr(config, "PRESSURE_BEARISH_MIN_RATIO", 0.6)

    bull_pressure_ok = bullish_pressure >= bull_p_min
    bear_pressure_ok = bearish_pressure >= bear_p_min

    if trend_up and (last_adx >= adx_strong) and bull_pressure_ok and rsi_ok_buy:
        direction = "CALL"
        stop_loss = current_candle['low'] - (last_atr * 0.5)
        take_profit = entry_price + (last_atr * 2.0)
    elif trend_down and (last_adx >= adx_strong) and bear_pressure_ok and rsi_ok_sell:
        direction = "PUT"
        stop_loss = current_candle['high'] + (last_atr * 0.5)
        take_profit = entry_price - (last_atr * 2.0)

    if direction is None:
        return None

    signal = {
        'direction': direction,
        'entry_price': round(entry_price, 6),
        'stop_loss': round(stop_loss, 6),
        'take_profit': round(take_profit, 6),
        'indicators': {
            'ema_50': round(last_ema_50, 6),
            'ema_200': round(last_ema_200, 6),
            'rsi': round(last_rsi, 2),
            'atr': round(last_atr, 6),
            'adx': round(last_adx, 2),
            'bullish_pressure': round(bullish_pressure, 2),
            'bearish_pressure': round(bearish_pressure, 2),
            'pip_value': pip_value,
        }
    }
    logger.info(f"Signal generated: {signal['direction']} on {pair} at {entry_price}")
    return signal
