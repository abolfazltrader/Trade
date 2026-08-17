import os
import logging
import requests
import ccxt
from datetime import datetime, timedelta
from flask import Flask, request
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

TOKEN = os.environ.get("TELEGRAM_TOKEN")
if not TOKEN:
    raise ValueError("توکن ربات پیدا نشد!")

logging.basicConfig(level=logging.INFO)

# ===== توابع دریافت قیمت =====
def get_crypto_price(symbol="BTC/USDT"):
    try:
        exchange = ccxt.binance()
        ticker = exchange.fetch_ticker(symbol)
        return {"price": ticker["last"], "change": ticker["percentage"], "high": ticker["high"], "low": ticker["low"]}
    except:
        return None

def get_forex_price(pair="EURUSD"):
    try:
        url = f"https://api.frankfurter.app/latest?from={pair[:3]}&to={pair[3:]}"
        data = requests.get(url).json()
        if "rates" in data and pair[3:] in data["rates"]:
            return data["rates"][pair[3:]]
        return None
    except:
        return None

def get_usd_irt():
    try:
        url = "https://api.zarinpal.com/payment/unit-converter/v1/convert"
        params = {"amount": 1, "from_currency": "USD", "to_currency": "IRT"}
        data = requests.get(url, params=params, timeout=5).json()
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
    expiry = (datetime.now() + timedelta(days=7)).strftime('%Y/%m/%d')
    await update.message.reply_text(
        f"🎉 سلام {user.first_name}!\nبه ربات تحلیلگر بازار خوش آمدی.\n\n"
        f"🔹 این ربات به مدت ۷ روز کاملاً رایگان است.\n"
        f"📅 تاریخ انقضا: {expiry}\n"
        "از دکمه‌های زیر استفاده کن:",
        reply_markup=main_menu()
    )

# ===== مدیریت دکمه‌ها =====
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "price":
        await query.edit_message_text("📊 لطفاً یک ارز را انتخاب کن:", reply_markup=price_menu())
    elif data == "price_btc":
        info = get_crypto_price("BTC/USDT")
        msg = f"₿ بیت‌کوین: {info['price']:,.0f}$" if info else "❌ خطا"
        await query.edit_message_text(msg, reply_markup=back_button())
    elif data == "price_eth":
        info = get_crypto_price("ETH/USDT")
        msg = f"⟠ اتریوم: {info['price']:,.0f}$" if info else "❌ خطا"
        await query.edit_message_text(msg, reply_markup=back_button())
    elif data == "price_usdirt":
        price = get_usd_irt()
        msg = f"💵 دلار/تومان: {price:,.0f}" if price else "❌ خطا"
        await query.edit_message_text(msg, reply_markup=back_button())
    elif data == "price_eurusd":
        price = get_forex_price("EURUSD")
        msg = f"🇪🇺 یورو/دلار: {price:.4f}" if price else "❌ خطا"
        await query.edit_message_text(msg, reply_markup=back_button())
    elif data == "price_gbpusd":
        price = get_forex_price("GBPUSD")
        msg = f"🇬🇧 پوند/دلار: {price:.4f}" if price else "❌ خطا"
        await query.edit_message_text(msg, reply_markup=back_button())
    elif data == "price_gold":
        info = get_crypto_price("XAU/USD")
        msg = f"🥇 طلا: {info['price']:,.2f}$" if info else "❌ خطا"
        await query.edit_message_text(msg, reply_markup=back_button())
    elif data == "back_main":
        await query.edit_message_text("به منوی اصلی برگشتی:", reply_markup=main_menu())
    else:
        await query.edit_message_text("⏳ به‌زودی اضافه می‌شود.", reply_markup=back_button())

# ===== ایجاد Application =====
application = Application.builder().token(TOKEN).build()
application.add_handler(CommandHandler("start", start))
application.add_handler(CallbackQueryHandler(button_handler))

# ===== Flask با پشتیبانی از async =====
flask_app = Flask(__name__)

@flask_app.route('/')
def index():
    return "✅ ربات با Webhook فعال است!", 200

@flask_app.route('/webhook', methods=['POST'])
async def webhook():
    json_data = request.get_json(force=True)
    if not json_data:
        return "درخواست نامعتبر", 400
    update = Update.de_json(json_data, application.bot)
    await application.process_update(update)
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

if __name__ == '__main__':
    set_webhook()
    port = int(os.environ.get("PORT", 5000))
    logging.info(f"🚀 وب‌سرویس روی پورت {port} روشن شد...")
    flask_app.run(host='0.0.0.0', port=port)
