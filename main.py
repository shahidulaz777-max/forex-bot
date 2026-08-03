"""
main.py
Telegram bot entry point – interactive wizard, command parser, orchestrator,
and async task dispatcher with micro-gap defenses.
"""

import asyncio
import gc
import logging
import re
import time
from typing import Any, Dict, Optional

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
from security_rms import is_trading_allowed, load_daily_state
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
# Global user session storage with per-user locks
# -----------------------------------------------------------------------------
user_sessions: Dict[int, Dict[str, Any]] = {}
_user_locks: Dict[int, asyncio.Lock] = {}
_meta_lock = asyncio.Lock()

async def get_user_lock(user_id: int) -> asyncio.Lock:
    """Return an asyncio.Lock() for the given user, creating it if necessary."""
    if user_id not in _user_locks:
        async with _meta_lock:
            if user_id not in _user_locks:
                _user_locks[user_id] = asyncio.Lock()
    return _user_locks[user_id]

# -----------------------------------------------------------------------------
# Timeframe conversion helper
# -----------------------------------------------------------------------------
TIMEFRAME_TO_SECONDS = {
    "1m": 60,
    "2m": 120,
    "3m": 180,
    "5m": 300,
    "10m": 600,
}

def normalize_timeframe(tf: str) -> str:
    """Return lowercase timeframe string with 'm' suffix, e.g. '1m'."""
    tf = tf.strip().lower()
    if tf.endswith("m"):
        return tf
    if tf.isdigit():
        return tf + "m"
    return tf

# -----------------------------------------------------------------------------
# FloodWait shield with exponential backoff
# -----------------------------------------------------------------------------
async def safe_send_message(bot, chat_id: int, text: str, **kwargs) -> None:
    """
    Send a Telegram message with FloodWait retry.
    Implements exponential backoff (max 5 retries).
    """
    for attempt in range(5):
        try:
            return await bot.send_message(chat_id=chat_id, text=text, **kwargs)
        except RetryAfter as e:
            wait = e.retry_after
            logger.warning(f"FloodWait: sleeping {wait}s for chat {chat_id}")
            await asyncio.sleep(wait)
        except Exception as exc:
            logger.error(f"Failed to send message: {exc}")
            raise
    logger.error(f"Failed to send message after retries to {chat_id}")

# -----------------------------------------------------------------------------
# Inline keyboard for timeframe selection
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

# -----------------------------------------------------------------------------
# User wizard states
# -----------------------------------------------------------------------------
class WizardState:
    IDLE = 0
    AWAITING_CAPITAL = 1
    AWAITING_RISK = 2

# -----------------------------------------------------------------------------
# /start command
# -----------------------------------------------------------------------------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user.id not in config.AUTHORIZED_USER_IDS and config.AUTHORIZED_USER_IDS:
        await update.message.reply_text("⛔ Unauthorized. Contact administrator.")
        return

    async with await get_user_lock(user.id):
        user_sessions[user.id] = {
            "state": WizardState.AWAITING_CAPITAL,
            "capital": None,
            "risk_percent": None,
            "timeframe": config.DEFAULT_SIGNAL_TIMEFRAME,
            "active_pair": None,
            "orchestrator_task": None,
            "daily_state": load_daily_state(user.id),
        }

    await update.message.reply_text(
        "👋 Welcome to Quotex Phantom Bot!\n\n"
        "Please enter your starting account capital (USD, e.g. 1000):"
    )

# -----------------------------------------------------------------------------
# Callback handler for timeframe inline buttons
# -----------------------------------------------------------------------------
async def timeframe_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    async with await get_user_lock(user_id):
        session = user_sessions.get(user_id)
        if not session:
            await query.edit_message_text("Session expired. Please /start again.")
            return

        data = query.data
        if data.startswith("tf_"):
            tf = data[3:]
            session["timeframe"] = normalize_timeframe(tf)
            await query.edit_message_text(
                f"✅ Timeframe set to **{session['timeframe'].upper()}**.\n\n"
                "Now send me the trading pair you want to analyse (e.g. EURUSD)."
            )
            session["state"] = WizardState.IDLE

# -----------------------------------------------------------------------------
# Message handler for text inputs and commands
# -----------------------------------------------------------------------------
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user.id not in config.AUTHORIZED_USER_IDS and config.AUTHORIZED_USER_IDS:
        return

    text = update.message.text.strip()
    user_id = user.id

    async with await get_user_lock(user_id):
        session = user_sessions.get(user_id)
        if session is None:
            await update.message.reply_text("Session not found. Use /start.")
            return

        state = session.get("state", WizardState.IDLE)

        # Wizard step 1: capital input
        if state == WizardState.AWAITING_CAPITAL:
            try:
                capital = float(text)
                if capital <= 0:
                    raise ValueError
            except ValueError:
                await update.message.reply_text("❌ Enter a valid positive number for capital (e.g. 1000).")
                return
            session["capital"] = capital
            session["state"] = WizardState.AWAITING_RISK
            await update.message.reply_text("Great! Now enter your daily max loss limit (in %, e.g. 5):")
            return

        # Wizard step 2: risk percentage input
        if state == WizardState.AWAITING_RISK:
            try:
                risk = float(text)
                if risk <= 0 or risk > 100:
                    raise ValueError
            except ValueError:
                await update.message.reply_text("❌ Enter a valid percentage (0–100).")
                return
            session["risk_percent"] = risk
            await update.message.reply_text(
                "Select the signal timeframe:",
                reply_markup=build_timeframe_keyboard(),
            )
            session["state"] = WizardState.IDLE
            return

        # Global command handlers
        if text == "BOT.OFF":
            await stop_orchestrator(user_id, session)
            session["active_pair"] = None
            session["state"] = WizardState.IDLE
            await update.message.reply_text("🛑 Bot turned OFF. Trading halted. Use BOT.OM to resume.")
            return

        if text == "BOT.OM":
            if session["capital"] is None:
                await update.message.reply_text("⚠️ Complete setup with /start first.")
                return
            await update.message.reply_text("✅ Bot is ON. Send a pair to start analysis.")
            return

        if text == "OFF":
            if session.get("active_pair"):
                await update.message.reply_text(f"⏹️ Analysis for {session['active_pair']} stopped.")
            await stop_orchestrator(user_id, session)
            session["active_pair"] = None
            plt.close("all")
            gc.collect()
            await update.message.reply_text("Send me a new trading pair to start fresh.")
            return

        # Dynamic timeframe switching (e.g. 1M, 2M, 5M, 10M)
        possible_tf = normalize_timeframe(text)
        if possible_tf in TIMEFRAME_TO_SECONDS:
            session["timeframe"] = possible_tf
            await update.message.reply_text(f"🔄 Timeframe switched to {possible_tf.upper()}.")
            if session.get("active_pair") and session.get("orchestrator_task"):
                await restart_orchestrator(user_id, session, context)
            return

        # Pair input (6 letters like EURUSD)
        if re.fullmatch(r"[A-Za-z]{6}", text):
            pair = text.upper()
            if session.get("active_pair") and session["active_pair"] != pair:
                await update.message.reply_text(f"Switching analysis to {pair}...")
                await stop_orchestrator(user_id, session)
            session["active_pair"] = pair
            if session["capital"] is None:
                await update.message.reply_text("⚠️ Please /start first to set capital.")
                return
            await start_orchestrator(user_id, session, context)
            await update.message.reply_text(f"🎯 Analysing {pair} on {session['timeframe'].upper()} timeframe.\nType OFF to stop.")
            return

        await update.message.reply_text(
            "Unknown input. Valid options: Pair (e.g. EURUSD), Timeframe (1M, 2M, 5M), BOT.OFF, BOT.OM, OFF."
        )

# -----------------------------------------------------------------------------
# Orchestrator Task Management
# -----------------------------------------------------------------------------
async def start_orchestrator(user_id: int, session: Dict[str, Any], context: ContextTypes.DEFAULT_TYPE) -> None:
    if session.get("orchestrator_task") and not session["orchestrator_task"].done():
        session["orchestrator_task"].cancel()
    task = asyncio.create_task(analysis_orchestrator(user_id, session, context.bot))
    session["orchestrator_task"] = task

async def stop_orchestrator(user_id: int, session: Dict[str, Any]) -> None:
    task = session.get("orchestrator_task")
    if task and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    session["orchestrator_task"] = None

async def restart_orchestrator(user_id: int, session: Dict[str, Any], context: ContextTypes.DEFAULT_TYPE) -> None:
    await stop_orchestrator(user_id, session)
    if session.get("active_pair"):
        await start_orchestrator(user_id, session, context)

# -----------------------------------------------------------------------------
# Precision Orchestrator Loop
# -----------------------------------------------------------------------------
async def analysis_orchestrator(user_id: int, session: Dict[str, Any], bot: Any) -> None:
    pair = session["active_pair"]
    tf = session["timeframe"]
    period = TIMEFRAME_TO_SECONDS[tf]
    last_traded_candle = None

    while True:
        try:
            if not session.get("active_pair") or session["active_pair"] != pair:
                return

            now = time.time()
            next_close = (int(now / period) + 1) * period
            t8_time = next_close - config.PRE_CALCULATION_SECONDS
            t5_time = next_close - config.SIGNAL_DISPATCH_SECONDS

            if now > t8_time + config.CANDLE_BOUNDARY_TOLERANCE:
                await asyncio.sleep(max(0.1, next_close + 0.5 - time.time()))
                continue

            while time.time() < t8_time:
                await asyncio.sleep(0.05)
                if session.get("orchestrator_task") is None or session.get("active_pair") != pair:
                    return

            candle_open = next_close - period
            candle_identifier = int(candle_open)

            if candle_identifier == last_traded_candle:
                await asyncio.sleep(max(0.1, next_close + 0.5 - time.time()))
                continue

            signal = await analyze_signal(session, pair, tf, candle_open)

            now = time.time()
            if now < t5_time:
                await asyncio.sleep(t5_time - now)

            if not signal:
                await asyncio.sleep(max(0.1, next_close + 0.5 - time.time()))
                continue

            if not is_trading_allowed(session):
                logger.info(f"Trade blocked by RMS for {pair}")
                await asyncio.sleep(max(0.1, next_close + 0.5 - time.time()))
                continue

            last_traded_candle = candle_identifier
            session["last_signal_candle"] = candle_identifier

            loop = asyncio.get_running_loop()
            chart_task = loop.create_task(
                loop.run_in_executor(
                    None,
                    render_chart_proof,
                    session, pair, tf, signal, candle_open
                )
            )

            text_alert = (
                f"🔥 **{pair}** – **{tf.upper()}**\n"
                f"Signal: **{signal['direction']}**\n"
                f"Entry: {signal['entry_price']}\n"
                f"SL: {signal['stop_loss']}\n"
                f"TP: {signal['take_profit']}"
            )
            await safe_send_message(bot, user_id, text_alert, parse_mode="Markdown")

            try:
                chart_path = await chart_task
                await send_chart_via_telegram(bot, user_id, chart_path)
            except Exception as e:
                logger.error(f"Chart error: {e}")
            finally:
                plt.close("all")

            await asyncio.sleep(max(0.1, next_close + 0.5 - time.time()))

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"Orchestrator Error: {e}", exc_info=True)
            await asyncio.sleep(2)

# -----------------------------------------------------------------------------
# Application Setup
# -----------------------------------------------------------------------------
def main() -> None:
    app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CallbackQueryHandler(timeframe_callback, pattern="^tf_"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Starting Quotex Phantom Bot...")
    app.run_polling()

if __name__ == "__main__":
    main()
