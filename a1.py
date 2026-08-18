import os
import logging
import requests
import ccxt
import sqlite3
import time
from datetime import datetime, timedelta
from flask import Flask, request, jsonify
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton

# ---------- تنظیمات اولیه ----------
TOKEN = os.environ.get("TELEGRAM_TOKEN")
if not TOKEN:
    raise ValueError("TELEGRAM_TOKEN not set!")

BOT_USERNAME = "Crypto_forex_2026_bot"  # یوزرنیم ربات خود را وارد کنید
BASE_URL = "https://trade-i4js.onrender.com"  # آدرس سرویس Render خود را وارد کنید

bot = telebot.TeleBot(TOKEN)
app = Flask(__name__)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------- دیکشنری وضعیت جستجو ----------
waiting_for_symbol = {}

# ---------- دیتابیس ----------
conn = sqlite3.connect("trading_bot.db", check_same_thread=False)
c = conn.cursor()

c.execute("""CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    user_name TEXT,
    register_date TEXT,
    expiry_date TEXT
)""")

c.execute("""CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    plan TEXT,
    amount INTEGER,
    status TEXT,
    created_at TEXT
)""")

conn.commit()
logger.info("Database initialized.")

# ---------- توابع کمکی ----------
def get_user_expiry(user_id):
    c.execute("SELECT expiry_date FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    return row[0] if row else None

def set_user_expiry(user_id, days=7):
    expiry = (datetime.now() + timedelta(days=days)).isoformat()
    c.execute("INSERT OR REPLACE INTO users (user_id, expiry_date) VALUES (?, ?)", (user_id, expiry))
    conn.commit()

def is_user_expired(user_id):
    expiry_str = get_user_expiry(user_id)
    if not expiry_str:
        return True
    expiry = datetime.fromisoformat(expiry_str)
    return datetime.now() > expiry

def get_owner_name(user_id):
    try:
        chat = bot.get_chat(user_id)
        name = chat.first_name or ""
        if chat.last_name:
            name += " " + chat.last_name
        return name.strip() or "کاربر"
    except:
        return "کاربر"

# ---------- توابع دریافت قیمت (با خطاگیری و fallback) ----------
def get_crypto_price(symbol="BTC/USDT"):
    """دریافت قیمت از بایننس، در صورت خطا از CoinGecko"""
    try:
        exchange = ccxt.binance({
            'enableRateLimit': True,
            'timeout': 10000,
        })
        ticker = exchange.fetch_ticker(symbol)
        if ticker and ticker.get('last') is not None:
            return {
                "price": ticker["last"],
                "change": ticker.get("percentage", 0),
                "high": ticker.get("high", 0),
                "low": ticker.get("low", 0)
            }
    except Exception as e:
        logger.warning(f"Binance error for {symbol}: {e}")

    # Fallback به CoinGecko
    try:
        coin_id = symbol.split('/')[0].lower()
        url = f"https://api.coingecko.com/api/v3/simple/price?ids={coin_id}&vs_currencies=usd&include_24hr_change=true"
        resp = requests.get(url, timeout=10)
        data = resp.json()
        if coin_id in data and data[coin_id].get('usd') is not None:
            return {
                "price": data[coin_id]['usd'],
                "change": data[coin_id].get('usd_24h_change', 0),
                "high": None,
                "low": None
            }
    except Exception as e:
        logger.error(f"CoinGecko fallback error for {symbol}: {e}")

    return None

def get_forex_price(pair="EURUSD"):
    try:
        url = f"https://api.frankfurter.app/latest?from={pair[:3]}&to={pair[3:]}"
        resp = requests.get(url, timeout=10)
        data = resp.json()
        if "rates" in data and pair[3:] in data["rates"]:
            return data["rates"][pair[3:]]
    except Exception as e:
        logger.error(f"Forex error for {pair}: {e}")
    return None

def get_usd_irt():
    try:
        url = "https://api.zarinpal.com/payment/unit-converter/v1/convert"
        params = {"amount": 1, "from_currency": "USD", "to_currency": "IRT"}
        resp = requests.get(url, params=params, timeout=5)
        data = resp.json()
        if data.get("result") and "data" in data["result"]:
            return data["result"]["data"]["amount"]
    except Exception as e:
        logger.error(f"USD/IRT error: {e}")
    return None

def get_top_crypto(limit=20):
    """دریافت ۲۰ ارز برتر از CoinGecko بر اساس مارکت‌کپ"""
    try:
        url = "https://api.coingecko.com/api/v3/coins/markets"
        params = {
            "vs_currency": "usd",
            "order": "market_cap_desc",
            "per_page": limit,
            "page": 1,
            "sparkline": "false"
        }
        resp = requests.get(url, params=params, timeout=15)
        data = resp.json()
        if not isinstance(data, list):
            return None
        result = []
        for coin in data:
            result.append({
                'symbol': coin['symbol'].upper(),
                'price': coin['current_price'],
                'change': coin['price_change_percentage_24h']
            })
        return result
    except Exception as e:
        logger.error(f"Top crypto error: {e}")
        return None

def get_crypto_price_by_symbol(symbol):
    """دریافت قیمت یک ارز با نماد دلخواه (مثل ADA) با fallback"""
    try:
        # ابتدا با بایننس
        exchange = ccxt.binance({'enableRateLimit': True, 'timeout': 10000})
        if not symbol.endswith('/USDT'):
            symbol = f"{symbol.upper()}/USDT"
        ticker = exchange.fetch_ticker(symbol)
        if ticker and ticker.get('last') is not None:
            return {
                'price': ticker['last'],
                'change': ticker.get('percentage', 0),
                'high': ticker.get('high', 0),
                'low': ticker.get('low', 0)
            }
    except Exception as e:
        logger.warning(f"Binance symbol error for {symbol}: {e}")

    # Fallback به CoinGecko
    try:
        coin_id = symbol.split('/')[0].lower()
        url = f"https://api.coingecko.com/api/v3/simple/price?ids={coin_id}&vs_currencies=usd&include_24hr_change=true"
        resp = requests.get(url, timeout=10)
        data = resp.json()
        if coin_id in data and data[coin_id].get('usd') is not None:
            return {
                'price': data[coin_id]['usd'],
                'change': data[coin_id].get('usd_24h_change', 0),
                'high': None,
                'low': None
            }
    except Exception as e:
        logger.error(f"CoinGecko symbol fallback error: {e}")

    return None

# ---------- دکمه‌های منو ----------
def main_menu_keyboard():
    keyboard = ReplyKeyboardMarkup(row_width=2, resize_keyboard=True)
    btn_price = KeyboardButton("📊 قیمت لحظه‌ای")
    btn_news = KeyboardButton("📰 اخبار امروز و هفته")
    btn_signal = KeyboardButton("📈 سیگنال معاملاتی")
    btn_analyze = KeyboardButton("🔍 تحلیل ارز دلخواه")
    btn_suggest = KeyboardButton("🎯 پیشنهاد خرید")
    btn_panel = KeyboardButton("👤 پنل کاربری")
    btn_help = KeyboardButton("ℹ️ راهنما")
    keyboard.add(btn_price, btn_news, btn_signal, btn_analyze, btn_suggest, btn_panel, btn_help)
    return keyboard

def price_menu_keyboard():
    keyboard = InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        InlineKeyboardButton("₿ بیت‌کوین", callback_data="price_btc"),
        InlineKeyboardButton("⟠ اتریوم", callback_data="price_eth"),
        InlineKeyboardButton("💵 دلار/تومان", callback_data="price_usdirt"),
        InlineKeyboardButton("🇪🇺 یورو/دلار", callback_data="price_eurusd"),
        InlineKeyboardButton("🥇 طلا (XAU/USD)", callback_data="price_gold"),
        InlineKeyboardButton("🇬🇧 پوند/دلار", callback_data="price_gbpusd")
    )
    keyboard.add(
        InlineKeyboardButton("📋 ۲۰ ارز برتر", callback_data="price_top20"),
        InlineKeyboardButton("🔍 جستجوی دستی", callback_data="price_search")
    )
    keyboard.add(InlineKeyboardButton("🔙 بازگشت به منو", callback_data="back_main"))
    return keyboard

def back_to_main_keyboard():
    keyboard = InlineKeyboardMarkup()
    keyboard.add(InlineKeyboardButton("🔙 بازگشت به منو", callback_data="back_main"))
    return keyboard

# ---------- دستور /start ----------
@bot.message_handler(commands=['start'])
def start(message):
    user_id = message.from_user.id
    name = get_owner_name(user_id)

    c.execute("INSERT OR IGNORE INTO users (user_id, user_name) VALUES (?, ?)", (user_id, name))
    c.execute("UPDATE users SET user_name = ? WHERE user_id = ?", (name, user_id))
    conn.commit()

    if not get_user_expiry(user_id):
        set_user_expiry(user_id, 7)

    expiry_date = get_user_expiry(user_id)
    if expiry_date:
        expiry_dt = datetime.fromisoformat(expiry_date)
        days_left = (expiry_dt - datetime.now()).days
        if days_left < 0:
            days_left = 0
    else:
        days_left = 0

    welcome = (
        f"🎉 سلام {name}!\n"
        "به ربات تحلیلگر بازار خوش آمدی.\n\n"
        "🔹 **نسخه رایگان ۷ روزه**\n"
        f"📅 روزهای باقی‌مانده: {days_left} روز\n\n"
        "از دکمه‌های زیر استفاده کن:"
    )
    bot.send_message(user_id, welcome, reply_markup=main_menu_keyboard())

# ---------- هندلر دکمه‌های اصلی ----------
@bot.message_handler(func=lambda msg: msg.text == "📊 قیمت لحظه‌ای")
def handle_price(message):
    user_id = message.chat.id
    if is_user_expired(user_id):
        bot.send_message(user_id, "⏰ دوره آزمایشی شما به پایان رسیده.")
        return
    bot.send_message(user_id, "📊 لطفاً یک گزینه را انتخاب کنید:", reply_markup=price_menu_keyboard())

@bot.message_handler(func=lambda msg: msg.text == "📰 اخبار امروز و هفته")
def handle_news(message):
    user_id = message.chat.id
    if is_user_expired(user_id):
        bot.send_message(user_id, "⏰ دوره آزمایشی شما به پایان رسیده.")
        return
    news_text = "📰 **اخبار امروز و هفته**\n\n(به‌زودی با API خبری تکمیل می‌شود.)"
    bot.send_message(user_id, news_text, parse_mode='Markdown')

@bot.message_handler(func=lambda msg: msg.text == "📈 سیگنال معاملاتی")
def handle_signal(message):
    user_id = message.chat.id
    if is_user_expired(user_id):
        bot.send_message(user_id, "⏰ دوره آزمایشی شما به پایان رسیده.")
        return
    signal_text = "📈 **سیگنال لحظه‌ای**\n\nفعلاً سیگنالی موجود نیست.\nبه‌زودی اضافه می‌شود."
    bot.send_message(user_id, signal_text, parse_mode='Markdown')

@bot.message_handler(func=lambda msg: msg.text == "🔍 تحلیل ارز دلخواه")
def handle_analyze(message):
    user_id = message.chat.id
    if is_user_expired(user_id):
        bot.send_message(user_id, "⏰ دوره آزمایشی شما به پایان رسیده.")
        return
    bot.send_message(user_id, "🔍 لطفاً نام ارز مورد نظر را وارد کنید (مثلاً BTC یا EURUSD):")
    bot.register_next_step_handler(message, analyze_step)

def analyze_step(message):
    user_id = message.chat.id
    symbol = message.text.strip().upper()
    if not symbol:
        bot.send_message(user_id, "❌ نام ارز معتبر وارد کنید.")
        return
    # تلاش برای دریافت قیمت
    if "/" in symbol:
        info = get_crypto_price(symbol)
        if info:
            text = f"📊 **تحلیل {symbol}**\n💰 قیمت: {info['price']:,.0f} $\n📊 تغییر: {info['change']:.2f}%\n"
            if info.get('high') and info.get('low'):
                text += f"📈 بالا: {info['high']:,.0f}\n📉 پایین: {info['low']:,.0f}"
        else:
            text = "❌ ارز مورد نظر یافت نشد."
    else:
        if len(symbol) == 6:
            pair = symbol
            price = get_forex_price(pair)
            if price:
                text = f"📊 **تحلیل {pair[:3]}/{pair[3:]}**\n💰 قیمت: {price:.4f}"
            else:
                text = "❌ جفت‌ارز یافت نشد."
        else:
            info = get_crypto_price_by_symbol(symbol)
            if info:
                text = f"📊 **تحلیل {symbol.upper()}**\n💰 قیمت: {info['price']:,.2f} $\n📊 تغییر: {info['change']:.2f}%\n"
                if info.get('high') and info.get('low'):
                    text += f"📈 بالا: {info['high']:,.2f}\n📉 پایین: {info['low']:,.2f}"
            else:
                text = "❌ ارز یا جفت‌ارز یافت نشد."
    bot.send_message(user_id, text)

@bot.message_handler(func=lambda msg: msg.text == "🎯 پیشنهاد خرید")
def handle_suggest(message):
    user_id = message.chat.id
    if is_user_expired(user_id):
        bot.send_message(user_id, "⏰ دوره آزمایشی شما به پایان رسیده.")
        return
    suggest_text = "🎯 **پیشنهاد خرید**\n\nبر اساس تحلیل‌های فعلی، ارزهای زیر پتانسیل رشد دارند:\n• بیت‌کوین (BTC)\n• اتریوم (ETH)\n• طلا (XAU)"
    bot.send_message(user_id, suggest_text, parse_mode='Markdown')

@bot.message_handler(func=lambda msg: msg.text == "👤 پنل کاربری")
def handle_panel(message):
    user_id = message.chat.id
    expiry_str = get_user_expiry(user_id)
    if expiry_str:
        expiry_dt = datetime.fromisoformat(expiry_str)
        days_left = (expiry_dt - datetime.now()).days
        if days_left < 0:
            days_left = 0
    else:
        days_left = 0
    name = get_owner_name(user_id)
    text = (
        f"👤 **پنل کاربری**\n\n"
        f"نام: {name}\n"
        f"شناسه: {user_id}\n"
        f"📅 روزهای باقی‌مانده: {days_left}\n"
        f"نوع اشتراک: رایگان (۷ روزه)"
    )
    bot.send_message(user_id, text, parse_mode='Markdown')

@bot.message_handler(func=lambda msg: msg.text == "ℹ️ راهنما")
def handle_help(message):
    help_text = (
        "ℹ️ **راهنما**\n\n"
        "📊 قیمت لحظه‌ای: دریافت قیمت کریپتو، فارکس و طلا\n"
        "📰 اخبار: اخبار روز و هفته (به‌زودی)\n"
        "📈 سیگنال: سیگنال‌های خرید و فروش (به‌زودی)\n"
        "🔍 تحلیل: تحلیل تکنیکال و بنیادی ارز دلخواه\n"
        "🎯 پیشنهاد خرید: ارزهای مناسب برای سرمایه‌گذاری\n"
        "👤 پنل کاربری: مشاهده وضعیت حساب\n\n"
        "پشتیبانی: @YourSupport"
    )
    bot.send_message(message.chat.id, help_text, parse_mode='Markdown')

# ---------- هندلرهای کالبک قیمت ----------
@bot.callback_query_handler(func=lambda call: call.data.startswith("price_"))
def callback_price(call):
    user_id = call.from_user.id
    if is_user_expired(user_id):
        bot.answer_callback_query(call.id, "⏰ دوره آزمایشی شما به پایان رسیده.", show_alert=True)
        return

    data = call.data
    reply = ""

    if data == "price_btc":
        info = get_crypto_price("BTC/USDT")
        if info:
            reply = f"₿ **بیت‌کوین (BTC/USDT)**\n💰 قیمت: {info['price']:,.0f} $\n📊 تغییر ۲۴h: {info['change']:.2f}%\n"
            if info.get('high') and info.get('low'):
                reply += f"📈 بالا: {info['high']:,.0f}\n📉 پایین: {info['low']:,.0f}"
        else:
            reply = "❌ خطا در دریافت قیمت بیت‌کوین."

    elif data == "price_eth":
        info = get_crypto_price("ETH/USDT")
        if info:
            reply = f"⟠ **اتریوم (ETH/USDT)**\n💰 قیمت: {info['price']:,.2f} $\n📊 تغییر ۲۴h: {info['change']:.2f}%\n"
            if info.get('high') and info.get('low'):
                reply += f"📈 بالا: {info['high']:,.2f}\n📉 پایین: {info['low']:,.2f}"
        else:
            reply = "❌ خطا در دریافت قیمت اتریوم."

    elif data == "price_usdirt":
        price = get_usd_irt()
        if price:
            reply = f"💵 **دلار/تومان (USD/IRT)**\n💰 قیمت: {price:,.0f} تومان"
        else:
            reply = "❌ خطا در دریافت قیمت دلار/تومان."

    elif data == "price_eurusd":
        price = get_forex_price("EURUSD")
        if price:
            reply = f"🇪🇺 **یورو/دلار (EUR/USD)**\n💰 قیمت: {price:.4f}"
        else:
            reply = "❌ خطا در دریافت قیمت یورو/دلار."

    elif data == "price_gbpusd":
        price = get_forex_price("GBPUSD")
        if price:
            reply = f"🇬🇧 **پوند/دلار (GBP/USD)**\n💰 قیمت: {price:.4f}"
        else:
            reply = "❌ خطا در دریافت قیمت پوند/دلار."

    elif data == "price_gold":
        info = get_crypto_price("XAU/USD")
        if info:
            reply = f"🥇 **طلا (XAU/USD)**\n💰 قیمت: {info['price']:,.2f} $\n📊 تغییر ۲۴h: {info['change']:.2f}%"
        else:
            reply = "❌ خطا در دریافت قیمت طلا."

    elif data == "price_top20":
        top_list = get_top_crypto(20)
        if top_list:
            text = "📋 **۲۰ ارز برتر کریپتو (بر اساس مارکت‌کپ):**\n\n"
            for idx, item in enumerate(top_list, 1):
                change_emoji = "🟢" if item['change'] and item['change'] >= 0 else "🔴"
                text += f"{idx}. {item['symbol']}: ${item['price']:,.2f} {change_emoji} {item['change']:.2f}%\n"
            reply = text
        else:
            reply = "❌ خطا در دریافت لیست ارزهای برتر."

        bot.edit_message_text(reply, call.message.chat.id, call.message.message_id, parse_mode='Markdown', reply_markup=back_to_main_keyboard())
        bot.answer_callback_query(call.id)
        return

    elif data == "price_search":
        bot.edit_message_text(
            "🔍 **جستجوی قیمت ارز**\n\nلطفاً نام نماد ارز مورد نظر را تایپ کنید (مثلاً `ADA` یا `BTC`).",
            call.message.chat.id,
            call.message.message_id,
            parse_mode='Markdown'
        )
        waiting_for_symbol[user_id] = True
        bot.answer_callback_query(call.id)
        return

    bot.edit_message_text(reply, call.message.chat.id, call.message.message_id, parse_mode='Markdown', reply_markup=back_to_main_keyboard())
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "back_main")
def callback_back_main(call):
    bot.answer_callback_query(call.id)
    bot.edit_message_text("به منوی اصلی برگشتید.", call.message.chat.id, call.message.message_id, reply_markup=None)
    bot.send_message(call.message.chat.id, "🔽 از دکمه‌های زیر استفاده کنید:", reply_markup=main_menu_keyboard())

# ---------- هندلر پیام‌های متنی (جستجوی دستی) ----------
@bot.message_handler(func=lambda msg: True)
def handle_text_messages(message):
    user_id = message.chat.id
    text = message.text.strip()

    if waiting_for_symbol.get(user_id, False):
        waiting_for_symbol[user_id] = False
        info = get_crypto_price_by_symbol(text)
        if info:
            reply = f"💰 **قیمت {text.upper()}**\n"
            reply += f"قیمت: {info['price']:,.2f} $\n"
            reply += f"تغییر ۲۴h: {info['change']:.2f}%\n"
            if info.get('high') and info.get('low'):
                reply += f"بالاترین: {info['high']:,.2f}\nپایین‌ترین: {info['low']:,.2f}"
            bot.send_message(user_id, reply, parse_mode='Markdown')
        else:
            bot.send_message(user_id, f"❌ نماد `{text}` یافت نشد. لطفاً از نمادهای معتبر استفاده کنید.", parse_mode='Markdown')
        bot.send_message(user_id, "🔙 برای ادامه جستجو یا بازگشت، از دکمه‌های منوی قیمت استفاده کنید:", reply_markup=price_menu_keyboard())
        return

# ---------- مسیرهای Webhook ----------
@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        json_str = request.get_data().decode('UTF-8')
        update = telebot.types.Update.de_json(json_str)
        bot.process_new_updates([update])
        return jsonify({"status": "ok"}), 200
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return jsonify({"status": "error"}), 500

@app.route('/')
def index():
    return "ربات تحلیلگر بازار فعال است", 200

def set_webhook():
    bot.remove_webhook()
    time.sleep(1)
    webhook_url = f"{BASE_URL}/webhook"
    if bot.set_webhook(url=webhook_url):
        logger.info(f"Webhook set to {webhook_url}")
    else:
        logger.error("Webhook setting failed")

# ---------- اجرا ----------
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    set_webhook()
    app.run(host='0.0.0.0', port=port)
