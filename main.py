import pandas as pd
import numpy as np
import requests
import asyncio
import logging
import time
import os
import io
import sys
import json
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
    filters
)

# MetaTrader 5 Engine Import
try:
    import MetaTrader5 as mt5
    MT5_AVAILABLE = True
except ImportError:
    MT5_AVAILABLE = False
    print("⚠️ MetaTrader5 library not found. Run this on Windows PC/VPS with MT5 installed.")

# Logging Setup
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

TELEGRAM_TOKEN = "8926811082:AAH4T7FmcB2pcrwHuLA18TnPF3LV2mktaDc"

# ===== EXNESS MT5 REAL TRADING CREDENTIALS =====
EXNESS_LOGIN = 279697056
EXNESS_PASSWORD = "YOUR_EXNESS_MT5_PASSWORD"  # 👈 আপনার এক্সনেস MT5 পাসওয়ার্ড বসান
EXNESS_SERVER = "Exness-MT5Trial8"             # 👈 আপনার সঠিক এক্সনেস সার্ভার নেম

# ===== SAFE ADMIN CONFIGURATION SYSTEM =====
CONFIG_FILE = "admin_config.json"

def load_admin_credentials():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                return data.get("admin_user", "admin1"), data.get("admin_pass", "admin2")
        except Exception:
            pass
    return "admin1", "admin2"

def save_admin_credentials(user, pwd):
    try:
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump({"admin_user": user, "admin_pass": pwd}, f)
        return True
    except Exception:
        return False

# State Constants
STATE_WAIT_CAPITAL = "WAIT_CAPITAL"
STATE_WAIT_RISK = "WAIT_RISK"
STATE_WAIT_PAIR = "WAIT_PAIR"
STATE_WAIT_ADMIN_USER = "WAIT_ADMIN_USER"
STATE_WAIT_ADMIN_PASS = "WAIT_ADMIN_PASS"
STATE_WAIT_ADMIN_CODE = "WAIT_ADMIN_CODE"
STATE_WAIT_NEW_ADMIN_USER = "WAIT_NEW_ADMIN_USER"
STATE_WAIT_NEW_ADMIN_PASS = "WAIT_NEW_ADMIN_PASS"

user_states = {}
user_capital = {}
user_risk = {}
user_lockout = {}
active_trades = {}
monitoring_pairs = {}
admin_session = {}

# ==================== FLASK SERVER FOR CLOUD KEEP-ALIVE ====================
app_flask = Flask(__name__)

@app_flask.route('/')
def home():
    return "Institutional Forex AI Trading Engine is Active 24/7!"

def run_flask():
    port = int(os.environ.get("PORT", 5000))
    app_flask.run(host="0.0.0.0", port=port)

# ==================== EXNESS MT5 DIRECT DATA ENGINE ====================

def init_mt5_connection():
    if not MT5_AVAILABLE:
        return False
    if not mt5.initialize():
        logging.error(f"MT5 Initialization failed: {mt5.last_error()}")
        return False
    
    authorized = mt5.login(EXNESS_LOGIN, password=EXNESS_PASSWORD, server=EXNESS_SERVER)
    if authorized:
        logging.info(f"✅ Connected to Exness MT5 Account ({EXNESS_LOGIN}) successfully!")
        return True
    else:
        logging.error(f"❌ MT5 Login failed: {mt5.last_error()}")
        return False

def format_symbol(pair):
    pair = pair.upper().strip().replace("/", "").replace(" ", "")
    if pair == "GOLD":
        return "XAUUSD"
    elif pair == "SILVER":
        return "XAGUSD"
    return pair

def get_exness_candles(symbol, timeframe=mt5.TIMEFRAME_M5, count=100):
    if not MT5_AVAILABLE or not mt5.terminal_info():
        init_mt5_connection()

    rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, count)
    if rates is None or len(rates) == 0:
        return pd.DataFrame()

    df = pd.DataFrame(rates)
    df['Timestamp'] = pd.to_datetime(df['time'], unit='s')
    df.rename(columns={'open': 'Open', 'high': 'High', 'low': 'Low', 'close': 'Close'}, inplace=True)
    return df[['Timestamp', 'Open', 'High', 'Low', 'Close']]

# ==================== RISK & LOT SIZE CALCULATOR ====================

def calculate_position_size(capital, risk_pct, sl_pips, symbol):
    risk_amount = capital * (risk_pct / 100.0)
    # Default Forex Lot Calculation Standard
    pip_value = 10.0  # Standard Lot Pip Value
    if "XAU" in symbol:
        pip_value = 100.0
    
    lot_size = round(risk_amount / (sl_pips * pip_value), 2)
    lot_size = max(0.01, min(lot_size, 10.0))  # Safeguard Lot limit
    return lot_size

# ==================== INSTITUTIONAL EXNESS AUTO-TRADER ====================

def execute_exness_trade(symbol, action, entry_price, tp_price, sl_price, capital, risk_pct):
    if not MT5_AVAILABLE:
        return False, "MT5 System is not initialized on this OS."

    if not mt5.symbol_select(symbol, True):
        return False, f"Symbol '{symbol}' is not available in Exness."

    sl_pips = abs(entry_price - sl_price)
    lot_size = calculate_position_size(capital, risk_pct, sl_pips, symbol)

    order_type = mt5.ORDER_TYPE_BUY if action == "BUY" else mt5.ORDER_TYPE_SELL
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return False, "Failed to get live Ask/Bid ticks from Exness."

    price = tick.ask if action == "BUY" else tick.bid

    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": float(lot_size),
        "type": order_type,
        "price": price,
        "sl": float(sl_price),
        "tp": float(tp_price),
        "deviation": 20,
        "magic": 888111,
        "comment": "Institutional AI Real-Money Auto Trade",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }

    result = mt5.order_send(request)
    if result.retcode != mt5.TRADE_RETCODE_DONE:
        return False, f"Trade Execution Failed: {result.comment} (Code: {result.retcode})"

    return True, f"✅ **Live Order Executed!** Ticket: `{result.order}` | Lot: `{lot_size}`"

# ==================== ADVANCED AI MULTI-INDICATOR SIGNAL ENGINE ====================

def analyze_forex_market(pair_name):
    symbol = format_symbol(pair_name)
    
    df_5m = get_exness_candles(symbol, mt5.TIMEFRAME_M5, 100)
    df_15m = get_exness_candles(symbol, mt5.TIMEFRAME_M15, 100)

    if df_5m.empty or len(df_5m) < 30:
        return "INVALID_PAIR", f"❌ **'{symbol}'** পেয়ারটির ডাটা Exness থেকে পাওয়া যায়নি।", None

    # Technical Indicators (5M Timeframe)
    df_5m['EMA_20'] = df_5m['Close'].ewm(span=20, adjust=False).mean()
    df_5m['EMA_50'] = df_5m['Close'].ewm(span=50, adjust=False).mean()

    delta = df_5m['Close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    rs = gain / loss
    df_5m['RSI'] = 100 - (100 / (1 + rs))

    exp1 = df_5m['Close'].ewm(span=12, adjust=False).mean()
    exp2 = df_5m['Close'].ewm(span=26, adjust=False).mean()
    df_5m['MACD'] = exp1 - exp2
    df_5m['Signal_Line'] = df_5m['MACD'].ewm(span=9, adjust=False).mean()

    high_low = df_5m['High'] - df_5m['Low']
    high_close = np.abs(df_5m['High'] - df_5m['Close'].shift())
    low_close = np.abs(df_5m['Low'] - df_5m['Close'].shift())
    ranges = pd.concat([high_low, high_close, low_close], axis=1)
    true_range = np.max(ranges, axis=1)
    df_5m['ATR'] = true_range.rolling(14).mean()

    curr_price = df_5m['Close'].iloc[-1]
    rsi_5m = df_5m['RSI'].iloc[-1]
    ema20 = df_5m['EMA_20'].iloc[-1]
    ema50 = df_5m['EMA_50'].iloc[-1]
    macd = df_5m['MACD'].iloc[-1]
    sig_line = df_5m['Signal_Line'].iloc[-1]
    atr = df_5m['ATR'].iloc[-1] if not np.isnan(df_5m['ATR'].iloc[-1]) else curr_price * 0.0015

    # 15M Higher Timeframe Confirmation
    trend_15m = "NEUTRAL"
    if not df_15m.empty and len(df_15m) > 20:
        ema20_15m = df_15m['Close'].ewm(span=20, adjust=False).mean().iloc[-1]
        close_15m = df_15m['Close'].iloc[-1]
        trend_15m = "BULLISH" if close_15m > ema20_15m else "BEARISH"

    # Strict Institutional Entry Criteria
    is_buy = (curr_price > ema20) and (ema20 > ema50) and (rsi_5m > 48) and (macd > sig_line) and (trend_15m != "BEARISH")
    is_sell = (curr_price < ema20) and (ema20 < ema50) and (rsi_5m < 52) and (macd < sig_line) and (trend_15m != "BULLISH")

    if is_buy:
        tp = curr_price + (atr * 2.0)
        sl = curr_price - (atr * 1.5)
        return "BUY", curr_price, tp, sl, df_5m
    elif is_sell:
        tp = curr_price - (atr * 2.0)
        sl = curr_price + (atr * 1.5)
        return "SELL", curr_price, tp, sl, df_5m
    else:
        if curr_price >= ema20:
            tp = curr_price + (atr * 1.8)
            sl = curr_price - (atr * 1.2)
            return "BUY", curr_price, tp, sl, df_5m
        else:
            tp = curr_price - (atr * 1.8)
            sl = curr_price + (atr * 1.2)
            return "SELL", curr_price, tp, sl, df_5m

# ==================== ADVANCED CHART GENERATOR ====================

def generate_signal_chart(df_5m, pair_name, entry, tp, sl, sig_type):
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(9, 6), gridspec_kw={'height_ratios': [3, 1]})
    df_subset = df_5m.tail(30).copy()
    df_subset['DateIndex'] = range(len(df_subset))
    
    up = df_subset[df_subset['Close'] >= df_subset['Open']]
    down = df_subset[df_subset['Close'] < df_subset['Open']]
    
    ax1.vlines(up['DateIndex'], up['Low'], up['High'], color='#26a69a', linewidth=1.2)
    ax1.vlines(down['DateIndex'], down['Low'], down['High'], color='#ef5350', linewidth=1.2)
    ax1.bar(up['DateIndex'], up['Close'] - up['Open'], 0.6, bottom=up['Open'], color='#26a69a', edgecolor='#26a69a')
    ax1.bar(down['DateIndex'], down['Open'] - down['Close'], 0.6, bottom=down['Close'], color='#ef5350', edgecolor='#ef5350')

    ax1.plot(df_subset['DateIndex'], df_subset['EMA_20'], color='#29b6f6', linewidth=1.2, label='EMA 20')
    ax1.plot(df_subset['DateIndex'], df_subset['EMA_50'], color='#ff9800', linewidth=1.2, label='EMA 50')

    ax1.axhline(y=entry, color='#ffeb3b', linestyle='--', linewidth=1.5, label=f'Entry: {entry:.5f}')
    ax1.axhline(y=tp, color='#4caf50', linestyle='-.', linewidth=1.5, label=f'TP: {tp:.5f}')
    ax1.axhline(y=sl, color='#f44336', linestyle='-.', linewidth=1.5, label=f'SL: {sl:.5f}')

    ax1.set_title(f"{pair_name} - Exness Direct Institutional Analysis ({sig_type})", color='white', fontsize=11, fontweight='bold')
    ax1.legend(loc='upper left', facecolor='#1e1e1e', edgecolor='none', labelcolor='white', fontsize=7)
    ax1.grid(True, color='#2a2a2a', linestyle=':', alpha=0.6)
    ax1.set_facecolor('#121212')

    ax2.plot(df_subset['DateIndex'], df_subset['RSI'], color='#ab47bc', linewidth=1.2, label='RSI (14)')
    ax2.axhline(y=70, color='#f44336', linestyle=':', linewidth=1)
    ax2.axhline(y=30, color='#4caf50', linestyle=':', linewidth=1)
    ax2.set_facecolor('#121212')
    ax2.grid(True, color='#2a2a2a', linestyle=':', alpha=0.6)
    ax2.legend(loc='upper left', facecolor='#1e1e1e', edgecolor='none', labelcolor='white', fontsize=7)
    ax2.tick_params(colors='white', labelsize=7)

    fig.patch.set_facecolor('#1a1a1a')
    ax1.tick_params(colors='white', labelsize=7)
    plt.tight_layout()

    buf = io.BytesIO()
    plt.savefig(buf, format='png', dpi=120)
    buf.seek(0)
    plt.close(fig)
    return buf

# ==================== TELEGRAM BOT HANDLERS ====================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    user_states[user_id] = STATE_WAIT_CAPITAL
    await update.message.reply_text(
        "🤖 **Institutional Real-Money Forex AI Auto-Trader Engine**\n\n"
        "আপনার মোট **ক্যাপিটাল/ব্যালেন্স** টাইপ করুন (যেমন: `1000`):",
        parse_mode="Markdown"
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    text = update.message.text.strip() if update.message.text else ""
    doc = update.message.document

    admin_user, admin_pass = load_admin_credentials()

    if text.lower() == "admin":
        user_states[user_id] = STATE_WAIT_ADMIN_USER
        admin_session[user_id] = {}
        await update.message.reply_text("আপনার ইউজার নেমটি দেন:")
        return

    current_state = user_states.get(user_id)

    if current_state == STATE_WAIT_ADMIN_USER:
        if text == admin_user:
            admin_session[user_id]["auth_user"] = True
            user_states[user_id] = STATE_WAIT_ADMIN_PASS
            await update.message.reply_text("আপনার পাসওয়ার্ডটি দেন:")
        else:
            user_states[user_id] = None
            await update.message.reply_text("❌ ভুল ইউজারনেম! প্রসেস বাতিল করা হলো।")
        return

    elif current_state == STATE_WAIT_ADMIN_PASS:
        if text == admin_pass:
            user_states[user_id] = STATE_WAIT_ADMIN_CODE
            await update.message.reply_text(
                "🛠️ **ডাইরেক্ট আপডেট প্যানেল** 🛠️\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "স্বাগতম! কোড আপডেট করতে চাইলে ফাইল আপলোড করুন অথবা কোড পেস্ট করে নিচে 'Ko' বা 'OK' লিখুন।\n"
                "🔑 **ইউজারনেম ও পাসওয়ার্ড পরিবর্তন করতে '1' লিখুন।**\n"
                "⚠️ **প্যানেল থেকে বের হতে 'No' লিখুন।**"
            )
        else:
            user_states[user_id] = None
            await update.message.reply_text("❌ ভুল পাসওয়ার্ড!")
        return

    elif current_state == STATE_WAIT_ADMIN_CODE:
        if text.lower() == "no":
            user_states[user_id] = None
            await update.message.reply_text("❌ আপডেট প্যানেল বন্ধ করা হয়েছে।")
            return

        if text == "1":
            user_states[user_id] = STATE_WAIT_NEW_ADMIN_USER
            await update.message.reply_text("নতুন অ্যাডমিন ইউজারনেমটি লিখুন:")
            return

        new_code = None
        text_content = text or update.message.caption or ""

        if doc and doc.file_name.endswith('.py'):
            file = await doc.get_file()
            new_code = await file.download_as_bytearray()
        elif text_content and ("import" in text_content or "def " in text_content or len(text_content) > 20):
            lines = text_content.split('\n')
            code_lines = [l for l in lines if l.strip().upper() not in ["KO", "OK"]]
            new_code = "\n".join(code_lines).encode('utf-8')

        if not new_code:
            await update.message.reply_text("❌ ভ্যালিড পাইথন কোড পাওয়া যায়নি।")
            return

        try:
            current_script = os.path.abspath(__file__)
            with open(current_script, 'wb') as f:
                f.write(new_code)
                
            await update.message.reply_text("✅ **সফল!** কোড সফলভাবে আপডেট হয়ে অটো-রিস্টার্ট নিয়েছে...")
            os.execl(sys.executable, sys.executable, *sys.argv)
        except Exception as e:
            await update.message.reply_text(f"❌ কোড আপডেট করতে সমস্যা হয়েছে: {e}")
            user_states[user_id] = None
        return

    elif current_state == STATE_WAIT_NEW_ADMIN_USER:
        admin_session[user_id]["new_user"] = text
        user_states[user_id] = STATE_WAIT_NEW_ADMIN_PASS
        await update.message.reply_text("এখন নতুন অ্যাডমিন পাসওয়ার্ডটি লিখুন:")
        return

    elif current_state == STATE_WAIT_NEW_ADMIN_PASS:
        new_pass = text
        curr_admin, _ = load_admin_credentials()
        new_user = admin_session.get(user_id, {}).get("new_user", curr_admin)

        if save_admin_credentials(new_user, new_pass):
            user_states[user_id] = None
            await update.message.reply_text(f"✅ **সফল!** ইউজারনেম (`{new_user}`) এবং পাসওয়ার্ড আপডেট হয়েছে।")
        else:
            user_states[user_id] = None
            await update.message.reply_text("❌ তথ্য সেভ করতে সমস্যা হয়েছে।")
        return

    # Trading Flow
    if current_state == STATE_WAIT_CAPITAL:
        try:
            capital = float(text.replace(',', ''))
            user_capital[user_id] = capital
            user_states[user_id] = STATE_WAIT_RISK
            await update.message.reply_text(
                f"✅ **ক্যাপিটাল: ${capital:,.2f}**\n\n"
                f"প্রতিটি ট্রেডে কত % রিস্ক নিতে চান? (যেমন: `1` বা `2`):",
                parse_mode="Markdown"
            )
        except ValueError:
            await update.message.reply_text("❌ সঠিক সংখ্যা লিখুন:")
        return

    elif current_state == STATE_WAIT_RISK:
        try:
            risk = float(text.replace(',', ''))
            user_risk[user_id] = risk
            user_states[user_id] = STATE_WAIT_PAIR
            await update.message.reply_text(
                f"✅ **রিস্ক: {risk}%**\n\n"
                f"ট্রেড করার জন্য ফরেক্স পেয়ার টাইপ করুন (যেমন: `EURUSD`, `XAUUSD`, `GBPJPY`):",
                parse_mode="Markdown"
            )
        except ValueError:
            await update.message.reply_text("❌ সঠিক সংখ্যা লিখুন:")
        return

    elif current_state == STATE_WAIT_PAIR:
        pair_name = text.upper()
        capital = user_capital.get(user_id, 1000.0)
        risk_pct = user_risk.get(user_id, 1.0)

        wait_msg = await update.message.reply_text(f"🔍 Exness লাইভ সার্ভার থেকে **{pair_name}** স্ক্যান ও অটো এক্সিকিউট করা হচ্ছে...", parse_mode="Markdown")

        res = analyze_forex_market(pair_name)

        if res[0] in ["BUY", "SELL"]:
            sig_type, entry, tp, sl, df_5m = res
            symbol = format_symbol(pair_name)
            
            # --- DIRECT EXNESS TRADE EXECUTION ---
            success, trade_msg = execute_exness_trade(symbol, sig_type, entry, tp, sl, capital, risk_pct)

            msg = (
                f"🚀 **INSTITUTIONAL {sig_type} SIGNAL** 🚀\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"📍 **Pair:** `{pair_name}`\n"
                f"📌 **Order Parameters:**\n"
                f"• Entry: `{entry:.5f}`\n"
                f"• TP: `{tp:.5f}`\n"
                f"• SL: `{sl:.5f}`\n\n"
                f"⚡ **Exness Status:** {trade_msg}"
            )

            try:
                buf = generate_signal_chart(df_5m, pair_name, entry, tp, sl, sig_type)
                await wait_msg.delete()
                await update.message.reply_photo(photo=buf, caption=msg, parse_mode="Markdown")
            except Exception:
                await wait_msg.edit_text(msg, parse_mode="Markdown")
        else:
            _, error_msg, _ = res
            await wait_msg.edit_text(error_msg, parse_mode="Markdown")

        await update.message.reply_text("👉 নতুন পেয়ারের নাম লিখে পাঠান:")
        return

    await update.message.reply_text("বট চালু করতে `/start` কমান্ড দিন।", parse_mode="Markdown")

# ==================== MAIN INITIALIZATION ====================

async def main():
    # Exness MT5 Initialization on Start
    init_mt5_connection()

    flask_thread = Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)
    
    while True:
        await asyncio.sleep(1)

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("Bot Stopped.")
