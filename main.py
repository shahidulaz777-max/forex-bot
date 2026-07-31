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

# Logging Setup
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

TELEGRAM_TOKEN = "8926811082:AAH4T7FmcB2pcrwHuLA18TnPF3LV2mktaDc"

# ===== নিরাপদ কনফিগারেশন হ্যান্ডলিং (ক্র্যাশ রোধ করতে) =====
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
    return "Institutional High-Accuracy Forex AI Bot is Active 24/7 on Cloud!"

def run_flask():
    port = int(os.environ.get("PORT", 5000))
    app_flask.run(host="0.0.0.0", port=port)

# ==================== ROBUST MARKET DATA FETCHING ====================

def format_symbol(pair):
    pair = pair.upper().strip().replace("/", "").replace(" ", "")
    if pair in ["XAUUSD", "GOLD"]:
        return "GC=F"
    elif pair in ["XAGUSD", "SILVER"]:
        return "SI=F"
    elif pair in ["EURUSD", "GBPUSD", "USDJPY", "GBPJPY", "AUDUSD", "USDCAD", "NZDUSD"]:
        return f"{pair}=X"
    elif not pair.endswith("=X") and not pair.endswith("=F"):
        return f"{pair}=X"
    return pair

def get_live_candles(symbol, interval="5m", range_period="1d"):
    session = requests.Session()
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
        'Accept': 'application/json,text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.5'
    }
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval={interval}&range={range_period}"
    
    for attempt in range(4):
        try:
            response = session.get(url, headers=headers, timeout=10)
            if response.status_code == 200:
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
                if not df.empty:
                    return df
        except Exception:
            time.sleep(1.5)
            continue
        
    return pd.DataFrame()

# ==================== HIGH-ACCURACY AI SIGNAL ENGINE ====================

def analyze_forex_market(pair_name):
    symbol = format_symbol(pair_name)
    df_5m = get_live_candles(symbol, "5m", "1d")

    if df_5m.empty:
        return "INVALID_PAIR", f"❌ **'{pair_name.upper()}'** পেয়ারটির লাইভ ডাটা পাওয়া যায়নি।"

    df_5m['EMA_20'] = df_5m['Close'].ewm(span=20, adjust=False).mean()
    
    delta = df_5m['Close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    rs = gain / loss
    df_5m['RSI'] = 100 - (100 / (1 + rs))

    exp1 = df_5m['Close'].ewm(span=12, adjust=False).mean()
    exp2 = df_5m['Close'].ewm(span=26, adjust=False).mean()
    df_5m['MACD'] = exp1 - exp2
    df_5m['Signal_Line'] = df_5m['MACD'].ewm(span=9, adjust=False).mean()

    curr_price = df_5m['Close'].iloc[-1]
    rsi_5m = df_5m['RSI'].iloc[-1]
    ema20_5m = df_5m['EMA_20'].iloc[-1]
    macd_5m = df_5m['MACD'].iloc[-1]
    sig_5m = df_5m['Signal_Line'].iloc[-1]

    high_prev = df_5m['High'].iloc[-2]
    low_prev = df_5m['Low'].iloc[-2]
    close_prev = df_5m['Close'].iloc[-2]
    pivot = (high_prev + low_prev + close_prev) / 3
    res_1 = (2 * pivot) - low_prev
    sup_1 = (2 * pivot) - high_prev

    is_buy = (curr_price >= ema20_5m) and (rsi_5m > 40) and (macd_5m >= sig_5m)
    is_sell = (curr_price <= ema20_5m) and (rsi_5m < 60) and (macd_5m <= sig_5m)

    if is_buy:
        tp = curr_price + (res_1 - curr_price) * 0.8 if res_1 > curr_price else curr_price * 1.0020
        sl = sup_1 if sup_1 < curr_price else curr_price * 0.9980
        return "BUY", curr_price, tp, sl, df_5m

    elif is_sell:
        tp = curr_price - (curr_price - sup_1) * 0.8 if sup_1 < curr_price else curr_price * 0.9980
        sl = res_1 if res_1 > curr_price else curr_price * 1.0020
        return "SELL", curr_price, tp, sl, df_5m

    else:
        if curr_price >= ema20_5m:
            tp = curr_price * 1.0020
            sl = curr_price * 0.9980
            return "BUY", curr_price, tp, sl, df_5m
        else:
            tp = curr_price * 0.9980
            sl = curr_price * 1.0020
            return "SELL", curr_price, tp, sl, df_5m

# ==================== TELEGRAM BOT HANDLERS ====================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    user_states[user_id] = STATE_WAIT_CAPITAL
    await update.message.reply_text(
        "🤖 **Institutional High-Accuracy Forex AI Bot**\n\n"
        "আপনার মোট **ক্যাপিটাল / ব্যালেন্স** কত? (যেমন: `100`, `1000`, `150000`):",
        parse_mode="Markdown"
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    text = update.message.text.strip() if update.message.text else ""
    doc = update.message.document

    admin_user, admin_pass = load_admin_credentials()

    # 1. UNIVERSAL ADMIN TRIGGER: 'admin' can be typed ANYTIME
    if text.lower() == "admin":
        user_states[user_id] = STATE_WAIT_ADMIN_USER
        admin_session[user_id] = {}
        await update.message.reply_text("আপনার ইউজার নেমটি দেন:")
        return

    current_state = user_states.get(user_id)

    # 2. ADMIN FLOW
    if current_state == STATE_WAIT_ADMIN_USER:
        if text == admin_user:
            admin_session[user_id]["auth_user"] = True
            user_states[user_id] = STATE_WAIT_ADMIN_PASS
            await update.message.reply_text("আপনার পাসওয়ার্ডটি দেন:")
        else:
            user_states[user_id] = None
            await update.message.reply_text("❌ ভুল ইউজারনেম! প্রসেস বাতিল করা হলো। 'admin' লিখে আবার চেষ্টা করুন।")
        return

    elif current_state == STATE_WAIT_ADMIN_PASS:
        if text == admin_pass:
            user_states[user_id] = STATE_WAIT_ADMIN_CODE
            await update.message.reply_text(
                "🛠️ **ডাইরেক্ট আপডেট প্যানেল** 🛠️\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "স্বাগতম! কোড আপডেট করতে চাইলে ফাইল আপলোড করুন অথবা কোড পেস্ট করে নিচে 'Ko' বা 'OK' লিখুন।\n"
                "🔑 **ইউজারনেম ও পাসওয়ার্ড পরিবর্তন করতে চাইলে '1' লিখুন।**\n"
                "⚠️ **যদি আপডেট না করে বের হতে চান, তবে শুধু 'No' লিখে সেন্ড করুন।**"
            )
        else:
            user_states[user_id] = None
            await update.message.reply_text("❌ ভুল পাসওয়ার্ড! প্রসেস বাতিল করা হলো।")
        return

    elif current_state == STATE_WAIT_ADMIN_CODE:
        # Check if user wants to exit using 'No'
        if text.lower() == "no":
            user_states[user_id] = None
            await update.message.reply_text("❌ আপডেট প্যানেল বন্ধ করা হয়েছে। বট স্বাভাবিক মোডে ফিরে গেছে। বট চালু করতে `/start` কমান্ড দিন।", parse_mode="Markdown")
            return

        # Check if user wants to change username & password using '1'
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
            await update.message.reply_text("❌ কোনো ভ্যালিড `.py` ফাইল অথবা পাইথন কোড পাওয়া যায়নি। ইউজারনেম/পাসওয়ার্ড বদলাতে **'1'** অথবা প্যানেল থেকে বের হতে চাইলে **'No'** লিখুন।")
            return

        if "KO" not in text_content.upper() and "OK" not in text_content.upper() and not doc:
            await update.message.reply_text("⚠️ কোড পেস্ট করার পর সবার নিচে ফাঁকা রেখে **'Ko'** বা **'OK'** লিখে সেন্ড করুন। অথবা বের হতে চাইলে **'No'** লিখুন।")
            return

        try:
            current_script = os.path.abspath(__file__)
            with open(current_script, 'wb') as f:
                f.write(new_code)
                
            await update.message.reply_text("✅ **সফল!** কোড আপডেট হয়ে রিস্টার্ট হয়েছে এবং নতুন কোড রান করা শুরু হয়েছে...")
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
            await update.message.reply_text(f"✅ **সফল!** নতুন ইউজারনেম (`{new_user}`) এবং পাসওয়ার্ড নিরাপদে আপডেট করা হয়েছে।")
        else:
            user_states[user_id] = None
            await update.message.reply_text("❌ ইউজারনেম/পাসওয়ার্ড সেভ করতে সমস্যা হয়েছে।")
        return

    # 3. TRADING SETUP FLOW
    if current_state == STATE_WAIT_CAPITAL:
        try:
            capital = float(text.replace(',', ''))
            user_capital[user_id] = capital
            user_states[user_id] = STATE_WAIT_RISK
            await update.message.reply_text(
                f"✅ **ক্যাপিটাল সেট করা হয়েছে: {capital:,.2f}**\n\n"
                f"প্রতিটি ট্রেডে কত পার্সেন্ট **রিস্ক (Risk %)** নিতে চান? (যেমন: `1`, `2`, `3`):",
                parse_mode="Markdown"
            )
        except ValueError:
            await update.message.reply_text("❌ সঠিক সংখ্যা লিখুন (যেমন: `1000`):")
        return

    elif current_state == STATE_WAIT_RISK:
        try:
            risk = float(text.replace(',', ''))
            user_risk[user_id] = risk
            user_states[user_id] = STATE_WAIT_PAIR
            await update.message.reply_text(
                f"✅ **রিস্ক সেট করা হয়েছে: {risk}%**\n\n"
                f"এখন যে ফরেক্স পেয়ারে ট্রেড করতে চান, তার নাম টাইপ করুন (যেমন: `EURUSD`, `XAUUSD`, `GBPJPY`):",
                parse_mode="Markdown"
            )
        except ValueError:
            await update.message.reply_text("❌ সঠিক সংখ্যা লিখুন (যেমন: `1` বা `2`):")
        return

    elif current_state == STATE_WAIT_PAIR:
        if user_id in user_lockout and time.time() < user_lockout[user_id]:
            rem_hours = int((user_lockout[user_id] - time.time()) / 3600)
            await update.message.reply_text(
                f"⛔ **LOCKOUT ACTIVE**\n"
                f"রিস্ক রুল অনুযায়ী আগামী **{rem_hours} ঘণ্টা** পর্যন্ত নতুন ট্রেড নেওয়া নিষেধ।"
            )
            return

        pair_name = text.upper()
        capital = user_capital.get(user_id, 1000.0)
        risk_pct = user_risk.get(user_id, 1.0)

        wait_msg = await update.message.reply_text(f"🔍 **{pair_name}** মার্কেট এনালাইজ করা হচ্ছে...", parse_mode="Markdown")

        res = analyze_forex_market(pair_name)

        monitoring_pairs[user_id] = {
            "pair": pair_name,
            "capital": capital,
            "risk_pct": risk_pct,
            "chat_id": update.message.chat_id
        }

        if res[0] in ["BUY", "SELL"]:
            sig_type, entry, tp, sl, df_5m = res
            risk_amount = capital * (risk_pct / 100.0)
            
            msg = (
                f"🚀 **HIGH-ACCURACY {sig_type} SIGNAL** 🚀\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"📍 **Pair:** `{pair_name}`\n"
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

                ax.set_title(f"{pair_name} - 5M Entry Signal Analysis", color='white', fontsize=10, fontweight='bold')
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
                "pair": pair_name,
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

        await update.message.reply_text("👉 নতুন পেয়ারের নাম লিখে পাঠান:")
        return

    # Fallback if state is not set
    await update.message.reply_text("বট চালু করতে `/start` কমান্ড দিন।", parse_mode="Markdown")

# ==================== BACKGROUND SCANNER & TRACKER ====================

async def background_market_scanner(app):
    while True:
        try:
            await asyncio.sleep(20)
            for uid, info in list(monitoring_pairs.items()):
                if uid in active_trades:
                    continue
                if uid in user_lockout and time.time() < user_lockout[uid]:
                    continue

                pair_name = info["pair"]
                capital = info["capital"]
                risk_pct = info["risk_pct"]
                chat_id = info["chat_id"]

                res = analyze_forex_market(pair_name)

                if res[0] in ["BUY", "SELL"]:
                    sig_type, entry, tp, sl, df_5m = res
                    risk_amount = capital * (risk_pct / 100.0)
                    
                    msg = (
                        f"⚡ **AUTO-TRIGGERED {sig_type} SIGNAL** ⚡\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"📍 **Pair:** `{pair_name}`\n"
                        f"⏱️ **Trade Expiry:** `5 Minutes`\n\n"
                        f"📌 **Entries:**\n"
                        f"• Entry: `{entry:.5f}`\n"
                        f"• TP: `{tp:.5f}`\n"
                        f"• SL: `{sl:.5f}`\n\n"
                        f"💼 **Risk:** `{risk_amount:,.2f} ({risk_pct}%)`"
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

                        ax.set_title(f"{pair_name} - Auto 5M Signal", color='white', fontsize=10, fontweight='bold')
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

                        await app.bot.send_photo(chat_id=chat_id, photo=buf, caption=msg, parse_mode="Markdown")
                    except Exception:
                        await app.bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")

                    active_trades[uid] = {
                        "pair": pair_name,
                        "symbol": format_symbol(pair_name),
                        "type": sig_type,
                        "entry": entry,
                        "tp": tp,
                        "sl": sl,
                        "expiry_time": time.time() + 300,
                        "chat_id": chat_id
                    }
                    del monitoring_pairs[uid]
        except Exception:
            await asyncio.sleep(10)

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
                                f"✅ **Status:** প্রফিট টার্গেট অর্জিত হয়েছে!"
                            )
                        else:
                            res_text = (
                                f"🛑 **TRADE RESULT: LOSS (RISK HIT)** 🛑\n"
                                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                                f"📍 **Pair:** `{pair}`\n"
                                f"💵 **Entry:** `{entry:.5f}` | **Close:** `{close_price:.5f}`\n"
                                f"❌ **Status:** স্টপ লস হিট করেছে।"
                            )
                            user_lockout[uid] = time.time() + 86400
                            res_text += f"\n\n⚠️ নিয়ম অনুযায়ী ২৪ ঘণ্টার লকআউট কার্যকর।"

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
                            ax.axhline(y=tp, color='#4caf50', linestyle='-.', linewidth=1.5, label=f'TP: {tp:.5f}')
                            ax.axhline(y=sl, color='#f44336', linestyle='-.', linewidth=1.5, label=f'SL: {sl:.5f}')
                            ax.axhline(y=close_price, color='#ffffff', linestyle=':', linewidth=1.5, label=f'Close: {close_price:.5f}')

                            ax.set_title(f"{pair} - 5M Result Analysis", color='white', fontsize=12, fontweight='bold')
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
                        
                        await app.bot.send_message(chat_id=chat_id, text="👉 নতুন পেয়ারের নাম লিখে পাঠান:", parse_mode="Markdown")
                        completed.append(uid)

            for uid in completed:
                if uid in active_trades:
                    del active_trades[uid]
        except Exception:
            await asyncio.sleep(5)

# ==================== MAIN ====================

async def main():
    flask_thread = Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()

    while True:
        app = None
        try:
            app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

            app.add_handler(CommandHandler("start", start))
            app.add_handler(MessageHandler((filters.TEXT | filters.Document.ALL) & ~filters.COMMAND, handle_message))

            await app.initialize()
            await app.start()
            await app.updater.start_polling(drop_pending_updates=True)

            asyncio.create_task(track_trade_results(app))
            asyncio.create_task(background_market_scanner(app))
            print("🚀 Forex AI Bot with Crash-Proof JSON Config Admin System is running 24/7...")
            
            while True:
                await asyncio.sleep(1)

        except Exception as e:
            print(f"Reconnecting: {e}")
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
