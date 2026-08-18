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
from concurrent.futures import ThreadPoolExecutor, as_completed

# ---------- تنظیمات اولیه ----------
TOKEN = os.environ.get("TELEGRAM_TOKEN")
if not TOKEN:
    raise ValueError("TELEGRAM_TOKEN not set!")

BOT_USERNAME = "Crypto_forex_2026_bot"
BASE_URL = "https://trade-i4js.onrender.com"

ADMIN_IDS = [6542890217]  # آیدی مدیر

bot = telebot.TeleBot(TOKEN)
app = Flask(__name__)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

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
    if user_id in ADMIN_IDS:
        return False
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

# ========== سیستم قدرتمند دریافت قیمت (چندمنبعی با کش) ==========
class PriceFetcher:
    def __init__(self):
        self.cache = {}
        self.cache_time = 15  # کش به مدت ۱۵ ثانیه
        self.executor = ThreadPoolExecutor(max_workers=4)
        self.binance = ccxt.binance({'enableRateLimit': True, 'timeout': 6000})
        self.kraken = ccxt.kraken({'enableRateLimit': True, 'timeout': 6000})
        self.okx = ccxt.okx({'enableRateLimit': True, 'timeout': 6000})
        self.coingecko_session = requests.Session()

    def _get_cached(self, key):
        if key in self.cache:
            data, timestamp = self.cache[key]
            if (datetime.now() - timestamp).seconds < self.cache_time:
                return data
        return None

    def _set_cache(self, key, data):
        self.cache[key] = (data, datetime.now())

    def _fetch_binance(self, symbol):
        try:
            ticker = self.binance.fetch_ticker(symbol)
            if ticker and ticker.get('last') is not None:
                return {
                    'price': ticker['last'],
                    'change': ticker.get('percentage', 0),
                    'high': ticker.get('high', 0),
                    'low': ticker.get('low', 0),
                    'source': 'Binance'
                }
        except Exception:
            pass
        return None

    def _fetch_kraken(self, symbol):
        try:
            if symbol == "BTC/USDT":
                kraken_symbol = "XBT/USD"
            else:
                kraken_symbol = symbol.replace('/USDT', '/USD')
            ticker = self.kraken.fetch_ticker(kraken_symbol)
            if ticker and ticker.get('last') is not None:
                return {
                    'price': ticker['last'],
                    'change': ticker.get('percentage', 0),
                    'high': ticker.get('high', 0),
                    'low': ticker.get('low', 0),
                    'source': 'Kraken'
                }
        except Exception:
            pass
        return None

    def _fetch_okx(self, symbol):
        try:
            ticker = self.okx.fetch_ticker(symbol)
            if ticker and ticker.get('last') is not None:
                return {
                    'price': ticker['last'],
                    'change': ticker.get('percentage', 0),
                    'high': ticker.get('high', 0),
                    'low': ticker.get('low', 0),
                    'source': 'OKX'
                }
        except Exception:
            pass
        return None

    def _fetch_coingecko(self, symbol):
        try:
            coin_id = symbol.split('/')[0].lower()
            url = f"https://api.coingecko.com/api/v3/simple/price?ids={coin_id}&vs_currencies=usd&include_24hr_change=true"
            resp = self.coingecko_session.get(url, timeout=4)
            data = resp.json()
            if coin_id in data and data[coin_id].get('usd') is not None:
                return {
                    'price': data[coin_id]['usd'],
                    'change': data[coin_id].get('usd_24h_change', 0),
                    'high': None,
                    'low': None,
                    'source': 'CoinGecko'
                }
        except Exception:
            pass
        return None

    def get_price(self, symbol, force_refresh=False):
        cache_key = f"price_{symbol}"
        if not force_refresh:
            cached = self._get_cached(cache_key)
            if cached:
                return cached

        sources = [
            self._fetch_binance,
            self._fetch_kraken,
            self._fetch_okx,
            self._fetch_coingecko
        ]

        futures = [self.executor.submit(src, symbol) for src in sources]
        start_time = time.time()
        for future in as_completed(futures, timeout=3):
            result = future.result()
            if result and result.get('price') is not None:
                self._set_cache(cache_key, result)
                return result
            if time.time() - start_time > 3:
                break

        return None

fetcher = PriceFetcher()

# ---------- توابع دریافت قیمت ----------
def get_crypto_price(symbol="BTC/USDT"):
    return fetcher.get_price(symbol)

def get_forex_price(pair="EURUSD"):
    try:
        url = f"https://api.frankfurter.app/latest?from={pair[:3]}&to={pair[3:]}"
        resp = requests.get(url, timeout=4)
        data = resp.json()
        if "rates" in data and pair[3:] in data["rates"]:
            return data["rates"][pair[3:]]
    except Exception:
        pass
    return None

def get_usd_irt():
    cache_key = "usd_irt"
    cached = fetcher._get_cached(cache_key)
    if cached:
        return cached
    try:
        url = "https://api.zarinpal.com/payment/unit-converter/v1/convert"
        params = {"amount": 1, "from_currency": "USD", "to_currency": "IRT"}
        resp = requests.get(url, params=params, timeout=4)
        data = resp.json()
        if data.get("result") and "data" in data["result"]:
            price = data["result"]["data"]["amount"]
            fetcher._set_cache(cache_key, price)
            return price
    except Exception:
        pass
    return None

def get_top_crypto(limit=20):
    cache_key = "top20"
    cached = fetcher._get_cached(cache_key)
    if cached:
        return cached
    try:
        url = "https://api.coingecko.com/api/v3/coins/markets"
        params = {"vs_currency": "usd", "order": "market_cap_desc", "per_page": limit, "page": 1, "sparkline": "false"}
        resp = requests.get(url, params=params, timeout=6)
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
        fetcher._set_cache(cache_key, result)
        return result
    except Exception:
        return None

def get_crypto_price_by_symbol(symbol):
    try:
        if not symbol.endswith('/USDT'):
            symbol = f"{symbol.upper()}/USDT"
        return fetcher.get_price(symbol)
    except Exception:
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
        InlineKeyboardButton("🔍 جستجوی دستی و لیست برترین‌ها", callback_data="price_search")
    )
    keyboard.add(InlineKeyboardButton("🔙 بازگشت به منو", callback_data="back_main"))
    return keyboard

def back_to_main_keyboard():
    keyboard = InlineKeyboardMarkup()
    keyboard.add(InlineKeyboardButton("🔙 بازگشت به منو", callback_data="back_main"))
    return keyboard

# ========== منوی انتخاب از ۲۰ ارز برتر ==========
def top20_menu_keyboard():
    top_list = get_top_crypto(20)
    if not top_list:
        keyboard = InlineKeyboardMarkup()
        keyboard.add(InlineKeyboardButton("❌ خطا در دریافت لیست", callback_data="back_main"))
        return keyboard

    keyboard = InlineKeyboardMarkup(row_width=3)
    for item in top_list:
        symbol = item['symbol']
        keyboard.add(InlineKeyboardButton(symbol, callback_data=f"top_select_{symbol}"))
    keyboard.add(InlineKeyboardButton("🔙 بازگشت به منوی قیمت", callback_data="back_to_price"))
    return keyboard

# ---------- دستور /start ----------
@bot.message_handler(commands=['start'])
def start(message):
    user_id = message.from_user.id
    name = get_owner_name(user_id)

    c.execute("INSERT OR IGNORE INTO users (user_id, user_name) VALUES (?, ?)", (user_id, name))
    c.execute("UPDATE users SET user_name = ? WHERE user_id = ?", (name, user_id))
    conn.commit()

    if not get_user_expiry(user_id) and user_id not in ADMIN_IDS:
        set_user_expiry(user_id, 7)

    expiry_date = get_user_expiry(user_id)
    if expiry_date and user_id not in ADMIN_IDS:
        expiry_dt = datetime.fromisoformat(expiry_date)
        days_left = (expiry_dt - datetime.now()).days
        if days_left < 0:
            days_left = 0
    else:
        days_left = "نامحدود (مدیر)" if user_id in ADMIN_IDS else 0

    if user_id in ADMIN_IDS:
        welcome = f"🎉 سلام {name} (مدیر)!\nبه ربات تحلیلگر بازار خوش آمدی.\n\n🔹 **دسترسی دائمی و رایگان**\n📅 وضعیت: نامحدود\n\nاز دکمه‌های زیر استفاده کن:"
    else:
        welcome = f"🎉 سلام {name}!\nبه ربات تحلیلگر بازار خوش آمدی.\n\n🔹 **نسخه رایگان ۷ روزه**\n📅 روزهای باقی‌مانده: {days_left} روز\n\nاز دکمه‌های زیر استفاده کن:"
    bot.send_message(user_id, welcome, reply_markup=main_menu_keyboard())

# ---------- پنل مدیریت ----------
@bot.message_handler(commands=['admin'])
def admin_panel(message):
    user_id = message.from_user.id
    if user_id not in ADMIN_IDS:
        bot.reply_to(message, "❌ شما دسترسی به این بخش ندارید!")
        return
    text = "🔐 **پنل مدیریت**\n\nسلام مدیر گرامی!\nشما دسترسی دائمی به ربات دارید.\n\n• برای مشاهده آمار، از دستور /stats استفاده کنید.\n• برای ارسال پیام به همه کاربران، از /broadcast استفاده کنید."
    bot.send_message(user_id, text, parse_mode='Markdown')

@bot.message_handler(commands=['stats'])
def stats_command(message):
    user_id = message.from_user.id
    if user_id not in ADMIN_IDS:
        return
    c.execute("SELECT COUNT(*) FROM users")
    total_users = c.fetchone()[0]
    bot.send_message(user_id, f"📊 **آمار ربات**\n\n👤 تعداد کل کاربران: {total_users}", parse_mode='Markdown')

# ---------- بقیه هندلرها ----------
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
    bot.send_message(user_id, "📰 **اخبار امروز و هفته**\n\n(به‌زودی با API خبری تکمیل می‌شود.)", parse_mode='Markdown')

@bot.message_handler(func=lambda msg: msg.text == "📈 سیگنال معاملاتی")
def handle_signal(message):
    user_id = message.chat.id
    if is_user_expired(user_id):
        bot.send_message(user_id, "⏰ دوره آزمایشی شما به پایان رسیده.")
        return
    bot.send_message(user_id, "📈 **سیگنال لحظه‌ای**\n\nفعلاً سیگنالی موجود نیست.\nبه‌زودی اضافه می‌شود.", parse_mode='Markdown')

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
    if "/" in symbol:
        info = get_crypto_price(symbol)
        if info:
            text = f"📊 **تحلیل {symbol}**\n💰 قیمت: {info['price']:,.0f} $\n📊 تغییر: {info['change']:.2f}%\n"
            if info.get('high') and info.get('low'):
                text += f"📈 بالا: {info['high']:,.0f}\n📉 پایین: {info['low']:,.0f}"
            text += f"\n📌 منبع: {info.get('source', 'نامشخص')}"
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
                text += f"\n📌 منبع: {info.get('source', 'نامشخص')}"
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
    if user_id in ADMIN_IDS:
        text = f"👤 **پنل کاربری (مدیر)**\n\nنام: {get_owner_name(user_id)}\nشناسه: {user_id}\n📅 وضعیت: نامحدود\nنوع اشتراک: دائمی"
    else:
        expiry_str = get_user_expiry(user_id)
        if expiry_str:
            expiry_dt = datetime.fromisoformat(expiry_str)
            days_left = (expiry_dt - datetime.now()).days
            if days_left < 0:
                days_left = 0
        else:
            days_left = 0
        name = get_owner_name(user_id)
        text = f"👤 **پنل کاربری**\n\nنام: {name}\nشناسه: {user_id}\n📅 روزهای باقی‌مانده: {days_left}\nنوع اشتراک: رایگان (۷ روزه)"
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
                reply += f"📈 بالا: {info['high']:,.0f}\n📉 پایین: {info['low']:,.0f}\n"
            reply += f"📌 منبع: {info.get('source', 'نامشخص')}"
        else:
            reply = "❌ خطا در دریافت قیمت بیت‌کوین."

    elif data == "price_eth":
        info = get_crypto_price("ETH/USDT")
        if info:
            reply = f"⟠ **اتریوم (ETH/USDT)**\n💰 قیمت: {info['price']:,.2f} $\n📊 تغییر ۲۴h: {info['change']:.2f}%\n"
            if info.get('high') and info.get('low'):
                reply += f"📈 بالا: {info['high']:,.2f}\n📉 پایین: {info['low']:,.2f}\n"
            reply += f"📌 منبع: {info.get('source', 'نامشخص')}"
        else:
            reply = "❌ خطا در دریافت قیمت اتریوم."

    elif data == "price_usdirt":
        price = get_usd_irt()
        if price:
            reply = f"💵 **دلار/تومان (USD/IRT)**\n💰 قیمت: {price:,.0f} تومان\n📌 منبع: زرین‌پال"
        else:
            reply = "❌ خطا در دریافت قیمت دلار/تومان."

    elif data == "price_eurusd":
        price = get_forex_price("EURUSD")
        if price:
            reply = f"🇪🇺 **یورو/دلار (EUR/USD)**\n💰 قیمت: {price:.4f}\n📌 منبع: Frankfurter"
        else:
            reply = "❌ خطا در دریافت قیمت یورو/دلار."

    elif data == "price_gbpusd":
        price = get_forex_price("GBPUSD")
        if price:
            reply = f"🇬🇧 **پوند/دلار (GBP/USD)**\n💰 قیمت: {price:.4f}\n📌 منبع: Frankfurter"
        else:
            reply = "❌ خطا در دریافت قیمت پوند/دلار."

    elif data == "price_gold":
        info = get_crypto_price("XAU/USD")
        if info:
            reply = f"🥇 **طلا (XAU/USD)**\n💰 قیمت: {info['price']:,.2f} $\n📊 تغییر ۲۴h: {info['change']:.2f}%\n📌 منبع: {info.get('source', 'نامشخص')}"
        else:
            reply = "❌ خطا در دریافت قیمت طلا."

    elif data == "price_search":
        # نمایش منوی ۲۰ ارز برتر + امکان جستجوی دستی
        top_keyboard = top20_menu_keyboard()
        bot.edit_message_text(
            "🔍 **جستجوی دستی یا انتخاب از لیست برترین‌ها**\n\n"
            "• روی یکی از دکمه‌های زیر کلیک کنید تا قیمت آن را ببینید.\n"
            "• یا نام نماد مورد نظر (مثلاً `ADA` یا `BTC`) را تایپ کنید.",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=top_keyboard,
            parse_mode='Markdown'
        )
        waiting_for_symbol[user_id] = True
        bot.answer_callback_query(call.id)
        return

    bot.edit_message_text(reply, call.message.chat.id, call.message.message_id, parse_mode='Markdown', reply_markup=back_to_main_keyboard())
    bot.answer_callback_query(call.id)

# ========== کالبک انتخاب ارز از لیست ۲۰ تایی ==========
@bot.callback_query_handler(func=lambda call: call.data.startswith("top_select_"))
def callback_top_select(call):
    user_id = call.from_user.id
    if is_user_expired(user_id):
        bot.answer_callback_query(call.id, "⏰ دوره آزمایشی شما به پایان رسیده.", show_alert=True)
        return

    symbol = call.data.split("top_select_")[1]
    # تبدیل نماد به فرمت USDT
    if not symbol.endswith('/USDT'):
        symbol = f"{symbol}/USDT"

    info = get_crypto_price(symbol)
    if info:
        reply = f"💰 **قیمت {symbol.replace('/USDT', '')}**\n"
        reply += f"قیمت: {info['price']:,.2f} $\n"
        reply += f"تغییر ۲۴h: {info['change']:.2f}%\n"
        if info.get('high') and info.get('low'):
            reply += f"بالاترین: {info['high']:,.2f}\nپایین‌ترین: {info['low']:,.2f}\n"
        reply += f"📌 منبع: {info.get('source', 'نامشخص')}"
    else:
        reply = f"❌ خطا در دریافت قیمت {symbol.replace('/USDT', '')}."

    bot.edit_message_text(reply, call.message.chat.id, call.message.message_id, parse_mode='Markdown', reply_markup=back_to_main_keyboard())
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "back_to_price")
def callback_back_to_price(call):
    bot.answer_callback_query(call.id)
    bot.edit_message_text("📊 لطفاً یک گزینه را انتخاب کنید:", call.message.chat.id, call.message.message_id, reply_markup=price_menu_keyboard())

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
        # اگر کاربر عدد یا کاراکتر خاص وارد کرده، پیام راهنما
        if not text.isalpha() and '/' not in text:
            bot.send_message(user_id, "❌ لطفاً یک نماد معتبر وارد کنید (مثلاً `ADA` یا `BTC`).", parse_mode='Markdown')
            bot.send_message(user_id, "🔙 برای ادامه جستجو یا بازگشت، از دکمه‌های منوی قیمت استفاده کنید:", reply_markup=price_menu_keyboard())
            return

        info = get_crypto_price_by_symbol(text)
        if info:
            reply = f"💰 **قیمت {text.upper()}**\n"
            reply += f"قیمت: {info['price']:,.2f} $\n"
            reply += f"تغییر ۲۴h: {info['change']:.2f}%\n"
            if info.get('high') and info.get('low'):
                reply += f"بالاترین: {info['high']:,.2f}\nپایین‌ترین: {info['low']:,.2f}\n"
            reply += f"📌 منبع: {info.get('source', 'نامشخص')}"
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
