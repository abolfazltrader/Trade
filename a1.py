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
    raise ValueError("TELEGRAM_TOKEN not set!")

logging.basicConfig(level=logging.INFO)

# ===== توابع دریافت قیمت (بدون تغییر) =====
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

# ===== دکمه‌ها (بدون تغییر) =====
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

# ===== مدیریت دکمه‌ها (خلاصه شده) =====
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "price":
        await query.edit_message_text("📊 لطفاً یک ارز را انتخاب کن:", reply_markup=price_menu())
    elif data.startswith("price_"):
        symbol_map = {
            "price_btc": ("BTC/USDT", "₿ بیت‌کوین"),
            "price_eth": ("ETH/USDT", "⟠ اتریوم"),
            "price_gold": ("XAU/USD", "🥇 طلا"),
        }
        forex_map = {
            "price_eurusd": ("EURUSD", "🇪🇺 یورو/دلار"),
            "price_gbpusd": ("GBPUSD", "🇬🇧 پوند/دلار"),
        }
        msg = "❌ خطا در دریافت قیمت."
        if data == "price_usdirt":
            price = get_usd_irt()
            if price:
                msg = f"💵 **دلار/تومان (USD/IRT)**\n💰 قیمت: {price:,.0f} تومان\n🕒 {datetime.now().strftime('%H:%M:%S')}"
        elif data in symbol_map:
            symbol, name = symbol_map[data]
            info = get_crypto_price(symbol)
            if info:
                msg = f"{name} **({symbol})**\n💰 قیمت: {info['price']:,.0f} $\n📊 تغییر ۲۴h: {info['change']:.2f}%\n🕒 {datetime.now().strftime('%H:%M:%S')}"
        elif data in forex_map:
            pair, name = forex_map[data]
            price = get_forex_price(pair)
            if price:
                msg = f"{name} **({pair[:3]}/{pair[3:]})**\n💰 قیمت: {price:.4f}\n🕒 {datetime.now().strftime('%H:%M:%S')}"
        await query.edit_message_text(msg, reply_markup=back_button())
    elif data == "back_main":
        await query.edit_message_text("به منوی اصلی برگشتی:", reply_markup=main_menu())
    else:
        await query.edit_message_text("⏳ این بخش به‌زودی اضافه می‌شود.", reply_markup=back_button())

# ===== ایجاد Application (یک نمونه ثابت) =====
application = Application.builder().token(TOKEN).build()
application.add_handler(CommandHandler("start", start))
application.add_handler(CallbackQueryHandler(button_handler))

# ===== Flask =====
flask_app = Flask(__name__)

@flask_app.route('/')
def index():
    return "✅ ربات فعال است!", 200

@flask_app.route('/webhook', methods=['POST'])
def webhook():
    """دریافت درخواست از تلگرام و پردازش آن به صورت غیرهمزمان"""
    json_data = request.get_json(force=True)
    if not json_data:
        return "Invalid", 400
    update = Update.de_json(json_data, application.bot)
    # اجرای پردازش در یک تسک غیرهمزمان (تا درخواست Flask مسدود نشود)
    asyncio.create_task(application.process_update(update))
    return "OK", 200

def set_webhook():
    """تنظیم Webhook با آدرس واقعی سرویس"""
    base_url = "https://trade-i4js.onrender.com"  # ← آدرس خودت را اینجا بگذار
    webhook_url = f"{base_url}/webhook"
    url = f"https://api.telegram.org/bot{TOKEN}/setWebhook"
    resp = requests.post(url, json={"url": webhook_url})
    if resp.status_code == 200:
        logging.info(f"✅ Webhook set: {resp.json()}")
    else:
        logging.error(f"❌ Webhook error: {resp.text}")

if __name__ == '__main__':
    # تنظیم Webhook در اولین اجرا
    set_webhook()
    port = int(os.environ.get("PORT", 5000))
    logging.info(f"🚀 Flask server running on port {port}")
    flask_app.run(host='0.0.0.0', port=port)
