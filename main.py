"""
main.py
Telegram bot entry point – advanced command handlers,
background trading engine, graceful shutdown.
"""

import asyncio
import gc
import logging
import os
import re
import signal
import sys
import time
from typing import Any, Dict, Optional

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.error import RetryAfter

import config
from strategy_engine import analyze_signal
from security_rms import is_trading_allowed, update_daily_state, load_daily_state
from custom_modules import render_chart_proof, send_chart_via_telegram

# -----------------------------------------------------------------------------
# Logging
# -----------------------------------------------------------------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# -----------------------------------------------------------------------------
# Global state
# -----------------------------------------------------------------------------
user_sessions: Dict[int, Dict[str, Any]] = {}
_user_locks: Dict[int, asyncio.Lock] = {}
orchestrator_tasks: Dict[int, asyncio.Task] = {}
meta_lock = asyncio.Lock()
app: Optional[Application] = None

# -----------------------------------------------------------------------------
# Lock helpers
# -----------------------------------------------------------------------------
async def get_user_lock(user_id: int) -> asyncio.Lock:
    if user_id not in _user_locks:
        async with meta_lock:
            if user_id not in _user_locks:
                _user_locks[user_id] = asyncio.Lock()
    return _user_locks[user_id]

# -----------------------------------------------------------------------------
# FloodWait shield
# -----------------------------------------------------------------------------
async def safe_send_message(bot, chat_id: int, text: str, **kwargs) -> Any:
    for attempt in range(5):
        try:
            return await bot.send_message(chat_id=chat_id, text=text, **kwargs)
        except RetryAfter as e:
            wait = e.retry_after
            logger.warning(f"FloodWait: sleeping {wait}s for chat {chat_id}")
            await asyncio.sleep(wait)
        except Exception as exc:
            logger.error(f"Failed to send message: {exc}")
            break
    return None

# -----------------------------------------------------------------------------
# Inline keyboards
# -----------------------------------------------------------------------------
def build_timeframe_keyboard() -> InlineKeyboardMarkup:
    keyboard = [
        [
            InlineKeyboardButton("1m", callback_data="tf_1m"),
            InlineKeyboardButton("2m", callback_data="tf_2m"),
            InlineKeyboardButton("3m", callback_data="tf_3m"),
            InlineKeyboardButton("5m", callback_data="tf_5m"),
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

TRADING_PAIRS = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "NZDUSD", "EURJPY", "GBPJPY"]

def build_pair_keyboard() -> InlineKeyboardMarkup:
    keyboard = []
    row = []
    for pair in TRADING_PAIRS:
        row.append(InlineKeyboardButton(pair, callback_data=f"pair_{pair}"))
        if len(row) == 3:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    return InlineKeyboardMarkup(keyboard)

# -----------------------------------------------------------------------------
# Wizard state (text input)
# -----------------------------------------------------------------------------
class WizardState:
    IDLE = 0
    AWAITING_CAPITAL = 1
    AWAITING_RISK = 2

# -----------------------------------------------------------------------------
# Utility: stop orchestrator for a user
# -----------------------------------------------------------------------------
async def stop_user_orchestrator(user_id: int):
    task = orchestrator_tasks.pop(user_id, None)
    if task and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

# -----------------------------------------------------------------------------
# Command: /start
# -----------------------------------------------------------------------------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    auth_ids = getattr(config, "AUTHORIZED_USER_IDS", [])
    if auth_ids and user.id not in auth_ids:
        await update.message.reply_text("⛔ Unauthorized user.")
        return

    async with await get_user_lock(user.id):
        session = user_sessions.get(user.id)
        if session is None:
            default_tf = getattr(config, "DEFAULT_SIGNAL_TIMEFRAME", "1m")
            session = {
                "state": WizardState.AWAITING_CAPITAL,
                "capital": None,
                "risk_percent": None,
                "timeframe": default_tf,
                "active_pair": None,
                "daily_state": load_daily_state(user.id),
                "strategy_toggles": {},
            }
            user_sessions[user.id] = session
            await update.message.reply_text(
                "👋 Welcome! Please enter your starting account capital in USD (e.g., 100):"
            )
        else:
            await update.message.reply_text(
                "Already configured. Use /status to see current settings, "
                "or send a pair name (e.g. EURUSD) to start trading."
            )

# -----------------------------------------------------------------------------
# Command: /help
# -----------------------------------------------------------------------------
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    help_text = (
        "📚 **Bot Commands**\n"
        "/start - Initial setup wizard\n"
        "/help - Display command list\n"
        "/status - Show account balance & risk state\n"
        "/stop - Stop active trading session\n"
        "/setcapital <amount> - Update account capital\n"
        "/setrisk <percentage> - Update daily max loss limit %\n"
        "/selectpair - Choose pair from interactive menu\n\n"
        "💡 You can type currency pairs directly (e.g., EURUSD) or timeframes (1m, 5m).\n"
        "Use `BOT.OFF` to freeze execution, `BOT.OM` to resume, `OFF` to drop active pair."
    )
    await update.message.reply_text(help_text, parse_mode="Markdown")

# -----------------------------------------------------------------------------
# Command: /status
# -----------------------------------------------------------------------------
async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    async with await get_user_lock(user.id):
        session = user_sessions.get(user.id)
        if not session:
            await update.message.reply_text("No active session found. Use /start.")
            return
        cap = session.get("capital", "N/A")
        risk = session.get("risk_percent", "N/A")
        tf = session.get("timeframe", "N/A")
        pair = session.get("active_pair", "None")
        daily_state = session.get("daily_state", {})
        daily_loss = daily_state.get("daily_loss", 0.0)
        locked = daily_state.get("locked", False)
        cooldown = daily_state.get("cooldown_until")

        cooldown_str = "None"
        if cooldown and time.time() < cooldown:
            cooldown_str = time.strftime('%H:%M:%S', time.localtime(cooldown))

        status_msg = (
            f"💰 Capital: ${cap}\n"
            f"📉 Daily Loss Limit: {risk}% (Current loss: ${abs(daily_loss):.2f})\n"
            f"⏱️ Timeframe: {tf}\n"
            f"📊 Active Pair: {pair}\n"
            f"🔒 Locked: {'Yes' if locked else 'No'}\n"
            f"⏸️ Cooldown Active: {cooldown_str}"
        )
        await update.message.reply_text(status_msg)

# -----------------------------------------------------------------------------
# Command: /stop
# -----------------------------------------------------------------------------
async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    async with await get_user_lock(user.id):
        session = user_sessions.get(user.id)
        if session:
            await stop_user_orchestrator(user.id)
            session["active_pair"] = None
            session["state"] = WizardState.IDLE
            plt.close("all")
            gc.collect()
        await update.message.reply_text("🛑 All background trading tasks stopped.")

# -----------------------------------------------------------------------------
# Command: /setcapital
# -----------------------------------------------------------------------------
async def setcapital_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not context.args:
        await update.message.reply_text("Usage: /setcapital <amount>")
        return
    try:
        new_capital = float(context.args[0])
        if new_capital <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Please provide a valid positive number.")
        return

    async with await get_user_lock(user.id):
        session = user_sessions.get(user.id)
        if not session:
            await update.message.reply_text("No active session. Use /start.")
            return
        session["capital"] = new_capital
        if session.get("daily_state"):
            session["daily_state"]["capital"] = new_capital
        await update.message.reply_text(f"✅ Capital updated to ${new_capital:.2f}")

# -----------------------------------------------------------------------------
# Command: /setrisk
# -----------------------------------------------------------------------------
async def setrisk_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not context.args:
        await update.message.reply_text("Usage: /setrisk <percentage>")
        return
    try:
        new_risk = float(context.args[0])
        if not (0 < new_risk <= 100):
            raise ValueError
    except ValueError:
        await update.message.reply_text("Provide a risk percentage between 0 and 100.")
        return

    async with await get_user_lock(user.id):
        session = user_sessions.get(user.id)
        if not session:
            await update.message.reply_text("No active session. Use /start.")
            return
        session["risk_percent"] = new_risk
        if session.get("daily_state"):
            session["daily_state"]["risk_percent"] = new_risk
        await update.message.reply_text(f"✅ Daily loss limit set to {new_risk}%")

# -----------------------------------------------------------------------------
# Command: /selectpair
# -----------------------------------------------------------------------------
async def selectpair_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Choose a trading pair:",
        reply_markup=build_pair_keyboard()
    )

# -----------------------------------------------------------------------------
# Callback Query Handler
# -----------------------------------------------------------------------------
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = query.data

    async with await get_user_lock(user_id):
        session = user_sessions.get(user_id)
        if not session:
            await query.edit_message_text("Session expired. Please /start again.")
            return

        if data.startswith("tf_"):
            tf = data[3:].lower()
            session["timeframe"] = tf
            await query.edit_message_text(f"✅ Timeframe set to {tf.upper()}.")
            if session.get("active_pair"):
                await restart_orchestrator_for_user(user_id, session)
            return

        if data.startswith("pair_"):
            pair = data[5:].upper()
            session["active_pair"] = pair
            session["state"] = WizardState.IDLE
            await query.edit_message_text(f"🎯 Active pair set to {pair}. Starting background engine...")
            await start_orchestrator_for_user(user_id, session)
            return

# -----------------------------------------------------------------------------
# Message Handler
# -----------------------------------------------------------------------------
async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    auth_ids = getattr(config, "AUTHORIZED_USER_IDS", [])
    if auth_ids and user.id not in auth_ids:
        return

    text = update.message.text.strip()
    user_id = user.id

    async with await get_user_lock(user_id):
        session = user_sessions.get(user_id)
        if session is None:
            await update.message.reply_text("Session not found. Send /start to setup.")
            return

        state = session.get("state", WizardState.IDLE)

        # Wizard state processing
        if state == WizardState.AWAITING_CAPITAL:
            try:
                capital = float(text)
                if capital <= 0:
                    raise ValueError
            except ValueError:
                await update.message.reply_text("❌ Please enter a positive number for capital.")
                return
            session["capital"] = capital
            session["state"] = WizardState.AWAITING_RISK
            await update.message.reply_text("Enter daily max loss limit percentage (e.g. 5 for 5%):")
            return

        if state == WizardState.AWAITING_RISK:
            try:
                risk = float(text)
                if not (0 < risk <= 100):
                    raise ValueError
            except ValueError:
                await update.message.reply_text("❌ Enter a valid risk percentage (1-100).")
                return
            session["risk_percent"] = risk
            session["state"] = WizardState.IDLE
            if session.get("daily_state"):
                session["daily_state"]["capital"] = session["capital"]
                session["daily_state"]["risk_percent"] = risk
            await update.message.reply_text(
                "Setup complete! Select target timeframe:",
                reply_markup=build_timeframe_keyboard()
            )
            return

        # Text Control Directives
        if text.upper() == "BOT.OFF":
            await stop_user_orchestrator(user_id)
            session["active_pair"] = None
            await update.message.reply_text("🛑 Bot paused. Send BOT.OM to resume.")
            return

        if text.upper() == "BOT.OM":
            if session.get("capital") is None:
                await update.message.reply_text("⚠️ Complete setup first using /start.")
                return
            await update.message.reply_text("✅ Bot active. Send a pair or use /selectpair.")
            return

        if text.upper() == "OFF":
            if session.get("active_pair"):
                await update.message.reply_text(f"⏹️ Trading session for {session['active_pair']} terminated.")
            await stop_user_orchestrator(user_id)
            session["active_pair"] = None
            plt.close("all")
            gc.collect()
            return

        # Direct Timeframe Switch (e.g., 1m, 5m)
        possible_tf = text.lower()
        tf_dict = getattr(config, "TIMEFRAME_TO_SECONDS", {"1m": 60, "2m": 120, "3m": 180, "5m": 300})
        if possible_tf in tf_dict:
            session["timeframe"] = possible_tf
            await update.message.reply_text(f"🔄 Timeframe set to {possible_tf.upper()}.")
            if session.get("active_pair"):
                await restart_orchestrator_for_user(user_id, session)
            return

        # Direct Currency Pair Input (e.g. EURUSD)
        if re.fullmatch(r"^[a-zA-Z]{6}$", text):
            pair = text.upper()
            if session.get("capital") is None:
                await update.message.reply_text("⚠️ Set capital first via /start.")
                return
            session["active_pair"] = pair
            await start_orchestrator_for_user(user_id, session)
            await update.message.reply_text(f"🎯 Analysis active for {pair} on [{session['timeframe'].upper()}].")
            return

        await update.message.reply_text("Unrecognized command. Type /help for assistance.")

# -----------------------------------------------------------------------------
# Orchestrator Management
# -----------------------------------------------------------------------------
async def start_orchestrator_for_user(user_id: int, session: Dict[str, Any]) -> None:
    await stop_user_orchestrator(user_id)
    task = asyncio.create_task(analysis_orchestrator(user_id, session))
    orchestrator_tasks[user_id] = task

async def restart_orchestrator_for_user(user_id: int, session: Dict[str, Any]) -> None:
    await stop_user_orchestrator(user_id)
    if session.get("active_pair"):
        await start_orchestrator_for_user(user_id, session)

async def analysis_orchestrator(user_id: int, session: Dict[str, Any]) -> None:
    """Continuous background loop per active user trading session."""
    if app is None:
        logger.error("Application instance not initialized.")
        return

    bot = app.bot
    pair = session.get("active_pair")
    tf = session.get("timeframe", "1m")
    tf_dict = getattr(config, "TIMEFRAME_TO_SECONDS", {"1m": 60, "2m": 120, "3m": 180, "5m": 300})
    period = tf_dict.get(tf, 60)
    last_processed_candle = None

    pre_calc_sec = getattr(config, "PRE_CALCULATION_SECONDS", 8)
    dispatch_sec = getattr(config, "SIGNAL_DISPATCH_SECONDS", 5)

    while True:
        try:
            if not session.get("active_pair") or session["active_pair"] != pair:
                logger.info(f"Orchestrator for {pair} terminated.")
                return

            now = time.time()
            next_close = (int(now / period) + 1) * period
            t8_time = next_close - pre_calc_sec
            t5_time = next_close - dispatch_sec

            if now > t8_time + 2:
                sleep_duration = max(1.0, next_close - time.time() + 0.5)
                await asyncio.sleep(sleep_duration)
                continue

            while time.time() < t8_time:
                await asyncio.sleep(0.1)
                if session.get("active_pair") != pair:
                    return

            candle_open = next_close - period
            candle_id = int(candle_open)

            if candle_id == last_processed_candle:
                sleep_duration = max(1.0, next_close - time.time() + 0.5)
                await asyncio.sleep(sleep_duration)
                continue

            # Analyze Signal
            signal_data = await analyze_signal(session, pair, tf, candle_open)

            now = time.time()
            if now < t5_time:
                await asyncio.sleep(t5_time - now)

            if signal_data:
                if is_trading_allowed(session):
                    last_processed_candle = candle_id
                    session["last_signal_candle"] = candle_id

                    # Render Proof Chart in Thread
                    loop = asyncio.get_running_loop()
                    chart_path = await loop.run_in_executor(
                        None, render_chart_proof, session, pair, tf, signal_data, candle_open
                    )

                    text_alert = (
                        f"🔥 **{pair}** – **{tf.upper()}**\n"
                        f"Signal: **{signal_data.get('direction', 'N/A')}**\n"
                        f"Entry: {signal_data.get('entry_price', 'N/A')}\n"
                        f"SL: {signal_data.get('stop_loss', 'N/A')}\n"
                        f"TP: {signal_data.get('take_profit', 'N/A')}"
                    )
                    await safe_send_message(bot, user_id, text_alert, parse_mode="Markdown")

                    if chart_path and os.path.exists(chart_path):
                        await send_chart_via_telegram(bot, user_id, chart_path)

            sleep_duration = max(1.0, next_close - time.time() + 0.5)
            await asyncio.sleep(sleep_duration)

        except asyncio.CancelledError:
            logger.info(f"Orchestrator task for {pair} cancelled.")
            break
        except Exception as e:
            logger.error(f"Error in orchestrator loop for {pair}: {e}", exc_info=True)
            await asyncio.sleep(2)

# -----------------------------------------------------------------------------
# Main Entry Point
# -----------------------------------------------------------------------------
def main() -> None:
    global app
    token = getattr(config, "TELEGRAM_BOT_TOKEN", "")
    if not token or token == "YOUR_TELEGRAM_BOT_TOKEN_HERE":
        logger.error("TELEGRAM_BOT_TOKEN is missing in config.py!")
        sys.exit(1)

    app = Application.builder().token(token).build()

    # Register Handlers
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("stop", stop_command))
    app.add_handler(CommandHandler("setcapital", setcapital_command))
    app.add_handler(CommandHandler("setrisk", setrisk_command))
    app.add_handler(CommandHandler("selectpair", selectpair_command))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))

    logger.info("⚡ Quotex Phantom Bot is starting polling...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
