import asyncio
import io
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, Tuple, List, Optional
import aiohttp
import pandas as pd
import yfinance as yf
import mplfinance as mpf

logger = logging.getLogger("StrategyEngine")

# =====================================================================
# 1. REAL MARKET DATA FILTER (NO OTC)
# =====================================================================
class RealMarketData:
    """
    Handles Real Forex Markets from official feeds (Yahoo Finance API).
    Strictly filters out all OTC (Over-The-Counter) synthetic assets.
    """
    REAL_ASSET_MAP = {
        "EURUSD": "EURUSD=X",
        "GBPUSD": "GBPUSD=X",
        "USDJPY": "USDJPY=X",
        "AUDUSD": "AUDUSD=X",
        "USDCAD": "USDCAD=X",
        "EURGBP": "EURGBP=X",
        "USDCHF": "USDCHF=X",
        "NZDUSD": "NZDUSD=X"
    }

    @classmethod
    def get_supported_assets(cls) -> List[str]:
        return list(cls.REAL_ASSET_MAP.keys())

    @classmethod
    def is_valid_real_market(cls, symbol: str) -> bool:
        clean_symbol = symbol.upper().replace("_OTC", "").replace(" (OTC)", "").strip()
        return clean_symbol in cls.REAL_ASSET_MAP and "OTC" not in symbol.upper()

    @classmethod
    async def fetch_candles(cls, symbol: str, timeframe: str = "1m", period: str = "1d") -> pd.DataFrame:
        """Fetches historical real market candles asynchronously."""
        if not cls.is_valid_real_market(symbol):
            raise ValueError(f"OTC or Unsupported asset '{symbol}' rejected. Real markets only!")

        ticker = cls.REAL_ASSET_MAP[symbol]
        
        def _download():
            data = yf.download(ticker, period=period, interval=timeframe, progress=False)
            if data.empty:
                return pd.DataFrame()
            if isinstance(data.columns, pd.MultiIndex):
                data.columns = data.columns.get_level_values(0)
            
            df = data[['Open', 'High', 'Low', 'Close', 'Volume']].dropna()
            df.index = pd.to_datetime(df.index)
            return df

        df = await asyncio.to_thread(_download)
        return df


# =====================================================================
# 2. HIGH-IMPACT NEWS FILTER ENGINE
# =====================================================================
class NewsFilter:
    """
    Fetches Economic Calendar data (Forex Factory JSON API) to automatically
    pause signals during High-Impact news releases (+/- 30 mins).
    """
    NEWS_URL = "https://nss.forexfactory.com/calendar/v1/week.json"

    def __init__(self, buffer_minutes: int = 30):
        self.buffer_minutes = buffer_minutes
        self.cached_events: List[Dict[str, Any]] = []
        self.last_fetch: float = 0.0

    async def fetch_economic_calendar(self):
        """Fetches weekly high-impact events."""
        now = time.time()
        if now - self.last_fetch < 3600 and self.cached_events:
            return

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(self.NEWS_URL, timeout=10) as response:
                    if response.status == 200:
                        data = await response.json()
                        self.cached_events = [
                            event for event in data 
                            if str(event.get("impact", "")).lower() in ["high", "3"]
                        ]
                        self.last_fetch = now
                        logger.info(f"[News Filter] Cached {len(self.cached_events)} high-impact events.")
        except Exception as e:
            logger.error(f"[News Filter Error]: {e}")

    async def is_news_blocked(self, symbol: str) -> Tuple[bool, str]:
        """Checks if trading pair is blocked due to high-impact news on involved currencies."""
        await self.fetch_economic_calendar()
        
        base_curr = symbol[:3].upper()
        quote_curr = symbol[3:].upper()
        now_utc = datetime.now(timezone.utc)
        buffer_delta = timedelta(minutes=self.buffer_minutes)

        for event in self.cached_events:
            event_currency = str(event.get("country", "")).upper()
            if event_currency in [base_curr, quote_curr]:
                event_date_str = str(event.get("date", ""))
                try:
                    event_time = datetime.fromisoformat(event_date_str.replace("Z", "+00:00"))
                    if (event_time - buffer_delta) <= now_utc <= (event_time + buffer_delta):
                        title = event.get("title", "High-Impact Event")
                        return True, f"High-Impact News: '{title}' ({event_currency}) at {event_time.strftime('%H:%M UTC')}"
                except ValueError:
                    continue

        return False, "No high-impact news active."


# =====================================================================
# 3. MULTI-TIMEFRAME PRICE ACTION & S/R STRATEGY
# =====================================================================
class MTFPriceActionStrategy:
    """
    Multi-Timeframe Strategy Engine:
    - 5m Chart: Identifies key Support and Resistance levels.
    - 1m Chart: Triggers Bullish/Bearish Engulfing breakouts at 5m zones.
    """
    def __init__(self, sr_lookback_candles: int = 50, tolerance_pct: float = 0.12):
        self.sr_lookback = sr_lookback_candles
        self.tolerance_pct = tolerance_pct / 100.0

    def calculate_5m_sr_levels(self, df_5m: pd.DataFrame) -> Tuple[float, float]:
        """Calculates major Support and Resistance from 5m data using Swing Extrema."""
        recent_data = df_5m if len(df_5m) < self.sr_lookback else df_5m.tail(self.sr_lookback)
        support_zone = float(recent_data['Low'].min())
        resistance_zone = float(recent_data['High'].max())
        return support_zone, resistance_zone

    def analyze(self, df_1m: pd.DataFrame, df_5m: pd.DataFrame) -> Dict[str, Any]:
        """Executes Multi-Timeframe Price Action logic."""
        if len(df_1m) < 3 or len(df_5m) < 10:
            return {"signal": "HOLD", "reason": "Insufficient candles"}

        support_5m, resistance_5m = self.calculate_5m_sr_levels(df_5m)

        prev_candle = df_1m.iloc[-2]  # Candle N-1 (Completed)
        curr_candle = df_1m.iloc[-1]  # Candle N (Signal completed candle)

        prev_open, prev_high, prev_low, prev_close = prev_candle['Open'], prev_candle['High'], prev_candle['Low'], prev_candle['Close']
        curr_open, curr_high, curr_low, curr_close = curr_candle['Open'], curr_candle['High'], curr_candle['Low'], curr_candle['Close']

        near_support = abs(min(prev_low, curr_low) - support_5m) / support_5m <= self.tolerance_pct
        near_resistance = abs(max(prev_high, curr_high) - resistance_5m) / resistance_5m <= self.tolerance_pct

        # CALL LOGIC: 5m Support -> Prev 1m RED -> Curr 1m GREEN Engulfing prev HIGH
        is_prev_red = prev_close < prev_open
        is_curr_green = curr_close > curr_open
        bullish_engulfing = curr_close > prev_high

        if near_support and is_prev_red and is_curr_green and bullish_engulfing:
            return {
                "signal": "CALL",
                "reason": f"Bullish Engulfing at 5m Support (`{support_5m:.5f}`)",
                "support": support_5m,
                "resistance": resistance_5m
            }

        # PUT LOGIC: 5m Resistance -> Prev 1m GREEN -> Curr 1m RED Engulfing prev LOW
        is_prev_green = prev_close > prev_open
        is_curr_red = curr_close < curr_open
        bearish_engulfing = curr_close < prev_low

        if near_resistance and is_prev_green and is_curr_red and bearish_engulfing:
            return {
                "signal": "PUT",
                "reason": f"Bearish Engulfing at 5m Resistance (`{resistance_5m:.5f}`)",
                "support": support_5m,
                "resistance": resistance_5m
            }

        return {
            "signal": "HOLD",
            "reason": "No MTF setup detected",
            "support": support_5m,
            "resistance": resistance_5m
        }


# =====================================================================
# 4. CHART RENDERING & ALERT FORMATTING ENGINE
# =====================================================================
def generate_signal_chart(df_1m: pd.DataFrame, symbol: str, support: float, resistance: float, signal: str) -> io.BytesIO:
    """Renders candlestick chart with 5m Support & Resistance horizontal zones."""
    mc = mpf.make_marketcolors(up='#00E676', down='#FF1744', edge='inherit', wick='inherit', volume='in')
    style = mpf.make_mpf_style(base_mpf_style='nightclouds', marketcolors=mc, gridstyle=':')
    hlines = dict(hlines=[support, resistance], colors=['#00E676', '#FF1744'], linestyle='--', linewidths=1.5)

    buf = io.BytesIO()
    mpf.plot(
        df_1m.tail(35),
        type='candle',
        style=style,
        hlines=hlines,
        title=f"\n{symbol} Real Market - {signal} Setup (5m S/R Lines)",
        ylabel='Price',
        volume=False,
        savefig=dict(fname=buf, format='png', dpi=120, bbox_inches='tight')
    )
    buf.seek(0)
    return buf


def format_telegram_alert(symbol: str, signal: str, reason: str, support: float, resistance: float, trade_amount: float) -> Tuple[str, datetime]:
    """Formats 3-4 minutes prior alert caption and target execution time."""
    target_time = datetime.now(timezone.utc) + timedelta(minutes=3)
    action_emoji = "🟢 BUY (CALL)" if signal == "CALL" else "🔴 SELL (PUT)"

    caption = (
        f"🚨 **PRE-SIGNAL ALERT (REAL MARKET)** 🚨\n\n"
        f"• **Asset:** `{symbol}` (No OTC)\n"
        f"• **Direction:** {action_emoji}\n"
        f"• **Strategy:** {reason}\n"
        f"• **Target Execution Time:** `{target_time.strftime('%H:%M:%S UTC')}` (In 3-4 Mins)\n"
        f"• **Suggested Trade Size:** `${trade_amount:.2f} USD`\n"
        f"• **5m Support Zone:** `{support:.5f}`\n"
        f"• **5m Resistance Zone:** `{resistance:.5f}`\n\n"
        f"⚠️ _Prepare your trade on Quotex for 1m expiry at the target time!_"
    )
    return caption, target_time
