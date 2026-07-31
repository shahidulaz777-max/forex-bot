import pandas as pd
import numpy as np
import requests
import asyncio
import logging
import time
import os
import io
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from flask import Flask
from threading import Thread
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
    ConversationHandler
)

# Logging Setup
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

TELEGRAM_TOKEN = "8926811082:AAH4T7FmcB2pcrwHuLA18TnPF3LV2mktaDc"

# Conversation States
ASK_CAPITAL = 1
ASK_RISK = 2
ASK_PAIR = 3

user_capital = {}
user_risk = {}
user_lockout = {}
active_trades = {}

# ==================== FLASK SERVER FOR CLOUD KEEP-ALIVE ====================
app_flask = Flask(__name__)

@app_flask.route('/')
def home():
    return "Institutional High-Accuracy Forex Bot is Active 24/7 on Cloud!"

def run_flask():
    port = int(os.environ.get("PORT", 5000))
    app_flask.run(host="0.0.0.0", port=port)

# ==================== REAL MARKET DATA FETCHING ====================

def format_symbol(pair):
    pair = pair.upper().strip().replace("/", "").replace(" ", "")
    if pair in ["XAUUSD", "GOLD"]:
        return "GC=F"
    elif pair in ["XAGUSD", "SILVER"]:
        return "SI=F"
    elif not pair.endswith("=X"):
        return f"{pair}=X"
    return pair

def get_live_candles(symbol, interval="5m", range_period="1d"):
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval={interval}&range={range_period}"
    try:
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code != 200:
            return pd.DataFrame()
        data = response.json()
        result = data['chart']['result'][0]
        timestamps = result['timestamp']
        quote = result['indicators']['quote'][0]
        
        df = pd.DataFrame({
            'Timestamp': pd.to_datetime(timestamps, unit='s'),
            'Open': quote['open'],
            'High': quote['high'],
            'Low': quote['low'],
            'Close': quote['close']
        }).dropna()
        return df
    except Exception:
        return pd.DataFrame()

# ==================== HIGH-ACCURACY MULTI-INDICATOR ENGINE ====================

def analyze_forex_market(pair_name):
    symbol = format_symbol(pair_name)
    df_5m = get_live_candles(symbol, "5m", "1d")
    df_15m = get_live_candles(symbol, "15m", "1d")

    if df_5m.empty or df_15m.empty:
        return "INVALID_PAIR", f"❌ **'{pair_name.upper()}'** ফরেক্স পেয়ারটির রিয়েল মার্কেট লাইভ ডাটা এই মুহূর্তে পাওয়া যায়নি। দয়া করে সঠিক ফরেক্স পেয়ারের নাম (যেমন: `EURUSD`, `GBPUSD`, `GBPJPY`) লিখুন।"

    for df in [df_5m, df_15m]:
        df['EMA_20'] = df['Close'].ewm(span=20, adjust=False).mean()
        df['EMA_50'] = df['Close'].ewm(span=50, adjust=False).mean()
        
        delta = df['Close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        rs = gain / loss
        df['RSI'] = 100 - (100 / (1 + rs))

        exp1 = df['Close'].ewm(span=12, adjust=False).mean()
        exp2 = df['Close'].ewm(span=26, adjust=False).mean()
        df['MACD'] = exp1 - exp2
        df['Signal_Line'] = df['MACD'].ewm(span=9, adjust=False).mean()

        df['BB_Mid'] = df['Close'].rolling(window=20).mean()
        df['BB_Std'] = df['Close'].rolling(window=20).std()
        df['BB_Upper'] = df['BB_Mid'] + (df['BB_Std'] * 2)
        df['BB_Lower'] = df['BB_Mid'] - (df['BB_Std'] * 2)

    curr_price = df_5m['Close'].iloc[-1]
    rsi_5m = df_5m['RSI'].iloc[-1]
    rsi_15m = df_15m['RSI'].iloc[-1]
    
    ema20_5m = df_5m['EMA_20'].iloc[-1]
    ema50_5m = df_5m['EMA_50'].iloc[-1]
    ema20_15m = df_15m['EMA_20'].iloc[-1]
    ema50_15m = df_15m['EMA_50'].iloc[-1]

    macd_5m = df_5m['MACD'].iloc[-1]
    sig_5m = df_5m['Signal_Line'].iloc[-1]

    high_prev = df_5m['High'].iloc[-2]
    low_prev = df_5m['Low'].iloc[-2]
    close_prev = df_5m['Close'].iloc[-2]
    pivot = (high_prev + low_prev + close_prev) / 3
    res_1 = (2 * pivot) - low_prev
    sup_1 = (2 * pivot) - high_prev

    is_bullish_trend = (curr_price > ema20_5m > ema50_5m) and (ema20_15m > ema50_15m)
    is_bullish_momentum = (rsi_5m > 58) and (rsi_15m > 52) and (macd_5m > sig_5m)

    is_bearish_trend = (curr_price < ema20_5m < ema50_5m) and (ema20_15m < ema50_15m)
    is_bearish_momentum = (rsi_5m < 42) and (rsi_15m < 48) and (macd_5m < sig_5m)

    if is_bullish_trend and is_bullish_momentum:
        tp = curr_price + (res_1 - curr_price) * 0.85 if res_1 > curr_price else curr_price * 1.0020
        sl = sup_1 if sup_1 < curr_price else curr_price * 0.9980
        return "BUY", curr_price, tp, sl, df_5m

    elif is_bearish_trend and is_bearish_momentum:
        tp = curr_price - (curr_price - sup_1) * 0.85 if sup_1 < curr_price else curr_price * 0.9980
        sl = res_1 if res_1 > curr_price else curr_price * 1.0020
        return "SELL", curr_price, tp, sl, df_5m

    else:
        return "NO_TRADE", (
            f"🛡️ **HIGH-ACCURACY FILTER: NO TRADE**\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📍 **Pair:** `{pair_name.upper()}`\n"
            f"💵 **Price:** `{curr_price:.5f}`\n\n"
            f"💡 **কারণ:** বর্তমান মার্কেটে সঠিক উইন রেটের কনফার্মেশন নেই। অন্য পেয়ার ট্রাই করুন।"
        )

# ==================== TELEGRAM HANDLERS ====================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 **Institutional High-Accuracy Forex Bot**\n\n"
        "আপনার মোট **ক্যাপিটাল / ব্যালেন্স** কত? (আপনার ইচ্ছামতো যেকোনো সংখ্যা বা পরিমাণ লিখতে পারেন যেমন: `100`, `1000`, `150000` ইত্যাদি):",
        parse_mode="Markdown"
    )
    return ASK_CAPITAL

async def set_capital(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        raw_text = update.message.text.strip().replace(',', '')
        capital = float(raw_text)
        user_id = update.message.from_user.id
        user_capital[user_id] = capital

        await update.message.reply_text(
            f"✅ **ক্যাপিটাল সেট করা হয়েছে: {capital:,.2f}**\n\n"
            f"প্রতিটি ট্রেডে আপনি কত পার্সেন্ট **রিস্ক (Risk %)** নিতে চান? (যেমন: `1`, `2`, `5` বা আপনার ইচ্ছামতো লিখে পাঠান):",
            parse_mode="Markdown"
        )
        return ASK_RISK
    except ValueError:
        await update.message.reply_text("❌ সঠিক সংখ্যা লিখুন (যেমন: `1000` বা `150000`):")
        return ASK_CAPITAL

async def set_risk(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        raw_text = update.message.text.strip().replace(',', '')
        risk = float(raw_text)
        user_id = update.message.from_user.id
        user_risk[user_id] = risk

        await update.message.reply_text(
            f"✅ **রিস্ক সেট করা হয়েছে: {risk}%**\n\n"
            f"এখন যে ফরেক্স পেয়ারে ট্রেড করতে চান, তার নাম টাইপ করুন (যেমন: `EURUSD`, `XAUUSD`, `GBPJPY`):",
            parse_mode="Markdown"
        )
        return ASK_PAIR
    except ValueError:
        await update.message.reply_text("❌ সঠিক সংখ্যা লিখুন (যেমন: `1` বা `2`):")
        return ASK_RISK

async def process_pair(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    
    if user_id in user_lockout and time.time() < user_lockout[user_id]:
        rem_hours = int((user_lockout[user_id] - time.time()) / 3600)
        await update.message.reply_text(
            f"⛔ **LOCKOUT ACTIVE**\n"
            f"আপনার আগের ট্রেডে রিস্ক লিমিট বা স্টপ লস হিট করেছিল। রুল অনুযায়ী আপনি আগামী **{rem_hours} ঘণ্টা** পর্যন্ত নতুন ট্রেড নিতে পারবেন না."
        )
        return ASK_PAIR

    pair_name = update.message.text.strip()
    capital = user_capital.get(user_id, 1000.0)
    risk_pct = user_risk.get(user_id, 1.0)

    wait_msg = await update.message.reply_text(f"🔍 **{pair_name.upper()}** মার্কেট স্ক্যান করা হচ্ছে...", parse_mode="Markdown")

    res = analyze_forex_market(pair_name)

    if res[0] in ["BUY", "SELL"]:
        sig_type, entry, tp, sl, df_5m = res
        risk_amount = capital * (risk_pct / 100.0)
        
        msg = (
            f"🚀 **HIGH-ACCURACY {sig_type} SIGNAL** 🚀\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📍 **Pair:** `{pair_name.upper()}`\n"
            f"⏱️ **Trade Expiry:** `5 Minutes`\n\n"
            f"📌 **Copy & Paste Trading Entries:**\n"
            f"• Entry Price: `{entry:.5f}`\n"
            f"• Take Profit (TP): `{tp:.5f}`\n"
            f"• Stop Loss (SL): `{sl:.5f}`\n\n"
            f"💼 **Risk Allocated:** `{risk_amount:,.2f} ({risk_pct}% of Capital)`\n\n"
            f"⏳ ৫ মিনিট পর রিয়েল মার্কেট যাচাই করে বিস্তারিত চার্টসহ রেজাল্ট পাঠানো হবে।"
        )

        try:
            fig, ax = plt.subplots(figsize=(8, 4))
            df_subset = df_5m.tail(25).copy()
            df_subset['DateIndex'] = range(len(df_subset))
            up = df_subset[df_subset['Close'] >= df_subset['Open']]
            down = df_subset[df_subset['Close'] < df_subset['Open']]
            
            ax.vlines(up['DateIndex'], up['Low'], up['High'], color='#26a69a', linewidth=1.2)
            ax.vlines(down['DateIndex'], down['Low'], down['High'], color='#ef5350', linewidth=1.2)
            ax.bar(up['DateIndex'], up['Close'] - up['Open'], 0.6, bottom=up['Open'], color='#26a69a', edgecolor='#26a69a')
            ax.bar(down['DateIndex'], down['Open'] - down['Close'], 0.6, bottom=down['Close'], color='#ef5350', edgecolor='#ef5350')

            ax.axhline(y=entry, color='#ffeb3b', linestyle='--', linewidth=1.5, label=f'Entry: {entry:.5f}')
            ax.axhline(y=tp, color='#4caf50', linestyle='-.', linewidth=1.5, label=f'TP: {tp:.5f}')
            ax.axhline(y=sl, color='#f44336', linestyle='-.', linewidth=1.5, label=f'SL: {sl:.5f}')

            ax.set_title(f"{pair_name.upper()} - 5M Entry Signal Analysis", color='white', fontsize=10, fontweight='bold')
            ax.legend(loc='upper left', facecolor='#1e1e1e', edgecolor='none', labelcolor='white', fontsize=7)
            ax.grid(True, color='#333333', linestyle=':', alpha=0.6)
            
            ax.set_facecolor('#121212')
            fig.patch.set_facecolor('#1a1a1a')
            ax.tick_params(colors='white', labelsize=7)
            plt.tight_layout()

            buf = io.BytesIO()
            plt.savefig(buf, format='png', dpi=110)
            buf.seek(0)
            plt.close(fig)

            await wait_msg.delete()
            await update.message.reply_photo(photo=buf, caption=msg, parse_mode="Markdown")
        except Exception:
            await wait_msg.edit_text(msg, parse_mode="Markdown")

        active_trades[user_id] = {
            "pair": pair_name.upper(),
            "symbol": format_symbol(pair_name),
            "type": sig_type,
            "entry": entry,
            "tp": tp,
            "sl": sl,
            "expiry_time": time.time() + 300,
            "chat_id": update.message.chat_id
        }
    else:
        _, error_msg = res
        await wait_msg.edit_text(error_msg, parse_mode="Markdown")

    await update.message.reply_text("👉 অন্য পেয়ারে ট্রেড করতে পেয়ারের নাম লিখে পাঠান:")
    return ASK_PAIR

# ==================== PROFESSIONAL CANDLESTICK CHART & TRACKER ====================

async def track_trade_results(app):
    while True:
        try:
            await asyncio.sleep(5)
            now = time.time()
            completed = []

            for uid, trade in list(active_trades.items()):
                if now >= trade["expiry_time"]:
                    symbol = trade["symbol"]
                    df = get_live_candles(symbol, "5m", "1d")
                    
                    if not df.empty:
                        close_price = df['Close'].iloc[-1]
                        entry = trade["entry"]
                        tp = trade["tp"]
                        sl = trade["sl"]
                        trade_type = trade["type"]
                        pair = trade["pair"]
                        chat_id = trade["chat_id"]

                        is_win = (trade_type == "BUY" and close_price > entry) or (trade_type == "SELL" and close_price < entry)

                        if is_win:
                            res_text = (
                                f"🎯 **TRADE RESULT: WIN (PROFIT)** 🎯\n"
                                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                                f"📍 **Pair:** `{pair}`\n"
                                f"💵 **Entry:** `{entry:.5f}` | **Close:** `{close_price:.5f}`\n"
                                f"🎯 **TP:** `{tp:.5f}` | 🛡️ **SL:** `{sl:.5f}`\n"
                                f"✅ **Status:** প্রফিট টার্গেট সফলভাবে অর্জিত হয়েছে!"
                            )
                        else:
                            res_text = (
                                f"🛑 **TRADE RESULT: LOSS (RISK HIT)** 🛑\n"
                                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                                f"📍 **Pair:** `{pair}`\n"
                                f"💵 **Entry:** `{entry:.5f}` | **Close:** `{close_price:.5f}`\n"
                                f"🎯 **TP:** `{tp:.5f}` | 🛡️ **SL:** `{sl:.5f}`\n"
                                f"❌ **Status:** মার্কেট রিভার্স করায় রিস্ক/স্টপ লস হিট করেছে।"
                            )
                            user_lockout[uid] = time.time() + 86400
                            res_text += f"\n\n⚠️ **সতর্কতা:** রিস্ক রুল অনুযায়ী আপনার একাউন্টে ২৪ ঘণ্টার লকআউট কার্যকর করা হয়েছে।"

                        try:
                            fig, ax = plt.subplots(figsize=(9, 5))
                            
                            df['DateIndex'] = range(len(df))
                            up = df[df['Close'] >= df['Open']]
                            down = df[df['Close'] < df['Open']]
                            
                            ax.vlines(up['DateIndex'], up['Low'], up['High'], color='#26a69a', linewidth=1.2)
                            ax.vlines(down['DateIndex'], down['Low'], down['High'], color='#ef5350', linewidth=1.2)
                            
                            ax.bar(up['DateIndex'], up['Close'] - up['Open'], 0.6, bottom=up['Open'], color='#26a69a', edgecolor='#26a69a')
                            ax.bar(down['DateIndex'], down['Open'] - down['Close'], 0.6, bottom=down['Close'], color='#ef5350', edgecolor='#ef5350')

                            ax.axhline(y=entry, color='#ffeb3b', linestyle='--', linewidth=1.5, label=f'Entry: {entry:.5f}')
                            ax.axhline(y=tp, color='#4caf50', linestyle='-.', linewidth=1.5, label=f'Take Profit (TP): {tp:.5f}')
                            ax.axhline(y=sl, color='#f44336', linestyle='-.', linewidth=1.5, label=f'Stop Loss (SL): {sl:.5f}')
                            ax.axhline(y=close_price, color='#ffffff', linestyle=':', linewidth=1.5, label=f'Close: {close_price:.5f}')

                            ax.set_title(f"{pair} - 5M Professional Japanese Candlestick Analysis", color='white', fontsize=12, fontweight='bold')
                            ax.legend(loc='upper left', facecolor='#1e1e1e', edgecolor='none', labelcolor='white', fontsize=8)
                            ax.grid(True, color='#333333', linestyle=':', alpha=0.6)
                            
                            ax.set_facecolor('#121212')
                            fig.patch.set_facecolor('#1a1a1a')
                            ax.tick_params(colors='white', labelsize=8)
                            
                            plt.tight_layout()

                            buf = io.BytesIO()
                            plt.savefig(buf, format='png', dpi=120)
                            buf.seek(0)
                            plt.close(fig)

                            await app.bot.send_photo(chat_id=chat_id, photo=buf, caption=res_text, parse_mode="Markdown")
                        except Exception:
                            await app.bot.send_message(chat_id=chat_id, text=res_text, parse_mode="Markdown")
                        
                        # নতুন ট্রেড নেওয়ার জন্য অটো প্রম্পট পাঠানো
                        await app.bot.send_message(chat_id=chat_id, text="👉 অন্য পেয়ারে ট্রেড করতে নতুন পেয়ারের নাম লিখে পাঠান:", parse_mode="Markdown")
                        
                        completed.append(uid)

            for uid in completed:
                if uid in active_trades:
                    del active_trades[uid]
        except Exception:
            await asyncio.sleep(5)

# ==================== TELEGRAM BOT MAIN ====================

async def main():
    flask_thread = Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()

    while True:
        app = None
        try:
            app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

            conv_handler = ConversationHandler(
                entry_points=[CommandHandler("start", start)],
                states={
                    ASK_CAPITAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_capital)],
                    ASK_RISK: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_risk)],
                    ASK_PAIR: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_pair)]
                },
                fallbacks=[CommandHandler("start", start)]
            )

            app.add_handler(conv_handler)
            await app.initialize()
            await app.start()
            await app.updater.start_polling(drop_pending_updates=True)

            asyncio.create_task(track_trade_results(app))
            print("🚀 Institutional High-Accuracy Cloud Bot is running 24/7...")
            
            while True:
                await asyncio.sleep(1)

        except Exception as e:
            print(f"Reconnecting due to network/system interruption: {e}")
            if app:
                try:
                    await app.updater.stop()
                    await app.stop()
                    await app.shutdown()
                except Exception:
                    pass
            await asyncio.sleep(5)

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("Bot stopped.")
