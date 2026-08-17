import os
import logging
import requests
import ccxt
import asyncio
from datetime import datetime, timedelta
from flask import Flask, request
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

# ===== تنظیمات اولیه =====
TOKEN = os.environ.get("TELEGRAM_TOKEN")
if not TOKEN:
    raise ValueError("توکن ربات پیدا نشد! متغیر TELEGRAM_TOKEN را تنظیم کن.")

logging.basicConfig(level=logging.INFO)

# ===== توابع دریافت قیمت =====
def get_crypto_price(symbol="BTC/USDT"):
    try:
        exchange = ccxt.binance()
        ticker = exchange.fetch_ticker(symbol)
        return {
            "price": ticker["last"],
            "change": ticker["percentage"],
            "high": ticker["high"],
            "low": ticker["low"]
        }
    except:
        return None

def get_forex_price(pair="EURUSD"):
    try:
        url = f"https://api.frankfurter.app/latest?from={pair[:3]}&to={pair[3:]}"
        response = requests.get(url)
        data = response.json()
        if "rates" in data and pair[3:] in data["rates"]:
            return data["rates"][pair[3:]]
        return None
    except:
        return None

def get_usd_irt():
    try:
        url = "https://api.zarinpal.com/payment/unit-converter/v1/convert"
        params = {"amount": 1, "from_currency": "USD", "to_currency": "IRT"}
        response = requests.get(url, params=params, timeout=5)
        data = response.json()
        if data.get("result") and "data" in data["result"]:
            return data["result"]["data"]["amount"]
        return None
    except:
        return None

# ===== دکمه‌ها =====
def main_menu():
    keyboard = [
        [InlineKeyboardButton("📊 قیمت لحظه‌ای", callback_data="price")],
        [InlineKeyboardButton("📰 اخبار امروز و هفته", callback_data="news")],
        [InlineKeyboardButton("📈 سیگنال معاملاتی", callback_data="signal")],
        [InlineKeyboardButton("🔍 تحلیل ارز دلخواه", callback_data="analyze")],
        [InlineKeyboardButton("🎯 پیشنهاد خرید", callback_data="suggest")],
        [InlineKeyboardButton("👤 پنل کاربری", callback_data="panel")],
        [InlineKeyboardButton("ℹ️ راهنما", callback_data="help")],
    ]
    return InlineKeyboardMarkup(keyboard)

def price_menu():
    keyboard = [
        [InlineKeyboardButton("₿ بیت‌کوین", callback_data="price_btc")],
        [InlineKeyboardButton("⟠ اتریوم", callback_data="price_eth")],
        [InlineKeyboardButton("💵 دلار/تومان", callback_data="price_usdirt")],
        [InlineKeyboardButton("🇪🇺 یورو/دلار", callback_data="price_eurusd")],
        [InlineKeyboardButton("🥇 طلا (XAU/USD)", callback_data="price_gold")],
        [InlineKeyboardButton("🇬🇧 پوند/دلار", callback_data="price_gbpusd")],
        [InlineKeyboardButton("🔙 بازگشت", callback_data="back_main")],
    ]
    return InlineKeyboardMarkup(keyboard)

def back_button():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت", callback_data="back_main")]])

# ===== دستور /start =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    expiry_date = (datetime.now() + timedelta(days=7)).strftime('%Y/%m/%d')
    await update.message.reply_text(
        f"🎉 سلام {user.first_name}!\n"
        "به ربات تحلیلگر بازار خوش آمدی.\n\n"
        "🔹 این ربات به مدت ۷ روز کاملاً رایگان است.\n"
        "🔹 امکانات:\n"
        "   - قیمت لحظه‌ای کریپتو و فارکس\n"
        "   - اخبار روز و هفته\n"
        "   - سیگنال‌های معاملاتی\n"
        "   - تحلیل ارز دلخواه\n"
        "   - پیشنهاد ارزهای مناسب خرید\n\n"
        f"📅 تاریخ انقضا: {expiry_date}\n"
        "از دکمه‌های زیر استفاده کن:",
        reply_markup=main_menu()
    )

# ===== مدیریت دکمه‌ها =====
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "price":
        await query.edit_message_text(
            "📊 لطفاً یک ارز را انتخاب کن:",
            reply_markup=price_menu()
        )

    elif data == "price_btc":
        info = get_crypto_price("BTC/USDT")
        if info:
            msg = f"₿ **بیت‌کوین (BTC/USDT)**\n"
            msg += f"💰 قیمت: {info['price']:,.0f} دلار\n"
            msg += f"📊 تغییر ۲۴h: {info['change']:.2f}%\n"
            msg += f"📈 بالاترین: {info['high']:,.0f}\n"
            msg += f"📉 پایین‌ترین: {info['low']:,.0f}\n"
            msg += f"🕒 {datetime.now().strftime('%H:%M:%S')}"
        else:
            msg = "❌ خطا در دریافت قیمت. لحظاتی دیگر تلاش کن."
        await query.edit_message_text(msg, reply_markup=back_button())

    elif data == "price_eth":
        info = get_crypto_price("ETH/USDT")
        if info:
            msg = f"⟠ **اتریوم (ETH/USDT)**\n"
            msg += f"💰 قیمت: {info['price']:,.0f} دلار\n"
            msg += f"📊 تغییر ۲۴h: {info['change']:.2f}%\n"
            msg += f"📈 بالاترین: {info['high']:,.0f}\n"
            msg += f"📉 پایین‌ترین: {info['low']:,.0f}\n"
            msg += f"🕒 {datetime.now().strftime('%H:%M:%S')}"
        else:
            msg = "❌ خطا در دریافت قیمت."
        await query.edit_message_text(msg, reply_markup=back_button())

    elif data == "price_usdirt":
        price = get_usd_irt()
        if price:
            msg = f"💵 **دلار/تومان (USD/IRT)**\n"
            msg += f"💰 قیمت: {price:,.0f} تومان\n"
            msg += f"🕒 {datetime.now().strftime('%H:%M:%S')}"
        else:
            msg = "❌ خطا در دریافت قیمت."
        await query.edit_message_text(msg, reply_markup=back_button())

    elif data == "price_eurusd":
        price = get_forex_price("EURUSD")
        if price:
            msg = f"🇪🇺 **یورو/دلار (EUR/USD)**\n"
            msg += f"💰 قیمت: {price:.4f}\n"
            msg += f"🕒 {datetime.now().strftime('%H:%M:%S')}"
        else:
            msg = "❌ خطا در دریافت قیمت."
        await query.edit_message_text(msg, reply_markup=back_button())

    elif data == "price_gbpusd":
        price = get_forex_price("GBPUSD")
        if price:
            msg = f"🇬🇧 **پوند/دلار (GBP/USD)**\n"
            msg += f"💰 قیمت: {price:.4f}\n"
            msg += f"🕒 {datetime.now().strftime('%H:%M:%S')}"
        else:
            msg = "❌ خطا در دریافت قیمت."
        await query.edit_message_text(msg, reply_markup=back_button())

    elif data == "price_gold":
        info = get_crypto_price("XAU/USD")
        if info:
            msg = f"🥇 **طلا (XAU/USD)**\n"
            msg += f"💰 قیمت: {info['price']:,.2f} دلار\n"
            msg += f"📊 تغییر ۲۴h: {info['change']:.2f}%\n"
            msg += f"🕒 {datetime.now().strftime('%H:%M:%S')}"
        else:
            msg = "❌ خطا در دریافت قیمت."
        await query.edit_message_text(msg, reply_markup=back_button())

    elif data in ["news", "signal", "analyze", "suggest", "panel"]:
        await query.edit_message_text(
            f"⏳ این بخش به‌زودی اضافه می‌شود.\n"
            "همین حالا می‌توانی از بخش قیمت‌های لحظه‌ای استفاده کنی.",
            reply_markup=back_button()
        )

    elif data == "help":
        await query.edit_message_text(
            "ℹ️ **راهنما**\n\n"
            "📊 قیمت لحظه‌ای: قیمت لحظه‌ای کریپتو، فارکس و طلا\n"
            "📰 اخبار: اخبار امروز و هفته (به‌زودی)\n"
            "📈 سیگنال: سیگنال‌های خرید و فروش (به‌زودی)\n"
            "🔍 تحلیل: تحلیل تکنیکال و بنیادی (به‌زودی)\n"
            "🎯 پیشنهاد خرید: ارزهای مناسب خرید (به‌زودی)\n\n"
            "پشتیبانی: @YourSupport",
            reply_markup=back_button()
        )

    elif data == "back_main":
        await query.edit_message_text(
            "به منوی اصلی برگشتی:",
            reply_markup=main_menu()
        )

# ===== ایجاد Application به صورت سراسری =====
application = Application.builder().token(TOKEN).build()
application.add_handler(CommandHandler("start", start))
application.add_handler(CallbackQueryHandler(button_handler))

# ===== Flask =====
flask_app = Flask(__name__)

@flask_app.route('/')
def index():
    return "✅ ربات با Webhook فعال است!", 200

@flask_app.route('/webhook', methods=['POST'])
def webhook():
    """دریافت درخواست از تلگرام و پردازش آن (همزمان)"""
    json_data = request.get_json(force=True)
    if not json_data:
        return "درخواست نامعتبر", 400

    update = Update.de_json(json_data, application.bot)
    # پردازش به صورت همزمان با استفاده از asyncio.run()
    try:
        asyncio.run(application.process_update(update))
    except Exception as e:
        logging.error(f"خطا در پردازش: {e}")
        return "خطا", 500

    return "OK", 200

# ===== تنظیم Webhook =====
def set_webhook():
    base_url = "https://trade-i4js.onrender.com"  # ← آدرس خودت را بگذار
    webhook_url = f"{base_url}/webhook"
    url = f"https://api.telegram.org/bot{TOKEN}/setWebhook"
    response = requests.post(url, json={"url": webhook_url})
    if response.status_code == 200:
        logging.info(f"✅ Webhook تنظیم شد: {webhook_url}")
    else:
        logging.error(f"❌ خطا در تنظیم Webhook: {response.text}")

# ===== ورودی اصلی =====
if __name__ == '__main__':
    set_webhook()
    port = int(os.environ.get("PORT", 5000))
    logging.info(f"🚀 وب‌سرویس روی پورت {port} روشن شد...")
    flask_app.run(host='0.0.0.0', port=port)
