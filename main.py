import asyncio
import logging
import sys
from aiogram import Bot, Dispatcher, Router, F, types
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

# Configuration Import
try:
    from config import config
except ImportError:
    class Config:
        BOT_TOKEN = "8926811082:AAH4T7FmcB2pcrwHuLA18TnPF3LV2mktaDc"
        CHAT_ID = "6657180457"
        DEFAULT_ASSET = "EURUSD"
        TIMEFRAME = 60
    config = Config()

# Configure Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - [%(levelname)s] - %(name)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("TelegramBaseBot")

# FSM States for Interactive Startup & Onboarding
class OnboardingState(StatesGroup):
    waiting_for_balance = State()
    waiting_for_risk_pct = State()

# Router Initialization
router = Router()

# Helper: Interactive Expiry Keyboard Builder
def get_expiry_keyboard(symbol: str, signal_type: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="⏱️ 1 Min Expiry", callback_data=f"trade:{symbol}:{signal_type}:1m")
    builder.button(text="⏱️ 5 Min Expiry", callback_data=f"trade:{symbol}:{signal_type}:5m")
    builder.adjust(2)
    return builder.as_markup()

# Command Handlers
@router.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await message.answer(
        "⚡ **Quotex Real-Market Trading Bot**\n\n"
        "Welcome! Let's initialize your trading session parameters.\n\n"
        "👉 **Step 1:** Enter your current account balance in USD (e.g., `1000`):",
        parse_mode="Markdown"
    )
    await state.set_state(OnboardingState.waiting_for_balance)

@router.message(StateFilter(OnboardingState.waiting_for_balance))
async def process_balance(message: types.Message, state: FSMContext):
    try:
        balance = float(message.text.replace("$", "").strip())
        if balance <= 0:
            raise ValueError()
        await state.update_data(balance=balance)
        await message.answer(
            f"✅ Account Balance set to **${balance:.2f}**\n\n"
            f"👉 **Step 2:** Enter your maximum daily risk percentage (e.g., `5` for 5%):",
            parse_mode="Markdown"
        )
        await state.set_state(OnboardingState.waiting_for_risk_pct)
    except ValueError:
        await message.answer("❌ Invalid input! Please enter a valid number (e.g., `1000`).")

@router.message(StateFilter(OnboardingState.waiting_for_risk_pct))
async def process_risk_pct(message: types.Message, state: FSMContext):
    try:
        risk_pct = float(message.text.replace("%", "").strip())
        if risk_pct <= 0 or risk_pct > 50:
            raise ValueError()

        data = await state.get_data()
        balance = data.get("balance", 1000.0)
        await state.clear()

        max_loss = balance * (risk_pct / 100.0)
        suggested_trade = balance * 0.015

        await message.answer(
            f"🎯 **Setup Complete & Risk Parameters Applied**\n\n"
            f"• **Balance:** `${balance:.2f} USD`\n"
            f"• **Daily Risk Limit:** `{risk_pct}%` (`${max_loss:.2f} USD`)\n"
            f"• **Suggested Trade Size:** `${suggested_trade:.2f} USD`\n"
            f"• **Mode:** Real Market Pairs (OTC Filtered)\n\n"
            f"Bot is ready! Available Commands:\n"
            f"• `/status` - View bot operational status\n"
            f"• `/help` - View usage guide",
            parse_mode="Markdown"
        )
    except ValueError:
        await message.answer("❌ Invalid percentage! Please enter a number between `1` and `50` (e.g., `5`).")

@router.message(Command("status"))
async def cmd_status(message: types.Message):
    await message.answer(
        "📊 **Bot Status:** Operational 🟢\n"
        "• **Interface:** Telegram Base Bot Active\n"
        "• **Mode:** Ready for signals",
        parse_mode="Markdown"
    )

@router.message(Command("help"))
async def cmd_help(message: types.Message):
    await message.answer(
        "📖 **Help & Instructions**\n\n"
        "1. Use `/start` to configure your initial account balance and daily risk limit.\n"
        "2. When a signal is generated, you will receive a chart preview 3-4 minutes prior.\n"
        "3. Click the interactive inline buttons (`1 Min Expiry` / `5 Min Expiry`) to select trade expiry.",
        parse_mode="Markdown"
    )

# Callback Handler for Inline Expiry Selection
@router.callback_query(F.data.startswith("trade:"))
async def process_trade_callback(callback: types.CallbackQuery):
    _, symbol, action, expiry = callback.data.split(":")
    await callback.answer(f"Selected {expiry} expiry for {action} on {symbol}")
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(
        f"✅ **Trade Selected**\n\n"
        f"• **Asset:** `{symbol}`\n"
        f"• **Action:** `{action}`\n"
        f"• **Expiry:** `{expiry}`",
        parse_mode="Markdown"
    )

# Main Entrypoint
async def main():
    logger.info("Initializing Telegram Base Bot...")
    bot = Bot(token=config.BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    
    dp.include_router(router)

    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())
