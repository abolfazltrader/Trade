import os
import logging
import requests
import ccxt
import sqlite3
import time
import xml.etree.ElementTree as ET
import re
from datetime import datetime, timedelta
from flask import Flask, request, jsonify
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache

# ---------- تنظیمات اولیه ----------
TOKEN = os.environ.get("TELEGRAM_TOKEN")
if not TOKEN:
    raise ValueError("TELEGRAM_TOKEN not set!")

BOT_USERNAME = "Crypto_forex_2026_bot"
BASE_URL = "https://trade-i4js.onrender.com"

ADMIN_IDS = [6542890217]

bot = telebot.TeleBot(TOKEN)
app = Flask(__name__)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

waiting_for_symbol = {}

# ========== لیست نمادهای معتبر برای اخبار ==========
VALID_CRYPTO_SYMBOLS = {
    "BTC", "ETH", "DOGE", "BNB", "XRP", "SOL", "TON", "TRX", "LTC", "DOT",
    "AVAX", "LINK", "MATIC", "POL", "SUI", "LEO", "SHIB", "HBAR", "XLM",
    "BCH", "HYPE", "BGB", "XMR", "PI", "PEPE", "UNI", "APT", "OKB",
    "ONDO", "NEAR", "TRUMP", "TAO", "ICP", "KAS", "ETC", "AAVE", "MNT",
    "ENA", "FIL", "FARTCOIN"
}

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

# ========== سیستم دریافت قیمت (چندمنبعی با کش) ==========
class PriceFetcher:
    def __init__(self):
        self.cache = {}
        self.cache_time = 60
        self.executor = ThreadPoolExecutor(max_workers=5)
        self.binance = ccxt.binance({'enableRateLimit': True, 'timeout': 8000})
        self.kraken = ccxt.kraken({'enableRateLimit': True, 'timeout': 8000})
        self.okx = ccxt.okx({'enableRateLimit': True, 'timeout': 8000})
        self.kucoin = ccxt.kucoin({'enableRateLimit': True, 'timeout': 8000})
        self.coingecko_session = requests.Session()
        self.retry_count = 2

    def _get_cached(self, key):
        if key in self.cache:
            data, timestamp = self.cache[key]
            if (datetime.now() - timestamp).seconds < self.cache_time:
                return data
        return None

    def _set_cache(self, key, data):
        self.cache[key] = (data, datetime.now())

    def _fetch_with_retry(self, fetch_func, symbol, retries=2):
        for attempt in range(retries):
            try:
                result = fetch_func(symbol)
                if result and result.get('price') is not None:
                    return result
            except Exception as e:
                logger.warning(f"Attempt {attempt+1} failed for {symbol}: {e}")
                time.sleep(0.5)
        return None

    def _fetch_binance(self, symbol):
        try:
            ticker = self.binance.fetch_ticker(symbol)
            if ticker and ticker.get('last') is not None:
                return {'price': ticker['last'], 'change': ticker.get('percentage', 0),
                        'high': ticker.get('high', 0), 'low': ticker.get('low', 0), 'source': 'Binance'}
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
                return {'price': ticker['last'], 'change': ticker.get('percentage', 0),
                        'high': ticker.get('high', 0), 'low': ticker.get('low', 0), 'source': 'Kraken'}
        except Exception:
            pass
        return None

    def _fetch_okx(self, symbol):
        try:
            ticker = self.okx.fetch_ticker(symbol)
            if ticker and ticker.get('last') is not None:
                return {'price': ticker['last'], 'change': ticker.get('percentage', 0),
                        'high': ticker.get('high', 0), 'low': ticker.get('low', 0), 'source': 'OKX'}
        except Exception:
            pass
        return None

    def _fetch_kucoin(self, symbol):
        try:
            ticker = self.kucoin.fetch_ticker(symbol)
            if ticker and ticker.get('last') is not None:
                return {'price': ticker['last'], 'change': ticker.get('percentage', 0),
                        'high': ticker.get('high', 0), 'low': ticker.get('low', 0), 'source': 'KuCoin'}
        except Exception:
            pass
        return None

    def _fetch_coingecko(self, symbol):
        try:
            coin_id = symbol.split('/')[0].lower()
            url = f"https://api.coingecko.com/api/v3/simple/price?ids={coin_id}&vs_currencies=usd&include_24hr_change=true"
            resp = self.coingecko_session.get(url, timeout=5)
            data = resp.json()
            if coin_id in data and data[coin_id].get('usd') is not None:
                return {'price': data[coin_id]['usd'], 'change': data[coin_id].get('usd_24h_change', 0),
                        'high': None, 'low': None, 'source': 'CoinGecko'}
        except Exception:
            pass
        return None

    def get_crypto_price(self, symbol="BTC/USDT"):
        cache_key = f"crypto_{symbol}"
        cached = self._get_cached(cache_key)
        if cached:
            return cached

        sources = [
            self._fetch_binance,
            self._fetch_kucoin,
            self._fetch_okx,
            self._fetch_kraken,
            self._fetch_coingecko
        ]

        futures = [self.executor.submit(self._fetch_with_retry, src, symbol) for src in sources]
        start_time = time.time()
        for future in as_completed(futures, timeout=4):
            result = future.result()
            if result and result.get('price') is not None:
                self._set_cache(cache_key, result)
                return result
            if time.time() - start_time > 4:
                break

        return None

fetcher = PriceFetcher()

# ---------- توابع عمومی دریافت قیمت ----------
def get_crypto_price(symbol="BTC/USDT"):
    return fetcher.get_crypto_price(symbol)

def get_usd_irt():
    cache_key = "usd_irt"
    cached = fetcher._get_cached(cache_key)
    if cached:
        return cached
    try:
        url = "https://api.zarinpal.com/payment/unit-converter/v1/convert"
        params = {"amount": 1, "from_currency": "USD", "to_currency": "IRT"}
        resp = requests.get(url, params=params, timeout=5)
        data = resp.json()
        if data.get("result") and "data" in data["result"]:
            price = data["result"]["data"]["amount"]
            if price:
                fetcher._set_cache(cache_key, price)
                return price
    except Exception:
        pass
    try:
        url = "https://api.tgju.org/v1/market/price/USD"
        resp = requests.get(url, timeout=5)
        data = resp.json()
        if data.get("status") == "success" and "price" in data.get("data", {}):
            price = data["data"]["price"]
            if price:
                fetcher._set_cache(cache_key, price)
                return price
    except Exception:
        pass
    try:
        url = "https://exir.ir/api/price/USD"
        resp = requests.get(url, timeout=5)
        data = resp.json()
        if "price" in data:
            price = data["price"]
            if price:
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
        resp = requests.get(url, params=params, timeout=8)
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
        return fetcher.get_crypto_price(symbol)
    except Exception:
        return None

# ========== تابع دریافت اخبار (CryptoPanic + Fallback گوگل نیوز) ==========
@lru_cache(maxsize=100)
def get_crypto_news_cached(symbol, limit=5):
    """نسخه کش‌شده برای کاهش درخواست‌های تکراری"""
    return get_crypto_news(symbol, limit)

def get_crypto_news(symbol, limit=5):
    """
    دریافت اخبار مربوط به یک ارز:
    1. اول از CryptoPanic (رایگان، بدون کلید)
    2. در صورت خطا، از گوگل نیوز به عنوان Fallback
    """
    # ===== منبع اول: CryptoPanic =====
    try:
        url = "https://cryptopanic.com/api/v1/posts/"
        params = {
            'auth_token': '',  # خالی برای دسترسی رایگان (محدودیت دارد)
            'currencies': symbol.lower(),
            'kind': 'news',
            'public': 'true',
            'filter': 'hot'  # داغ‌ترین اخبار
        }
        response = requests.get(url, params=params, timeout=8)
        data = response.json()
        
        if data.get('results'):
            # ساختاردهی اخبار به صورت لیست
            news_items = []
            for post in data['results'][:limit]:
                title = post.get('title', 'بدون عنوان')
                link = post.get('url', '#')
                # تشخیص احساسات (اگر وجود داشت)
                sentiment = 'neutral'
                for tag in post.get('tags', []):
                    if tag.get('slug') in ['bullish', 'positive']:
                        sentiment = 'positive'
                        break
                    elif tag.get('slug') in ['bearish', 'negative']:
                        sentiment = 'negative'
                        break
                news_items.append({
                    'title': title,
                    'link': link,
                    'sentiment': sentiment
                })
            
            # دسته‌بندی اخبار
            positive_news = [n for n in news_items if n['sentiment'] == 'positive']
            negative_news = [n for n in news_items if n['sentiment'] == 'negative']
            neutral_news = [n for n in news_items if n['sentiment'] == 'neutral']
            
            text = f"📰 **اخبار مربوط به {symbol.upper()}** (CryptoPanic)\n\n"
            if positive_news:
                text += "🟢 **اخبار مثبت:**\n"
                for item in positive_news[:3]:
                    text += f"• {item['title']}\n"
                text += "\n"
            if negative_news:
                text += "🔴 **اخبار منفی:**\n"
                for item in negative_news[:3]:
                    text += f"• {item['title']}\n"
                text += "\n"
            if neutral_news:
                text += "⚪ **اخبار خنثی:**\n"
                for item in neutral_news[:3]:
                    text += f"• {item['title']}\n"
                text += "\n"
            
            # سنتیمنت کلی
            pos_count = len(positive_news)
            neg_count = len(negative_news)
            if pos_count > neg_count:
                sentiment_text = "🟢 **مثبت**"
                trading_result = "✅ خرید (Long)"
            elif neg_count > pos_count:
                sentiment_text = "🔴 **منفی**"
                trading_result = "❌ فروش (Short)"
            else:
                sentiment_text = "⚪ **خنثی**"
                trading_result = "⏳ انتظار / بدون سیگنال روشن"
            
            text += f"📌 **سنتیمنت کلی بازار:** {sentiment_text}\n"
            text += f"💡 **نتیجه معاملاتی:** {trading_result}\n\n"
            text += f"🔗 [مشاهده همه اخبار در CryptoPanic](https://cryptopanic.com/news/{symbol.lower()})"
            
            if len(text) > 4096:
                text = text[:4000] + "\n\n... (ادامه در لینک)"
            
            return text
            
    except Exception as e:
        logger.warning(f"CryptoPanic error for {symbol}: {e}")
        # در صورت خطا، به Fallback برو
    
    # ===== منبع دوم (Fallback): گوگل نیوز با User-Agent مناسب و تلاش مجدد =====
    try:
        query = f"{symbol} cryptocurrency news"
        rss_url = f"https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        # تلاش مجدد تا ۲ بار
        response = None
        for attempt in range(2):
            try:
                response = requests.get(rss_url, headers=headers, timeout=10)
                if response.status_code == 200:
                    break
                elif response.status_code == 503 and attempt == 0:
                    time.sleep(2)
                    continue
                else:
                    return f"❌ سرویس گوگل نیوز در دسترس نیست (کد {response.status_code}). لطفاً بعداً تلاش کنید."
            except Exception as e:
                if attempt == 1:
                    raise
                time.sleep(1)
        
        if response is None:
            return "❌ خطا در دریافت اخبار از گوگل نیوز."
        
        response.raise_for_status()
        
        root = ET.fromstring(response.content)
        channel = root.find('channel')
        if channel is None:
            return f"❌ ساختار RSS نامعتبر است."
        
        items = channel.findall('item')
        if not items:
            return f"📭 هیچ خبری برای `{symbol}` یافت نشد."
        
        news_text = f"📰 **اخبار مربوط به {symbol.upper()}** (Google News)\n\n"
        count = 0
        for item in items[:limit]:
            title_elem = item.find('title')
            title = title_elem.text if title_elem is not None else "بدون عنوان"
            link_elem = item.find('link')
            link = link_elem.text if link_elem is not None else "#"
            desc_elem = item.find('description')
            description = desc_elem.text if desc_elem is not None else ""
            if description:
                description = re.sub(r'<[^>]+>', '', description)
                description = description[:200] + "..." if len(description) > 200 else description
            
            count += 1
            news_text += f"**{count}. {title}**\n"
            if description:
                news_text += f"📌 {description}\n"
            news_text += f"🔗 [مشاهده کامل خبر]({link})\n\n"
        
        if count == 0:
            return f"📭 هیچ خبری برای `{symbol}` در دسترس نیست."
        
        if len(news_text) > 4096:
            news_text = news_text[:4000] + "\n\n... (ادامه اخبار در لینک‌ها)"
        
        return news_text
        
    except requests.exceptions.Timeout:
        logger.error(f"Timeout for symbol: {symbol}")
        return f"⏰ زمان دریافت اخبار برای `{symbol}` به پایان رسید. لطفاً مجدداً تلاش کنید."
    except requests.exceptions.ConnectionError:
        logger.error(f"Connection error for symbol: {symbol}")
        return f"❌ مشکل در اتصال به اینترنت. لطفاً دقایقی دیگر تلاش کنید."
    except ET.ParseError as e:
        logger.error(f"XML Parse error for {symbol}: {e}")
        return f"❌ خطا در پردازش اخبار برای `{symbol}`. لطفاً مجدداً تلاش کنید."
    except Exception as e:
        logger.error(f"Unexpected error for {symbol}: {e}")
        return f"❌ خطای غیرمنتظره در دریافت اخبار برای `{symbol}`: {str(e)[:100]}"

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
        InlineKeyboardButton("₿ BTC", callback_data="price_btc"),
        InlineKeyboardButton("⟠ ETH", callback_data="price_eth"),
        InlineKeyboardButton("💵 USDT", callback_data="price_usdt"),
        InlineKeyboardButton("🇪🇺 EUR/USD", callback_data="price_eurusd"),
        InlineKeyboardButton("🥇 XAU/USD", callback_data="price_gold"),
        InlineKeyboardButton("🇬🇧 GBP/USD", callback_data="price_gbpusd")
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
    
    waiting_for_symbol[user_id] = "news_symbol"
    
    symbols_list = ", ".join(sorted(VALID_CRYPTO_SYMBOLS))
    help_text = (
        "🔍 **دریافت اخبار اختصاصی ارزهای دیجیتال**\n\n"
        "لطفاً فقط **نماد** ارز مورد نظر را وارد کنید.\n"
        "تحلیل اخبار برای نمادهای زیر در دسترس است:\n\n"
        f"`{symbols_list}`\n\n"
        "📌 مثال: `BTC` یا `ETH`\n\n"
        "⏳ اخبار هر ۲۴ ساعت به‌روزرسانی می‌شود.\n"
        "💰 نمایش اخبار **رایگان** است و از اعتبار شما کم نمی‌شود."
    )
    bot.send_message(user_id, help_text, parse_mode='Markdown')

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
    
    if symbol == "EURUSD":
        symbol = "EUR/USDT"
    elif symbol == "GBPUSD":
        symbol = "GBP/USDT"
    elif symbol == "XAUUSD":
        symbol = "XAU/USDT"
    
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
            pair = f"{symbol[:3]}/{symbol[3:]}"
            if pair in ["EUR/USD", "GBP/USD"]:
                pair = pair.replace("/USD", "/USDT")
            info = get_crypto_price(pair)
            if info:
                text = f"📊 **تحلیل {pair}**\n💰 قیمت: {info['price']:,.0f} $\n📊 تغییر: {info['change']:.2f}%\n"
                if info.get('high') and info.get('low'):
                    text += f"📈 بالا: {info['high']:,.0f}\n📉 پایین: {info['low']:,.0f}"
                text += f"\n📌 منبع: {info.get('source', 'نامشخص')}"
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
    suggest_text = "🎯 **پیشنهاد خرید**\n\nبر اساس تحلیل‌های فعلی، ارزهای زیر پتانسیل رشد دارند:\n• BTC\n• ETH\n• XAU"
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
        "📰 اخبار: دریافت اخبار اختصاصی هر ارز با وارد کردن نماد\n"
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
            reply = f"₿ **BTC/USDT**\n💰 قیمت: {info['price']:,.0f} $\n📊 تغییر ۲۴h: {info['change']:.2f}%\n"
            if info.get('high') and info.get('low'):
                reply += f"📈 بالا: {info['high']:,.0f}\n📉 پایین: {info['low']:,.0f}\n"
            reply += f"📌 منبع: {info.get('source', 'نامشخص')}"
        else:
            reply = "❌ خطا در دریافت قیمت BTC."

    elif data == "price_eth":
        info = get_crypto_price("ETH/USDT")
        if info:
            reply = f"⟠ **ETH/USDT**\n💰 قیمت: {info['price']:,.2f} $\n📊 تغییر ۲۴h: {info['change']:.2f}%\n"
            if info.get('high') and info.get('low'):
                reply += f"📈 بالا: {info['high']:,.2f}\n📉 پایین: {info['low']:,.2f}\n"
            reply += f"📌 منبع: {info.get('source', 'نامشخص')}"
        else:
            reply = "❌ خطا در دریافت قیمت ETH."

    elif data == "price_usdt":
        reply = f"💵 **USDT/USD**\n💰 قیمت: 1.00 $\n📊 تغییر ۲۴h: 0.00%\n📌 منبع: ثابت (استیبل‌کوین)"

    elif data == "price_eurusd":
        info = get_crypto_price("EUR/USDT")
        if info:
            reply = f"🇪🇺 **EUR/USD**\n💰 قیمت: {info['price']:,.4f} $\n📊 تغییر ۲۴h: {info['change']:.2f}%\n"
            if info.get('high') and info.get('low'):
                reply += f"📈 بالا: {info['high']:,.4f}\n📉 پایین: {info['low']:,.4f}\n"
            reply += f"📌 منبع: {info.get('source', 'نامشخص')}"
        else:
            reply = "❌ خطا در دریافت قیمت EUR/USD."

    elif data == "price_gbpusd":
        info = get_crypto_price("GBP/USDT")
        if info:
            reply = f"🇬🇧 **GBP/USD**\n💰 قیمت: {info['price']:,.4f} $\n📊 تغییر ۲۴h: {info['change']:.2f}%\n"
            if info.get('high') and info.get('low'):
                reply += f"📈 بالا: {info['high']:,.4f}\n📉 پایین: {info['low']:,.4f}\n"
            reply += f"📌 منبع: {info.get('source', 'نامشخص')}"
        else:
            reply = "❌ خطا در دریافت قیمت GBP/USD."

    elif data == "price_gold":
        info = get_crypto_price("XAU/USDT")
        if info:
            reply = f"🥇 **XAU/USD**\n💰 قیمت: {info['price']:,.2f} $\n📊 تغییر ۲۴h: {info['change']:.2f}%\n"
            if info.get('high') and info.get('low'):
                reply += f"📈 بالا: {info['high']:,.2f}\n📉 پایین: {info['low']:,.2f}\n"
            reply += f"📌 منبع: {info.get('source', 'نامشخص')}"
        else:
            reply = "❌ خطا در دریافت قیمت XAU/USD."

    bot.edit_message_text(reply, call.message.chat.id, call.message.message_id, parse_mode='Markdown', reply_markup=back_to_main_keyboard())
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "back_main")
def callback_back_main(call):
    bot.answer_callback_query(call.id)
    bot.edit_message_text("به منوی اصلی برگشتید.", call.message.chat.id, call.message.message_id, reply_markup=None)
    bot.send_message(call.message.chat.id, "🔽 از دکمه‌های زیر استفاده کنید:", reply_markup=main_menu_keyboard())

# ---------- هندلر پیام‌های متنی (برای دریافت نماد اخبار) ----------
@bot.message_handler(func=lambda msg: True)
def handle_text_messages(message):
    user_id = message.chat.id
    text = message.text.strip().upper()

    # ===== اگر کاربر در حالت دریافت نماد اخبار است =====
    if waiting_for_symbol.get(user_id) == "news_symbol":
        # حذف وضعیت انتظار (حتی اگر خطا رخ دهد)
        waiting_for_symbol.pop(user_id, None)
        
        # اعتبارسنجی نماد
        if text not in VALID_CRYPTO_SYMBOLS:
            symbols_list = ", ".join(sorted(VALID_CRYPTO_SYMBOLS))
            bot.send_message(
                user_id,
                f"❌ نماد `{text}` معتبر نیست.\n\nلطفاً یکی از نمادهای زیر را وارد کنید:\n`{symbols_list}`",
                parse_mode='Markdown'
            )
            # بازگشت به حالت انتظار برای تلاش مجدد
            waiting_for_symbol[user_id] = "news_symbol"
            return
        
        # پیام "در حال دریافت..."
        processing_msg = bot.send_message(
            user_id,
            f"⏳ در حال دریافت اخبار مربوط به **{text}**... لطفاً صبر کنید.",
            parse_mode='Markdown'
        )
        
        try:
            # دریافت اخبار (با کش)
            news_text = get_crypto_news_cached(text, limit=5)
            
            # ارسال پیام جدید (به‌جای ویرایش)
            bot.send_message(
                user_id,
                news_text,
                parse_mode='Markdown',
                disable_web_page_preview=True
            )
            
            # حذف پیام "در حال دریافت..."
            try:
                bot.delete_message(user_id, processing_msg.message_id)
            except Exception as e:
                logger.warning(f"Could not delete processing message: {e}")
                
        except Exception as e:
            logger.error(f"Error in news processing: {e}")
            # اگر خطایی رخ داد، پیام خطا را ارسال کن
            bot.send_message(
                user_id,
                f"❌ خطا در دریافت اخبار برای `{text}`. لطفاً مجدداً تلاش کنید.\n\n{str(e)[:100]}",
                parse_mode='Markdown'
            )
            # حذف پیام "در حال دریافت..."
            try:
                bot.delete_message(user_id, processing_msg.message_id)
            except:
                pass
        
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
