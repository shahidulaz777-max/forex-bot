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
# 1. MARKET STATUS & WEEKEND FILTER
# =====================================================================
class MarketStatus:
    """
    Checks if standard Forex/Binary markets are currently open or closed for weekends.
    Forex markets close Friday at ~22:00 UTC and reopen Sunday at ~22:00 UTC.
    """

    @staticmethod
    def is_market_open() -> Tuple[bool, str]:
        now_utc = datetime.now(timezone.utc)
        weekday = now_utc.weekday()  # Monday = 0, Sunday = 6
        hour = now_utc.hour

        # Friday after 22:00 UTC
        if weekday == 4 and hour >= 22:
            return False, "🔒 Market Closed (Friday Weekend Close)"
        # All Saturday
        if weekday == 5:
            return False, "🔒 Market Closed (Saturday Weekend)"
        # Sunday before 22:00 UTC
        if weekday == 6 and hour < 22:
            return False, "🔒 Market Closed (Reopens Sunday 22:00 UTC)"

        return True, "🟢 Market Open"


# =====================================================================
# 2. DYNAMIC MARKET & ASSET DATA ACCESS (STRICT REAL MARKET - NO OTC)
# =====================================================================
class RealMarketData:
    """
    Dynamically fetches real market candle data for any Forex pair (e.g. EURUSD, GBPJPY).
    Strictly filters out all OTC (Over-The-Counter) assets.
    """
    
    # Pre-defined list of primary Real Market Forex pairs
    KNOWN_REAL_PAIRS = {
        "EURUSD": "EURUSD=X",
        "GBPUSD": "GBPUSD=X",
        "USDJPY": "USDJPY=X",
        "AUDUSD": "AUDUSD=X",
        "USDCAD": "USDCAD=X",
        "EURGBP": "EURGBP=X",
        "USDCHF": "USDCHF=X",
        "NZDUSD": "NZDUSD=X",
        "GBPJPY": "GBPJPY=X",
        "EURJPY": "EURJPY=X",
        "AUDJPY": "AUDJPY=X"
    }

    @classmethod
    def get_supported_assets(cls) -> List[str]:
        """Returns list of primary real market forex pairs."""
        return list(cls.KNOWN_REAL_PAIRS.keys())

    @classmethod
    def format_ticker(cls, symbol: str) -> str:
        """Converts user input (e.g. 'EURUSD' or 'GBP/JPY') into Yahoo Finance ticker format."""
        clean = symbol.upper().replace("_OTC", "").replace(" (OTC)", "").replace("/", "").replace("=X", "").strip()
        if clean in cls.KNOWN_REAL_PAIRS:
            return cls.KNOWN_REAL_PAIRS[clean]
        if len(clean) == 6:
            return f"{clean}=X"
        return clean

    @classmethod
    def is_valid_symbol(cls, symbol: str) -> bool:
        """Ensures symbol is not an OTC market."""
        return "OTC" not in symbol.upper()

    @classmethod
    async def fetch_candles(cls, symbol: str, timeframe: str = "1m", period: str = "1d") -> pd.DataFrame:
        """Asynchronously fetches candle data with retry and fallback error handling."""
        if not cls.is_valid_symbol(symbol):
            raise ValueError(f"OTC Asset '{symbol}' rejected. Only Real Forex Markets are allowed.")

        ticker = cls.format_ticker(symbol)

        def _download():
            try:
                data = yf.download(ticker, period=period, interval=timeframe, progress=False)
                if data.empty:
                    return pd.DataFrame()
                if isinstance(data.columns, pd.MultiIndex):
                    data.columns = data.columns.get_level_values(0)

                df = data[['Open', 'High', 'Low', 'Close', 'Volume']].dropna()
                df.index = pd.to_datetime(df.index)
                return df
            except Exception as ex:
                logger.error(f"[Data Fetch Error] {ticker}: {ex}")
                return pd.DataFrame()

        return await asyncio.to_thread(_download)


# =====================================================================
# 3. HIGH-IMPACT NEWS FILTER ENGINE
# =====================================================================
class NewsFilter:
    """Fetches high-impact economic news to pause signals during high volatility."""
    NEWS_URL = "https://nss.forexfactory.com/calendar/v1/week.json"

    def __init__(self, buffer_minutes: int = 30):
        self.buffer_minutes = buffer_minutes
        self.cached_events: List[Dict[str, Any]] = []
        self.last_fetch: float = 0.0

    async def fetch_economic_calendar(self):
        """Fetches weekly calendar data from Forex Factory API."""
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
        clean = symbol.upper().replace("/", "").replace("=X", "")
        base_curr = clean[:3]
        quote_curr = clean[3:6] if len(clean) >= 6 else ""

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
                        return True, f"⚠️ High-Impact News: '{title}' ({event_currency}) at {event_time.strftime('%H:%M UTC')}"
                except ValueError:
                    continue

        return False, "No high-impact news active."


# =====================================================================
# 4. FLEXIBLE EXPIRY & ADAPTIVE MULTI-TIMEFRAME S/R LOGIC
# =====================================================================
class AdaptiveMTFStrategy:
    """
    Adaptive Strategy Engine:
    - 1m Expiry ➔ Support & Resistance calculated strictly from 5-Minute (5m) candles.
      Signal timing strictly within 5 to 10 seconds before candle close.
    - Multi-minute Expiry (2m-10m) ➔ Support & Resistance calculated strictly from 10-Minute (10m) candles.
      Signal timing strictly within 20 to 40 seconds before candle close.
    """
    def __init__(self, sr_lookback_candles: int = 50, tolerance_pct: float = 0.12):
        self.sr_lookback = sr_lookback_candles
        self.tolerance_pct = tolerance_pct / 100.0

    def calculate_sr_levels(self, df_higher_tf: pd.DataFrame) -> Tuple[float, float]:
        """Calculates Support and Resistance boundaries from the mapped higher timeframe."""
        recent_data = df_higher_tf if len(df_higher_tf) < self.sr_lookback else df_higher_tf.tail(self.sr_lookback)
        support_zone = float(recent_data['Low'].min())
        resistance_zone = float(recent_data['High'].max())
        return support_zone, resistance_zone

    def seconds_until_candle_close(self, df_1m: pd.DataFrame) -> int:
        """Calculates remaining seconds until the current active 1m candle closes."""
        now = datetime.now(timezone.utc)
        return 60 - now.second

    def analyze(self, df_entry: pd.DataFrame, df_sr: pd.DataFrame, expiry_minutes: int = 1) -> Dict[str, Any]:
        """
        Performs full multi-timeframe price action analysis and adaptive timing checks.
        """
        if len(df_entry) < 3 or len(df_sr) < 10:
            return {"signal": "HOLD", "reason": "Insufficient candle data"}

        # 1. Map higher timeframe Support/Resistance
        support, resistance = self.calculate_sr_levels(df_sr)

        # 2. Extract recent completed 1m candles
        prev_candle = df_entry.iloc[-2]  # Candle N-1 (Completed)
        curr_candle = df_entry.iloc[-1]  # Candle N (Signal candle)

        prev_open, prev_high, prev_low, prev_close = prev_candle['Open'], prev_candle['High'], prev_candle['Low'], prev_candle['Close']
        curr_open, curr_high, curr_low, curr_close = curr_candle['Open'], curr_candle['High'], curr_candle['Low'], curr_candle['Close']

        near_support = abs(min(prev_low, curr_low) - support) / support <= self.tolerance_pct
        near_resistance = abs(max(prev_high, curr_high) - resistance) / resistance <= self.tolerance_pct

        # 3. Check Adaptive Timing Windows
        rem_sec = self.seconds_until_candle_close(df_entry)
        if expiry_minutes == 1:
            # Strictly 5 to 10 seconds before candle close for 1m expiry
            timing_valid = 5 <= rem_sec <= 10
        else:
            # Strictly 20 to 40 seconds before candle close for multi-minute expiries (2m-10m)
            timing_valid = 20 <= rem_sec <= 40

        # -------------------------------------------------------------
        # CALL LOGIC: Bullish Engulfing at Support Zone
        # -------------------------------------------------------------
        is_prev_red = prev_close < prev_open
        is_curr_green = curr_close > curr_open
        bullish_engulfing = curr_close > prev_high  # Close breaks/engulfs Red High

        if near_support and is_prev_red and is_curr_green and bullish_engulfing and timing_valid:
            return {
                "signal": "CALL",
                "reason": f"Bullish Engulfing at Support (`{support:.5f}`)",
                "support": support,
                "resistance": resistance,
                "seconds_remaining": rem_sec
            }

        # -------------------------------------------------------------
        # PUT LOGIC: Bearish Engulfing at Resistance Zone
        # -------------------------------------------------------------
        is_prev_green = prev_close > prev_open
        is_curr_red = curr_close < curr_open
        bearish_engulfing = curr_close < prev_low  # Close breaks/engulfs Green Low

        if near_resistance and is_prev_green and is_curr_red and bearish_engulfing and timing_valid:
            return {
                "signal": "PUT",
                "reason": f"Bearish Engulfing at Resistance (`{resistance:.5f}`)",
                "support": support,
                "resistance": resistance,
                "seconds_remaining": rem_sec
            }

        return {
            "signal": "HOLD",
            "reason": f"No trigger or outside timing window ({rem_sec}s remaining)",
            "support": support,
            "resistance": resistance
        }


# =====================================================================
# 5. CHART VISUALIZATION GENERATOR
# =====================================================================
def generate_signal_chart(df: pd.DataFrame, symbol: str, support: float, resistance: float, signal: str, expiry: int) -> io.BytesIO:
    """Renders clean candlestick chart image with overlay Support and Resistance lines."""
    mc = mpf.make_marketcolors(up='#00E676', down='#FF1744', edge='inherit', wick='inherit', volume='in')
    style = mpf.make_mpf_style(base_mpf_style='nightclouds', marketcolors=mc, gridstyle=':')
    hlines = dict(hlines=[support, resistance], colors=['#00E676', '#FF1744'], linestyle='--', linewidths=1.5)

    buf = io.BytesIO()
    mpf.plot(
        df.tail(35),
        type='candle',
        style=style,
        hlines=hlines,
        title=f"\n{symbol} Real Market - {signal} ({expiry}m Expiry)",
        ylabel='Price',
        volume=False,
        savefig=dict(fname=buf, format='png', dpi=120, bbox_inches='tight')
    )
    buf.seek(0)
    return buf
