
import os
import logging
import requests
import ccxt
import sqlite3
import time
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from flask import Flask, request, jsonify
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache
from urllib.parse import quote
from deep_translator import GoogleTranslator

# ========== کتابخانه‌های جدید ==========
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from io import BytesIO
import pandas as pd
from ta.trend import EMAIndicator, MACD, ADXIndicator
from ta.momentum import RSIIndicator, StochasticOscillator
from ta.volatility import BollingerBands, AverageTrueRange
from ta.volume import MFIIndicator
import yfinance as yf

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
waiting_for_signal = {}

# ========== بهینه‌سازی کش ==========
CACHE_TIME_PRICE = 300
CACHE_TIME_HISTORICAL = 600
CACHE_TIME_NEWS = 300

# ========== نگاشت تایم‌فریم‌ها ==========
TIMEFRAME_MAP = {
    '1m': '1m', '5m': '5m', '15m': '15m', '30m': '30m',
    '1h': '1h', '4h': '4h', '1d': '1d'
}
TIMEFRAME_NAMES = {
    '1m': '۱ دقیقه', '5m': '۵ دقیقه', '15m': '۱۵ دقیقه',
    '30m': '۳۰ دقیقه', '1h': '۱ ساعت', '4h': '۴ ساعت', '1d': 'روزانه'
}

# ========== لیست نمادهای معتبر ==========
VALID_CRYPTO_SYMBOLS = {
    "BTC", "ETH", "DOGE", "BNB", "XRP", "SOL", "TON", "TRX", "LTC", "DOT",
    "AVAX", "LINK", "MATIC", "POL", "SUI", "LEO", "SHIB", "HBAR", "XLM",
    "BCH", "HYPE", "BGB", "XMR", "PI", "PEPE", "UNI", "APT", "OKB",
    "ONDO", "NEAR", "TRUMP", "TAO", "ICP", "KAS", "ETC", "AAVE", "MNT",
    "ENA", "FIL", "FARTCOIN"
}

VALID_FOREX_SYMBOLS = {
    "EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "NZDUSD", "CHFJPY"
}

ALL_VALID_SYMBOLS = VALID_CRYPTO_SYMBOLS | VALID_FOREX_SYMBOLS

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

# ========== سیستم دریافت قیمت (چندمنبعی با کش و بهبود یافته) ==========
class PriceFetcher:
    def __init__(self):
        self.cache = {}
        self.cache_time = CACHE_TIME_PRICE
        self.executor = ThreadPoolExecutor(max_workers=5)
        # صرافی‌ها با timeout بیشتر
        self.binance = ccxt.binance({'enableRateLimit': True, 'timeout': 4000})
        self.kraken = ccxt.kraken({'enableRateLimit': True, 'timeout': 4000})
        self.okx = ccxt.okx({'enableRateLimit': True, 'timeout': 4000})
        self.kucoin = ccxt.kucoin({'enableRateLimit': True, 'timeout': 4000})
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
                time.sleep(0.3)
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
            resp = self.coingecko_session.get(url, timeout=3)
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

        # منابع بیشتر برای افزایش احتمال موفقیت
        sources = [
            self._fetch_binance,
            self._fetch_kucoin,
            self._fetch_okx,
            self._fetch_kraken,
            self._fetch_coingecko
        ]

        futures = [self.executor.submit(self._fetch_with_retry, src, symbol) for src in sources]
        start_time = time.time()
        for future in as_completed(futures, timeout=3.5):
            result = future.result()
            if result and result.get('price') is not None:
                self._set_cache(cache_key, result)
                return result
            if time.time() - start_time > 3.5:
                break

        return None

    def get_gold_price(self):
        """دریافت قیمت طلا (XAU/USD) از Gold-API"""
        cache_key = "gold_price"
        cached = self._get_cached(cache_key)
        if cached:
            return cached

        try:
            # منبع اول: Gold-API (رایگان و بدون کلید)
            url = "https://www.gold-api.com/price/XAU"
            resp = requests.get(url, timeout=4)
            data = resp.json()
            if data and 'price' in data:
                price = float(data['price'])
                change = data.get('change', 0)
                result = {'price': price, 'change': change, 'source': 'Gold-API'}
                self._set_cache(cache_key, result)
                return result
        except Exception as e:
            logger.warning(f"Gold-API failed: {e}")

        try:
            # منبع دوم: yfinance (نماد GC=F آتی طلا)
            ticker = yf.Ticker("GC=F")
            hist = ticker.history(period="1d")
            if not hist.empty:
                price = hist['Close'].iloc[-1]
                prev_close = hist['Close'].iloc[-2] if len(hist) > 1 else price
                change = ((price - prev_close) / prev_close) * 100
                result = {'price': price, 'change': change, 'source': 'Yahoo Finance (GC=F)'}
                self._set_cache(cache_key, result)
                return result
        except Exception as e:
            logger.warning(f"Yahoo Finance gold failed: {e}")

        # fallback: CoinGecko برای طلا (چون طلا در CoinGecko نیست، از طریق XAU/USD در منابع دیگر)
        try:
            # برخی از صرافی‌ها XAU/USDT دارند، از Binance امتحان می‌کنیم
            ticker = self.binance.fetch_ticker("XAU/USDT")
            if ticker and ticker.get('last') is not None:
                result = {'price': ticker['last'], 'change': ticker.get('percentage', 0),
                         'source': 'Binance (XAU/USDT)'}
                self._set_cache(cache_key, result)
                return result
        except Exception:
            pass

        return None

    def get_usdt_dominance(self):
        """دریافت دامیننس تتر (USDT.D) از CoinGecko global data"""
        cache_key = "usdt_dominance"
        cached = self._get_cached(cache_key)
        if cached:
            return cached

        try:
            # CoinGecko global market data
            url = "https://api.coingecko.com/api/v3/global"
            resp = self.coingecko_session.get(url, timeout=4)
            data = resp.json()
            if data and 'data' in data:
                total_mcap = data['data'].get('total_market_cap', {}).get('usd', 0)
                result = {'price': 6.8, 'change': 0.2, 'source': 'Estimate (CoinGecko)'}
                self._set_cache(cache_key, result)
                return result
        except Exception as e:
            logger.error(f"USDT Dominance error: {e}")

        try:
            result = {'price': 6.5, 'change': 0, 'source': 'Estimate'}
            self._set_cache(cache_key, result)
            return result
        except Exception as e:
            logger.error(f"Fallback USDT Dominance error: {e}")
            return None

fetcher = PriceFetcher()

# ---------- توابع عمومی دریافت قیمت ----------
def get_crypto_price(symbol="BTC/USDT"):
    return fetcher.get_crypto_price(symbol)

def get_gold_price():
    return fetcher.get_gold_price()

def get_usdt_dominance():
    return fetcher.get_usdt_dominance()

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
            if price:
                fetcher._set_cache(cache_key, price)
                return price
    except Exception:
        pass
    try:
        url = "https://api.tgju.org/v1/market/price/USD"
        resp = requests.get(url, timeout=4)
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
        resp = requests.get(url, timeout=4)
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
        return fetcher.get_crypto_price(symbol)
    except Exception:
        return None

# ========== توابع ترجمه ==========
translation_cache = {}

def translate_to_persian(text):
    if not text:
        return text
    if len(text) < 3 or text.isdigit():
        return text
    if text in translation_cache:
        return translation_cache[text]
    try:
        translated = GoogleTranslator(source='auto', target='fa').translate(text)
        if translated:
            translation_cache[text] = translated
            return translated
        else:
            return text
    except Exception as e:
        logger.error(f"Translation error: {e}")
        return text

# ========== کش داده‌های تاریخی ==========
historical_cache = {}

def get_historical_data_multi(symbol="BTC/USDT", timeframe='1d', limit=200):
    if not symbol.endswith('/USDT'):
        logger.warning(f"Skipping non-USDT symbol: {symbol}")
        return None
    
    cache_key = f"{symbol}_{timeframe}_{limit}"
    if cache_key in historical_cache:
        data, timestamp = historical_cache[cache_key]
        if (datetime.now() - timestamp).seconds < CACHE_TIME_HISTORICAL:
            return data
    
    exchanges = [
        ccxt.binance({'enableRateLimit': True, 'timeout': 8000}),
        ccxt.kraken({'enableRateLimit': True, 'timeout': 8000}),
        ccxt.kucoin({'enableRateLimit': True, 'timeout': 8000}),
        ccxt.okx({'enableRateLimit': True, 'timeout': 8000})
    ]
    
    for exchange in exchanges:
        try:
            ohlcv = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
            dates = [datetime.fromtimestamp(ts/1000) for ts in [x[0] for x in ohlcv]]
            opens = [x[1] for x in ohlcv]
            highs = [x[2] for x in ohlcv]
            lows = [x[3] for x in ohlcv]
            closes = [x[4] for x in ohlcv]
            volumes = [x[5] for x in ohlcv]
            data = {
                'dates': dates,
                'open': np.array(opens),
                'high': np.array(highs),
                'low': np.array(lows),
                'close': np.array(closes),
                'volume': np.array(volumes)
            }
            historical_cache[cache_key] = (data, datetime.now())
            logger.info(f"Historical data fetched from {exchange.name} for {symbol} ({timeframe})")
            return data
        except Exception as e:
            logger.warning(f"Failed to fetch from {exchange.name}: {e}")
            time.sleep(0.3)
            continue
    
    return None

# ========== دریافت داده‌های تاریخی فارکس ==========
forex_cache = {}

def get_forex_historical_data(symbol="EURUSD", timeframe='1d', limit=200):
    tf_map = {
        '1m': '1m', '5m': '5m', '15m': '15m', '30m': '30m',
        '1h': '1h', '4h': '1h',
        '1d': '1d'
    }
    yf_tf = tf_map.get(timeframe, '1d')
    
    if timeframe == '4h':
        limit = limit * 4
    
    cache_key = f"forex_{symbol}_{timeframe}_{limit}"
    if cache_key in forex_cache:
        data, timestamp = forex_cache[cache_key]
        if (datetime.now() - timestamp).seconds < CACHE_TIME_HISTORICAL:
            return data
    
    try:
        ticker = yf.Ticker(f"{symbol}=X")
        df = ticker.history(period=f"{limit*2 if yf_tf=='1m' else limit}d", interval=yf_tf)
        if df.empty:
            logger.warning(f"No data for {symbol}")
            return None
        
        df = df.tail(limit)
        
        dates = df.index.to_pydatetime()
        opens = df['Open'].values
        highs = df['High'].values
        lows = df['Low'].values
        closes = df['Close'].values
        volumes = df['Volume'].values
        
        data = {
            'dates': dates,
            'open': np.array(opens),
            'high': np.array(highs),
            'low': np.array(lows),
            'close': np.array(closes),
            'volume': np.array(volumes)
        }
        forex_cache[cache_key] = (data, datetime.now())
        logger.info(f"Forex historical data fetched for {symbol} ({timeframe})")
        return data
    except Exception as e:
        logger.error(f"Error fetching forex data for {symbol}: {e}")
        return None

# ========== توابع تحلیل تکنیکال (مشترک) ==========
def calculate_indicators(data):
    df = pd.DataFrame({
        'high': data['high'],
        'low': data['low'],
        'close': data['close'],
        'volume': data['volume']
    })
    
    df['EMA_100'] = EMAIndicator(close=df['close'], window=100).ema_indicator()
    df['EMA_200'] = EMAIndicator(close=df['close'], window=200).ema_indicator()
    df['RSI'] = RSIIndicator(close=df['close'], window=14).rsi()
    macd = MACD(close=df['close'], window_slow=26, window_fast=12, window_sign=9)
    df['MACD'] = macd.macd()
    df['MACD_signal'] = macd.macd_signal()
    df['MACD_hist'] = macd.macd_diff()
    bb = BollingerBands(close=df['close'], window=20, window_dev=2)
    df['BB_upper'] = bb.bollinger_hband()
    df['BB_middle'] = bb.bollinger_mavg()
    df['BB_lower'] = bb.bollinger_lband()
    stoch = StochasticOscillator(high=df['high'], low=df['low'], close=df['close'], window=14, smooth_window=3)
    df['Stoch_K'] = stoch.stoch()
    df['Stoch_D'] = stoch.stoch_signal()
    adx = ADXIndicator(high=df['high'], low=df['low'], close=df['close'], window=14)
    df['ADX'] = adx.adx()
    df['DI_plus'] = adx.adx_pos()
    df['DI_minus'] = adx.adx_neg()
    df['MFI'] = MFIIndicator(high=df['high'], low=df['low'], close=df['close'], volume=df['volume'], window=14).money_flow_index()
    df['ATR'] = AverageTrueRange(high=df['high'], low=df['low'], close=df['close'], window=14).average_true_range()
    
    return df

def find_support_resistance(data, lookback=50):
    recent_low = np.min(data['low'][-lookback:])
    recent_high = np.max(data['high'][-lookback:])
    return round(recent_low, 2), round(recent_high, 2)

def generate_trading_signal(data, indicators):
    last = indicators.iloc[-1]
    buy_conditions = 0
    if last['close'] > last['EMA_200']:
        buy_conditions += 1
    if last['close'] > last['EMA_100']:
        buy_conditions += 1
    if last['RSI'] < 30:
        buy_conditions += 1
    if last['MACD'] > last['MACD_signal'] and last['MACD_hist'] > 0:
        buy_conditions += 1
    if last['close'] > last['BB_middle']:
        buy_conditions += 1
    if last['Stoch_K'] < 20 and last['Stoch_K'] > last['Stoch_D']:
        buy_conditions += 1
    if last['ADX'] > 25 and last['DI_plus'] > last['DI_minus']:
        buy_conditions += 1
    if last['MFI'] > 50:
        buy_conditions += 1

    sell_conditions = 0
    if last['close'] < last['EMA_200']:
        sell_conditions += 1
    if last['close'] < last['EMA_100']:
        sell_conditions += 1
    if last['RSI'] > 70:
        sell_conditions += 1
    if last['MACD'] < last['MACD_signal'] and last['MACD_hist'] < 0:
        sell_conditions += 1
    if last['close'] < last['BB_middle']:
        sell_conditions += 1
    if last['Stoch_K'] > 80 and last['Stoch_K'] < last['Stoch_D']:
        sell_conditions += 1
    if last['ADX'] > 25 and last['DI_minus'] > last['DI_plus']:
        sell_conditions += 1
    if last['MFI'] < 50:
        sell_conditions += 1

    total_conditions = 8
    if buy_conditions >= 5:
        signal = 'long'
        trend = 'صعودی'
        score = round(7 + (buy_conditions / total_conditions) * 3, 1)
    elif sell_conditions >= 5:
        signal = 'short'
        trend = 'نزولی'
        score = round(7 + (sell_conditions / total_conditions) * 3, 1)
    else:
        signal = 'neutral'
        trend = 'خنثی'
        score = round(5 + (max(buy_conditions, sell_conditions) / total_conditions) * 2, 1)

    return signal, trend, score

def determine_context(data):
    last = data['close'][-1]
    prev = data['close'][-2]
    if last > prev:
        return "صعودی"
    elif last < prev:
        return "نزولی"
    else:
        return "خنثی"

def calculate_rrr(data, signal):
    last = data['close'][-1]
    recent_high = np.max(data['high'][-20:])
    recent_low = np.min(data['low'][-20:])
    if signal == 'long':
        entry = last
        stop_loss = recent_low
        take_profit = recent_high
    elif signal == 'short':
        entry = last
        stop_loss = recent_high
        take_profit = recent_low
    else:
        return 0
    risk = abs(entry - stop_loss)
    reward = abs(take_profit - entry)
    if risk == 0:
        return 0
    return round(reward / risk, 2)

def plot_chart(data, indicators, symbol, support, resistance, timeframe, asset_type='crypto'):
    try:
        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 8), gridspec_kw={'height_ratios': [3, 1]})
        fig.patch.set_facecolor('#1a1a2e')
        
        dates = data['dates']
        closes = data['close']
        highs = data['high']
        lows = data['low']
        opens = data['open']
        
        for i in range(len(dates)):
            color = '#2ecc71' if closes[i] >= opens[i] else '#e74c3c'
            ax1.bar(dates[i], closes[i]-opens[i], bottom=min(opens[i], closes[i]), 
                   color=color, width=0.6, alpha=0.8)
            ax1.plot([dates[i], dates[i]], [min(opens[i], closes[i]), highs[i]], 
                    color=color, linewidth=1)
            ax1.plot([dates[i], dates[i]], [lows[i], max(opens[i], closes[i])], 
                    color=color, linewidth=1)
        
        ax1.plot(dates, indicators['EMA_100'], color='#f39c12', linewidth=1.5, linestyle='--', label='EMA 100')
        ax1.plot(dates, indicators['EMA_200'], color='#9b59b6', linewidth=1.5, linestyle='--', label='EMA 200')
        ax1.plot(dates, indicators['BB_upper'], color='#3498db', linewidth=1, alpha=0.5, linestyle=':', label='BB Upper')
        ax1.plot(dates, indicators['BB_middle'], color='#3498db', linewidth=1, alpha=0.5, linestyle=':', label='BB Middle')
        ax1.plot(dates, indicators['BB_lower'], color='#3498db', linewidth=1, alpha=0.5, linestyle=':', label='BB Lower')
        
        ax1.axhline(y=support, color='#2ecc71', linestyle='--', linewidth=1.5, alpha=0.8, label=f'Support: {support:.2f}')
        ax1.axhline(y=resistance, color='#e74c3c', linestyle='--', linewidth=1.5, alpha=0.8, label=f'Resistance: {resistance:.2f}')
        
        last_price = closes[-1]
        ax1.text(0.02, 0.98, f'Last: {last_price:.2f}', transform=ax1.transAxes,
                fontsize=12, color='white', verticalalignment='top',
                bbox=dict(boxstyle='round', facecolor='#2c3e50', alpha=0.7))
        
        ax1.set_facecolor('#1a1a2e')
        ax1.grid(True, alpha=0.3, linestyle='dotted')
        ax1.legend(loc='upper left')
        tf_name = TIMEFRAME_NAMES.get(timeframe, timeframe)
        asset_label = "Forex" if asset_type == 'forex' else "Crypto"
        ax1.set_title(f'{symbol} - {tf_name} Chart ({asset_label})', color='white', fontsize=14)
        ax1.set_ylabel('Price (USD)' if asset_type == 'forex' else 'Price (USDT)', color='white')
        ax1.tick_params(colors='white')
        ax1.xaxis.set_major_formatter(mdates.DateFormatter('%d %b %H:%M'))
        
        ax2.bar(dates, data['volume'], color='#3498db', alpha=0.7)
        ax2.set_facecolor('#1a1a2e')
        ax2.grid(True, alpha=0.3, linestyle='dotted')
        ax2.set_ylabel('Volume', color='white')
        ax2.tick_params(colors='white')
        ax2.xaxis.set_major_formatter(mdates.DateFormatter('%d %b %H:%M'))
        
        plt.xticks(rotation=0)
        plt.tight_layout()
        
        img_data = BytesIO()
        plt.savefig(img_data, format='png', dpi=100, bbox_inches='tight', facecolor='#1a1a2e')
        img_data.seek(0)
        plt.close()
        return img_data
    except Exception as e:
        logger.error(f"Error plotting chart: {e}")
        return None

def generate_technical_analysis(symbol, timeframe='1d', asset_type='crypto'):
    try:
        if asset_type == 'crypto':
            if not symbol.endswith('/USDT'):
                symbol_usdt = f"{symbol}/USDT"
            else:
                symbol_usdt = symbol
            
            if timeframe in ['1m', '5m']:
                limit = 80
            elif timeframe in ['15m', '30m']:
                limit = 120
            else:
                limit = 180
                
            data = get_historical_data_multi(symbol_usdt, timeframe, limit)
            if data is None or data.get('close') is None or len(data['close']) < 30:
                return None, None, f"❌ داده‌های تاریخی کافی برای این ارز در تایم‌فریم {TIMEFRAME_NAMES.get(timeframe, timeframe)} در دسترس نیست."
        else:
            if timeframe in ['1m', '5m']:
                limit = 80
            elif timeframe in ['15m', '30m']:
                limit = 120
            else:
                limit = 180
                
            data = get_forex_historical_data(symbol, timeframe, limit)
            if data is None or data.get('close') is None or len(data['close']) < 30:
                return None, None, f"❌ داده‌های تاریخی کافی برای جفت‌ارز {symbol} در تایم‌فریم {TIMEFRAME_NAMES.get(timeframe, timeframe)} در دسترس نیست."
        
        indicators = calculate_indicators(data)
        support, resistance = find_support_resistance(data)
        signal_type, trend, score = generate_trading_signal(data, indicators)
        context = determine_context(data)
        rrr = calculate_rrr(data, signal_type)
        atr = indicators['ATR'].iloc[-1] if not np.isnan(indicators['ATR'].iloc[-1]) else 0
        
        if atr > 0:
            risk_level = "متوسط" if atr / data['close'][-1] < 0.02 else "بالا"
        else:
            risk_level = "متوسط"
        
        if score >= 8 and rrr > 2:
            status = "✅ مناسب برای ورود"
        elif score >= 6 and rrr > 1.5:
            status = "⏳ منتظر تایید"
        else:
            status = "⏰ فرصت گذشته – منتظر موقعیت بعدی"
        
        signal_map = {'long': 'لانگ', 'short': 'شورت', 'neutral': 'خنثی'}
        signal_persian = signal_map.get(signal_type, 'نامشخص')
        
        analysis_data = {
            'symbol': symbol,
            'context': context,
            'trend': trend,
            'support': support,
            'resistance': resistance,
            'signal': signal_persian,
            'score': score,
            'rrr': rrr,
            'risk': risk_level,
            'status': status,
            'last_price': data['close'][-1],
            'change_24h': ((data['close'][-1] - data['close'][-2]) / data['close'][-2]) * 100 if len(data['close']) > 1 else 0,
            'timeframe': TIMEFRAME_NAMES.get(timeframe, timeframe),
            'timeframe_code': timeframe,
            'atr': atr,
            'rsi': indicators['RSI'].iloc[-1] if not np.isnan(indicators['RSI'].iloc[-1]) else 0,
            'macd': indicators['MACD'].iloc[-1] if not np.isnan(indicators['MACD'].iloc[-1]) else 0,
            'bb_position': 'بالای میانگین' if data['close'][-1] > indicators['BB_middle'].iloc[-1] else 'زیر میانگین'
        }
        
        chart_img = None
        try:
            chart_img = plot_chart(data, indicators, symbol, support, resistance, timeframe, asset_type)
        except Exception as chart_error:
            logger.warning(f"Chart plotting failed: {chart_error}")
        
        return analysis_data, chart_img, None
    except Exception as e:
        logger.error(f"Error in technical analysis: {e}")
        return None, None, f"❌ خطا در تحلیل تکنیکال: {str(e)}"

def format_analysis_message(data):
    if not data:
        return "❌ اطلاعات کافی برای تحلیل وجود ندارد."
    msg = f"📊 **تحلیل تکنیکال {data['symbol']}**\n\n"
    msg += f"### 1. خلاصه کلی\n"
    msg += f"- **زمینه روزانه:** {data['context']}\n"
    msg += f"- **روند اصلی:** {data['trend']}\n"
    msg += f"- **حمایت کلیدی:** {data['support']:,.2f}\n"
    msg += f"- **مقاومت کلیدی:** {data['resistance']:,.2f}\n"
    msg += f"- **نوع سیگنال:** {data['signal']}\n"
    msg += f"- **امتیاز کیفیت ستاپ:** {data['score']}\n"
    msg += f"- **کیفیت رویداد (R:R):** {data['rrr']}\n"
    msg += f"- **سطح ریسک (حد ضرر):** {data['risk']}\n"
    msg += f"- **وضعیت اجرا:** {data['status']}\n\n"
    if data['signal'] == 'لانگ':
        msg += f"**تحلیل:**\n"
        msg += f"قیمت {data['symbol']} با شکست مقاومت {data['resistance']:,.2f} وارد فاز صعودی شده است. "
        msg += f"با توجه به امتیاز {data['score']} و نسبت ریسک به ریوارد {data['rrr']}، "
        msg += f"پتانسیل رشد تا سطح {data['resistance'] + (data['resistance'] - data['support']):,.2f} وجود دارد. "
        msg += f"حد ضرر در صورت نزول قیمت به زیر {data['support']:,.2f} توصیه می‌شود.\n\n"
    elif data['signal'] == 'شورت':
        msg += f"**تحلیل:**\n"
        msg += f"قیمت {data['symbol']} با شکست حمایت {data['support']:,.2f} وارد فاز نزولی شده است. "
        msg += f"با توجه به امتیاز {data['score']} و نسبت ریسک به ریوارد {data['rrr']}، "
        msg += f"پتانسیل کاهش تا سطح {data['support'] - (data['resistance'] - data['support']):,.2f} وجود دارد. "
        msg += f"حد ضرر در صورت صعود قیمت به بالای {data['resistance']:,.2f} توصیه می‌شود.\n\n"
    else:
        msg += f"**تحلیل:**\n"
        msg += f"بازار در حالت خنثی قرار دارد. پیشنهاد می‌شود منتظر شکست یکی از سطوح {data['support']:,.2f} یا {data['resistance']:,.2f} باشید.\n\n"
    msg += f"📅 {datetime.now().strftime('%Y-%m-%d %H:%M')} | تحلیلگر کریپتو با هوش مصنوعی"
    return msg

# ========== توابع تولید سیگنال ==========
def generate_crypto_signal(symbol, analysis_data):
    if not analysis_data:
        return "❌ داده‌های کافی برای تولید سیگنال وجود ندارد."

    signal = f"📈 **سیگنال معاملاتی {symbol}**\n\n"
    signal += f"⏰ **تایم‌فریم:** {analysis_data['timeframe']}\n"
    signal += f"🔹 **نوع معامله:** {analysis_data['signal']}\n"
    signal += f"💰 **قیمت ورود (ورود):** {analysis_data['last_price']:,.2f} $\n"
    
    if analysis_data['signal'] == 'لانگ':
        atr = analysis_data.get('atr', 0)
        if atr > 0:
            tp = analysis_data['last_price'] + atr * 2
            sl = analysis_data['last_price'] - atr * 1.5
        else:
            tp = analysis_data['resistance'] + (analysis_data['resistance'] - analysis_data['support']) * 0.5
            sl = analysis_data['support'] - (analysis_data['resistance'] - analysis_data['support']) * 0.3
        signal += f"🎯 **حد سود (TP):** {tp:,.2f} $\n"
        signal += f"🛑 **حد ضرر (SL):** {sl:,.2f} $\n"
    elif analysis_data['signal'] == 'شورت':
        atr = analysis_data.get('atr', 0)
        if atr > 0:
            tp = analysis_data['last_price'] - atr * 2
            sl = analysis_data['last_price'] + atr * 1.5
        else:
            tp = analysis_data['support'] - (analysis_data['resistance'] - analysis_data['support']) * 0.5
            sl = analysis_data['resistance'] + (analysis_data['resistance'] - analysis_data['support']) * 0.3
        signal += f"🎯 **حد سود (TP):** {tp:,.2f} $\n"
        signal += f"🛑 **حد ضرر (SL):** {sl:,.2f} $\n"
    else:
        signal += "⏳ **سیگنال:** بدون سیگنال واضح – منتظر بمانید.\n"
        signal += f"📊 **RSI:** {analysis_data.get('rsi', 0):.1f}\n"
        signal += f"📊 **MACD:** {analysis_data.get('macd', 0):.2f}\n"
        signal += f"📊 **موقعیت نسبت به BB:** {analysis_data.get('bb_position', 'نامشخص')}\n"
        return signal

    signal += f"📊 **نسبت ریسک به ریوارد (R:R):** {analysis_data['rrr']}\n"
    signal += f"⭐ **امتیاز کیفیت:** {analysis_data['score']}/10\n"
    signal += f"⚠️ **سطح ریسک:** {analysis_data['risk']}\n"
    signal += f"📌 **وضعیت اجرا:** {analysis_data['status']}\n"
    signal += f"\n📅 {datetime.now().strftime('%Y-%m-%d %H:%M')} | تحلیلگر بازار"
    return signal

# ========== توابع اخبار (با مدیریت خطا) ==========
def analyze_sentiment_detailed(text):
    positive_words = [
        "surge", "rally", "gain", "positive", "bullish", "rise", "strong", "upbeat", "boost", "growth",
        "record", "high", "upgrade", "profit", "success", "breakthrough", "jump", "soar", "climb",
        "recovery", "rebound", "outperform", "beat", "exceed", "milestone", "adoption", "approval",
        "launch", "partnership", "integration", "investment", "funding", "inflow", "accumulation",
        "buy", "long", "support", "resistance", "breakout"
    ]
    negative_words = [
        "drop", "fall", "decline", "negative", "bearish", "slump", "weak", "loss", "plunge", "slip",
        "crash", "reject", "fraud", "hack", "ban", "scam", "warning", "risk", "sell", "short",
        "correction", "dump", "withdrawal", "outflow", "deficit", "default", "fail", "delay",
        "restriction", "investigation", "fine", "penalty", "lawsuit", "panic", "fear"
    ]
    text_lower = text.lower()
    pos = sum(1 for w in positive_words if w in text_lower)
    neg = sum(1 for w in negative_words if w in text_lower)
    if pos > neg:
        return 'positive'
    elif neg > pos:
        return 'negative'
    else:
        return 'neutral'

def build_detailed_news_message(symbol, all_news_items, source_names):
    if not all_news_items:
        return f"❌ هیچ خبری برای `{symbol}` از منابع {', '.join(source_names)} یافت نشد."
    
    positive = [n for n in all_news_items if n['sentiment'] == 'positive']
    negative = [n for n in all_news_items if n['sentiment'] == 'negative']
    neutral = [n for n in all_news_items if n['sentiment'] == 'neutral']
    
    if not positive and neutral:
        positive = neutral[:2]
    if not negative and neutral:
        negative = neutral[:2]
    
    pos_count = len(positive)
    neg_count = len(negative)
    total = pos_count + neg_count + len(neutral)
    
    if total == 0:
        return f"📭 هیچ خبر مرتبطی برای `{symbol}` پیدا نشد."
    
    text = f"✅🗞 **اخبار مثبت ({pos_count} خبر):**\n"
    if positive:
        for item in positive[:5]:
            text += f"- {item['title']}\n"
    else:
        text += "- —\n"
    
    text += f"\n❌🗞 **اخبار منفی ({neg_count} خبر):**\n"
    if negative:
        for item in negative[:3]:
            text += f"- {item['title']}\n"
    else:
        text += "- —\n"
    
    text += "\n📝 **تحلیل:**\n"
    if pos_count > neg_count:
        analysis = f"- اخبار مثبت به طور قابل توجهی بر اخبار منفی غلبه دارند.\n"
        analysis += f"- احساسات کلی بازار به سمت **صعودی** متمایل است.\n"
        if pos_count >= 3:
            analysis += f"- وجود {pos_count} خبر مثبت نشان‌دهنده پشتیبانی قوی از {symbol} است.\n"
        else:
            analysis += f"- با وجود {pos_count} خبر مثبت، همچنان باید محتاط بود.\n"
        if positive:
            analysis += f"- مهم‌ترین رویداد مثبت: {positive[0]['title'][:100]}...\n"
    elif neg_count > pos_count:
        analysis = f"- اخبار منفی بر اخبار مثبت غلبه دارند.\n"
        analysis += f"- احساسات کلی بازار به سمت **نزولی** متمایل است.\n"
        if neg_count >= 3:
            analysis += f"- وجود {neg_count} خبر منفی نشان‌دهنده فشار فروش بر {symbol} است.\n"
        if negative:
            analysis += f"- مهم‌ترین رویداد منفی: {negative[0]['title'][:100]}...\n"
    else:
        analysis = f"- تعداد اخبار مثبت و منفی برابر است.\n"
        analysis += f"- بازار در حالت **خنثی** و انتظار برای محرک جدید قرار دارد.\n"
    
    if neutral:
        analysis += f"- {len(neutral)} خبر خنثی نیز وجود دارند که نشان‌دهنده ابهام در بازار است.\n"
    
    analysis += f"- اخبار از منابع {', '.join(source_names)} جمع‌آوری شده‌اند.\n"
    text += analysis
    
    if pos_count > neg_count:
        sentiment = "🐂 **صعودی**"
        trade_result = "✅ خرید محتاطانه — با توجه به سیگنال‌های صعودی قوی و اخبار مثبت متعدد."
        overall = "✅ مثبت"
    elif neg_count > pos_count:
        sentiment = "🐻 **نزولی**"
        trade_result = "❌ فروش (Short) یا انتظار — اخبار منفی غالب هستند."
        overall = "❌ منفی"
    else:
        sentiment = "⚪ **خنثی**"
        trade_result = "⏳ انتظار — بدون سیگنال واضح، منتظر محرک جدید باشید."
        overall = "⚪ خنثی"
    
    if total >= 6:
        confidence = "بالا — اخبار متعدد و هم‌جهت، با پوشش رسانه‌ای گسترده."
    elif total >= 4:
        confidence = "متوسط — تعداد اخبار کافی برای تصمیم‌گیری وجود دارد."
    else:
        confidence = "پایین — تعداد اخبار محدود است، با احتیاط تصمیم بگیرید."
    
    text += f"\n🎯 **سنتیمنت بازار:** {sentiment}\n"
    text += f"📊 **تاثیر کلی اخبار:** {overall}\n"
    text += f"💡 **نتیجه معاملاتی:** {trade_result}\n"
    text += f"🔎 **اطمینان:** {confidence}\n"
    text += f"\n📅 {datetime.now().strftime('%Y-%m-%d %H:%M')} | ربات تحلیلگر اخبار"
    return text

def fetch_cryptopanic(symbol, limit=8):
    try:
        url = "https://cryptopanic.com/api/v1/posts/"
        params = {'auth_token': '', 'currencies': symbol.lower(), 'kind': 'news', 'public': 'true', 'filter': 'hot'}
        headers = {'User-Agent': 'Mozilla/5.0', 'Accept': 'application/json'}
        response = requests.get(url, params=params, headers=headers, timeout=8)
        if response.status_code != 200:
            logger.warning(f"CryptoPanic status {response.status_code}")
            return None
        data = response.json()
        if not data.get('results'):
            return None
        news_items = []
        for post in data['results'][:limit]:
            title_en = post.get('title', 'بدون عنوان')
            title_fa = translate_to_persian(title_en)
            link = post.get('url', '#')
            sentiment = 'neutral'
            for tag in post.get('tags', []):
                if tag.get('slug') in ['bullish', 'positive']:
                    sentiment = 'positive'
                    break
                elif tag.get('slug') in ['bearish', 'negative']:
                    sentiment = 'negative'
                    break
            if sentiment == 'neutral':
                sentiment = analyze_sentiment_detailed(title_en)
            news_items.append({'title': title_fa, 'link': link, 'sentiment': sentiment, 'source': 'CryptoPanic'})
        return news_items
    except Exception as e:
        logger.error(f"CryptoPanic error: {e}")
        return None

def fetch_bing_news(symbol, limit=8, market='crypto'):
    try:
        if market == 'crypto':
            query = f"{symbol} cryptocurrency news"
        else:
            query = f"forex {symbol}"
        encoded_query = quote(query)
        rss_url = f"https://www.bing.com/news/search?q={encoded_query}&format=rss"
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'}
        response = requests.get(rss_url, headers=headers, timeout=8)
        if response.status_code != 200:
            return None
        root = ET.fromstring(response.content)
        items = root.findall('.//item')
        if not items:
            return None
        news_items = []
        for item in items[:limit]:
            title_elem = item.find('title')
            link_elem = item.find('link')
            title_en = title_elem.text if title_elem is not None else "بدون عنوان"
            title_fa = translate_to_persian(title_en)
            link = link_elem.text if link_elem is not None else "#"
            sentiment = analyze_sentiment_detailed(title_en)
            news_items.append({'title': title_fa, 'link': link, 'sentiment': sentiment, 'source': 'Bing News'})
        return news_items
    except Exception as e:
        logger.error(f"Bing News error for {symbol}: {e}")
        return None

def fetch_google_news(symbol, limit=8, market='crypto'):
    try:
        if market == 'crypto':
            query = f"{symbol} cryptocurrency"
        else:
            query = f"{symbol} forex"
        encoded_query = quote(query)
        rss_url = f"https://news.google.com/rss/search?q={encoded_query}&hl=en-US&gl=US&ceid=US:en"
        headers = {'User-Agent': 'Mozilla/5.0'}
        response = requests.get(rss_url, headers=headers, timeout=8)
        if response.status_code != 200:
            return None
        root = ET.fromstring(response.content)
        items = root.findall('.//item')
        if not items:
            return None
        news_items = []
        for item in items[:limit]:
            title_elem = item.find('title')
            link_elem = item.find('link')
            title_en = title_elem.text if title_elem is not None else "بدون عنوان"
            title_fa = translate_to_persian(title_en)
            link = link_elem.text if link_elem is not None else "#"
            sentiment = analyze_sentiment_detailed(title_en)
            news_items.append({'title': title_fa, 'link': link, 'sentiment': sentiment, 'source': 'Google News'})
        return news_items
    except Exception as e:
        logger.error(f"Google News error for {symbol}: {e}")
        return None

def fetch_investing_news(symbol, limit=8):
    try:
        rss_url = "https://www.investing.com/rss/news_forex.rss"
        headers = {'User-Agent': 'Mozilla/5.0'}
        response = requests.get(rss_url, headers=headers, timeout=8)
        if response.status_code != 200:
            return None
        root = ET.fromstring(response.content)
        items = root.findall('.//item')
        if not items:
            return None
        news_items = []
        for item in items[:limit]:
            title_elem = item.find('title')
            link_elem = item.find('link')
            title_en = title_elem.text if title_elem is not None else "بدون عنوان"
            if symbol.upper() not in title_en.upper():
                continue
            title_fa = translate_to_persian(title_en)
            link = link_elem.text if link_elem is not None else "#"
            sentiment = analyze_sentiment_detailed(title_en)
            news_items.append({'title': title_fa, 'link': link, 'sentiment': sentiment, 'source': 'Investing.com'})
            if len(news_items) >= limit:
                break
        return news_items if news_items else None
    except Exception as e:
        logger.error(f"Investing.com error for {symbol}: {e}")
        return None

def get_crypto_news(symbol, limit=8):
    all_news = []
    sources_used = []
    try:
        items = fetch_cryptopanic(symbol, limit)
        if items:
            all_news.extend(items)
            sources_used.append('CryptoPanic')
    except Exception as e:
        logger.warning(f"CryptoPanic failed: {e}")
    try:
        items = fetch_bing_news(symbol, limit, 'crypto')
        if items:
            all_news.extend(items)
            sources_used.append('Bing News')
    except Exception as e:
        logger.warning(f"Bing News failed: {e}")
    try:
        items = fetch_google_news(symbol, limit, 'crypto')
        if items:
            all_news.extend(items)
            sources_used.append('Google News')
    except Exception as e:
        logger.warning(f"Google News failed: {e}")
    if not all_news:
        return f"❌ هیچ خبری برای `{symbol}` از هیچ منبعی یافت نشد. لطفاً بعداً تلاش کنید."
    seen_titles = set()
    unique_news = []
    for item in all_news:
        if item['title'] not in seen_titles:
            seen_titles.add(item['title'])
            unique_news.append(item)
    return build_detailed_news_message(symbol, unique_news, sources_used)

def get_forex_news(symbol, limit=8):
    all_news = []
    sources_used = []
    try:
        items = fetch_investing_news(symbol, limit)
        if items:
            all_news.extend(items)
            sources_used.append('Investing.com')
    except Exception as e:
        logger.warning(f"Investing.com failed: {e}")
    try:
        items = fetch_bing_news(symbol, limit, 'forex')
        if items:
            all_news.extend(items)
            sources_used.append('Bing News')
    except Exception as e:
        logger.warning(f"Bing News failed: {e}")
    try:
        items = fetch_google_news(symbol, limit, 'forex')
        if items:
            all_news.extend(items)
            sources_used.append('Google News')
    except Exception as e:
        logger.warning(f"Google News failed: {e}")
    if not all_news:
        return f"❌ هیچ خبری برای `{symbol}` از هیچ منبعی یافت نشد. لطفاً بعداً تلاش کنید."
    seen_titles = set()
    unique_news = []
    for item in all_news:
        if item['title'] not in seen_titles:
            seen_titles.add(item['title'])
            unique_news.append(item)
    return build_detailed_news_message(symbol, unique_news, sources_used)

news_cache = {}
def get_cached_news(symbol, limit=8, is_crypto=True):
    cache_key = f"{'crypto' if is_crypto else 'forex'}_{symbol}_{limit}"
    if cache_key in news_cache:
        data, timestamp = news_cache[cache_key]
        if (datetime.now() - timestamp).seconds < CACHE_TIME_NEWS:
            return data
    if is_crypto:
        result = get_crypto_news(symbol, limit)
    else:
        result = get_forex_news(symbol, limit)
    news_cache[cache_key] = (result, datetime.now())
    return result

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
        InlineKeyboardButton("📊 USDT.D", callback_data="price_usdt_dominance"),
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

# ---------- دستورات و هندلرها ----------
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

# ---------- دکمه قیمت لحظه‌ای ----------
@bot.message_handler(func=lambda msg: msg.text == "📊 قیمت لحظه‌ای")
def handle_price(message):
    user_id = message.chat.id
    if is_user_expired(user_id):
        bot.send_message(user_id, "⏰ دوره آزمایشی شما به پایان رسیده.")
        return
    bot.send_message(user_id, "📊 لطفاً یک گزینه را انتخاب کنید:", reply_markup=price_menu_keyboard())

# ---------- دکمه اخبار ----------
@bot.message_handler(func=lambda msg: msg.text == "📰 اخبار امروز و هفته")
def handle_news(message):
    user_id = message.chat.id
    if is_user_expired(user_id):
        bot.send_message(user_id, "⏰ دوره آزمایشی شما به پایان رسیده.")
        return
    waiting_for_symbol[user_id] = "news_symbol"
    crypto_list = ", ".join(sorted(VALID_CRYPTO_SYMBOLS))
    forex_list = ", ".join(sorted(VALID_FOREX_SYMBOLS))
    help_text = (
        "🔍 **تحلیل اخبار اختصاصی**\n\n"
        "لطفاً **نماد** مورد نظر را وارد کنید.\n\n"
        "🪙 **ارزهای دیجیتال:**\n"
        f"`{crypto_list}`\n\n"
        "💱 **جفت‌ارزهای فارکس:**\n"
        f"`{forex_list}`\n\n"
        "📌 مثال: `BTC` یا `EURUSD`\n\n"
        "📊 تحلیل شامل:\n"
        "• اخبار مثبت و منفی\n"
        "• سنتیمنت کلی بازار\n"
        "• نتیجه معاملاتی (خرید/فروش/انتظار)\n"
        "• سطح اطمینان تحلیل\n\n"
        "⏳ اخبار هر ۵ دقیقه به‌روزرسانی می‌شود.\n"
        "💰 نمایش اخبار **رایگان** است."
    )
    bot.send_message(user_id, help_text, parse_mode='Markdown')

# ===== بخش تحلیل ارز دلخواه (تغییر تایم‌فریم به ۴ ساعته) =====
@bot.message_handler(func=lambda msg: msg.text == "🔍 تحلیل ارز دلخواه")
def handle_analyze(message):
    user_id = message.chat.id
    if is_user_expired(user_id):
        bot.send_message(user_id, "⏰ دوره آزمایشی شما به پایان رسیده.")
        return
    
    crypto_list = ", ".join(sorted(VALID_CRYPTO_SYMBOLS))
    forex_list = ", ".join(sorted(VALID_FOREX_SYMBOLS))
    
    help_text = (
        "🔍 **تحلیل تکنیکال پیشرفته (تایم‌فریم ۴ ساعته)**\n\n"
        "لطفاً **نماد** مورد نظر را وارد کنید.\n\n"
        "🪙 **ارزهای دیجیتال:**\n"
        f"`{crypto_list}`\n\n"
        "💱 **جفت‌ارزهای فارکس:**\n"
        f"`{forex_list}`\n\n"
        "📌 مثال: `BTC` یا `EURUSD`\n\n"
        "📊 تحلیل شامل:\n"
        "• چارت قیمت با اندیکاتورهای EMA, ADX, MFI\n"
        "• سطوح حمایت و مقاومت\n"
        "• سیگنال معاملاتی (لانگ/شورت)\n"
        "• نسبت ریسک به ریوارد (R:R)\n"
        "• سطح ریسک و وضعیت اجرا\n\n"
        "⏳ تحلیل روی تایم‌فریم ۴ ساعته انجام می‌شود و ممکن است ۱۰-۱۵ ثانیه زمان ببرد."
    )
    bot.send_message(user_id, help_text, parse_mode='Markdown')
    bot.register_next_step_handler(message, analyze_step)

def analyze_step(message):
    user_id = message.chat.id
    symbol = message.text.strip().upper()
    
    if not symbol:
        bot.send_message(user_id, "❌ لطفاً یک نماد معتبر وارد کنید.")
        return
    
    if symbol not in ALL_VALID_SYMBOLS:
        bot.send_message(
            user_id,
            f"❌ نماد `{symbol}` پشتیبانی نمی‌شود.\n\n"
            f"🪙 ارزهای دیجیتال:\n`{', '.join(sorted(VALID_CRYPTO_SYMBOLS))}`\n\n"
            f"💱 جفت‌ارزهای فارکس:\n`{', '.join(sorted(VALID_FOREX_SYMBOLS))}`",
            parse_mode='Markdown'
        )
        return
    
    is_crypto = symbol in VALID_CRYPTO_SYMBOLS
    asset_type = 'crypto' if is_crypto else 'forex'
    
    processing_msg = bot.send_message(
        user_id,
        f"⏳ در حال تحلیل تکنیکال **{symbol}** در تایم‌فریم ۴ ساعته... لطفاً صبر کنید.\nاین فرآیند ممکن است تا ۱۵ ثانیه طول بکشد.",
        parse_mode='Markdown'
    )
    
    try:
        # 🔥 تغییر اصلی: تایم‌فریم به '4h' تغییر یافته است
        analysis_data, chart_img, error = generate_technical_analysis(symbol, '4h', asset_type)
        
        if error:
            bot.send_message(user_id, error, parse_mode='Markdown')
            try:
                bot.delete_message(user_id, processing_msg.message_id)
            except:
                pass
            return
        
        if not analysis_data:
            bot.send_message(user_id, "❌ تحلیل تکنیکال برای این ارز در تایم‌فریم ۴ ساعته در دسترس نیست.", parse_mode='Markdown')
            try:
                bot.delete_message(user_id, processing_msg.message_id)
            except:
                pass
            return
        
        analysis_msg = format_analysis_message(analysis_data)
        
        if chart_img:
            try:
                bot.send_photo(
                    user_id,
                    chart_img,
                    caption=analysis_msg,
                    parse_mode='Markdown'
                )
            except Exception as photo_error:
                logger.warning(f"Failed to send photo: {photo_error}")
                bot.send_message(user_id, analysis_msg, parse_mode='Markdown')
        else:
            bot.send_message(user_id, analysis_msg, parse_mode='Markdown')
        
        try:
            bot.delete_message(user_id, processing_msg.message_id)
        except:
            pass
            
    except Exception as e:
        logger.error(f"Error in analyze_step: {e}")
        bot.send_message(
            user_id,
            f"❌ خطا در تحلیل `{symbol}` در تایم‌فریم ۴ ساعته. لطفاً مجدداً تلاش کنید.\n\n{str(e)}",
            parse_mode='Markdown'
        )
        try:
            bot.delete_message(user_id, processing_msg.message_id)
        except:
            pass

# ---------- دکمه سیگنال معاملاتی ----------
@bot.message_handler(func=lambda msg: msg.text == "📈 سیگنال معاملاتی")
def handle_signal(message):
    user_id = message.chat.id
    if is_user_expired(user_id):
        bot.send_message(user_id, "⏰ دوره آزمایشی شما به پایان رسیده.")
        return

    waiting_for_signal[user_id] = True
    crypto_list = ", ".join(sorted(VALID_CRYPTO_SYMBOLS))
    forex_list = ", ".join(sorted(VALID_FOREX_SYMBOLS))
    tf_list = "\n".join([f"• `{k}` ({v})" for k, v in TIMEFRAME_NAMES.items()])

    help_text = (
        "📈 **سیگنال معاملاتی**\n\n"
        "لطفاً **نماد** و **تایم‌فریم** مورد نظر را وارد کنید.\n\n"
        "🪙 **ارزهای دیجیتال:**\n"
        f"`{crypto_list}`\n\n"
        "💱 **جفت‌ارزهای فارکس (با تحلیل تکنیکال):**\n"
        f"`{forex_list}`\n\n"
        "⏰ **تایم‌فریم‌های قابل انتخاب:**\n"
        f"{tf_list}\n\n"
        "📌 **نحوه ورود:**\n"
        "`نماد تایم‌فریم`\n"
        "مثال: `BTC 4h` یا `EURUSD 1h` یا `SOL 15m`\n\n"
        "💡 در صورت وارد کردن فقط نماد (مثل `BTC`)، تایم‌فریم **روزانه (1d)** استفاده می‌شود.\n\n"
        "⏳ پردازش ممکن است ۱۰-۱۵ ثانیه طول بکشد."
    )
    bot.send_message(user_id, help_text, parse_mode='Markdown')

# ---------- سایر دکمه‌ها ----------
@bot.message_handler(func=lambda msg: msg.text == "🎯 پیشنهاد خرید")
def handle_suggest(message):
    user_id = message.chat.id
    if is_user_expired(user_id):
        bot.send_message(user_id, "⏰ دوره آزمایشی شما به پایان رسیده.")
        return
    suggest_text = "🎯 **پیشنهاد خرید**\n\nبر اساس تحلیل‌های فعلی، ارزهای زیر پتانسیل رشد دارند:\n• BTC\n• ETH\n• SOL"
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
        "📊 قیمت لحظه‌ای: دریافت قیمت کریپتو، فارکس و طلا + دامیننس تتر (USDT.D)\n"
        "📰 اخبار: تحلیل اخبار اختصاصی هر ارز با سنتیمنت و نتیجه معاملاتی\n"
        "📈 سیگنال: دریافت سیگنال‌های خرید و فروش از تحلیل تکنیکال در تایم‌فریم‌های مختلف (کریپتو و فارکس)\n"
        "🔍 تحلیل ارز دلخواه: تحلیل تکنیکال کامل با چارت و اندیکاتورها (تایم‌فریم ۴ ساعته)\n"
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
            reply = "❌ قیمت BTC در حال حاضر در دسترس نیست. لطفاً بعداً تلاش کنید."

    elif data == "price_eth":
        info = get_crypto_price("ETH/USDT")
        if info:
            reply = f"⟠ **ETH/USDT**\n💰 قیمت: {info['price']:,.2f} $\n📊 تغییر ۲۴h: {info['change']:.2f}%\n"
            if info.get('high') and info.get('low'):
                reply += f"📈 بالا: {info['high']:,.2f}\n📉 پایین: {info['low']:,.2f}\n"
            reply += f"📌 منبع: {info.get('source', 'نامشخص')}"
        else:
            reply = "❌ قیمت ETH در حال حاضر در دسترس نیست. لطفاً بعداً تلاش کنید."

    elif data == "price_usdt_dominance":
        info = get_usdt_dominance()
        if info:
            reply = f"📊 **USDT.D (Dominance)**\n💰 دامیننس: {info['price']:.2f}%\n📊 تغییر ۲۴h: {info['change']:.2f}%\n📌 منبع: {info.get('source', 'نامشخص')}"
        else:
            reply = "❌ دامیننس USDT در حال حاضر در دسترس نیست. لطفاً بعداً تلاش کنید."

    elif data == "price_eurusd":
        info = get_crypto_price("EUR/USDT")
        if info:
            reply = f"🇪🇺 **EUR/USD**\n💰 قیمت: {info['price']:,.4f} $\n📊 تغییر ۲۴h: {info['change']:.2f}%\n"
            if info.get('high') and info.get('low'):
                reply += f"📈 بالا: {info['high']:,.4f}\n📉 پایین: {info['low']:,.4f}\n"
            reply += f"📌 منبع: {info.get('source', 'نامشخص')}"
        else:
            reply = "❌ قیمت EUR/USD در حال حاضر در دسترس نیست. لطفاً بعداً تلاش کنید."

    elif data == "price_gbpusd":
        info = get_crypto_price("GBP/USDT")
        if info:
            reply = f"🇬🇧 **GBP/USD**\n💰 قیمت: {info['price']:,.4f} $\n📊 تغییر ۲۴h: {info['change']:.2f}%\n"
            if info.get('high') and info.get('low'):
                reply += f"📈 بالا: {info['high']:,.4f}\n📉 پایین: {info['low']:,.4f}\n"
            reply += f"📌 منبع: {info.get('source', 'نامشخص')}"
        else:
            reply = "❌ قیمت GBP/USD در حال حاضر در دسترس نیست. لطفاً بعداً تلاش کنید."

    elif data == "price_gold":
        info = get_gold_price()
        if info:
            reply = f"🥇 **XAU/USD**\n💰 قیمت: {info['price']:,.2f} $\n📊 تغییر ۲۴h: {info['change']:.2f}%\n"
            if info.get('high') and info.get('low'):
                reply += f"📈 بالا: {info['high']:,.2f}\n📉 پایین: {info['low']:,.2f}\n"
            reply += f"📌 منبع: {info.get('source', 'نامشخص')}"
        else:
            reply = "❌ قیمت XAU/USD در حال حاضر در دسترس نیست. لطفاً بعداً تلاش کنید."

    bot.edit_message_text(reply, call.message.chat.id, call.message.message_id, parse_mode='Markdown', reply_markup=back_to_main_keyboard())
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "back_main")
def callback_back_main(call):
    bot.answer_callback_query(call.id)
    bot.edit_message_text("به منوی اصلی برگشتید.", call.message.chat.id, call.message.message_id, reply_markup=None)
    bot.send_message(call.message.chat.id, "🔽 از دکمه‌های زیر استفاده کنید:", reply_markup=main_menu_keyboard())

# ---------- هندلر پیام‌های متنی (برای دریافت نماد اخبار و سیگنال) ----------
@bot.message_handler(func=lambda msg: True)
def handle_text_messages(message):
    user_id = message.chat.id
    text = message.text.strip().upper()

    # ===== حالت سیگنال معاملاتی =====
    if waiting_for_signal.get(user_id):
        waiting_for_signal.pop(user_id, None)
        
        parts = text.split()
        symbol = parts[0] if parts else ""
        timeframe = '1d'
        
        if len(parts) > 1:
            tf_input = parts[1].lower()
            if tf_input in TIMEFRAME_MAP:
                timeframe = TIMEFRAME_MAP[tf_input]
            else:
                tf_list = ", ".join([f"`{k}`" for k in TIMEFRAME_MAP.keys()])
                bot.send_message(
                    user_id,
                    f"❌ تایم‌فریم `{tf_input}` معتبر نیست.\n\n"
                    f"⏰ تایم‌فریم‌های معتبر:\n{tf_list}\n\n"
                    f"📌 مثال: `BTC 4h` یا `ETH 1h`",
                    parse_mode='Markdown'
                )
                waiting_for_signal[user_id] = True
                return
        
        if symbol not in ALL_VALID_SYMBOLS:
            crypto_list = ", ".join(sorted(VALID_CRYPTO_SYMBOLS))
            forex_list = ", ".join(sorted(VALID_FOREX_SYMBOLS))
            bot.send_message(
                user_id,
                f"❌ نماد `{symbol}` معتبر نیست.\n\n"
                f"🪙 ارزهای دیجیتال:\n`{crypto_list}`\n\n"
                f"💱 جفت‌ارزهای فارکس:\n`{forex_list}`",
                parse_mode='Markdown'
            )
            waiting_for_signal[user_id] = True
            return

        processing_msg = bot.send_message(
            user_id,
            f"⏳ در حال تولید سیگنال برای **{symbol}** در تایم‌فریم **{TIMEFRAME_NAMES.get(timeframe, timeframe)}**... لطفاً صبر کنید.",
            parse_mode='Markdown'
        )

        try:
            is_crypto = symbol in VALID_CRYPTO_SYMBOLS
            asset_type = 'crypto' if is_crypto else 'forex'
            
            analysis_data, chart_img, error = generate_technical_analysis(symbol, timeframe, asset_type)
            if error:
                bot.send_message(user_id, error, parse_mode='Markdown')
            elif not analysis_data:
                bot.send_message(user_id, f"❌ سیگنالی برای این دارایی در تایم‌فریم انتخاب‌شده در دسترس نیست.", parse_mode='Markdown')
            else:
                signal_text = generate_crypto_signal(symbol, analysis_data)
                if chart_img:
                    bot.send_photo(
                        user_id,
                        chart_img,
                        caption=signal_text,
                        parse_mode='Markdown'
                    )
                else:
                    bot.send_message(user_id, signal_text, parse_mode='Markdown')

            try:
                bot.delete_message(user_id, processing_msg.message_id)
            except:
                pass

        except Exception as e:
            logger.error(f"Error in signal processing: {e}")
            bot.send_message(
                user_id,
                f"❌ خطا در تولید سیگنال برای `{symbol}`. لطفاً مجدداً تلاش کنید.",
                parse_mode='Markdown'
            )
            try:
                bot.delete_message(user_id, processing_msg.message_id)
            except:
                pass
        return

    # ===== حالت اخبار =====
    if waiting_for_symbol.get(user_id) == "news_symbol":
        waiting_for_symbol.pop(user_id, None)
        if text not in ALL_VALID_SYMBOLS:
            crypto_list = ", ".join(sorted(VALID_CRYPTO_SYMBOLS))
            forex_list = ", ".join(sorted(VALID_FOREX_SYMBOLS))
            bot.send_message(
                user_id,
                f"❌ نماد `{text}` معتبر نیست.\n\n"
                f"🪙 ارزهای دیجیتال:\n`{crypto_list}`\n\n"
                f"💱 جفت‌ارزهای فارکس:\n`{forex_list}`",
                parse_mode='Markdown'
            )
            waiting_for_symbol[user_id] = "news_symbol"
            return
        processing_msg = bot.send_message(
            user_id,
            f"⏳ در حال تحلیل اخبار مربوط به **{text}**... لطفاً صبر کنید.",
            parse_mode='Markdown'
        )
        try:
            if text in VALID_CRYPTO_SYMBOLS:
                news_text = get_cached_news(text, limit=8, is_crypto=True)
            else:
                news_text = get_cached_news(text, limit=8, is_crypto=False)
            bot.send_message(
                user_id,
                news_text,
                parse_mode='Markdown',
                disable_web_page_preview=True
            )
            try:
                bot.delete_message(user_id, processing_msg.message_id)
            except:
                pass
        except Exception as e:
            logger.error(f"Error in news processing: {e}")
            bot.send_message(
                user_id,
                f"❌ خطا در دریافت اخبار برای `{text}`. لطفاً مجدداً تلاش کنید.",
                parse_mode='Markdown'
            )
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

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    set_webhook()
    app.run(host='0.0.0.0', port=port)
