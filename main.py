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

ASK_CAPITAL = 1
ASK_PAIR = 2

user_capital = {}
active_trades = {}

# ==================== FLASK SERVER FOR CLOUD KEEP-ALIVE ====================
app_flask = Flask(__name__)

@app_flask.route('/')
def home():
    return "Institutional Forex Bot is Active on Cloud Server!"

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
    headers = {'User-Agent': 'Mozilla/5.0'}
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval={interval}&range={range_period}"
    try:
        response = requests.get(url, headers=headers, timeout=8)
        if response.status_code != 200:
            return pd.DataFrame()
        data = response.json()
        result = data['chart']['result'][0]
        timestamps = result['timestamp']
        quote = result['indicators']['quote'][0]
        
        df = pd.DataFrame({
            'Timestamp': pd.to_datetime(timestamps, unit='s'),
            'Close': quote['close'],
            'High': quote['high'],
            'Low': quote['low']
        }).dropna()
        return df
    except Exception:
        return pd.DataFrame()

# ==================== SIGNAL ENGINE ====================

def analyze_forex_market(pair_name):
    symbol = format_symbol(pair_name)
    df_1m = get_live_candles(symbol, "1m", "1d")
    df_5m = get_live_candles(symbol, "5m", "1d")

    if df_1m.empty or df_5m.empty:
        return "INVALID_PAIR", f"❌ **'{pair_name}'** ফরেক্স পেয়ারটির রিয়েল মার্কেট ডাটা পাওয়া যায়নি। সঠিক ফরেক্স পেয়ারের নাম লিখুন।"

    # Technical Indicators
    df_5m['EMA_20'] = df_5m['Close'].ewm(span=20, adjust=False).mean()
    df_5m['EMA_50'] = df_5m['Close'].ewm(span=50, adjust=False).mean()
    
    delta = df_5m['Close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    rs = gain / loss
    df_5m['RSI'] = 100 - (100 / (1 + rs))

    curr_price = df_5m['Close'].iloc[-1]
    rsi_5m = df_5m['RSI'].iloc[-1]
    ema20 = df_5m['EMA_20'].iloc[-1]
    ema50 = df_5m['EMA_50'].iloc[-1]

    high_prev = df_5m['High'].iloc[-2]
    low_prev = df_5m['Low'].iloc[-2]
    close_prev = df_5m['Close'].iloc[-2]
    pivot = (high_prev + low_prev + close_prev) / 3
    res_1 = (2 * pivot) - low_prev
    sup_1 = (2 * pivot) - high_prev

    if 45 <= rsi_5m <= 55:
        return "NO_TRADE", (
            f"⚠️ **NO SAFE TRADE ENTRY**\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📍 **Pair:** `{pair_name.upper()}`\n"
            f"💵 **Price:** `{curr_price:.5f}`\n\n"
            f"💡 **কারণ:** মার্কেট সাইডওয়েতে আছে।"
        )

    # Bullish Signal
    if curr_price > ema20 > ema50 and rsi_5m > 55:
        tp = curr_price + (res_1 - curr_price) * 0.8 if res_1 > curr_price else curr_price * 1.0015
        sl = sup_1 if sup_1 < curr_price else curr_price * 0.9985
        return "BUY", curr_price, tp, sl

    # Bearish Signal
    elif curr_price < ema20 < ema50 and rsi_5m < 45:
        tp = curr_price - (curr_price - sup_1) * 0.8 if sup_1 < curr_price else curr_price * 0.9985
        sl = res_1 if res_1 > curr_price else curr_price * 1.0015
        return "SELL", curr_price, tp, sl

    else:
        return "NO_TRADE", (
            f"🚫 **MARKET IS NOT GOOD FOR TRADING**\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📍 **Pair:** `{pair_name.upper()}`\n"
            f"💵 **Price:** `{curr_price:.5f}`\n\n"
            f"💡 **কারণ:** ট্রেন্ড ও ইন্ডিকেটরের কনফার্মেশন নেই।"
        )

# ==================== TELEGRAM HANDLERS ====================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 **Institutional Real-Market Forex Bot**\n\n"
        "আপনার মোট **ক্যাপিটাল / ব্যালেন্স** কত ডলার? (যেমন: `1000` লিখে পাঠান):",
        parse_mode="Markdown"
    )
    return ASK_CAPITAL

async def set_capital(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        capital = float(update.message.text.strip())
        user_id = update.message.from_user.id
        user_capital[user_id] = capital

        await update.message.reply_text(
            f"✅ **ক্যাপিটাল সেট করা হয়েছে: ${capital:.2f}**\n\n"
            f"এখন যে ফরেক্স পেয়ারে ট্রেড করতে চান, তার নাম টাইপ করুন (যেমন: `EURUSD`, `XAUUSD`):",
            parse_mode="Markdown"
        )
        return ASK_PAIR
    except ValueError:
        await update.message.reply_text("❌ শুধু সংখ্যা লিখুন (যেমন: `1000`):")
        return ASK_CAPITAL

async def process_pair(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pair_name = update.message.text.strip()
    user_id = update.message.from_user.id
    capital = user_capital.get(user_id, 1000.0)

    wait_msg = await update.message.reply_text(f"🔍 **{pair_name.upper()}** রিয়েল মার্কেট এনালাইজ করা হচ্ছে...", parse_mode="Markdown")

    res = analyze_forex_market(pair_name)

    if res[0] in ["BUY", "SELL"]:
        sig_type, entry, tp, sl = res
        trade_amount = capital * 0.02

        msg = (
            f"🚀 **STRONG {sig_type} SIGNAL** 🚀\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📍 **Pair:** `{pair_name.upper()}`\n"
            f"⏱️ **Trade Expiry:** `5 Minutes`\n\n"
            f"📌 **Trading Entry Points:**\n"
            f"• Entry Price: `{entry:.5f}`\n"
            f"• Take Profit (TP): `{tp:.5f}`\n"
            f"• Stop Loss (SL): `{sl:.5f}`\n\n"
            f"💼 **Recommended Amount:** `${trade_amount:.2f}`\n\n"
            f"⏳ ৫ মিনিট পর রিয়েল মার্কেট যাচাই করে রেজাল্ট ও চার্ট পাঠানো হবে।"
        )
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

# ==================== BACKGROUND TASK & CHART GENERATOR ====================

async def track_trade_results(app):
    while True:
        try:
            await asyncio.sleep(5)
            now = time.time()
            completed = []

            for uid, trade in list(active_trades.items()):
                if now >= trade["expiry_time"]:
                    symbol = trade["symbol"]
                    df = get_live_candles(symbol, "1m", "1d")
                    
                    if not df.empty:
                        close_price = df['Close'].iloc[-1]
                        entry = trade["entry"]
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
                                f"✅ **Status:** Profit target achieved!"
                            )
                        else:
                            res_text = (
                                f"🛑 **TRADE RESULT: LOSS** 🛑\n"
                                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                                f"📍 **Pair:** `{pair}`\n"
                                f"💵 **Entry:** `{entry:.5f}` | **Close:** `{close_price:.5f}`\n"
                                f"❌ **Status:** Market reversed."
                            )

                        # Generate Chart Image to show result visually
                        try:
                            plt.figure(figsize=(7, 4))
                            plt.plot(df['Timestamp'], df['Close'], label='Market Price', color='#00ffcc', linewidth=2)
                            plt.axhline(y=entry, color='#ffff00', linestyle='--', label=f'Entry: {entry:.5f}')
                            plt.axhline(y=close_price, color='#ff4444' if not is_win else '#00ff00', linestyle='-', label=f'Close: {close_price:.5f}')
                            plt.title(f"{pair} - 5M Trade Result Chart", color='white', fontsize=12)
                            plt.legend(loc='upper left', facecolor='#222222', edgecolor='none', labelcolor='white')
                            plt.grid(True, color='#333333', linestyle=':')
                            
                            plt.gca().set_facecolor('#121212')
                            plt.gcf().patch.set_facecolor('#1a1a1a')
                            plt.tight_layout()

                            buf = io.BytesIO()
                            plt.savefig(buf, format='png', dpi=100)
                            buf.seek(0)
                            plt.close()

                            await app.bot.send_photo(chat_id=chat_id, photo=buf, caption=res_text, parse_mode="Markdown")
                        except Exception as img_err:
                            # Fallback to text message if image generation fails
                            await app.bot.send_message(chat_id=chat_id, text=res_text, parse_mode="Markdown")
                        
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
                    ASK_PAIR: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_pair)]
                },
                fallbacks=[CommandHandler("start", start)]
            )

            app.add_handler(conv_handler)
            await app.initialize()
            await app.start()
            await app.updater.start_polling(drop_pending_updates=True)

            asyncio.create_task(track_trade_results(app))
            print("🚀 Real-Market Cloud Bot with Chart Generator is running 24/7...")
            
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
