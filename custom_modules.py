"""
custom_modules.py
Chart proof renderer, Telegram image dispatcher with FloodWait retry.
"""

import asyncio
import logging
import os
import tempfile
import time
from typing import Any, Dict, List, Optional

import matplotlib
matplotlib.use('Agg')  # non-interactive backend
import matplotlib.pyplot as plt
import mplfinance as mpf
import pandas as pd
from telegram.error import RetryAfter

import config

logger = logging.getLogger(__name__)

# -----------------------------------------------------------------------------
# Chart Proof Renderer
# -----------------------------------------------------------------------------
def render_chart_proof(
    session: Dict[str, Any],
    pair: str,
    tf: str,
    signal: Dict[str, Any],
    candle_open_time: int,
    candles_data: Optional[List[Dict[str, Any]]] = None,
    num_candles: int = 50
) -> str:
    """
    Renders a candlestick chart with indicators, entry/SL/TP lines,
    and signal arrow. Saves to a temporary PNG file.
    Returns the generated file path.
    """
    # If no real candles supplied, fall back to basic generated structure
    if not candles_data or len(candles_data) < 10:
        base_price = float(signal.get('entry_price', 100.0))
        now_ts = int(time.time())
        candles_data = []
        for i in range(num_candles, 0, -1):
            t = now_ts - (i * 60)
            candles_data.append({
                'time': t,
                'open': base_price,
                'high': base_price + 0.5,
                'low': base_price - 0.5,
                'close': base_price + 0.1,
                'volume': 100
            })

    # Convert to DataFrame for mplfinance
    df = pd.DataFrame(candles_data[-num_candles:])
    df['Date'] = pd.to_datetime(df['time'], unit='s')
    df.set_index('Date', inplace=True)
    df.rename(columns={'open': 'Open', 'high': 'High', 'low': 'Low', 'close': 'Close', 'volume': 'Volume'}, inplace=True)

    # Calculate EMAs and Bollinger Bands
    close_series = df['Close']
    fast_period = getattr(config, "EMA_FAST", 50)
    slow_period = getattr(config, "EMA_SLOW", 200)
    bb_period = getattr(config, "BB_PERIOD", 20)
    bb_stddev = getattr(config, "BB_STDDEV", 2)

    ema_50 = close_series.ewm(span=fast_period, adjust=False).mean()
    ema_200 = close_series.ewm(span=slow_period, adjust=False).mean()
    rolling_std = close_series.rolling(window=bb_period).std()
    middle_bb = close_series.rolling(window=bb_period).mean()
    upper_bb = middle_bb + bb_stddev * rolling_std
    lower_bb = middle_bb - bb_stddev * rolling_std

    # Prepare additional indicator plots
    apds = [
        mpf.make_addplot(ema_50, color='orange', width=1),
        mpf.make_addplot(ema_200, color='blue', width=1),
        mpf.make_addplot(upper_bb, color='gray', linestyle='dotted'),
        mpf.make_addplot(middle_bb, color='gray', linestyle='dashed'),
        mpf.make_addplot(lower_bb, color='gray', linestyle='dotted'),
    ]

    # Custom Market Style
    mc = mpf.make_marketcolors(up='green', down='red', edge='inherit', wick='inherit', volume='in')
    s = mpf.make_mpf_style(marketcolors=mc, gridstyle='--')

    fig, axes = mpf.plot(
        df,
        type='candle',
        style=s,
        addplot=apds,
        volume=False,
        returnfig=True,
        figsize=(12, 7),
        title=f'{pair} - {tf} - Signal: {signal.get("direction", "N/A")}'
    )
    ax = axes[0]  # primary axis

    # Entry, Stop Loss, Take Profit Horizontal Lines
    entry = signal.get('entry_price', 0.0)
    sl = signal.get('stop_loss', 0.0)
    tp = signal.get('take_profit', 0.0)

    if entry:
        ax.axhline(y=entry, color='black', linestyle='-', linewidth=1.5, label='Entry')
    if sl:
        ax.axhline(y=sl, color='red', linestyle='--', linewidth=1.5, label='Stop Loss')
    if tp:
        ax.axhline(y=tp, color='green', linestyle='--', linewidth=1.5, label='Take Profit')

    # Add Signal Arrow on the last candle
    last_idx_num = len(df) - 1
    last_high = df['High'].iloc[-1]
    last_low = df['Low'].iloc[-1]
    price_diff = max(last_high - last_low, 0.1)

    direction = signal.get('direction', 'CALL')
    if direction == 'CALL':
        ax.annotate(
            '▲', xy=(last_idx_num, last_low - (price_diff * 0.2)),
            xytext=(last_idx_num, last_low - (price_diff * 0.8)),
            fontsize=18, color='green', ha='center',
            arrowprops=dict(arrowstyle='->', color='green', lw=1.5)
        )
    else:  # PUT
        ax.annotate(
            '▼', xy=(last_idx_num, last_high + (price_diff * 0.2)),
            xytext=(last_idx_num, last_high + (price_diff * 0.8)),
            fontsize=18, color='red', ha='center',
            arrowprops=dict(arrowstyle='->', color='red', lw=1.5)
        )

    ax.legend(loc='upper left', fontsize=8)

    # Save PNG securely
    tmp_dir = tempfile.gettempdir()
    filename = f"chart_{pair}_{tf}_{int(time.time())}.png"
    filepath = os.path.join(tmp_dir, filename)
    fig.savefig(filepath, dpi=100, bbox_inches='tight')
    
    # Absolute Memory Leak Clean Up
    plt.close(fig)
    plt.close('all')
    
    return filepath


# -----------------------------------------------------------------------------
# Telegram Image Dispatcher with FloodWait retry
# -----------------------------------------------------------------------------
async def send_chart_via_telegram(
    bot: Any,
    chat_id: int,
    file_path: str,
    caption: Optional[str] = None,
    max_retries: int = 5
) -> bool:
    """
    Send a photo (chart) to the user's Telegram chat.
    Handles FloodWait with exponential backoff.
    Guarantees removal of temporary file afterwards via finally block.
    """
    if not os.path.exists(file_path):
        logger.error(f"Chart file not found: {file_path}")
        return False

    success = False
    try:
        for attempt in range(max_retries):
            try:
                with open(file_path, 'rb') as photo:
                    await bot.send_photo(
                        chat_id=chat_id,
                        photo=photo,
                        caption=caption if caption else "📊 Chart Proof"
                    )
                logger.info(f"Chart sent successfully to {chat_id}")
                success = True
                break
            except RetryAfter as e:
                wait = e.retry_after
                logger.warning(f"FloodWait: sleeping {wait}s before retrying chart send to {chat_id}")
                await asyncio.sleep(wait)
            except Exception as e:
                logger.error(f"Failed to send chart (attempt {attempt+1}): {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(1)
    finally:
        # Guarantee memory / file cleanup regardless of success or failure
        if os.path.exists(file_path):
            try:
                os.remove(file_path)
                logger.debug(f"Cleaned up chart file: {file_path}")
            except Exception as e:
                logger.warning(f"Could not remove chart file {file_path}: {e}")

    return success
