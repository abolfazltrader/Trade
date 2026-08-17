import sqlite3
import threading
import time
import requests
import uuid
import logging
from collections import defaultdict
from datetime import datetime, timedelta
from flask import Flask, request, jsonify
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
import os

# ---------- تنظیمات لاگ ----------
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ---------- تنظیمات امنیتی و متغیرهای محیطی ----------
TOKEN = os.environ.get("TELEGRAM_TOKEN")
if not TOKEN:
    raise ValueError("TOKEN environment variable not set!")

BOT_USERNAME = "Crypto_forex_2026_bot"
BASE_URL = "https://trade-i4js.onrender.com"

# دریافت لیست ادمین‌ها از Environment Variables (به صورت کاما جدا)
ADMIN_IDS_STR = os.environ.get('ADMIN_IDS', '')
ADMIN_IDS = [int(id.strip()) for id in ADMIN_IDS_STR.split(',') if id.strip()] if ADMIN_IDS_STR else [8521463103, 5333419558]

bot = telebot.TeleBot(TOKEN)
app = Flask(__name__)

# محدودیت نرخ ارسال (Rate Limiting) - تاخیر بین پیام‌ها به ثانیه
RATE_LIMIT_DELAY = 0.3

# محدودیت نرخ کلیک (Click Rate Limiting) - جلوگیری از کلیک‌های سریع
CLICK_COOLDOWN = 1  # زمان مورد نیاز بین دو کلیک (ثانیه)
user_last_click = defaultdict(float)

def is_rate_limited(user_id):
    """بررسی می‌کند که کاربر در بازه زمانی مجاز کلیک کرده است یا خیر"""
    now = time.time()
    if now - user_last_click[user_id] < CLICK_COOLDOWN:
        return True
    user_last_click[user_id] = now
    return False

# ---------- دیتابیس (سازگار با PostgreSQL و SQLite) ----------
DATABASE_URL = os.environ.get('DATABASE_URL')

if DATABASE_URL:
    # استفاده از PostgreSQL در Render
    import psycopg2
    from psycopg2.extras import RealDictCursor
    
    conn = psycopg2.connect(DATABASE_URL, sslmode='require')
    c = conn.cursor()
    
    # ایجاد جدول‌ها (بدون DROP TABLE تا داده‌ها از بین نروند)
    c.execute("""CREATE TABLE IF NOT EXISTS users (
        telegram_id BIGINT PRIMARY KEY,
        link_code TEXT UNIQUE,
        user_name TEXT
    )""")
    
    c.execute("""CREATE TABLE IF NOT EXISTS pending_reports (
        id SERIAL PRIMARY KEY,
        link_code TEXT,
        owner_id BIGINT,
        clicker_id BIGINT,
        message_id BIGINT,
        expires_at TIMESTAMP,
        cancelled BOOLEAN DEFAULT FALSE
    )""")
    
    c.execute("""CREATE TABLE IF NOT EXISTS cancel_payments (
        id SERIAL PRIMARY KEY,
        report_id INTEGER,
        user_id BIGINT,
        authority TEXT UNIQUE,
        amount INTEGER,
        status TEXT DEFAULT 'pending',
        created_at TIMESTAMP
    )""")
    
    c.execute("""CREATE TABLE IF NOT EXISTS user_photos (
        user_id BIGINT PRIMARY KEY,
        photo_id TEXT
    )""")
    
    c.execute("""CREATE TABLE IF NOT EXISTS trapped_history (
        id SERIAL PRIMARY KEY,
        owner_id BIGINT,
        clicker_id BIGINT,
        clicker_name TEXT,
        clicker_username TEXT,
        trapped_at TEXT
    )""")
    
    # جدول محدودیت پیام ناشناس (ارسال اولیه)
    c.execute("""CREATE TABLE IF NOT EXISTS anonymous_limits (
        user_id BIGINT PRIMARY KEY,
        date TEXT,
        count INTEGER DEFAULT 0
    )""")
    
    # جدول محدودیت پاسخ‌های ناشناس
    c.execute("""CREATE TABLE IF NOT EXISTS reply_limits (
        user_id BIGINT PRIMARY KEY,
        date TEXT,
        count INTEGER DEFAULT 0
    )""")
    
    # جدول کاربران بلاک‌کننده
    c.execute("""CREATE TABLE IF NOT EXISTS blocked_users (
        user_id BIGINT PRIMARY KEY,
        blocked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")
    
    conn.commit()
    logger.info("Connected to PostgreSQL successfully!")
    
    # تعریف تابع lastrowid برای PostgreSQL
    def get_lastrowid():
        return c.lastrowid

else:
    # استفاده از SQLite برای تست محلی
    conn = sqlite3.connect("tracker.db", check_same_thread=False)
    c = conn.cursor()
    
    c.execute("""CREATE TABLE IF NOT EXISTS users (
        telegram_id INTEGER PRIMARY KEY,
        link_code TEXT UNIQUE,
        user_name TEXT
    )""")
    
    c.execute("""CREATE TABLE IF NOT EXISTS pending_reports (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        link_code TEXT,
        owner_id INTEGER,
        clicker_id INTEGER,
        message_id INTEGER,
        expires_at DATETIME,
        cancelled BOOLEAN DEFAULT FALSE
    )""")
    
    c.execute("""CREATE TABLE IF NOT EXISTS cancel_payments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        report_id INTEGER,
        user_id INTEGER,
        authority TEXT UNIQUE,
        amount INTEGER,
        status TEXT DEFAULT 'pending',
        created_at DATETIME
    )""")
    
    c.execute("""CREATE TABLE IF NOT EXISTS user_photos (
        user_id INTEGER PRIMARY KEY,
        photo_id TEXT
    )""")
    
    c.execute("""CREATE TABLE IF NOT EXISTS trapped_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        owner_id INTEGER,
        clicker_id INTEGER,
        clicker_name TEXT,
        clicker_username TEXT,
        trapped_at TEXT
    )""")
    
    # جدول محدودیت پیام ناشناس (ارسال اولیه)
    c.execute("""CREATE TABLE IF NOT EXISTS anonymous_limits (
        user_id INTEGER PRIMARY KEY,
        date TEXT,
        count INTEGER DEFAULT 0
    )""")
    
    # جدول محدودیت پاسخ‌های ناشناس
    c.execute("""CREATE TABLE IF NOT EXISTS reply_limits (
        user_id INTEGER PRIMARY KEY,
        date TEXT,
        count INTEGER DEFAULT 0
    )""")
    
    # جدول کاربران بلاک‌کننده
    c.execute("""CREATE TABLE IF NOT EXISTS blocked_users (
        user_id INTEGER PRIMARY KEY,
        blocked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")
    
    conn.commit()
    logger.info("Connected to SQLite successfully!")
    
    def get_lastrowid():
        return c.lastrowid

# ========== مدیریت مجدد اتصال دیتابیس ==========
def ensure_db_connection():
    """بررسی و بازیابی اتصال دیتابیس"""
    global conn, c
    try:
        c.execute("SELECT 1")
        c.fetchone()
        return True
    except Exception as e:
        logger.warning(f"Database connection lost: {e}. Reconnecting...")
        try:
            if DATABASE_URL:
                conn = psycopg2.connect(DATABASE_URL, sslmode='require')
                c = conn.cursor()
                logger.info("Database reconnected successfully!")
                return True
            else:
                conn = sqlite3.connect("tracker.db", check_same_thread=False)
                c = conn.cursor()
                logger.info("SQLite reconnected successfully!")
                return True
        except Exception as reconnect_error:
            logger.error(f"Failed to reconnect to database: {reconnect_error}")
            return False

# محدودیت اندازه دیکشنری‌های موقت
MAX_TEMP_SIZE = 500

def safe_dict_add(dictionary, key, value, max_size=MAX_TEMP_SIZE):
    """افزودن امن به دیکشنری با محدودیت اندازه"""
    if len(dictionary) >= max_size:
        # حذف اولین آیتم (FIFO)
        first_key = next(iter(dictionary))
        del dictionary[first_key]
    dictionary[key] = value

anonymous_temp = {}
reply_temp = {}  # دیکشنری موقت برای پاسخ ناشناس
ad_temp = {}  # دیکشنری موقت برای ذخیره اسم و لینک تبلیغات

# ========== پاک‌سازی خودکار دیکشنری‌های موقت ==========
def schedule_cleanup(user_id, dictionary):
    """حذف آیتم از دیکشنری پس از ۵ دقیقه"""
    def cleanup():
        if user_id in dictionary:
            del dictionary[user_id]
            logger.info(f"Cleaned up expired entry for user {user_id}")
    timer = threading.Timer(300, cleanup)  # 300 ثانیه = ۵ دقیقه
    timer.daemon = True  # تا با خروج برنامه بسته شود
    timer.start()

# ========== توابع کمکی برای ارسال با تلاش مجدد ==========
def send_message_safe(chat_id, text, **kwargs):
    time.sleep(RATE_LIMIT_DELAY)
    max_retries = 3
    for attempt in range(max_retries):
        try:
            return bot.send_message(chat_id, text, **kwargs)
        except Exception as e:
            error_str = str(e)
            if "Connection aborted" in error_str or "Connection reset" in error_str:
                logger.warning(f"Connection error on attempt {attempt+1}: {e}")
                time.sleep(2)
                continue
            elif "Forbidden: bot was blocked by the user" in error_str:
                try:
                    if DATABASE_URL:
                        c.execute("INSERT INTO blocked_users (user_id) VALUES (%s) ON CONFLICT (user_id) DO NOTHING", (chat_id,))
                    else:
                        c.execute("INSERT OR IGNORE INTO blocked_users (user_id) VALUES (?)", (chat_id,))
                    conn.commit()
                    logger.info(f"User {chat_id} marked as blocked.")
                except Exception as db_err:
                    logger.error(f"Error saving blocked user {chat_id}: {db_err}")
                return None
            else:
                logger.error(f"Error sending message to {chat_id}: {e}")
                return None
    logger.error(f"Failed to send message after {max_retries} attempts")
    return None

def send_photo_safe(chat_id, photo, **kwargs):
    time.sleep(RATE_LIMIT_DELAY)
    max_retries = 3
    for attempt in range(max_retries):
        try:
            return bot.send_photo(chat_id, photo, **kwargs)
        except Exception as e:
            error_str = str(e)
            if "Connection aborted" in error_str or "Connection reset" in error_str:
                logger.warning(f"Connection error on attempt {attempt+1}: {e}")
                time.sleep(2)
                continue
            elif "Forbidden: bot was blocked by the user" in error_str:
                try:
                    if DATABASE_URL:
                        c.execute("INSERT INTO blocked_users (user_id) VALUES (%s) ON CONFLICT (user_id) DO NOTHING", (chat_id,))
                    else:
                        c.execute("INSERT OR IGNORE INTO blocked_users (user_id) VALUES (?)", (chat_id,))
                    conn.commit()
                except:
                    pass
                return None
            else:
                logger.error(f"Error sending photo to {chat_id}: {e}")
                return None
    return None

def edit_message_text_safe(chat_id, message_id, text, **kwargs):
    time.sleep(RATE_LIMIT_DELAY)
    try:
        return bot.edit_message_text(text, chat_id, message_id, **kwargs)
    except Exception as e:
        if "Forbidden: bot was blocked by the user" in str(e):
            try:
                if DATABASE_URL:
                    c.execute("INSERT INTO blocked_users (user_id) VALUES (%s) ON CONFLICT (user_id) DO NOTHING", (chat_id,))
                else:
                    c.execute("INSERT OR IGNORE INTO blocked_users (user_id) VALUES (?)", (chat_id,))
                conn.commit()
            except:
                pass
        logger.error(f"Error editing message {message_id} in chat {chat_id}: {e}")
        return None

def delete_message_safe(chat_id, message_id):
    time.sleep(RATE_LIMIT_DELAY)
    try:
        return bot.delete_message(chat_id, message_id)
    except Exception as e:
        if "Forbidden: bot was blocked by the user" in str(e):
            try:
                if DATABASE_URL:
                    c.execute("INSERT INTO blocked_users (user_id) VALUES (%s) ON CONFLICT (user_id) DO NOTHING", (chat_id,))
                else:
                    c.execute("INSERT OR IGNORE INTO blocked_users (user_id) VALUES (?)", (chat_id,))
                conn.commit()
            except:
                pass
        logger.error(f"Error deleting message {message_id} in chat {chat_id}: {e}")
        return None

def edit_message_reply_markup_safe(chat_id, message_id, reply_markup):
    time.sleep(RATE_LIMIT_DELAY)
    try:
        return bot.edit_message_reply_markup(chat_id, message_id, reply_markup=reply_markup)
    except Exception as e:
        logger.error(f"Error editing reply markup: {e}")
        return None

# ========== توابع محدودیت پیام ناشناس ==========
MAX_ANONYMOUS_PER_DAY = 10
MAX_REPLIES_PER_DAY = 10

def can_send_anonymous(user_id):
    """بررسی می‌کند که کاربر امروز مجاز به ارسال پیام ناشناس است یا خیر"""
    try:
        today = datetime.now().strftime('%Y-%m-%d')
        if DATABASE_URL:
            c.execute("SELECT date, count FROM anonymous_limits WHERE user_id = %s", (user_id,))
        else:
            c.execute("SELECT date, count FROM anonymous_limits WHERE user_id = ?", (user_id,))
        row = c.fetchone()
        
        if row:
            date, count = row
            if date == today:
                if count >= MAX_ANONYMOUS_PER_DAY:
                    return False, count
                # به‌روزرسانی تعداد
                if DATABASE_URL:
                    c.execute("UPDATE anonymous_limits SET count = count + 1 WHERE user_id = %s", (user_id,))
                else:
                    c.execute("UPDATE anonymous_limits SET count = count + 1 WHERE user_id = ?", (user_id,))
                conn.commit()
                return True, count + 1
            else:
                # روز جدید است
                if DATABASE_URL:
                    c.execute("UPDATE anonymous_limits SET date = %s, count = 1 WHERE user_id = %s", (today, user_id))
                else:
                    c.execute("UPDATE anonymous_limits SET date = ?, count = 1 WHERE user_id = ?", (today, user_id))
                conn.commit()
                return True, 1
        else:
            # اولین پیام امروز
            if DATABASE_URL:
                c.execute("INSERT INTO anonymous_limits (user_id, date, count) VALUES (%s, %s, 1)", (user_id, today))
            else:
                c.execute("INSERT INTO anonymous_limits (user_id, date, count) VALUES (?, ?, 1)", (user_id, today))
            conn.commit()
            return True, 1
    except Exception as e:
        logger.error(f"Error in can_send_anonymous: {e}")
        return False, 0

def can_send_reply(user_id):
    """بررسی می‌کند که کاربر امروز مجاز به ارسال پاسخ ناشناس است یا خیر"""
    try:
        today = datetime.now().strftime('%Y-%m-%d')
        if DATABASE_URL:
            c.execute("SELECT date, count FROM reply_limits WHERE user_id = %s", (user_id,))
        else:
            c.execute("SELECT date, count FROM reply_limits WHERE user_id = ?", (user_id,))
        row = c.fetchone()
        
        if row:
            date, count = row
            if date == today:
                if count >= MAX_REPLIES_PER_DAY:
                    return False, count
                # به‌روزرسانی تعداد
                if DATABASE_URL:
                    c.execute("UPDATE reply_limits SET count = count + 1 WHERE user_id = %s", (user_id,))
                else:
                    c.execute("UPDATE reply_limits SET count = count + 1 WHERE user_id = ?", (user_id,))
                conn.commit()
                return True, count + 1
            else:
                # روز جدید است
                if DATABASE_URL:
                    c.execute("UPDATE reply_limits SET date = %s, count = 1 WHERE user_id = %s", (today, user_id))
                else:
                    c.execute("UPDATE reply_limits SET date = ?, count = 1 WHERE user_id = ?", (today, user_id))
                conn.commit()
                return True, 1
        else:
            # اولین پاسخ امروز
            if DATABASE_URL:
                c.execute("INSERT INTO reply_limits (user_id, date, count) VALUES (%s, %s, 1)", (user_id, today))
            else:
                c.execute("INSERT INTO reply_limits (user_id, date, count) VALUES (?, ?, 1)", (user_id, today))
            conn.commit()
            return True, 1
    except Exception as e:
        logger.error(f"Error in can_send_reply: {e}")
        return False, 0

# ========== توابع کمکی برای آمار ==========
def get_total_users():
    c.execute("SELECT COUNT(*) FROM users")
    return c.fetchone()[0]

def get_total_reports():
    c.execute("SELECT COUNT(*) FROM pending_reports WHERE cancelled = FALSE")
    return c.fetchone()[0]

def get_total_trapped():
    c.execute("SELECT COUNT(*) FROM trapped_history")
    return c.fetchone()[0]

def get_total_photos():
    c.execute("SELECT COUNT(*) FROM user_photos")
    return c.fetchone()[0]

def get_total_blocked():
    c.execute("SELECT COUNT(*) FROM blocked_users")
    return c.fetchone()[0]

# ========== پنل اصلی ==========
def main_panel(user_id, message_id=None):
    keyboard = ReplyKeyboardMarkup(row_width=2, resize_keyboard=True, one_time_keyboard=False)
    btn_get_link = KeyboardButton("🔗 دریافت لینک من")
    btn_buy_apple = KeyboardButton("🍎 خرید سیر")
    btn_set_photo = KeyboardButton("🖼 تنظیم عکس مچ گیری")
    btn_del_photo = KeyboardButton("🗑 حذف عکس مچ گیری")
    btn_trapped_list = KeyboardButton("📋 کاربران در تله افتاده اخیر")
    btn_help = KeyboardButton("❓ راهنما")
    
    keyboard.add(btn_get_link)
    keyboard.add(btn_buy_apple)
    keyboard.add(btn_set_photo, btn_del_photo)
    keyboard.add(btn_trapped_list, btn_help)
    
    panel_text = f"📱 **پنل کاربری**\n\n👤 کاربر: {get_owner_name(user_id)}\n\n❗️ **یک گزینه را انتخاب کنید...**"
    if message_id:
        try:
            edit_message_text_safe(user_id, message_id, panel_text, parse_mode='Markdown')
            send_message_safe(user_id, "🔽 از دکمه‌های زیر استفاده کنید:", reply_markup=keyboard)
        except:
            send_message_safe(user_id, panel_text, reply_markup=keyboard, parse_mode='Markdown')
    else:
        send_message_safe(user_id, panel_text, reply_markup=keyboard, parse_mode='Markdown')

# ---------- توابع اصلی ----------
def generate_link(telegram_id):
    code = str(telegram_id)
    if DATABASE_URL:
        # PostgreSQL
        c.execute("""INSERT INTO users (telegram_id, link_code) 
                     VALUES (%s, %s) 
                     ON CONFLICT (telegram_id) DO UPDATE SET link_code = EXCLUDED.link_code""", (telegram_id, code))
    else:
        # SQLite
        c.execute("INSERT OR REPLACE INTO users (telegram_id, link_code) VALUES (?, ?)", (telegram_id, code))
    conn.commit()
    return f"https://t.me/{BOT_USERNAME}?start=track_{code}"

def get_owner_id_by_code(code):
    try:
        return int(code)
    except:
        return None

def get_owner_name(owner_id):
    if DATABASE_URL:
        c.execute("SELECT user_name FROM users WHERE telegram_id = %s", (owner_id,))
    else:
        c.execute("SELECT user_name FROM users WHERE telegram_id = ?", (owner_id,))
    row = c.fetchone()
    if row and row[0]:
        return row[0]
    try:
        chat = bot.get_chat(owner_id)
        name = f"{chat.first_name or ''} {chat.last_name or ''}".strip()
        if name:
            if DATABASE_URL:
                c.execute("UPDATE users SET user_name = %s WHERE telegram_id = %s", (name, owner_id))
            else:
                c.execute("UPDATE users SET user_name = ? WHERE telegram_id = ?", (name, owner_id))
            conn.commit()
        return name if name else "کاربر"
    except:
        return "کاربر ناشناس"

def get_clicker_name(clicker_id):
    try:
        chat = bot.get_chat(clicker_id)
        return f"{chat.first_name or ''} {chat.last_name or ''}".strip()
    except:
        return "کاربر ناشناس"

def save_trapped_history(owner_id, clicker_id, clicker_name, clicker_username):
    try:
        trapped_time = datetime.now().isoformat()
        if DATABASE_URL:
            c.execute("INSERT INTO trapped_history (owner_id, clicker_id, clicker_name, clicker_username, trapped_at) VALUES (%s, %s, %s, %s, %s)",
                      (owner_id, clicker_id, clicker_name, clicker_username, trapped_time))
        else:
            c.execute("INSERT INTO trapped_history (owner_id, clicker_id, clicker_name, clicker_username, trapped_at) VALUES (?, ?, ?, ?, ?)",
                      (owner_id, clicker_id, clicker_name, clicker_username, trapped_time))
        conn.commit()
    except Exception as e:
        logger.error(f"Error saving trapped history: {e}")

# ========== تابع اصلاح‌شده برای ارسال پیام تله ==========
def delete_message_later(chat_id, message_id, delay, clicker_id, owner_name, report_id):
    time.sleep(delay)
    
    # بررسی پرداخت
    if DATABASE_URL:
        c.execute("SELECT status FROM cancel_payments WHERE report_id = %s AND status = 'paid'", (report_id,))
    else:
        c.execute("SELECT status FROM cancel_payments WHERE report_id = ? AND status = 'paid'", (report_id,))
    if c.fetchone():
        return
    
    # حذف پیام
    try:
        delete_message_safe(chat_id, message_id)
    except:
        pass
    
    # بررسی اینکه گزارش لغو نشده باشد
    if DATABASE_URL:
        c.execute("SELECT cancelled FROM pending_reports WHERE id = %s", (report_id,))
    else:
        c.execute("SELECT cancelled FROM pending_reports WHERE id = ?", (report_id,))
    result = c.fetchone()
    
    if result and result[0] == False:
        if DATABASE_URL:
            c.execute("SELECT owner_id, clicker_id FROM pending_reports WHERE id = %s", (report_id,))
        else:
            c.execute("SELECT owner_id, clicker_id FROM pending_reports WHERE id = ?", (report_id,))
        row = c.fetchone()
        
        if row:
            owner_id, clicker_id = row
            clicker_name = get_clicker_name(clicker_id)
            
            # ذخیره در تاریخچه
            try:
                chat = bot.get_chat(clicker_id)
                username = chat.username if chat.username else None
                save_trapped_history(owner_id, clicker_id, clicker_name, username)
            except:
                save_trapped_history(owner_id, clicker_id, clicker_name, None)
            
            # ========== ساخت دکمه‌ها با callback_data صحیح ==========
            keyboard = InlineKeyboardMarkup(row_width=2)
            
            # استفاده از str() برای اطمینان از تبدیل به رشته
            anon_callback = f"anon_{str(clicker_id)}_{str(owner_id)}"
            bio_callback = f"bio_{str(clicker_id)}_{str(owner_id)}"
            pv_callback = f"pv_{str(clicker_id)}_{str(owner_id)}"
            photo_callback = f"photo_{str(clicker_id)}_{str(owner_id)}"
            
            logger.info(f"🔗 Creating buttons with: {anon_callback}, {bio_callback}, {pv_callback}, {photo_callback}")
            
            keyboard.add(
                InlineKeyboardButton("💬 پیام ناشناس", callback_data=anon_callback),
                InlineKeyboardButton("📝 بیوگرافی", callback_data=bio_callback),
                InlineKeyboardButton("📨 پیوی", callback_data=pv_callback),
                InlineKeyboardButton("🖼 عکس پروفایل", callback_data=photo_callback)
            )
            
            report_msg = f"🎯 **یک فضول در تله افتاد!**\n\n👤 نام: {clicker_name}\n⏰ زمان: {datetime.now().strftime('%H:%M:%S')}"
            
            # ارسال پیام تله به صاحب لینک
            try:
                sent_msg = send_message_safe(owner_id, report_msg, parse_mode='Markdown', reply_markup=keyboard)
                if sent_msg:
                    logger.info(f"✅ Trap message sent to owner {owner_id} with buttons")
                else:
                    logger.error(f"❌ send_message_safe returned None for owner {owner_id}")
            except Exception as e:
                logger.error(f"❌ Failed to send trap message to {owner_id}: {e}")
            
            # پیام نهایی به کلیکر
            final_message = f"⏰ **زمان شما تمام شد!**\n\nگزارش فضولی شما به {owner_name} ارسال گردید.\n\n❗️ **از پنل زیر استفاده کنید:**"
            try:
                send_message_safe(clicker_id, final_message, parse_mode='Markdown')
                main_panel(clicker_id)
            except Exception as e:
                logger.error(f"❌ Failed to send final message to {clicker_id}: {e}")

# ---------- هندلر استارت (با پشتیبانی از لینک تبلیغاتی) ----------
@bot.message_handler(commands=['start'])
def start(message):
    user_id = message.from_user.id
    text = message.text
    name = message.from_user.first_name
    if message.from_user.last_name:
        name += " " + message.from_user.last_name
    
    if DATABASE_URL:
        c.execute("""INSERT INTO users (telegram_id, link_code, user_name) 
                     VALUES (%s, %s, %s) 
                     ON CONFLICT (telegram_id) DO NOTHING""", (user_id, str(user_id), name))
    else:
        c.execute("INSERT OR IGNORE INTO users (telegram_id, link_code, user_name) VALUES (?, ?, ?)",
                  (user_id, str(user_id), name))
    conn.commit()
    
    # ========== لینک تبلیغاتی (ad) ==========
    if text == "/start ad":
        keyboard = InlineKeyboardMarkup(row_width=2)
        keyboard.add(
            InlineKeyboardButton("💬 پیام ناشناس", callback_data="ad_anon"),
            InlineKeyboardButton("📝 بیوگرافی", callback_data="ad_bio"),
            InlineKeyboardButton("📨 پیوی", callback_data="ad_pv"),
            InlineKeyboardButton("🖼 عکس پروفایل", callback_data="ad_photo")
        )
        
        ad_welcome = (
            "👋 **به ربات «کی داره نگاه میکنه؟» خوش اومدی!** 😂\n\n"
            "با این ربات می‌تونی بفهمی چه کسانی پروفایلت رو چک می‌کنن!\n\n"
            "🔹 **چطور کار میکنه؟**\n"
            "1️⃣ یک لینک اختصاصی از ربات می‌گیری\n"
            "2️⃣ لینک رو توی بیوگرافی یا کانالت می‌ذاری\n"
            "3️⃣ هرکی کلیک کنه، می‌فهمی کی بوده! 😉\n\n"
            "👇 **برای شروع، روی یکی از دکمه‌های زیر کلیک کن:**"
        )
        
        send_message_safe(user_id, ad_welcome, reply_markup=keyboard, parse_mode='Markdown')
        return
    
    # ========== لینک معمولی (track_xxx) ==========
    if text.startswith("/start track_"):
        code = text.split("track_")[1]
        owner_id = get_owner_id_by_code(code)
        clicker_id = user_id
        if owner_id and owner_id != clicker_id:
            owner_name = get_owner_name(owner_id)
            keyboard = InlineKeyboardMarkup()
            keyboard.add(InlineKeyboardButton("❌ عدم ارسال گزارش فضولی", callback_data=f"cancel_{code}_{clicker_id}"))
            
            if DATABASE_URL:
                c.execute("SELECT photo_id FROM user_photos WHERE user_id = %s", (owner_id,))
            else:
                c.execute("SELECT photo_id FROM user_photos WHERE user_id = ?", (owner_id,))
            photo_row = c.fetchone()
            trap_photo = photo_row[0] if photo_row and photo_row[0] else None
            
            trap_text = f"⚠️ **نباید این فضولی رو میکردی!**\n\nالان این فضولیت برای {owner_name} ارسال شد، بهتره قبل از اینکه بیاد ببینه، خودت بهش بگی داشتی فضولی میکردی 😊\n\nبرای عدم ارسال دکمه زیر را فشار دهید (فرصت شما 1 دقیقه و 15 ثانیه)"
            
            if trap_photo:
                msg = send_photo_safe(clicker_id, trap_photo, caption=trap_text, reply_markup=keyboard, parse_mode='Markdown')
            else:
                msg = send_message_safe(clicker_id, trap_text, reply_markup=keyboard, parse_mode='Markdown')
            
            expires_at = datetime.now() + timedelta(seconds=75)
            if DATABASE_URL:
                c.execute("INSERT INTO pending_reports (link_code, owner_id, clicker_id, message_id, expires_at) VALUES (%s, %s, %s, %s, %s) RETURNING id",
                          (code, owner_id, clicker_id, msg.message_id, expires_at))
                report_id = c.fetchone()[0]
            else:
                c.execute("INSERT INTO pending_reports (link_code, owner_id, clicker_id, message_id, expires_at) VALUES (?, ?, ?, ?, ?)",
                          (code, owner_id, clicker_id, msg.message_id, expires_at))
                report_id = c.lastrowid
            conn.commit()
            
            threading.Thread(target=delete_message_later, args=(clicker_id, msg.message_id, 75, clicker_id, owner_name, report_id)).start()
        elif owner_id == clicker_id:
            send_message_safe(clicker_id, "⚠️ این لینک مال خودته!")
            main_panel(clicker_id)
        else:
            send_message_safe(clicker_id, "❌ لینک نامعتبر!")
            main_panel(clicker_id)
    else:
        main_panel(user_id)

# ---------- دریافت لینک من ----------
@bot.message_handler(func=lambda message: message.text == "🔗 دریافت لینک من")
def handle_get_my_link(message):
    user_id = message.from_user.id
    link = generate_link(user_id)
    keyboard = InlineKeyboardMarkup()
    keyboard.add(InlineKeyboardButton("📋 کپی لینک", callback_data=f"copy_link_{link}"))
    keyboard.add(InlineKeyboardButton("🔙 بازگشت به پنل", callback_data="back_to_panel"))
    send_message_safe(user_id, f"🔗 **لینک اختصاصی شما:**\n\n`{link}`\n\n✅ این لینک مخصوص شماست.\n📌 آن را در بیوگرافی یا کانال خود قرار دهید.\n⚠️ هر کسی روی این لینک کلیک کند، در تله می‌افتد!",
                     reply_markup=keyboard, parse_mode='Markdown')

@bot.callback_query_handler(func=lambda call: call.data.startswith("copy_link_"))
def copy_link_callback(call):
    bot.answer_callback_query(call.id, "✅ لینک با موفقیت کپی شد! (روی لینک نگه دارید و کپی کنید)", show_alert=True)

# ---------- دکمه‌های پنل ----------
@bot.message_handler(func=lambda message: message.text == "🍎 خرید سیر")
def handle_buy_apple(message):
    user_id = message.from_user.id
    hide_keyboard = ReplyKeyboardRemove()
    send_message_safe(user_id, "🍎 **خرید سیر**\n\nاین قابلیت به زودی اضافه می‌شود.\nبرای بازگشت به پنل، روی /start کلیک کنید.", reply_markup=hide_keyboard, parse_mode='Markdown')
    threading.Timer(2, lambda: main_panel(user_id)).start()

@bot.message_handler(func=lambda message: message.text == "🖼 تنظیم عکس مچ گیری")
def handle_set_photo(message):
    user_id = message.from_user.id
    hide_keyboard = ReplyKeyboardRemove()
    send_message_safe(user_id, "🖼 **تنظیم عکس مچ گیری**\n\nلطفاً عکس مورد نظر خود را ارسال کنید (حداکثر ۵ مگابایت):", reply_markup=hide_keyboard, parse_mode='Markdown')
    bot.register_next_step_handler(message, save_photo)

def save_photo(message):
    user_id = message.from_user.id
    if message.photo:
        # بررسی حجم عکس (دریافت اطلاعات فایل)
        file_info = bot.get_file(message.photo[-1].file_id)
        if file_info.file_size > 5 * 1024 * 1024:  # 5 مگابایت
            send_message_safe(user_id, "❌ حجم عکس بیش از ۵ مگابایت است. لطفاً عکس کوچک‌تری ارسال کنید.")
            main_panel(user_id)
            return
        
        file_id = message.photo[-1].file_id
        if DATABASE_URL:
            c.execute("INSERT INTO user_photos (user_id, photo_id) VALUES (%s, %s) ON CONFLICT (user_id) DO UPDATE SET photo_id = EXCLUDED.photo_id", (user_id, file_id))
        else:
            c.execute("INSERT OR REPLACE INTO user_photos (user_id, photo_id) VALUES (?, ?)", (user_id, file_id))
        conn.commit()
        send_message_safe(user_id, "✅ عکس مچ‌گیری شما با موفقیت ذخیره شد!\nاز این پس هنگام کلیک روی لینک شما، این عکس نمایش داده می‌شود.")
    else:
        send_message_safe(user_id, "❌ لطفاً یک عکس معتبر ارسال کنید.")
    main_panel(user_id)

# ========== دکمه حذف عکس مچ گیری ==========
@bot.message_handler(func=lambda message: message.text == "🗑 حذف عکس مچ گیری")
def delete_trap_photo(message):
    user_id = message.from_user.id
    if DATABASE_URL:
        c.execute("DELETE FROM user_photos WHERE user_id = %s", (user_id,))
    else:
        c.execute("DELETE FROM user_photos WHERE user_id = ?", (user_id,))
    conn.commit()
    send_message_safe(user_id, "🗑 عکس مچ‌گیری شما با موفقیت حذف شد.\nاز این پس هنگام کلیک روی لینک شما، عکسی نمایش داده نمی‌شود.")
    main_panel(user_id)

# ========== دکمه نمایش کاربران در تله افتاده اخیر ==========
@bot.message_handler(func=lambda message: message.text == "📋 کاربران در تله افتاده اخیر")
def show_trapped_list(message):
    user_id = message.from_user.id
    try:
        if DATABASE_URL:
            c.execute("SELECT clicker_name, clicker_username, trapped_at FROM trapped_history WHERE owner_id = %s ORDER BY trapped_at DESC LIMIT 20", (user_id,))
        else:
            c.execute("SELECT clicker_name, clicker_username, trapped_at FROM trapped_history WHERE owner_id = ? ORDER BY trapped_at DESC LIMIT 20", (user_id,))
        rows = c.fetchall()
        if not rows:
            send_message_safe(user_id, "📭 هیچ کاربری تا کنون در تله شما نیفتاده است.")
            return
        text = "📋 لیست کاربرانی که در تله شما افتاده‌اند (اخیر):\n\n"
        for i, row in enumerate(rows, 1):
            name = row[0] if row[0] else "نامشخص"
            username = row[1] if row[1] else "ندارد"
            trapped_str = row[2]
            try:
                if 'T' in trapped_str:
                    trapped_dt = datetime.fromisoformat(trapped_str)
                else:
                    trapped_dt = datetime.strptime(trapped_str, '%Y-%m-%d %H:%M:%S.%f')
            except:
                time_str = trapped_str
            else:
                time_str = trapped_dt.strftime('%Y/%m/%d %H:%M:%S')
            text += f"{i}. 👤 نام: {name}\n🆔 یوزرنیم: @{username if username != 'ندارد' else 'ندارد'}\n📅 زمان: {time_str}\n\n"
        if len(text) > 4000:
            parts = [text[i:i+4000] for i in range(0, len(text), 4000)]
            for part in parts:
                send_message_safe(user_id, part)
        else:
            send_message_safe(user_id, text)
    except Exception as e:
        send_message_safe(user_id, f"❌ خطا در نمایش تاریخچه: {e}")
        logger.error(f"Error in show_trapped_list: {e}")

# ========== دکمه راهنما ==========
@bot.message_handler(func=lambda message: message.text == "❓ راهنما")
def handle_help(message):
    user_id = message.from_user.id
    help_text = (
        "📚 **راهنمای جامع استفاده از ربات**\n\n"
        "با سلام و احترام. به بخش راهنمای ربات خوش آمدید. در این بخش با تمامی امکانات و نحوه عملکرد دقیق ربات آشنا خواهید شد:\n\n"
        "**۱. نحوه کارکرد ربات (سیستم مچ‌گیری):**\n"
        "شما می‌توانید با دریافت لینک اختصاصی خود از طریق ربات و قرار دادن آن در بخش بیوگرافی (Bio) حساب کاربری‌تان، متوجه شوید چه کسانی در حال بازدید از پروفایل شما هستند.\n"
        "🔹 *نکته:* امکان قرار دادن این لینک به صورت مخفی (Hyperlink) در بیوگرافی وجود دارد.\n"
        "به محض اینکه شخصی از روی کنجکاوی روی لینک شما کلیک کرده و وارد ربات شود، ربات فوراً پیامی با مضمون «یک فضول در تله افتاد!» برای شما ارسال می‌کند. این گزارش شامل اطلاعات کامل شخص است:\n"
        "▫️ نام کاربر\n"
        "▫️ آیدی (لینک ورود به پیوی)\n"
        "▫️ عکس پروفایل\n"
        "▫️ بیوگرافی (در صورت وجود)\n\n"
        "**۲. امکانات رایگان (بدون نیاز به اشتراک):**\n"
        "🔹 **ارسال پیام ناشناس:** می‌توانید از طریق ربات، برای شخصی که در تله شما افتاده است به صورت کاملاً ناشناس پیام ارسال کنید.\n"
        "🔹 **مشاهده بیوگرافی و عکس پروفایل و آیدی کاربران:** همه این قابلیت‌ها به صورت رایگان در دسترس است.\n\n"
        "**۳. شخصی‌سازی تله (عکس مچ‌گیری):**\n"
        "شما می‌توانید واکنش ربات به فردی که در تله می‌افتد را شخصی‌سازی کنید:\n"
        "🔹 **تنظیم عکس مچ‌گیری:** یک تصویر دلخواه تنظیم کنید تا به محض ورود شخص، آن عکس نیز برای وی ارسال شود.\n\n"
        "💬 در صورت بروز هرگونه مشکل یا داشتن سوالات بیشتر، با @Asd00120A در ارتباط باشید."
    )
    keyboard = InlineKeyboardMarkup()
    keyboard.add(InlineKeyboardButton("🔙 بازگشت به پنل", callback_data="back_to_panel"))
    hide_keyboard = ReplyKeyboardRemove()
    send_message_safe(user_id, help_text, reply_markup=keyboard, parse_mode='Markdown')

# ========== پیام ناشناس رایگان (با قابلیت پاسخ و محدودیت ۱۰ عدد در روز برای ارسال اولیه) ==========
@bot.callback_query_handler(func=lambda call: call.data.startswith("anon_"))
def anonymous_message(call):
    logger.info(f"💬 ANON CALLBACK RECEIVED: {call.data} from {call.from_user.id}")
    try:
        # بررسی محدودیت نرخ کلیک
        if is_rate_limited(call.from_user.id):
            bot.answer_callback_query(call.id, "⏳ لطفاً کمی صبر کنید!", show_alert=True)
            return

        # پارس کردن داده با split('_', 2) برای جلوگیری از خطا
        parts = call.data.split('_', 2)
        if len(parts) < 3:
            bot.answer_callback_query(call.id, "❌ داده نامعتبر!", show_alert=True)
            return
        _, clicker_id_str, owner_id_str = parts
        clicker_id = int(clicker_id_str)
        owner_id = int(owner_id_str)
        user_id = call.from_user.id

        if user_id != owner_id:
            bot.answer_callback_query(call.id, "این دکمه فقط برای صاحب لینک قابل استفاده است!", show_alert=True)
            return

        # بررسی محدودیت پیام ناشناس برای کاربر
        allowed, count = can_send_anonymous(user_id)
        if not allowed:
            bot.answer_callback_query(call.id, f"❌ شما امروز {MAX_ANONYMOUS_PER_DAY} پیام ناشناس ارسال کرده‌اید. فردا دوباره امتحان کنید.", show_alert=True)
            return

        safe_dict_add(anonymous_temp, user_id, clicker_id)
        schedule_cleanup(user_id, anonymous_temp)

        try:
            edit_message_reply_markup_safe(call.message.chat.id, call.message.message_id, reply_markup=None)
        except:
            pass

        cancel_keyboard = InlineKeyboardMarkup()
        cancel_keyboard.add(InlineKeyboardButton("❌ انصراف", callback_data="cancel_anonymous"))
        send_message_safe(user_id, f"💬 **ارسال پیام ناشناس** (امروز {count} از {MAX_ANONYMOUS_PER_DAY} ارسال)\n\nلطفاً متن پیام خود را ارسال کنید.\nاین پیام **به صورت ناشناس** برای کاربر فضول فرستاده خواهد شد.\n\n⚠️ توجه: نام و اطلاعات شما فاش نمی‌شود.",
                         reply_markup=cancel_keyboard, parse_mode='Markdown')
        bot.register_next_step_handler_by_chat_id(user_id, receive_anonymous_message, clicker_id, owner_id)
        bot.answer_callback_query(call.id, "✅")
    except Exception as e:
        logger.error(f"Error in anonymous_message: {e}")
        bot.answer_callback_query(call.id, f"❌ خطا: {str(e)}", show_alert=True)
        send_message_safe(call.message.chat.id, f"❌ خطا در ارسال پیام ناشناس: {str(e)}")

def receive_anonymous_message(message, clicker_id, owner_id):
    user_id = message.from_user.id
    if user_id not in anonymous_temp:
        return
    if message.text:
        anonymous_text = message.text
        try:
            keyboard = InlineKeyboardMarkup()
            keyboard.add(InlineKeyboardButton("💬 پاسخ ناشناس", callback_data=f"reply_anon_{clicker_id}_{owner_id}"))
            
            send_message_safe(
                clicker_id,
                f"💌 **پیام ناشناس**\n\nیک کاربر ناشناس به شما پیام داده است:\n\n「 {anonymous_text} 」\n\n🔹 شما می‌توانید با دکمه زیر پاسخ دهید.",
                reply_markup=keyboard,
                parse_mode='Markdown'
            )
            send_message_safe(user_id, "✅ **پیام شما با موفقیت ارسال شد!**\n\nپیام شما به صورت ناشناس برای کاربر فضول فرستاده شد.", parse_mode='Markdown')
        except Exception as e:
            send_message_safe(user_id, f"❌ **خطا در ارسال پیام**\n\nکاربر فضول ممکن است ربات را بلاک کرده باشد.\n\nخطا: {e}", parse_mode='Markdown')
    else:
        send_message_safe(user_id, "❌ لطفاً فقط متن ارسال کنید.", parse_mode='Markdown')
    anonymous_temp.pop(user_id, None)
    main_panel(user_id)

@bot.callback_query_handler(func=lambda call: call.data == "cancel_anonymous")
def cancel_anonymous(call):
    user_id = call.from_user.id
    anonymous_temp.pop(user_id, None)
    try:
        delete_message_safe(call.message.chat.id, call.message.message_id)
    except:
        pass
    send_message_safe(user_id, "❌ عملیات ارسال پیام ناشناس لغو شد.", parse_mode='Markdown')
    main_panel(user_id)

# ========== هندلر پاسخ به پیام ناشناس (با اعمال محدودیت ۱۰ پاسخ در روز) ==========
@bot.callback_query_handler(func=lambda call: call.data.startswith("reply_anon_"))
def reply_anonymous(call):
    # بررسی محدودیت نرخ کلیک
    if is_rate_limited(call.from_user.id):
        bot.answer_callback_query(call.id, "⏳ لطفاً کمی صبر کنید!", show_alert=True)
        return
    
    try:
        parts = call.data.split('_', 3)
        if len(parts) < 4:
            bot.answer_callback_query(call.id, "❌ داده نامعتبر!", show_alert=True)
            return
        _, _, clicker_id_str, owner_id_str = parts
        clicker_id = int(clicker_id_str)
        owner_id = int(owner_id_str)
        user_id = call.from_user.id

        # فقط گیرنده پیام اصلی (clicker) می‌تونه پاسخ بده
        if user_id != clicker_id:
            bot.answer_callback_query(call.id, "این دکمه برای شما نیست!", show_alert=True)
            return

        # ===== اعمال محدودیت برای پاسخ‌های ناشناس =====
        allowed, count = can_send_reply(user_id)
        if not allowed:
            bot.answer_callback_query(call.id, f"❌ شما امروز {MAX_REPLIES_PER_DAY} پاسخ ناشناس ارسال کرده‌اید. فردا دوباره امتحان کنید.", show_alert=True)
            return

        # ذخیره در دیکشنری موقت
        safe_dict_add(reply_temp, user_id, {"target": owner_id, "source": clicker_id})
        schedule_cleanup(user_id, reply_temp)

        # حذف دکمه‌های پیام قبلی
        try:
            edit_message_reply_markup_safe(call.message.chat.id, call.message.message_id, reply_markup=None)
        except Exception as e:
            logger.error(f"Error editing reply markup: {e}")

        # دکمه انصراف
        cancel_keyboard = InlineKeyboardMarkup()
        cancel_keyboard.add(InlineKeyboardButton("❌ انصراف", callback_data="cancel_reply"))

        # ارسال پیام برای دریافت متن پاسخ با نمایش تعداد باقی‌مانده
        remaining = MAX_REPLIES_PER_DAY - count
        send_message_safe(
            user_id,
            f"💬 **ارسال پاسخ ناشناس** (امروز {count} از {MAX_REPLIES_PER_DAY} ارسال - {remaining} تا باقی‌مانده)\n\nلطفاً متن پاسخ خود را ارسال کنید.\nاین پیام **به صورت ناشناس** برای کاربر قبلی فرستاده خواهد شد.\n\n⚠️ توجه: نام و اطلاعات شما فاش نمی‌شود.",
            reply_markup=cancel_keyboard,
            parse_mode='Markdown'
        )

        # ثبت هندلر مرحله بعد
        bot.register_next_step_handler_by_chat_id(user_id, receive_reply_message, clicker_id, owner_id)
        bot.answer_callback_query(call.id, "✅ پیام خود را ارسال کنید.")

    except Exception as e:
        bot.answer_callback_query(call.id, f"❌ خطا: {str(e)}", show_alert=True)
        logger.error(f"Error in reply_anonymous: {e}")

# ========== دریافت پاسخ از کاربر و ارسال به صاحب لینک (با دکمه پاسخ برای ادامه) ==========
def receive_reply_message(message, clicker_id, owner_id):
    user_id = message.from_user.id

    # بررسی اینکه آیا این کاربر درخواست پاسخ داده
    if user_id not in reply_temp:
        send_message_safe(user_id, "⏳ شما درخواست پاسخ نداده‌اید یا زمان آن تمام شده است.")
        main_panel(user_id)
        return

    # اگر پیام متنی نبود
    if not message.text:
        send_message_safe(user_id, "❌ لطفاً فقط متن ارسال کنید.", parse_mode='Markdown')
        # دوباره منتظر پیام متنی باشیم
        bot.register_next_step_handler_by_chat_id(user_id, receive_reply_message, clicker_id, owner_id)
        return

    reply_text = message.text.strip()
    if not reply_text:
        send_message_safe(user_id, "❌ متن نمی‌تواند خالی باشد. دوباره ارسال کنید.")
        bot.register_next_step_handler_by_chat_id(user_id, receive_reply_message, clicker_id, owner_id)
        return

    try:
        # ارسال پاسخ به owner (صاحب لینک) با دکمه پاسخ برای ادامه گفتگو
        keyboard = InlineKeyboardMarkup()
        # استفاده از دکمه "anon" با همان پارامترها تا owner بتواند پاسخ دهد
        keyboard.add(InlineKeyboardButton("💬 پاسخ ناشناس", callback_data=f"anon_{clicker_id}_{owner_id}"))

        send_message_safe(
            owner_id,
            f"💌 **پاسخ ناشناس**\n\nکاربر فضول به شما پاسخ داده است:\n\n「 {reply_text} 」\n\n🔹 شما می‌توانید با دکمه زیر پاسخ دهید.",
            reply_markup=keyboard,
            parse_mode='Markdown'
        )
        send_message_safe(user_id, "✅ **پاسخ شما با موفقیت ارسال شد!**", parse_mode='Markdown')
    except Exception as e:
        send_message_safe(user_id, f"❌ **خطا در ارسال پاسخ**\n\nخطا: {e}", parse_mode='Markdown')
        logger.error(f"Error sending reply: {e}")

    # پاک کردن دیکشنری موقت
    if user_id in reply_temp:
        del reply_temp[user_id]

    # بازگشت به پنل اصلی کاربر
    main_panel(user_id)

# ========== لغو پاسخ ناشناس ==========
@bot.callback_query_handler(func=lambda call: call.data == "cancel_reply")
def cancel_reply(call):
    user_id = call.from_user.id
    if user_id in reply_temp:
        del reply_temp[user_id]
    try:
        delete_message_safe(call.message.chat.id, call.message.message_id)
    except:
        pass
    send_message_safe(user_id, "❌ عملیات ارسال پاسخ لغو شد.", parse_mode='Markdown')
    main_panel(user_id)
    bot.answer_callback_query(call.id, "✅ لغو شد")

# ========== دکمه‌های تله و پرداخت تستی ==========
@bot.callback_query_handler(func=lambda call: call.data.startswith("cancel_"))
def cancel_report_payment_page(call):
    _, code, clicker_id = call.data.split("_")
    clicker_id = int(clicker_id)
    if call.from_user.id != clicker_id:
        bot.answer_callback_query(call.id, "این دکمه مال تو نیست!", show_alert=True)
        return
    
    # بررسی محدودیت نرخ کلیک
    if is_rate_limited(call.from_user.id):
        bot.answer_callback_query(call.id, "⏳ لطفاً کمی صبر کنید!", show_alert=True)
        return
    
    if DATABASE_URL:
        c.execute("SELECT id FROM pending_reports WHERE link_code = %s AND clicker_id = %s AND cancelled = FALSE ORDER BY id DESC LIMIT 1", (code, clicker_id))
    else:
        c.execute("SELECT id FROM pending_reports WHERE link_code = ? AND clicker_id = ? AND cancelled = FALSE ORDER BY id DESC LIMIT 1", (code, clicker_id))
    row = c.fetchone()
    if not row:
        bot.answer_callback_query(call.id, "گزارشی یافت نشد!", show_alert=True)
        return
    report_id = row[0]
    try:
        delete_message_safe(call.message.chat.id, call.message.message_id)
    except:
        pass
    keyboard = InlineKeyboardMarkup(row_width=1)
    keyboard.add(InlineKeyboardButton("📄 مشاهده جزئیات", callback_data=f"details_{report_id}"), InlineKeyboardButton("💳 پرداخت", callback_data=f"fake_pay_{report_id}"))
    payment_text = "💳 **درخواست پول**\n\n**لغو ارسال گزارش فضولی**\nبا پرداخت فقط ۶,۵۰۰ تومان، گزارش فضولی شما برای صاحب لینک ارسال نخواهد شد.\n\nلغو گزارش: 65000\nمبلغ: ۶۵,۰۰۰ ریال"
    send_message_safe(call.message.chat.id, payment_text, reply_markup=keyboard, parse_mode='Markdown')

@bot.callback_query_handler(func=lambda call: call.data.startswith("fake_pay_"))
def fake_payment(call):
    _, report_id = call.data.split("_")
    report_id = int(report_id)
    if DATABASE_URL:
        c.execute("UPDATE pending_reports SET cancelled = TRUE WHERE id = %s", (report_id,))
    else:
        c.execute("UPDATE pending_reports SET cancelled = TRUE WHERE id = ?", (report_id,))
    conn.commit()
    bot.answer_callback_query(call.id, "✅ پرداخت با موفقیت انجام شد!")
    try:
        delete_message_safe(call.message.chat.id, call.message.message_id)
    except:
        pass
    send_message_safe(call.message.chat.id, "✅ **پرداخت شما با موفقیت تایید شد!**\n\nگزارش فضولی شما لغو گردید و برای صاحب لینک ارسال نخواهد شد.\n\n🙏 از شما متشکریم.", parse_mode='Markdown')

@bot.callback_query_handler(func=lambda call: call.data.startswith("details_"))
def show_details(call):
    _, report_id = call.data.split("_")
    bot.answer_callback_query(call.id)
    details_msg = "📋 **جزئیات پرداخت**\n\n💰 مبلغ: ۶,۵۰۰ تومان (۶۵,۰۰۰ ریال)\n📝 دلیل: لغو ارسال گزارش فضولی\n⏱ زمان باقی مانده: کمتر از ۷۵ ثانیه\n\nپس از پرداخت موفق، گزارش شما ارسال نخواهد شد."
    send_message_safe(call.message.chat.id, details_msg, parse_mode='Markdown')

@bot.callback_query_handler(func=lambda call: call.data == "back_to_panel")
def back_to_panel_inline(call):
    # بررسی محدودیت نرخ کلیک
    if is_rate_limited(call.from_user.id):
        bot.answer_callback_query(call.id, "⏳ لطفاً کمی صبر کنید!", show_alert=True)
        return
    
    try:
        edit_message_reply_markup_safe(call.message.chat.id, call.message.message_id, reply_markup=None)
    except:
        pass
    main_panel(call.from_user.id)
    bot.answer_callback_query(call.id)

# ========== دریافت لینک از تبلیغات (نمایش پنل کاربری) ==========
@bot.callback_query_handler(func=lambda call: call.data == "get_my_link")
def get_my_link_from_ad(call):
    # بررسی محدودیت نرخ کلیک
    if is_rate_limited(call.from_user.id):
        bot.answer_callback_query(call.id, "⏳ لطفاً کمی صبر کنید!", show_alert=True)
        return
    
    user_id = call.from_user.id
    bot.answer_callback_query(call.id, "✅ در حال آماده‌سازی...")
    try:
        delete_message_safe(call.message.chat.id, call.message.message_id)
    except:
        pass
    main_panel(user_id)

# ========== ۴ دکمه اصلی (بیوگرافی، پیوی، عکس) با مدیریت کامل خطا ==========
@bot.callback_query_handler(func=lambda call: call.data.startswith("bio_"))
def show_bio(call):
    user_id = call.from_user.id
    logger.info(f"📝 bio_ callback received from user {user_id}: {call.data}")
    
    try:
        # اطمینان از اتصال دیتابیس
        if not ensure_db_connection():
            bot.answer_callback_query(call.id, "❌ خطا در اتصال به دیتابیس!", show_alert=True)
            return

        # بررسی محدودیت نرخ کلیک
        if is_rate_limited(user_id):
            bot.answer_callback_query(call.id, "⏳ لطفاً کمی صبر کنید!", show_alert=True)
            return

        # پارس کردن داده
        parts = call.data.split('_', 2)
        if len(parts) < 3:
            bot.answer_callback_query(call.id, "❌ داده نامعتبر!", show_alert=True)
            logger.error(f"Invalid bio data: {call.data}")
            return
            
        _, clicker_id_str, owner_id_str = parts
        clicker_id = int(clicker_id_str)
        owner_id = int(owner_id_str)
        
        logger.info(f"Bio request: user={user_id}, clicker={clicker_id}, owner={owner_id}")

        # بررسی دسترسی
        if user_id != owner_id:
            bot.answer_callback_query(call.id, "این دکمه فقط برای صاحب لینک قابل استفاده است!", show_alert=True)
            return

        # دریافت بیوگرافی
        try:
            chat = bot.get_chat(clicker_id)
            bio = getattr(chat, 'bio', None)
            
            if bio and bio.strip():
                send_message_safe(call.message.chat.id, f"📝 **بیوگرافی کاربر:**\n\n{bio}", parse_mode='Markdown')
            else:
                send_message_safe(call.message.chat.id, "❌ این کاربر بیوگرافی تنظیم نکرده است.")
            
            bot.answer_callback_query(call.id, "✅ بیوگرافی ارسال شد")
            
        except Exception as e:
            error_msg = str(e)
            logger.error(f"Error getting bio for {clicker_id}: {error_msg}")
            
            if "bot was blocked" in error_msg:
                send_message_safe(call.message.chat.id, "❌ کاربر ربات را بلاک کرده است.")
            elif "user not found" in error_msg.lower():
                send_message_safe(call.message.chat.id, "❌ کاربر مورد نظر یافت نشد.")
            else:
                send_message_safe(call.message.chat.id, f"❌ خطا در دریافت بیوگرافی: {error_msg[:100]}")
            
            bot.answer_callback_query(call.id, f"⚠️ خطا: {error_msg[:50]}", show_alert=True)
            
    except Exception as e:
        logger.error(f"🔥 Critical error in show_bio: {e}")
        bot.answer_callback_query(call.id, f"❌ خطای سیستمی: {str(e)[:50]}", show_alert=True)
        try:
            send_message_safe(call.message.chat.id, f"❌ خطا در دریافت بیوگرافی: {str(e)}")
        except:
            pass

@bot.callback_query_handler(func=lambda call: call.data.startswith("pv_"))
def send_pv(call):
    user_id = call.from_user.id
    logger.info(f"📨 pv_ callback received from user {user_id}: {call.data}")
    
    try:
        # اطمینان از اتصال دیتابیس
        if not ensure_db_connection():
            bot.answer_callback_query(call.id, "❌ خطا در اتصال به دیتابیس!", show_alert=True)
            return

        if is_rate_limited(user_id):
            bot.answer_callback_query(call.id, "⏳ لطفاً کمی صبر کنید!", show_alert=True)
            return

        parts = call.data.split('_', 2)
        if len(parts) < 3:
            bot.answer_callback_query(call.id, "❌ داده نامعتبر!", show_alert=True)
            return
            
        _, clicker_id_str, owner_id_str = parts
        clicker_id = int(clicker_id_str)
        owner_id = int(owner_id_str)

        if user_id != owner_id:
            bot.answer_callback_query(call.id, "این دکمه فقط برای صاحب لینک قابل استفاده است!", show_alert=True)
            return

        try:
            chat = bot.get_chat(clicker_id)
            username = chat.username
            
            if username:
                user_info = f"🆔 آیدی کاربر فضول:\n@{username}"
            else:
                user_info = f"🆔 آیدی کاربر فضول:\n{clicker_id}"
                
            send_message_safe(call.message.chat.id, user_info)
            bot.answer_callback_query(call.id, "✅ آیدی ارسال شد")
            
        except Exception as e:
            error_msg = str(e)
            logger.error(f"Error getting user info for {clicker_id}: {error_msg}")
            
            if "bot was blocked" in error_msg:
                send_message_safe(call.message.chat.id, "❌ کاربر ربات را بلاک کرده است.")
            else:
                send_message_safe(call.message.chat.id, f"❌ خطا در دریافت آیدی: {error_msg[:100]}")
            
            bot.answer_callback_query(call.id, f"⚠️ خطا: {error_msg[:50]}", show_alert=True)
            
    except Exception as e:
        logger.error(f"🔥 Critical error in send_pv: {e}")
        bot.answer_callback_query(call.id, f"❌ خطای سیستمی: {str(e)[:50]}", show_alert=True)

@bot.callback_query_handler(func=lambda call: call.data.startswith("photo_"))
def show_photo(call):
    user_id = call.from_user.id
    logger.info(f"🖼 photo_ callback received from user {user_id}: {call.data}")
    
    try:
        # اطمینان از اتصال دیتابیس
        if not ensure_db_connection():
            bot.answer_callback_query(call.id, "❌ خطا در اتصال به دیتابیس!", show_alert=True)
            return

        if is_rate_limited(user_id):
            bot.answer_callback_query(call.id, "⏳ لطفاً کمی صبر کنید!", show_alert=True)
            return

        parts = call.data.split('_', 2)
        if len(parts) < 3:
            bot.answer_callback_query(call.id, "❌ داده نامعتبر!", show_alert=True)
            return
            
        _, clicker_id_str, owner_id_str = parts
        clicker_id = int(clicker_id_str)
        owner_id = int(owner_id_str)

        if user_id != owner_id:
            bot.answer_callback_query(call.id, "این دکمه فقط برای صاحب لینک قابل استفاده است!", show_alert=True)
            return

        try:
            photos = bot.get_user_profile_photos(clicker_id, limit=1)
            
            if photos.total_count > 0:
                file_id = photos.photos[0][-1].file_id
                send_photo_safe(call.message.chat.id, file_id, caption="🖼 عکس پروفایل کاربر")
                bot.answer_callback_query(call.id, "✅ عکس ارسال شد")
            else:
                send_message_safe(call.message.chat.id, "❌ این کاربر عکس پروفایل ندارد.")
                bot.answer_callback_query(call.id, "⚠️ عکسی وجود ندارد")
                
        except Exception as e:
            error_msg = str(e)
            logger.error(f"Error getting photo for {clicker_id}: {error_msg}")
            
            if "bot was blocked" in error_msg:
                send_message_safe(call.message.chat.id, "❌ کاربر ربات را بلاک کرده است.")
            else:
                send_message_safe(call.message.chat.id, f"❌ خطا در دریافت عکس: {error_msg[:100]}")
            
            bot.answer_callback_query(call.id, f"⚠️ خطا: {error_msg[:50]}", show_alert=True)
            
    except Exception as e:
        logger.error(f"🔥 Critical error in show_photo: {e}")
        bot.answer_callback_query(call.id, f"❌ خطای سیستمی: {str(e)[:50]}", show_alert=True)

# ========== دکمه‌های تبلیغاتی (ad) ==========
@bot.callback_query_handler(func=lambda call: call.data.startswith("ad_"))
def ad_buttons(call):
    # بررسی محدودیت نرخ کلیک
    if is_rate_limited(call.from_user.id):
        bot.answer_callback_query(call.id, "⏳ لطفاً کمی صبر کنید!", show_alert=True)
        return
    
    action = call.data.split("_")[1]
    user_id = call.from_user.id
    
    messages = {
        "anon": "💬 **پیام ناشناس**\n\nبا دریافت لینک اختصاصی، می‌تونی به کاربرای فضول پیام ناشناس بدی!",
        "bio": "📝 **بیوگرافی**\n\nبا دریافت لینک اختصاصی، می‌تونی ببینی چه کسانی بیوگرافیت رو چک می‌کنن!",
        "pv": "📨 **پیوی**\n\nبا دریافت لینک اختصاصی، می‌تونی بفهمی چه کسانی پیوی‌ات رو چک می‌کنن!",
        "photo": "🖼 **عکس پروفایل**\n\nببین چه کسانی عکس پروفایلت رو می‌بینن!"
    }
    
    keyboard = InlineKeyboardMarkup()
    keyboard.add(InlineKeyboardButton("🔗 دریافت لینک من", callback_data="get_my_link"))
    
    send_message_safe(
        user_id,
        messages.get(action, "❌ گزینه نامعتبر!"),
        reply_markup=keyboard,
        parse_mode='Markdown'
    )
    
    bot.answer_callback_query(call.id, "✅")

# ========== پنل مدیریت (با دکمه‌های جدید) ==========
@bot.message_handler(commands=['admin'])
def admin_panel(message):
    user_id = message.from_user.id
    if user_id not in ADMIN_IDS:
        bot.reply_to(message, "❌ شما دسترسی به این بخش ندارید!")
        return
    
    keyboard = InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        InlineKeyboardButton("📊 آمار ربات", callback_data="admin_stats"),
        InlineKeyboardButton("👥 لیست کاربران", callback_data="admin_users"),
        InlineKeyboardButton("📋 گزارش‌های تله", callback_data="admin_reports"),
        InlineKeyboardButton("🖼 عکس‌های ذخیره شده", callback_data="admin_photos"),
        InlineKeyboardButton("🚫 کاربران بلاک‌کننده", callback_data="admin_blocked"),
        InlineKeyboardButton("📢 تبلیغات", callback_data="admin_advertise"),
        InlineKeyboardButton("📢 ارسال به همه کاربران", callback_data="admin_broadcast"),
        # InlineKeyboardButton("🗑 پاک کردن دیتابیس", callback_data="admin_clear"),
        InlineKeyboardButton("🔙 بستن پنل", callback_data="admin_close")
    )
    
    text = (
        "🔐 **پنل مدیریت ربات**\n\n"
        "سلام ادمین گرامی! از دکمه‌های زیر استفاده کن:\n\n"
        f"👤 کل کاربران: {get_total_users()}\n"
        f"📊 گزارش‌های فعال: {get_total_reports()}\n"
        f"🎯 کاربران در تله رفته: {get_total_trapped()}\n"
        f"🖼 عکس‌های مچ‌گیری: {get_total_photos()}\n"
        f"🚫 کاربران بلاک‌کننده: {get_total_blocked()}"
    )
    
    send_message_safe(user_id, text, reply_markup=keyboard, parse_mode='Markdown')

# ========== دستورات ادمین برای عیب‌یابی ==========
@bot.message_handler(commands=['dbcheck'])
def db_check_command(message):
    user_id = message.from_user.id
    if user_id not in ADMIN_IDS:
        return
    
    status = ensure_db_connection()
    if status:
        try:
            c.execute("SELECT COUNT(*) FROM users")
            count = c.fetchone()[0]
            send_message_safe(user_id, f"✅ اتصال دیتابیس برقرار است.\nتعداد کاربران: {count}")
        except Exception as e:
            send_message_safe(user_id, f"❌ خطا در خواندن دیتابیس: {e}")
    else:
        send_message_safe(user_id, "❌ اتصال دیتابیس برقرار نیست!")

@bot.message_handler(commands=['setwebhook'])
def set_webhook_command(message):
    user_id = message.from_user.id
    if user_id not in ADMIN_IDS:
        return
    
    bot.remove_webhook()
    time.sleep(1)
    webhook_url = f"{BASE_URL}/webhook"
    if bot.set_webhook(url=webhook_url):
        send_message_safe(user_id, f"✅ Webhook با موفقیت تنظیم شد:\n{webhook_url}")
    else:
        send_message_safe(user_id, f"❌ خطا در تنظیم Webhook:\n{webhook_url}")

# ========== دستور تست کالبک ==========
@bot.message_handler(commands=['testcallback'])
def test_callback_command(message):
    user_id = message.from_user.id
    keyboard = InlineKeyboardMarkup(row_width=1)
    keyboard.add(
        InlineKeyboardButton("🔴 تست کالبک 1", callback_data="test_1"),
        InlineKeyboardButton("🔴 تست کالبک 2", callback_data="test_2"),
        InlineKeyboardButton("🔴 تست با آیدی", callback_data=f"test_{user_id}_{12345}")
    )
    
    send_message_safe(
        user_id,
        "🧪 **تست کالبک‌ها**\n\nروی دکمه‌های زیر کلیک کنید تا ببینیم کالبک کار می‌کند یا نه.\n\nاگر پیام «✅ کالبک کار کرد» را دیدید، یعنی سیستم کالبک سالم است.",
        reply_markup=keyboard,
        parse_mode='Markdown'
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith("test_"))
def test_callback_handler(call):
    user_id = call.from_user.id
    logger.info(f"✅ TEST CALLBACK: {call.data} from {user_id}")
    
    bot.answer_callback_query(call.id, "✅ کالبک با موفقیت کار کرد!", show_alert=True)
    send_message_safe(call.message.chat.id, f"✅ **کالبک تست کار کرد!**\n\nداده: `{call.data}`\nکاربر: {user_id}", parse_mode='Markdown')

@bot.message_handler(commands=['test'])
def test_command(message):
    user_id = message.from_user.id
    # فقط برای ادمین‌ها
    if user_id not in ADMIN_IDS:
        return
    
    # تست اتصال به دیتابیس
    try:
        c.execute("SELECT 1")
        db_status = "✅ اتصال به دیتابیس برقرار است"
    except Exception as e:
        db_status = f"❌ خطا در اتصال به دیتابیس: {e}"
    
    # تست دریافت اطلاعات کاربر
    try:
        chat = bot.get_chat(user_id)
        user_info = f"✅ اطلاعات کاربر: {chat.first_name} (ID: {user_id})"
    except Exception as e:
        user_info = f"❌ خطا در دریافت اطلاعات کاربر: {e}"
    
    # تعداد کاربران
    try:
        total = get_total_users()
        users_count = f"✅ تعداد کاربران: {total}"
    except Exception as e:
        users_count = f"❌ خطا در شمارش کاربران: {e}"
    
    # ارسال نتیجه تست
    test_result = f"🧪 **نتایج تست:**\n\n{db_status}\n{user_info}\n{users_count}\n\nاگر خطایی مشاهده می‌کنید، آن را به ادمین گزارش دهید."
    send_message_safe(user_id, test_result, parse_mode='Markdown')

# ========== هندلر نمایش کاربران بلاک‌کننده ==========
@bot.callback_query_handler(func=lambda call: call.data == "admin_blocked")
def admin_blocked(call):
    user_id = call.from_user.id
    if user_id not in ADMIN_IDS:
        bot.answer_callback_query(call.id, "❌ شما دسترسی ندارید!", show_alert=True)
        return
    
    try:
        delete_message_safe(call.message.chat.id, call.message.message_id)
    except:
        pass
    
    if DATABASE_URL:
        c.execute("SELECT user_id, blocked_at FROM blocked_users ORDER BY blocked_at DESC LIMIT 50")
    else:
        c.execute("SELECT user_id, blocked_at FROM blocked_users ORDER BY blocked_at DESC LIMIT 50")
    
    rows = c.fetchall()
    
    if not rows:
        text = "🚫 هیچ کاربری ربات را بلاک نکرده است."
    else:
        text = "🚫 **لیست کاربرانی که ربات را بلاک کرده‌اند (۵۰ نفر اخیر):**\n\n"
        for uid, blocked_at in rows:
            # تلاش برای دریافت نام کاربر
            try:
                chat = bot.get_chat(uid)
                name = f"{chat.first_name or ''} {chat.last_name or ''}".strip()
                if not name:
                    name = "بدون نام"
            except:
                name = "کاربر ناشناس"
            
            # فرمت زمان
            try:
                if isinstance(blocked_at, str):
                    if 'T' in blocked_at:
                        dt = datetime.fromisoformat(blocked_at.replace('+00:00', ''))
                    else:
                        dt = datetime.strptime(blocked_at, '%Y-%m-%d %H:%M:%S')
                else:
                    dt = blocked_at
                time_str = dt.strftime('%Y/%m/%d %H:%M')
            except:
                time_str = str(blocked_at)
            
            text += f"• {name} (ID: `{uid}`) - {time_str}\n"
    
    keyboard = InlineKeyboardMarkup()
    keyboard.add(InlineKeyboardButton("🔙 بازگشت به پنل", callback_data="back_to_panel"))
    
    if len(text) > 4000:
        parts = [text[i:i+4000] for i in range(0, len(text), 4000)]
        for part in parts:
            send_message_safe(user_id, part, parse_mode='Markdown')
        send_message_safe(user_id, "🔙 برای بازگشت به پنل:", reply_markup=keyboard)
    else:
        send_message_safe(user_id, text, reply_markup=keyboard, parse_mode='Markdown')
    
    bot.answer_callback_query(call.id)

# ========== هندلر ارسال همگانی (Broadcast) با فیلتر کاربران بلاک‌کننده ==========
@bot.callback_query_handler(func=lambda call: call.data == "admin_broadcast")
def admin_broadcast(call):
    user_id = call.from_user.id
    if user_id not in ADMIN_IDS:
        bot.answer_callback_query(call.id, "❌ شما دسترسی ندارید!", show_alert=True)
        return
    
    bot.answer_callback_query(call.id, "📝 لطفاً متن پیام خود را ارسال کنید.")
    msg = send_message_safe(user_id, "📤 **ارسال به همه کاربران**\n\nلطفاً متن پیامی که می‌خواهید برای **همه کاربران** ارسال شود را وارد کنید.\n\n⚠️ می‌توانید از **مارک‌داون** و **لینک** استفاده کنید.\n\n📌 کاربرانی که ربات را بلاک کرده‌اند، به‌طور خودکار از لیست ارسال حذف می‌شوند.\n\nبرای لغو، /cancel را بفرستید.", parse_mode='Markdown')
    bot.register_next_step_handler_by_chat_id(user_id, broadcast_get_message, user_id, msg.message_id)

def broadcast_get_message(message, admin_id, prompt_msg_id):
    user_id = message.from_user.id
    if user_id not in ADMIN_IDS:
        return
    
    if message.text == "/cancel":
        send_message_safe(user_id, "❌ عملیات ارسال همگانی لغو شد.")
        try:
            delete_message_safe(user_id, prompt_msg_id)
        except:
            pass
        admin_panel(message)
        return
    
    broadcast_text = message.text
    try:
        delete_message_safe(user_id, prompt_msg_id)
    except:
        pass
    
    # دریافت لیست کاربرانی که بلاک نکرده‌اند
    if DATABASE_URL:
        c.execute("SELECT telegram_id FROM users WHERE telegram_id NOT IN (SELECT user_id FROM blocked_users)")
    else:
        c.execute("SELECT telegram_id FROM users WHERE telegram_id NOT IN (SELECT user_id FROM blocked_users)")
    users = c.fetchall()
    total = len(users)
    
    if total == 0:
        send_message_safe(user_id, "📭 هیچ کاربری برای ارسال وجود ندارد (همه کاربران بلاک کرده‌اند یا کاربری وجود ندارد)!")
        admin_panel(message)
        return
    
    send_message_safe(user_id, f"⏳ در حال ارسال پیام به {total} کاربر (کاربران بلاک‌کننده حذف شدند)... لطفاً صبر کنید.")
    
    success = 0
    failed = 0
    
    for idx, (uid,) in enumerate(users, 1):
        try:
            send_message_safe(uid, broadcast_text, parse_mode='Markdown')
            success += 1
        except Exception as e:
            failed += 1
            logger.error(f"Failed to send to {uid}: {e}")
        
        if idx % 30 == 0:
            time.sleep(0.5)
    
    report = (
        "✅ **ارسال همگانی کامل شد!**\n\n"
        f"👤 کاربران قابل ارسال: {total}\n"
        f"✅ ارسال موفق: {success}\n"
        f"❌ ارسال ناموفق: {failed}\n"
        f"🚫 کاربران بلاک‌کننده حذف شدند: {get_total_blocked()}"
    )
    send_message_safe(user_id, report, parse_mode='Markdown')
    admin_panel(message)

# ========== تبلیغات (دریافت اسم و لینک از ادمین و ساخت پیام تله) ==========
@bot.callback_query_handler(func=lambda call: call.data == "admin_advertise")
def admin_advertise(call):
    user_id = call.from_user.id
    if user_id not in ADMIN_IDS:
        bot.answer_callback_query(call.id, "❌ شما دسترسی ندارید!", show_alert=True)
        return
    
    try:
        delete_message_safe(call.message.chat.id, call.message.message_id)
    except:
        pass
    
    # ذخیره وضعیت در دیکشنری موقت
    safe_dict_add(ad_temp, user_id, {})
    
    # دکمه انصراف
    keyboard = InlineKeyboardMarkup()
    keyboard.add(InlineKeyboardButton("❌ انصراف", callback_data="cancel_ad"))
    
    send_message_safe(
        user_id,
        "📝 **مرحله ۱ از ۲**\n\n"
        "لطفاً **اسم** مورد نظر برای پیام تله را وارد کنید:\n"
        "(مثلاً: سارا، علی، یا هر اسم دلخواه)",
        reply_markup=keyboard,
        parse_mode='Markdown'
    )
    bot.register_next_step_handler_by_chat_id(user_id, ad_get_name, user_id)
    bot.answer_callback_query(call.id, "✅")

def ad_get_name(message, admin_id):
    user_id = message.from_user.id
    if user_id not in ADMIN_IDS:
        return
    
    # بررسی انصراف
    if message.text == "/cancel":
        cancel_ad_process(user_id)
        return
    
    name = message.text.strip()
    if not name:
        send_message_safe(user_id, "❌ اسم نمی‌تواند خالی باشد. دوباره تلاش کنید.")
        ad_restart(user_id)
        return
    
    # ذخیره اسم
    ad_temp[user_id]["name"] = name
    
    # دکمه انصراف
    keyboard = InlineKeyboardMarkup()
    keyboard.add(InlineKeyboardButton("❌ انصراف", callback_data="cancel_ad"))
    
    send_message_safe(
        user_id,
        "📝 **مرحله ۲ از ۲**\n\n"
        "لطفاً **لینک** مورد نظر را وارد کنید:\n"
        "(این لینک در دکمه‌های پیام ناشناس، پیوی و پروفایل قرار می‌گیرد)\n\n"
        "مثال: `https://t.me/staystrongs_bot?start=ad`",
        reply_markup=keyboard,
        parse_mode='Markdown'
    )
    bot.register_next_step_handler_by_chat_id(user_id, ad_get_link, user_id)

def ad_get_link(message, admin_id):
    user_id = message.from_user.id
    if user_id not in ADMIN_IDS:
        return
    
    # بررسی انصراف
    if message.text == "/cancel":
        cancel_ad_process(user_id)
        return
    
    link = message.text.strip()
    if not link.startswith(("http://", "https://")):
        send_message_safe(
            user_id,
            "❌ لینک باید با `http://` یا `https://` شروع شود. دوباره تلاش کنید.",
            parse_mode='Markdown'
        )
        # دوباره مرحله ۲
        keyboard = InlineKeyboardMarkup()
        keyboard.add(InlineKeyboardButton("❌ انصراف", callback_data="cancel_ad"))
        msg = send_message_safe(
            user_id,
            "لطفاً لینک خود را وارد کنید:",
            reply_markup=keyboard
        )
        bot.register_next_step_handler_by_chat_id(user_id, ad_get_link, user_id)
        return
    
    # ذخیره لینک
    ad_temp[user_id]["link"] = link
    
    # دریافت اسم
    name = ad_temp[user_id].get("name", "کاربر جدید")
    
    # ساخت پیام تله با اسم و لینک واردشده
    trap_message = (
        f"🎯 **یک فضول در تله افتاد!**\n\n"
        f"👤 نام: {name}\n"
        f"⏰ زمان: {datetime.now().strftime('%H:%M')}"
    )
    
    # دکمه‌ها با لینک واردشده توسط ادمین
    keyboard = InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        InlineKeyboardButton("💬 پیام ناشناس", url=link),
        InlineKeyboardButton("📝 بیوگرافی", url=link),
        InlineKeyboardButton("📨 پیوی", url=link),
        InlineKeyboardButton("🖼 پروفایل", url=link)
    )
    
    send_message_safe(
        user_id,
        trap_message,
        reply_markup=keyboard,
        parse_mode='Markdown'
    )
    
    # پاک کردن دیکشنری موقت
    if user_id in ad_temp:
        del ad_temp[user_id]

def ad_restart(user_id):
    """شروع مجدد فرآیند از مرحله ۱"""
    safe_dict_add(ad_temp, user_id, {})
    keyboard = InlineKeyboardMarkup()
    keyboard.add(InlineKeyboardButton("❌ انصراف", callback_data="cancel_ad"))
    send_message_safe(
        user_id,
        "📝 **مرحله ۱ از ۲**\n\n"
        "لطفاً **اسم** مورد نظر برای پیام تله را وارد کنید:",
        reply_markup=keyboard,
        parse_mode='Markdown'
    )
    bot.register_next_step_handler_by_chat_id(user_id, ad_get_name, user_id)

def cancel_ad_process(user_id):
    """لغو فرآیند و برگشت به پنل"""
    if user_id in ad_temp:
        del ad_temp[user_id]
    send_message_safe(user_id, "❌ عملیات ساخت تبلیغ لغو شد.")
    # برگشت به پنل ادمین (با ارسال یک پیام ساختگی)
    admin_panel(bot.message)

@bot.callback_query_handler(func=lambda call: call.data == "cancel_ad")
def cancel_ad_callback(call):
    user_id = call.from_user.id
    if user_id not in ADMIN_IDS:
        bot.answer_callback_query(call.id, "❌ شما دسترسی ندارید!", show_alert=True)
        return
    
    try:
        delete_message_safe(call.message.chat.id, call.message.message_id)
    except:
        pass
    
    cancel_ad_process(user_id)
    bot.answer_callback_query(call.id, "✅ لغو شد")

# ---------- بقیه بخش‌های پنل مدیریت ----------
@bot.callback_query_handler(func=lambda call: call.data == "admin_stats")
def admin_stats(call):
    user_id = call.from_user.id
    if user_id not in ADMIN_IDS:
        bot.answer_callback_query(call.id, "❌ شما دسترسی ندارید!", show_alert=True)
        return
    text = (
        "📊 **آمار کامل ربات**\n\n"
        f"👤 کل کاربران: {get_total_users()}\n"
        f"📝 گزارش‌های در انتظار: {get_total_reports()}\n"
        f"🎯 کاربران در تله رفته: {get_total_trapped()}\n"
        f"🖼 عکس‌های مچ‌گیری: {get_total_photos()}\n"
        f"🚫 کاربران بلاک‌کننده: {get_total_blocked()}"
    )
    edit_message_text_safe(call.message.chat.id, call.message.message_id, text, parse_mode='Markdown')
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "admin_users")
def admin_users(call):
    user_id = call.from_user.id
    if user_id not in ADMIN_IDS:
        bot.answer_callback_query(call.id, "❌ شما دسترسی ندارید!", show_alert=True)
        return
    
    # حذف پیام قبلی (پنل ادمین) برای جلوگیری از تداخل
    try:
        delete_message_safe(call.message.chat.id, call.message.message_id)
    except:
        pass
    
    try:
        # دریافت ۸۰ کاربر آخر (عدد ۸۰ به‌خوبی در محدوده مجاز قرار می‌گیرد)
        if DATABASE_URL:
            c.execute("SELECT telegram_id, user_name, link_code FROM users ORDER BY telegram_id DESC LIMIT 80")
        else:
            c.execute("SELECT telegram_id, user_name, link_code FROM users ORDER BY telegram_id DESC LIMIT 80")
        
        users = c.fetchall()
        
        if not users:
            text = "📭 هیچ کاربری در دیتابیس یافت نشد."
            send_message_safe(user_id, text, parse_mode='Markdown')
            # بازگشت به پنل ادمین با یک دکمه
            back_keyboard = InlineKeyboardMarkup()
            back_keyboard.add(InlineKeyboardButton("🔙 بازگشت به پنل", callback_data="back_to_panel"))
            send_message_safe(user_id, "برای بازگشت کلیک کنید:", reply_markup=back_keyboard)
            bot.answer_callback_query(call.id)
            return
        
        # شمارش کل کاربران
        c.execute("SELECT COUNT(*) FROM users")
        total_users = c.fetchone()[0]
        
        # ساخت لیست
        text = f"👥 **لیست {len(users)} کاربر از {total_users} کاربر کل (۸۰ کاربر آخر):**\n\n"
        for uid, name, code in users:
            display_name = name if name else "بدون نام"
            text += f"• {display_name} (ID: `{uid}`)\n"
        
        # دکمه بازگشت به پنل ادمین
        keyboard = InlineKeyboardMarkup()
        keyboard.add(InlineKeyboardButton("🔙 بازگشت به پنل", callback_data="back_to_panel"))
        
        # ارسال پیام (اگر طولانی بود، تقسیم می‌شود)
        if len(text) > 4000:
            parts = [text[i:i+4000] for i in range(0, len(text), 4000)]
            for i, part in enumerate(parts):
                if i == 0:
                    send_message_safe(user_id, part, parse_mode='Markdown')
                else:
                    send_message_safe(user_id, part, parse_mode='Markdown')
            # ارسال دکمه بازگشت بعد از آخرین بخش
            send_message_safe(user_id, "🔙 برای بازگشت به پنل:", reply_markup=keyboard)
        else:
            send_message_safe(user_id, text, reply_markup=keyboard, parse_mode='Markdown')
        
    except Exception as e:
        error_text = f"❌ خطا در دریافت لیست کاربران:\n\n`{str(e)}`"
        send_message_safe(user_id, error_text, parse_mode='Markdown')
        logger.error(f"Error in admin_users: {e}")
    
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "admin_reports")
def admin_reports(call):
    user_id = call.from_user.id
    if user_id not in ADMIN_IDS:
        bot.answer_callback_query(call.id, "❌ شما دسترسی ندارید!", show_alert=True)
        return
    if DATABASE_URL:
        c.execute("SELECT id, owner_id, clicker_id, expires_at, cancelled FROM pending_reports ORDER BY id DESC LIMIT 20")
    else:
        c.execute("SELECT id, owner_id, clicker_id, expires_at, cancelled FROM pending_reports ORDER BY id DESC LIMIT 20")
    reports = c.fetchall()
    if not reports:
        text = "📭 هیچ گزارشی یافت نشد."
    else:
        text = "📋 **۲۰ گزارش اخیر:**\n\n"
        for rid, owner_id, clicker_id, expires_at, cancelled in reports:
            owner_name = get_owner_name(owner_id)
            clicker_name = get_clicker_name(clicker_id)
            status = "❌ لغو شده" if cancelled else "⏳ در انتظار"
            text += f"• {owner_name} ← {clicker_name} [{status}]\n"
    edit_message_text_safe(call.message.chat.id, call.message.message_id, text, parse_mode='Markdown')
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "admin_photos")
def admin_photos(call):
    user_id = call.from_user.id
    if user_id not in ADMIN_IDS:
        bot.answer_callback_query(call.id, "❌ شما دسترسی ندارید!", show_alert=True)
        return
    if DATABASE_URL:
        c.execute("SELECT user_id, photo_id FROM user_photos LIMIT 20")
    else:
        c.execute("SELECT user_id, photo_id FROM user_photos LIMIT 20")
    photos = c.fetchall()
    if not photos:
        text = "📭 هیچ عکسی ذخیره نشده است."
    else:
        text = "🖼 **آخرین عکس‌های ذخیره شده:**\n\n"
        for uid, pid in photos:
            name = get_owner_name(uid)
            text += f"• {name} (ID: `{uid}`)\n"
    edit_message_text_safe(call.message.chat.id, call.message.message_id, text, parse_mode='Markdown')
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "admin_clear")
def admin_clear(call):
    user_id = call.from_user.id
    if user_id not in ADMIN_IDS:
        bot.answer_callback_query(call.id, "❌ شما دسترسی ندارید!", show_alert=True)
        return
    keyboard = InlineKeyboardMarkup()
    keyboard.add(
        InlineKeyboardButton("✅ بله، پاک کن!", callback_data="admin_confirm_clear"),
        InlineKeyboardButton("❌ نه، انصراف", callback_data="admin_close")
    )
    edit_message_text_safe(
        call.message.chat.id, call.message.message_id,
        "⚠️ **هشدار جدی!**\n\nآیا از پاک کردن تمام دیتابیس مطمئنی؟\n\n"
        "❗️ این عمل غیرقابل بازگشت است و تمام اطلاعات زیر حذف می‌شوند:\n"
        "• لیست کاربران\n"
        "• گزارش‌های تله\n"
        "• عکس‌های مچ‌گیری\n"
        "• تاریخچه کاربرانی که در تله افتاده‌اند\n"
        "• کاربران بلاک‌کننده",
        reply_markup=keyboard, parse_mode='Markdown'
    )
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "admin_confirm_clear")
def admin_confirm_clear(call):
    user_id = call.from_user.id
    if user_id not in ADMIN_IDS:
        bot.answer_callback_query(call.id, "❌ شما دسترسی ندارید!", show_alert=True)
        return
    if DATABASE_URL:
        c.execute("DELETE FROM users")
        c.execute("DELETE FROM pending_reports")
        c.execute("DELETE FROM user_photos")
        c.execute("DELETE FROM trapped_history")
        c.execute("DELETE FROM blocked_users")
    else:
        c.execute("DELETE FROM users")
        c.execute("DELETE FROM pending_reports")
        c.execute("DELETE FROM user_photos")
        c.execute("DELETE FROM trapped_history")
        c.execute("DELETE FROM blocked_users")
    conn.commit()
    edit_message_text_safe(
        call.message.chat.id, call.message.message_id,
        "✅ **دیتابیس با موفقیت پاک شد!**\n\n"
        "تمامی اطلاعات کاربران، گزارش‌ها و کاربران بلاک‌کننده حذف گردید.",
        parse_mode='Markdown'
    )
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "admin_close")
def admin_close(call):
    try:
        delete_message_safe(call.message.chat.id, call.message.message_id)
    except:
        edit_message_text_safe(call.message.chat.id, call.message.message_id, "🔒 پنل مدیریت بسته شد.")
    bot.answer_callback_query(call.id)

# ---------- مسیرهای Flask ----------
@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        update = telebot.types.Update.de_json(request.get_data().decode('UTF-8'))
        bot.process_new_updates([update])
        return jsonify({"status": "ok"}), 200
    except Exception as e:
        logger.error(f"Error in webhook: {e}")
        return jsonify({"status": "error"}), 500

@app.route('/')
def index():
    return "ربات آنلاین است", 200

def set_webhook():
    bot.remove_webhook()
    time.sleep(1)
    webhook_url = f"{BASE_URL}/webhook"
    if bot.set_webhook(url=webhook_url):
        logger.info(f"Webhook set successfully to {webhook_url}")
    else:
        logger.error(f"Failed to set webhook to {webhook_url}")

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    set_webhook()
    app.run(host='0.0.0.0', port=port)
