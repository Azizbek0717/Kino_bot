import os
import asyncio
import sqlite3
import logging
import json
import re
import shutil
from pathlib import Path
from datetime import datetime
from decimal import Decimal, InvalidOperation

import requests
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton, LabeledPrice
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, ConversationHandler, filters
)

# ============================================================
# SMM AI BOT — Python 3.9+ + python-telegram-bot 22.5
# 2-FILE VERSION: bot.py + rec_requirements.txt
#
# IMPORTANT:
# 1) Never put real API keys into public code.
# 2) Fill the CONFIG section or use environment variables.
# 3) The SMM API below expects a common SMM panel API:
#    action=services, action=add, action=status, action=balance
# 4) Number/Donate providers differ from service to service.
#    Their URLs/actions are configurable in the Admin panel.
# ============================================================

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO
)
log = logging.getLogger("prime_smm")

# ========================= CONFIG ==========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "8943982280:AAELPOPXdNrlJMQOMz6JINpAG-_C_KWcAd0").strip()

# Put numeric Telegram admin IDs here, e.g. "123456789,987654321"
ADMIN_IDS = {
    int(x.strip()) for x in os.getenv("ADMIN_IDS", "6022023269").split(",")
    if x.strip().isdigit()
}

DB_NAME = os.getenv("DB_NAME", "prime_smm.db")
DB_BACKUP_DIR = os.getenv("DB_BACKUP_DIR", "db_backups")


# SMM panel: common API format
SMM_API_URL = os.getenv("SMM_API_URL", "https://uzbek-seen.uz/api/v2").strip()
SMM_API_KEY = os.getenv("SMM_API_KEY", "4a00b31eca6d3ed890a69e069b779748").strip()

# Optional external APIs. Leave blank until you have their documentation.
NUMBER_API_URL = os.getenv("NUMBER_API_URL", "1f356cada9eec4b908d4d26ef4944523").strip()
NUMBER_API_KEY = os.getenv("NUMBER_API_KEY", "1f356cada9eec4b908d4d26ef4944523").strip()
DONATE_API_URL = os.getenv("DONATE_API_URL", "3deee12060bb567f500793276e0875d7").strip()
DONATE_API_KEY = os.getenv("DONATE_API_KEY", "3deee12060bb567f500793276e0875d7").strip()

class SecretRedactionFilter(logging.Filter):
    def filter(self, record):
        try:
            message = record.getMessage()
            for secret in (BOT_TOKEN, SMM_API_KEY, NUMBER_API_KEY, DONATE_API_KEY):
                if secret:
                    message = message.replace(secret, "[REDACTED]")
            record.msg = message
            record.args = ()
        except Exception:
            pass
        return True

for handler in logging.getLogger().handlers:
    handler.addFilter(SecretRedactionFilter())
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

# Comma separated usernames / @channels. Bot must be able to check membership.
FORCE_CHANNELS = [
    x.strip() for x in os.getenv("FORCE_CHANNELS", "").split(",")
    if x.strip()
]

CURRENCY = "so'm"

# Telegram Bot API 9.4+ button custom emoji IDs supplied for this bot.
# Button colors: success=green, primary=blue, danger=red, omitted=neutral/gray.
MENU_EMOJI_IDS = {
    "Nakrutka": "5377660214096974712",
    "Donat": "5409125874651903587",
    "Stars | Premium": "5447644863644320013",
    "Raqam olish": "5201990176175299013",
    "To'lov qilish": "5213403875670765022",
    "Pul ishlash": "5278467510604160626",
    "Hisobim": "5445221832074483553",
    "Buyurtmalarim": "5431499171045581032",
    "Bot qoidalari": "5373098009640836781",
    "Adminga murojaat": "5190498849440931467",
}

# Nakrutka platformalari uchun Telegram Premium custom emoji ID'lari.
PLATFORM_EMOJI_IDS = {
    "telegram": "5249148968625002018",
    "instagram": "5451643289218342306",
    "tiktok": "5855155960598762938",
    "youtube": "4999366254943798100",
}

PLATFORM_LABELS = {
    "telegram": "Telegram",
    "instagram": "Instagram",
    "tiktok": "TikTok",
    "youtube": "YouTube",
}

_TelegramInlineKeyboardButton = InlineKeyboardButton

def InlineKeyboardButton(text, *args, **kwargs):
    # PTB 22.5 sends Bot API 9.4 button style through api_kwargs.
    api_kwargs = dict(kwargs.pop("api_kwargs", {}) or {})
    api_kwargs.setdefault("style", "primary")
    kwargs["api_kwargs"] = api_kwargs
    return _TelegramInlineKeyboardButton(text, *args, **kwargs)


def menu_button(text, style=None):
    style = style or "primary"
    # PTB 22.5 does not expose Bot API 9.4/9.6 button fields as constructor
    # arguments, but it does support api_kwargs. Send the real Premium custom
    # emoji ID through api_kwargs and keep the button text EXACTLY equal to the
    # router text (e.g. "Nakrutka"), so the button never gets stuck.
    kwargs = {"icon_custom_emoji_id": MENU_EMOJI_IDS.get(text)}
    if style:
        kwargs["style"] = style
    kwargs = {k: v for k, v in kwargs.items() if v}
    return KeyboardButton(text, api_kwargs=kwargs)


# Prices here are defaults. Admin can change service markup in the panel.
DEFAULT_MARKUP = Decimal(os.getenv("DEFAULT_MARKUP", "20"))

# ======================= DATABASE ===========================

def db():
    con = sqlite3.connect(DB_NAME)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    con = db()
    cur = con.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        balance REAL DEFAULT 0,
        ref_by INTEGER DEFAULT NULL,
        ref_bonus REAL DEFAULT 0,
        created_at TEXT
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS settings(
        key TEXT PRIMARY KEY,
        value TEXT
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS services(
        id INTEGER PRIMARY KEY,
        provider_id TEXT,
        name TEXT,
        category TEXT,
        price REAL DEFAULT 0,
        min_order INTEGER DEFAULT 10,
        max_order INTEGER DEFAULT 100000,
        enabled INTEGER DEFAULT 1
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS orders(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        service_id INTEGER,
        link TEXT,
        quantity INTEGER,
        amount REAL,
        provider_order_id TEXT,
        status TEXT DEFAULT 'pending',
        created_at TEXT
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS payments(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        amount REAL,
        method TEXT,
        status TEXT,
        created_at TEXT
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS products(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        category TEXT,
        name TEXT,
        price REAL,
        provider_code TEXT DEFAULT '',
        enabled INTEGER DEFAULT 1
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS broadcasts(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        admin_id INTEGER,
        text TEXT,
        sent INTEGER DEFAULT 0,
        failed INTEGER DEFAULT 0,
        created_at TEXT
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS tickets(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        text TEXT,
        status TEXT DEFAULT 'open',
        created_at TEXT
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS premium_emojis(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        unicode_emoji TEXT UNIQUE,
        custom_emoji_id TEXT,
        active INTEGER DEFAULT 1
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS payment_requests(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        amount REAL,
        receipt_file_id TEXT,
        receipt_type TEXT,
        status TEXT DEFAULT 'pending',
        created_at TEXT,
        reviewed_at TEXT
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS number_inventory(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        phone TEXT NOT NULL UNIQUE,
        price REAL NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'available',
        sold_to INTEGER,
        sold_at TEXT,
        expires_at TEXT
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS number_orders(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        number_id INTEGER NOT NULL,
        price REAL NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        created_at TEXT NOT NULL,
        approved_at TEXT,
        expires_at TEXT
    )
    """)


    cur.execute("""
    CREATE TABLE IF NOT EXISTS promocodes(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT NOT NULL UNIQUE COLLATE NOCASE,
        reward REAL NOT NULL CHECK(reward > 0),
        usage_limit INTEGER NOT NULL CHECK(usage_limit > 0),
        used_count INTEGER NOT NULL DEFAULT 0,
        active INTEGER NOT NULL DEFAULT 1,
        created_by INTEGER,
        created_at TEXT NOT NULL
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS promo_redemptions(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        promo_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        amount REAL NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(promo_id, user_id)
    )
    """)

    defaults = {
        "ref_percent": "5",
        "ref_reward": "25",
        "min_payment": "500",
        "payment_card": "8600000000000000",
        "payment_owner": "Admin",
        "support": "@admin",
        "number_contract": "Raqamni faqat o'zingizga tegishli qonuniy maqsadlarda foydalaning. Raqam 10 daqiqa muddatga beriladi. Muddat tugagach xizmat avtomatik yakunlanadi. Telegram akkaunt login kodi faqat akkaunt egasining o'zi tomonidan kiritiladi.",
        "bot_name": "Prime SMM",
        "welcome": "Assalomu alaykum, xush kelibsiz 👋",
        "force_channels": "",
        "force_limit": "0",
        "premium_emoji_enabled": "1",
        "premium_default_emoji": "✨",
    }
    for k, v in defaults.items():
        cur.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (k, v))

    # Keep the supplied Premium custom emoji IDs available for message entities.
    for symbol, custom_id in {
        "🛍️": MENU_EMOJI_IDS["Nakrutka"],
        "🎮": MENU_EMOJI_IDS["Donat"],
        "✨": MENU_EMOJI_IDS["Stars | Premium"],
        "⭐": MENU_EMOJI_IDS["Stars | Premium"],
        "📱": MENU_EMOJI_IDS["Raqam olish"],
        "💳": MENU_EMOJI_IDS["To'lov qilish"],
        "💰": MENU_EMOJI_IDS["Pul ishlash"],
        "💼": MENU_EMOJI_IDS["Hisobim"],
        "🛒": MENU_EMOJI_IDS["Buyurtmalarim"],
        "📚": MENU_EMOJI_IDS["Bot qoidalari"],
        "👨‍💻": MENU_EMOJI_IDS["Adminga murojaat"],
        "🤖": "5330378912000160091",
    }.items():
        cur.execute(
            "INSERT INTO premium_emojis(unicode_emoji,custom_emoji_id,active) VALUES(?,?,1) "
            "ON CONFLICT(unicode_emoji) DO UPDATE SET custom_emoji_id=excluded.custom_emoji_id, active=1",
            (symbol, custom_id)
        )

    # Demo Donate products; admin can edit/delete them.
    cur.execute("SELECT COUNT(*) AS c FROM products")
    if cur.fetchone()["c"] == 0:
        cur.executemany(
            "INSERT INTO products(category,name,price,provider_code) VALUES(?,?,?,?)",
            [
                ("donate", "Free Fire — 100 diamond", 15000, "ff100"),
                ("donate", "Free Fire — 310 diamond", 42000, "ff310"),
                ("donate", "PUBG — 60 UC", 18000, "pubg60"),
                ("donate", "PUBG — 325 UC", 85000, "pubg325"),
            ]
        )

    con.commit()
    con.close()


def database_path():
    return os.path.abspath(DB_NAME)

def database_backup_dir():
    path = os.path.abspath(DB_BACKUP_DIR)
    os.makedirs(path, exist_ok=True)
    return path

def database_size_text(path=None):
    path = path or database_path()
    try:
        size = os.path.getsize(path)
    except OSError:
        return "0 B"
    units = ("B", "KB", "MB", "GB")
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return "0 B"

def is_valid_database(path):
    required = {
        "users", "settings", "services", "orders", "payments", "products",
        "broadcasts", "tickets", "premium_emojis", "payment_requests",
        "number_inventory", "number_orders", "promocodes", "promo_redemptions"
    }
    try:
        con = sqlite3.connect(path)
        rows = con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        tables = {r[0] for r in rows}
        con.execute("PRAGMA integrity_check").fetchone()
        con.close()
        return required.issubset(tables)
    except Exception:
        return False

def backup_current_database(prefix="backup"):
    src = database_path()
    if not os.path.exists(src):
        init_db()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dst = os.path.join(database_backup_dir(), f"{prefix}_{stamp}.db")
    shutil.copy2(src, dst)
    return dst

def database_backups():
    folder = database_backup_dir()
    files = []
    for name in os.listdir(folder):
        path = os.path.join(folder, name)
        if os.path.isfile(path) and name.lower().endswith((".db", ".sqlite", ".sqlite3")):
            files.append(path)
    return sorted(files, key=lambda x: os.path.getmtime(x), reverse=True)

def safe_database_name(name):
    base = os.path.basename(name or "database.db")
    base = re.sub(r"[^A-Za-z0-9_.-]+", "_", base)
    if not base.lower().endswith((".db", ".sqlite", ".sqlite3")):
        base += ".db"
    return base

def database_menu_markup():
    buttons = [
        [InlineKeyboardButton("📤 Joriy bazani yuborish", callback_data="db:send")],
        [InlineKeyboardButton("💾 Joriy bazadan backup", callback_data="db:backup")],
        [InlineKeyboardButton("➕ Baza fayli qo'shish", callback_data="db:upload")],
        [InlineKeyboardButton("📂 Saqlangan bazalar", callback_data="db:list")],
        [InlineKeyboardButton("⬅️ Admin panel", callback_data="adm:home")],
    ]
    return InlineKeyboardMarkup(buttons)

def database_info_text():
    backups = database_backups()
    return (
        "🗄 <b>Baza fayllari</b>\n\n"
        f"📄 Joriy baza: <code>{os.path.basename(database_path())}</code>\n"
        f"📦 Hajmi: <b>{database_size_text()}</b>\n"
        f"💾 Backup fayllari: <b>{len(backups)} ta</b>\n\n"
        "Bu bot SQLite bazasidan foydalanadi."
    )

def get_setting(key, default=""):
    con = db()
    row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    con.close()
    return row["value"] if row else default

def set_setting(key, value):
    con = db()
    con.execute(
        "INSERT INTO settings(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value))
    )
    con.commit()
    con.close()

def ensure_user(user):
    con = db()
    row = con.execute("SELECT id FROM users WHERE id=?", (user.id,)).fetchone()
    created = False
    if not row:
        con.execute(
            "INSERT INTO users(id,username,first_name,created_at) VALUES(?,?,?,?)",
            (user.id, user.username or "", user.first_name or "", datetime.now().isoformat())
        )
        created = True
    else:
        con.execute(
            "UPDATE users SET username=?, first_name=? WHERE id=?",
            (user.username or "", user.first_name or "", user.id)
        )
    con.commit()
    con.close()
    return created

def apply_referral(new_user_id, referrer_id):
    """Reward a referrer exactly once when a genuinely new user joins."""
    try:
        referrer_id = int(referrer_id)
    except (TypeError, ValueError):
        return 0
    if referrer_id == new_user_id or referrer_id <= 0:
        return 0

    reward = Decimal(get_setting("ref_reward", "25"))
    if reward <= 0:
        return 0

    con = db()
    try:
        con.execute("BEGIN IMMEDIATE")
        new_row = con.execute("SELECT ref_by FROM users WHERE id=?", (new_user_id,)).fetchone()
        ref_row = con.execute("SELECT id FROM users WHERE id=?", (referrer_id,)).fetchone()
        if not new_row or not ref_row or new_row["ref_by"] is not None:
            con.rollback()
            return 0
        con.execute("UPDATE users SET ref_by=? WHERE id=?", (referrer_id, new_user_id))
        con.execute(
            "UPDATE users SET balance=COALESCE(balance,0)+?, ref_bonus=COALESCE(ref_bonus,0)+? WHERE id=?",
            (float(reward), float(reward), referrer_id)
        )
        con.commit()
        return float(reward)
    except Exception:
        con.rollback()
        log.exception("Referral reward failed: new_user=%s referrer=%s", new_user_id, referrer_id)
        return 0
    finally:
        con.close()

def referral_stats(uid):
    con = db()
    row = con.execute(
        "SELECT COUNT(*) AS cnt, COALESCE(SUM(ref_bonus),0) AS earned FROM users WHERE id=?",
        (uid,)
    ).fetchone()
    count = con.execute("SELECT COUNT(*) AS cnt FROM users WHERE ref_by=?", (uid,)).fetchone()["cnt"]
    con.close()
    return int(count), float(row["earned"] or 0)

def get_balance(uid):
    con = db()
    row = con.execute("SELECT balance FROM users WHERE id=?", (uid,)).fetchone()
    con.close()
    return float(row["balance"]) if row else 0

def add_balance(uid, amount):
    con = db()
    con.execute("UPDATE users SET balance=balance+? WHERE id=?", (float(amount), uid))
    con.commit()
    con.close()

def take_balance(uid, amount):
    con = db()
    row = con.execute("SELECT balance FROM users WHERE id=?", (uid,)).fetchone()
    if not row or float(row["balance"]) < float(amount):
        con.close()
        return False
    con.execute("UPDATE users SET balance=balance-? WHERE id=?", (float(amount), uid))
    con.commit()
    con.close()
    return True

def is_admin(uid):
    return uid in ADMIN_IDS

def create_promocode(code, reward, usage_limit, admin_id):
    con = db()
    try:
        con.execute(
            "INSERT INTO promocodes(code,reward,usage_limit,created_by,created_at) "
            "VALUES(?,?,?,?,?)",
            (code.upper(), float(reward), usage_limit, admin_id, datetime.now().isoformat())
        )
        con.commit()
        return True
    except sqlite3.IntegrityError:
        con.rollback()
        return False
    finally:
        con.close()

def toggle_promocode(promo_id):
    con = db()
    cur = con.execute(
        "UPDATE promocodes SET active=CASE active WHEN 1 THEN 0 ELSE 1 END WHERE id=?",
        (promo_id,)
    )
    con.commit()
    changed = cur.rowcount > 0
    con.close()
    return changed

def redeem_promocode(user_id, code):
    con = db()
    try:
        con.execute("BEGIN IMMEDIATE")
        promo = con.execute(
            "SELECT id,reward,usage_limit,used_count,active FROM promocodes "
            "WHERE code=? COLLATE NOCASE",
            (code.strip(),)
        ).fetchone()
        if not promo:
            con.rollback()
            return "invalid", 0
        if not promo["active"]:
            con.rollback()
            return "inactive", 0
        if promo["used_count"] >= promo["usage_limit"]:
            con.rollback()
            return "limit", 0
        already_used = con.execute(
            "SELECT 1 FROM promo_redemptions WHERE promo_id=? AND user_id=?",
            (promo["id"], user_id)
        ).fetchone()
        if already_used:
            con.rollback()
            return "used", 0

        cur = con.execute(
            "UPDATE promocodes SET used_count=used_count+1 "
            "WHERE id=? AND active=1 AND used_count<usage_limit",
            (promo["id"],)
        )
        if cur.rowcount != 1:
            con.rollback()
            return "limit", 0
        con.execute(
            "UPDATE users SET balance=COALESCE(balance,0)+? WHERE id=?",
            (promo["reward"], user_id)
        )
        con.execute(
            "INSERT INTO promo_redemptions(promo_id,user_id,amount,created_at) VALUES(?,?,?,?)",
            (promo["id"], user_id, promo["reward"], datetime.now().isoformat())
        )
        con.commit()
        return "ok", float(promo["reward"])
    except sqlite3.IntegrityError:
        con.rollback()
        return "used", 0
    finally:
        con.close()

# ====================== KEYBOARDS ===========================

def main_menu():
    # Premium custom emoji icons are sent through api_kwargs for PTB 22.5.
    # Button text remains plain so message routing is reliable.
    return ReplyKeyboardMarkup([
        [menu_button("Nakrutka", "success"), menu_button("Donat", "success")],
        [menu_button("Stars | Premium", "primary"), menu_button("Raqam olish", "success")],
        [menu_button("To'lov qilish", "success"), menu_button("Hisobim")],
        [menu_button("Buyurtmalarim"), menu_button("Pul ishlash", "success")],
        [menu_button("Bot qoidalari", "danger"), menu_button("Adminga murojaat", "danger")],
    ], resize_keyboard=True)

def admin_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Statistika", callback_data="adm:stats"),
         InlineKeyboardButton("👥 Foydalanuvchilar", callback_data="adm:users")],
        [InlineKeyboardButton("🔎 ID orqali foydalanuvchi", callback_data="adm:user_search"),
         InlineKeyboardButton("💳 Balans boshqaruvi", callback_data="adm:balance")],
        [InlineKeyboardButton("🛍 Xizmatlar", callback_data="adm:services"),
         InlineKeyboardButton("💰 Narxlarni sozlash", callback_data="adm:prices")],
        [InlineKeyboardButton("🎮 Donat narxlari", callback_data="adm:donate")],
        [InlineKeyboardButton("📱 Raqamlar", callback_data="adm:numbers")],
        [InlineKeyboardButton("📢 Majburiy obuna", callback_data="adm:force"),
         InlineKeyboardButton("📣 Xabar yuborish", callback_data="adm:broadcast")],
        [InlineKeyboardButton("🔥 Tekin xizmat qo'shish", callback_data="adm:freeadd")],
        [InlineKeyboardButton("🎟 Promokodlar", callback_data="adm:promos")],
        [InlineKeyboardButton("👥 Do'st uchun bonus", callback_data="adm:referral")],
        [InlineKeyboardButton("✨ Premium emoji", callback_data="adm:emoji"),
         InlineKeyboardButton("⚙️ Sozlamalar", callback_data="adm:settings")],
        [InlineKeyboardButton("🗄 Baza fayllari", callback_data="adm:database")],
        [InlineKeyboardButton("🔄 SMM xizmatlarini yuklash", callback_data="adm:sync")],
    ])

def admin_back():
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Admin panel", callback_data="adm:home")]])

def get_force_channels():
    saved = get_setting("force_channels", "")
    if saved.strip():
        return [x.strip() for x in saved.split(",") if x.strip()]
    return list(FORCE_CHANNELS)

def premium_map():
    con = db()
    rows = con.execute("SELECT unicode_emoji, custom_emoji_id FROM premium_emojis WHERE active=1").fetchall()
    con.close()
    return {r["unicode_emoji"]: r["custom_emoji_id"] for r in rows if r["custom_emoji_id"]}

def premium_entities(text):
    if not text or get_setting("premium_emoji_enabled", "1") != "1":
        return []
    mp = premium_map()
    if not mp:
        return []
    from telegram import MessageEntity
    entities = []
    for symbol, custom_id in mp.items():
        pos = 0
        while True:
            pos = text.find(symbol, pos)
            if pos < 0:
                break
            # Telegram entity offsets/lengths use UTF-16 code units.
            prefix_units = len(text[:pos].encode("utf-16-le")) // 2
            symbol_units = len(symbol.encode("utf-16-le")) // 2
            entities.append(MessageEntity(
                type="custom_emoji",
                offset=prefix_units,
                length=symbol_units,
                custom_emoji_id=str(custom_id)
            ))
            pos += len(symbol)
    return sorted(entities, key=lambda x: x.offset)

# ===================== FORCE SUBSCRIBE ======================

async def check_subscription(bot, uid):
    channels = get_force_channels()
    if not channels:
        return True
    for channel in channels:
        try:
            member = await bot.get_chat_member(channel, uid)
            if member.status in ("left", "kicked"):
                return False
            if member.status == "restricted" and not getattr(member, "is_member", False):
                return False
        except Exception as exc:
            log.warning("Strict subscription check failed for %s: %s", channel, exc)
            return False
    return True

async def subscription_gate(update, context):
    if is_admin(update.effective_user.id):
        return True
    if await check_subscription(context.bot, update.effective_user.id):
        return True

    buttons = []
    for ch in get_force_channels():
        buttons.append([InlineKeyboardButton(
            f"🟨 {ch} — Obuna bo'lish",
            url=f"https://t.me/{ch.lstrip('@')}",
            api_kwargs={"style": "primary"}
        )])
    buttons.append([InlineKeyboardButton(
        "🟩 Tekshirish",
        callback_data="check_sub",
        api_kwargs={"style": "success"}
    )])
    text = (
        "⛔ <b>Botdan foydalanish uchun majburiy obuna</b>\n\n"
        "1️⃣ Barcha kanallarga obuna bo'ling.\n"
        "2️⃣ So'ng «🟩 Tekshirish» tugmasini bosing.\n\n"
        "❗ Barcha kanallarga obuna bo'lmaguningizcha botning boshqa bo'limlari ishlamaydi."
    )

    if update.callback_query:
        await update.callback_query.message.reply_text(
            text, reply_markup=InlineKeyboardMarkup(buttons)
        )
    else:
        await update.message.reply_text(
            text, reply_markup=InlineKeyboardMarkup(buttons)
        )
    return False

# ======================== SMM API ===========================

def smm_request(payload):
    if not SMM_API_URL or "YOUR-SMM-PANEL" in SMM_API_URL or not SMM_API_KEY:
        raise RuntimeError("SMM_API_URL / SMM_API_KEY sozlanmagan")

    data = dict(payload)
    data["key"] = SMM_API_KEY
    r = requests.post(SMM_API_URL, data=data, timeout=30)
    r.raise_for_status()
    try:
        return r.json()
    except Exception:
        return {"raw": r.text}

def sync_services_from_provider():
    data = smm_request({"action": "services"})
    if not isinstance(data, list):
        raise RuntimeError(f"API services javobi noto'g'ri: {data}")

    con = db()
    provider_ids = set()
    for s in data:
        provider_id = str(s.get("service", ""))
        if not provider_id:
            continue
        provider_ids.add(provider_id)
        name = str(s.get("name", provider_id))
        # Uzbek-Seen API provides a category field; use it first so
        # services are placed in the correct Telegram/Instagram/TikTok/YouTube menu.
        api_category = str(s.get("category", "") or "").lower()
        category = detect_category(name, api_category)
        provider_price = Decimal(str(s.get("rate", "0") or "0"))

        # API'dagi aynan 20 so'mlik xizmatlar "Tekin Xizmatlar"ga o'tadi.
        # Provider service ID, nomi, min/max va platforma ma'lumotlari saqlanadi,
        # faqat foydalanuvchi narxi 0 so'm qilinadi.
        if provider_price == Decimal("20"):
            price = Decimal("0")
        else:
            price = provider_price + DEFAULT_MARKUP

        existing = con.execute(
            "SELECT id FROM services WHERE provider_id=?", (provider_id,)
        ).fetchone()

        if existing:
            con.execute(
                "UPDATE services SET name=?, category=?, price=?, min_order=?, max_order=?, enabled=1 "
                "WHERE provider_id=?",
                (
                    name, category, float(price),
                    int(s.get("min", 1) or 1),
                    int(s.get("max", 1000000) or 1000000),
                    provider_id
                )
            )
        else:
            con.execute(
                "INSERT INTO services(provider_id,name,category,price,min_order,max_order) "
                "VALUES(?,?,?,?,?,?)",
                (
                    provider_id, name, category, float(price),
                    int(s.get("min", 1) or 1),
                    int(s.get("max", 1000000) or 1000000)
                )
            )
    # API'dan olib tashlangan eski provider xizmatlarini foydalanuvchi menyusidan yashiramiz.
    # provider_id=0 kabi qo'lda qo'shilgan tekin xizmatlarga tegmaymiz.
    if provider_ids:
        placeholders = ",".join("?" for _ in provider_ids)
        con.execute(
            f"UPDATE services SET enabled=0 WHERE provider_id != '' AND provider_id != '0' AND provider_id NOT IN ({placeholders})",
            tuple(provider_ids),
        )
    con.commit()
    con.close()
    return len(data)

def detect_category(name, api_category=""):
    # The provider's category is authoritative when it clearly names a platform.
    c = str(api_category or "").casefold()
    platform_patterns = (
        ("telegram", r"\btelegram\b|\btg\b|t\.me"),
        ("instagram", r"\binstagram\b|\binsta\b|instagram\.com"),
        ("tiktok", r"\btiktok\b|\btik\s*tok\b|tiktok\.com"),
        ("youtube", r"\byoutube\b|\byt\b|youtu\.be"),
    )
    for category, pattern in platform_patterns:
        if re.search(pattern, c):
            return category

    # If the API category is generic/empty, classify from the service name.
    n = str(name or "").casefold()
    for category, pattern in platform_patterns:
        if re.search(pattern, n):
            return category
    return "other"

def provider_add(service, link, quantity):
    return smm_request({
        "action": "add",
        "service": service["provider_id"],
        "link": link,
        "quantity": quantity
    })

def provider_status(order_id):
    return smm_request({"action": "status", "order": order_id})

def provider_balance():
    return smm_request({"action": "balance"})

# ======================= COMMON =============================

def money(v):
    return f"{float(v):,.0f}".replace(",", " ") + f" {CURRENCY}"

def parse_amount(s):
    try:
        x = Decimal(s.replace(" ", "").replace(",", "."))
        return x if x > 0 else None
    except InvalidOperation:
        return None

# Platform ichidagi xizmat turlari. Foydalanuvchi avval turini tanlaydi,
# keyin faqat shu turdagi xizmatlar va ularning narxlari chiqadi.
SERVICE_TYPE_RULES = {
    "subs": (
        "👥 Obunachi",
        ["follower", "followers", "subscriber", "subscribers", "obunachi", "obunachilar", "подписчик", "подписчики"]
    ),
    "likes": (
        "👍 Like",
        ["like", "likes", "like service", "layk", "лайк"]
    ),
    "reactions": (
        "💖 Reaksiyalar",
        ["reaction", "reactions", "reaksiya", "reaksiyalar", "реакц", "реакция", "реакции"]
    ),
    "views": (
        "👁 Ko'rishlar",
        ["view", "views", "ko'rish", "korish", "просмотр", "просмотры"]
    ),
    "comments": (
        "💬 Izohlar",
        ["comment", "comments", "izoh", "izohlar", "коммент"]
    ),
    "shares": (
        "🔄 Ulashishlar",
        ["share", "shares", "ulashish", "ulashishlar", "поделиться"]
    ),
    "saves": (
        "🔖 Saqlashlar",
        ["save", "saves", "saved", "saqlash", "сохран"]
    ),
}

def service_type_clause(service_type):
    if service_type == "all":
        return "", []
    keywords = SERVICE_TYPE_RULES.get(service_type, ("", []))[1]
    if not keywords:
        return "", []
    parts = []
    params = []
    for word in keywords:
        parts.append("LOWER(name) LIKE ?")
        params.append(f"%{word.casefold()}%")
    return " AND (" + " OR ".join(parts) + ")", params

def services_by_category(category, limit=10, offset=0, service_type="all"):
    con = db()
    type_filter, params = service_type_clause(service_type)
    if category == "all":
        sql = "SELECT * FROM services WHERE enabled=1" + type_filter + " ORDER BY id LIMIT ? OFFSET ?"
        query_params = [*params, limit, offset]
    else:
        sql = "SELECT * FROM services WHERE category=? AND enabled=1" + type_filter + " ORDER BY id LIMIT ? OFFSET ?"
        query_params = [category, *params, limit, offset]
    rows = con.execute(sql, query_params).fetchall()
    con.close()
    return rows

def service_count_by_category(category, service_type="all"):
    con = db()
    type_filter, params = service_type_clause(service_type)
    if category == "all":
        row = con.execute(
            "SELECT COUNT(*) AS total FROM services WHERE enabled=1" + type_filter,
            params
        ).fetchone()
    else:
        row = con.execute(
            "SELECT COUNT(*) AS total FROM services WHERE category=? AND enabled=1" + type_filter,
            [category, *params]
        ).fetchone()
    con.close()
    return int(row["total"])

def platform_button(category, callback_data):
    # Keep platform names clean; Telegram renders the Premium custom emoji
    # from icon_custom_emoji_id before the text.
    return InlineKeyboardButton(
        PLATFORM_LABELS[category],
        callback_data=callback_data,
        api_kwargs={"icon_custom_emoji_id": PLATFORM_EMOJI_IDS[category]},
    )

def category_keyboard():
    # Tartibli 2x2 platforma menyusi: har bir platforma o'z emoji'si bilan.
    return InlineKeyboardMarkup([
        [
            platform_button("telegram", "cat:telegram"),
            platform_button("instagram", "cat:instagram"),
        ],
        [
            platform_button("tiktok", "cat:tiktok"),
            platform_button("youtube", "cat:youtube"),
        ],
        [InlineKeyboardButton("📦 Barcha SMM xizmatlar", callback_data="cat:all:all:0",
                              api_kwargs={"style":"primary"})],
        [InlineKeyboardButton("🔥 Tekin Xizmatlar", callback_data="free:all",
                              api_kwargs={"style":"primary", "icon_custom_emoji_id":"5442939099906325301"})],
        [InlineKeyboardButton("⬅️ Menyu", callback_data="menu")],
    ])

def service_type_keyboard(category):
    # Telegramda provayder odatda Like emas, Reaksiya xizmatlarini beradi.
    # Qolgan platformalarda Like nomi saqlanadi.
    second_label = "💖 Reaksiyalar" if category == "telegram" else "👍 Like"
    second_type = "reactions" if category == "telegram" else "likes"
    rows = [
        [
            InlineKeyboardButton("👥 Obunachi", callback_data=f"cat:{category}:subs:0"),
            InlineKeyboardButton(second_label, callback_data=f"cat:{category}:{second_type}:0"),
        ],
        [
            InlineKeyboardButton("👁 Ko'rishlar", callback_data=f"cat:{category}:views:0"),
            InlineKeyboardButton("💬 Izohlar", callback_data=f"cat:{category}:comments:0"),
        ],
        [
            InlineKeyboardButton("🔄 Ulashishlar", callback_data=f"cat:{category}:shares:0"),
            InlineKeyboardButton("🔖 Saqlashlar", callback_data=f"cat:{category}:saves:0"),
        ],
        [InlineKeyboardButton("📦 Barcha xizmatlar", callback_data=f"cat:{category}:all:0")],
        [InlineKeyboardButton("🔥 Tekin Xizmatlar", callback_data=f"free:{category}",
                              api_kwargs={"style":"primary", "icon_custom_emoji_id":"5442939099906325301"})],
        [InlineKeyboardButton("⬅️ Platformalar", callback_data="nakrutka")],
    ]
    return InlineKeyboardMarkup(rows)

def get_service(sid):
    con = db()
    row = con.execute("SELECT * FROM services WHERE id=?", (sid,)).fetchone()
    con.close()
    return row

def clear_order_flow(context):
    for key in ("service_id", "order_step", "link", "quantity", "amount"):
        context.user_data.pop(key, None)

# ===================== USER HANDLERS ========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    is_new_user = ensure_user(update.effective_user)

    # /start ref_USER_ID: only a brand-new account can trigger the reward.
    # Apply it before the subscription gate so a forced-subscription screen
    # cannot make the one-time referral reward disappear.
    referral_reward = 0
    if is_new_user and context.args:
        arg = (context.args[0] or "").strip()
        if arg.startswith("ref_"):
            referral_reward = apply_referral(update.effective_user.id, arg[4:])

    if not await subscription_gate(update, context):
        return

    text = (
        "✨ <b>Assalomu alaykum, SMM AI Botga 🤖 xush kelibsiz 👋</b> ✨\n\n"
        "Iltimos, menyu orqali botni boshqaring."
    )
    await update.message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        entities=premium_entities(text),
        reply_markup=main_menu()
    )
    if referral_reward:
        await update.message.reply_text(
            f"🎉 Siz do'stingizning taklif havolasi orqali kirdingiz.\n"
            f"Do'stingizga {money(referral_reward)} bonus berildi.",
            reply_markup=main_menu()
        )

async def check_sub_callback(update, context):
    q = update.callback_query
    await q.answer()
    if await check_subscription(context.bot, q.from_user.id):
        await q.message.reply_text("✅ Obuna tasdiqlandi!", reply_markup=main_menu())
    else:
        await q.message.reply_text("❌ Hali barcha kanallarga obuna bo'lmagansiz.")

async def account(update, context):
    if not await subscription_gate(update, context):
        return
    uid = update.effective_user.id
    ensure_user(update.effective_user)
    await update.message.reply_text(
        f"💼 <b>Hisobim</b>\n\n"
        f"💰 Balans: <b>{money(get_balance(uid))}</b>\n"
        f"🆔 ID: <code>{uid}</code>",
        parse_mode=ParseMode.HTML
    )

async def topup(update, context):
    if not await subscription_gate(update, context):
        return
    context.user_data["payment_step"] = "amount"
    await update.message.reply_text(
        "💳 <b>To'lov qilish</b>\n\n"
        f"💰 Minimal summa: <b>{money(get_setting('min_payment', '500'))}</b>\n\n"
        "Qancha summa to'lamoqchi ekaningizni so'mda yuboring.",
        parse_mode=ParseMode.HTML,
    )

async def payment_amount_text(update, context):
    if context.user_data.get("payment_step") != "amount":
        return False
    amount = parse_amount(update.message.text.strip())
    min_amount = Decimal(get_setting("min_payment", "500"))
    if not amount or amount < min_amount:
        await update.message.reply_text(f"❌ Minimal to'lov: {money(min_amount)}")
        return True
    if amount != amount.to_integral_value():
        await update.message.reply_text("❌ Faqat butun so'mda to'lov yuboring.")
        return True
    context.user_data["payment_amount"] = float(amount)
    context.user_data["payment_step"] = "receipt"
    await update.message.reply_text(
        f"💳 <b>To'lov ma'lumotlari</b>\n\n"
        f"💰 Summa: <b>{money(amount)}</b>\n"
        f"💳 Karta: <code>{get_setting('payment_card')}</code>\n"
        f"👤 Karta egasi: <b>{get_setting('payment_owner')}</b>\n\n"
        "To'lovni amalga oshirgach, shu yerga chek/skrinshot yuboring.\n"
        "⏳ Admin tekshiradi va tasdiqlaydi.",
        parse_mode=ParseMode.HTML,
    )
    return True

async def payment_media(update, context):
    if context.user_data.get("payment_step") != "receipt":
        return False
    amount = context.user_data.get("payment_amount")
    if not amount:
        context.user_data.clear(); return False
    if update.message.photo:
        file_id = update.message.photo[-1].file_id; rtype = "photo"
    elif update.message.document:
        file_id = update.message.document.file_id; rtype = "document"
    else:
        return False
    con=db()
    cur=con.execute("INSERT INTO payment_requests(user_id,amount,receipt_file_id,receipt_type,status,created_at) VALUES(?,?,?,?,?,?)",
                    (update.effective_user.id,float(amount),file_id,rtype,"pending",datetime.now().isoformat()))
    rid=cur.lastrowid; con.commit(); con.close()
    context.user_data.clear()
    username = f"@{update.effective_user.username}" if update.effective_user.username else str(update.effective_user.id)
    kb=InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Tasdiqlash",callback_data=f"pay:approve:{rid}",api_kwargs={"style":"success"}),
        InlineKeyboardButton("❌ Bekor qilish",callback_data=f"pay:reject:{rid}",api_kwargs={"style":"danger"})
    ]])
    caption=(f"💵 <b>Yangi to'lov</b>\n\n🆔 #{rid}\n👤 {username}\n<code>{update.effective_user.id}</code>\n💰 Summa: <b>{money(amount)}</b>\n\nTasdiqlaysizmi?")
    for aid in ADMIN_IDS:
        try:
            if rtype=="photo": await context.bot.send_photo(aid,file_id,caption=caption,parse_mode=ParseMode.HTML,reply_markup=kb)
            else: await context.bot.send_document(aid,file_id,caption=caption,parse_mode=ParseMode.HTML,reply_markup=kb)
        except Exception as e: log.warning("Payment admin notify failed: %s",e)
    await update.message.reply_text("✅ Chek adminga yuborildi.\n⏳ Tasdiqlanishini kuting.",reply_markup=main_menu())
    return True

async def orders(update, context):
    if not await subscription_gate(update, context):
        return
    con = db()
    rows = con.execute(
        "SELECT id,service_id,quantity,amount,provider_order_id,status,created_at "
        "FROM orders WHERE user_id=? ORDER BY id DESC LIMIT 10",
        (update.effective_user.id,)
    ).fetchall()
    con.close()

    if not rows:
        await update.message.reply_text("🛒 Sizda hozircha buyurtmalar yo'q.")
        return

    out = ["🛒 <b>Buyurtmalarim</b>\n"]
    for r in rows:
        out.append(
            f"#{r['id']} | {r['status']} | {r['quantity']} dona | "
            f"{money(r['amount'])}"
        )
    await update.message.reply_text("\n".join(out), parse_mode=ParseMode.HTML)

async def earnings(update, context):
    if not await subscription_gate(update, context):
        return
    reward = get_setting("ref_reward", "25")
    bot_username = (await context.bot.get_me()).username
    link = f"https://t.me/{bot_username}?start=ref_{update.effective_user.id}"
    count, earned = referral_stats(update.effective_user.id)
    share_url = "https://t.me/share/url?url=" + requests.utils.quote(link, safe="") + "&text=" + requests.utils.quote("SMM AI Botga qo'shiling!", safe="")
    await update.message.reply_text(
        f"💰 <b>Pul ishlash</b>\n\n"
        f"👥 Har bir yangi do'st uchun: <b>{money(reward)}</b>\n"
        f"👤 Taklif qilgan do'stlar: <b>{count} ta</b>\n"
        f"💵 Referral daromad: <b>{money(earned)}</b>\n\n"
        f"🔗 <code>{link}</code>\n\n"
        f"Do'stingiz shu havola orqali botga birinchi marta kirsa, sizga bonus yoziladi.",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔗 Do'st taklif qilish", url=share_url)],
            [InlineKeyboardButton("🎟 Promokod kiritish", callback_data="promo:enter")]
        ])
    )

async def rules(update, context):
    await update.message.reply_text(
        "📚 <b>Bot qoidalari</b>\n\n"
        "1️⃣ Faqat o'zingizga tegishli yoki ruxsat berilgan sahifalarga buyurtma bering.\n"
        "2️⃣ Havolani buyurtma berishdan oldin tekshiring.\n"
        "3️⃣ Bir xil xizmatga takroriy buyurtma berishda oldingi buyurtma holatini kuting.\n"
        "4️⃣ Noto'g'ri link, yopiq profil yoki cheklangan kontent sababli bajarilmagan buyurtma uchun provayder qoidalari amal qiladi.\n"
        "5️⃣ Balans, to'lov va buyurtma ma'lumotlarini saqlab qo'ying.\n"
        "6️⃣ Muammo bo'lsa 👨‍💻 Adminga murojaat bo'limidan foydalaning.\n\n"
        "⚠️ Xizmatlar uchinchi tomon provayder API'si orqali bajarilishi mumkin.",
        parse_mode=ParseMode.HTML
    )

async def support(update, context):
    await update.message.reply_text(
        f"👨‍💻 Adminga murojaat: {get_setting('support', '@admin')}"
    )

# ====================== NAKRUTKA ============================

async def nakrutka(update, context):
    if not await subscription_gate(update, context):
        return
    await update.message.reply_text(
        "🛍 <b>Nakrutka xizmatlari</b>\n\nPlatformani tanlang:",
        reply_markup=category_keyboard(), parse_mode=ParseMode.HTML
    )

async def category_callback(update, context):
    if not is_admin(update.effective_user.id) and not await subscription_gate(update, context):
        return
    q = update.callback_query
    await q.answer()
    parts = q.data.split(":")
    category = parts[1] if len(parts) > 1 else ""

    # Platforma tanlanganda avval xizmat turini ko'rsatamiz.
    if len(parts) == 2:
        title = "📦 Barcha SMM xizmatlar" if category == "all" else PLATFORM_LABELS.get(category, category.title())
        await q.message.reply_text(
            f"🛍 <b>{title}</b>\n\n👉 Kerakli xizmat turini tanlang:",
            reply_markup=service_type_keyboard(category),
            parse_mode=ParseMode.HTML
        )
        return

    service_type = parts[2] if len(parts) > 2 else "all"
    try:
        page = max(0, int(parts[3])) if len(parts) > 3 else 0
    except ValueError:
        page = 0

    page_size = 10
    total = service_count_by_category(category, service_type)
    last_page = max(0, (total - 1) // page_size)
    page = min(page, last_page)
    rows = services_by_category(category, page_size, page * page_size, service_type)

    if not rows:
        label = SERVICE_TYPE_RULES.get(service_type, ("Xizmatlar", []))[0]
        await q.message.reply_text(
            f"⚠️ {PLATFORM_LABELS.get(category, category.title())} bo'yicha <b>{label}</b> xizmati topilmadi.",
            parse_mode=ParseMode.HTML,
            reply_markup=service_type_keyboard(category)
        )
        return

    buttons = []
    for r in rows:
        # Tanlangan tur ichidagi xizmatlargina chiqadi; narx ham faqat shu xizmatga tegishli.
        buttons.append([
            InlineKeyboardButton(
                f"{r['name'][:55]} — {money(r['price'])}/1000",
                callback_data=f"svc:{r['id']}",
            )
        ])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Oldingi", callback_data=f"cat:{category}:{service_type}:{page - 1}"))
    if page < last_page:
        nav.append(InlineKeyboardButton("Keyingi ➡️", callback_data=f"cat:{category}:{service_type}:{page + 1}"))
    if nav:
        buttons.append(nav)

    buttons.append([InlineKeyboardButton("🔙 Xizmat turlari", callback_data=f"cat:{category}")])

    title = "📦 Barcha SMM xizmatlar" if category == "all" else PLATFORM_LABELS.get(category, category.title())
    subtitle = SERVICE_TYPE_RULES.get(service_type, ("Barcha xizmatlar", []))[0] if service_type != "all" else "📦 Barcha xizmatlar"
    await q.message.reply_text(
        f"🛍 <b>{title} · {subtitle}</b>\n"
        f"Jami: <b>{total} ta</b>\n"
        f"Sahifa {page + 1}/{last_page + 1}\n\nXizmatni tanlang:",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode=ParseMode.HTML
    )

async def service_callback(update, context):
    if not is_admin(update.effective_user.id) and not await subscription_gate(update, context):
        return
    q = update.callback_query
    await q.answer()
    sid = int(q.data.split(":")[1])
    s = get_service(sid)
    if not s:
        await q.message.reply_text("❌ Xizmat topilmadi.")
        return

    context.user_data["service_id"] = sid
    context.user_data["order_step"] = "link"

    await q.message.reply_text(
        f"🛍 <b>{s['name']}</b>\n\n"
        f"💵 Narx: {money(s['price'])} / 1000\n"
        f"📦 Min: {s['min_order']} | Max: {s['max_order']}\n\n"
        "🔗 Havolani yuboring:",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ Bekor qilish", callback_data="order:cancel")
        ]]),
        parse_mode=ParseMode.HTML
    )

async def order_text(update, context):
    if context.user_data.get("order_step") != "link":
        return False

    sid = context.user_data.get("service_id")
    s = get_service(sid)
    if not s:
        context.user_data.clear()
        await update.message.reply_text("❌ Xizmat topilmadi.")
        return True

    link = update.message.text.strip()
    context.user_data["link"] = link
    context.user_data["order_step"] = "quantity"

    await update.message.reply_text(
        f"🔢 Miqdorni yuboring.\n"
        f"Min: {s['min_order']} | Max: {s['max_order']}",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ Bekor qilish", callback_data="order:cancel")
        ]])
    )
    return True

async def quantity_text(update, context):
    if context.user_data.get("order_step") != "quantity":
        return False

    sid = context.user_data.get("service_id")
    s = get_service(sid)
    try:
        qty = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ Faqat son yuboring.")
        return True

    if qty < s["min_order"] or qty > s["max_order"]:
        await update.message.reply_text(
            f"❌ Miqdor {s['min_order']} dan {s['max_order']} gacha bo'lishi kerak."
        )
        return True

    amount = (Decimal(str(s["price"])) * Decimal(qty) / Decimal(1000)).quantize(Decimal("0.01"))
    bal = Decimal(str(get_balance(update.effective_user.id)))

    if bal < amount:
        context.user_data.clear()
        await update.message.reply_text(
            f"❌ Balansingiz yetarli emas.\n"
            f"Kerak: {money(amount)}\n"
            f"Balans: {money(bal)}\n\n"
            "💳 To'lov qilish bo'limidan balansni to'ldiring."
        )
        return True

    context.user_data["quantity"] = qty
    context.user_data["amount"] = float(amount)
    context.user_data["order_step"] = "confirm"

    await update.message.reply_text(
        f"🧾 <b>Buyurtmani tasdiqlash</b>\n\n"
        f"Xizmat: {s['name']}\n"
        f"Havola: {context.user_data['link']}\n"
        f"Miqdor: {qty}\n"
        f"Narx: {money(amount)}\n\n"
        "Tasdiqlaysizmi?",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Tasdiqlash", callback_data="order:yes"),
             InlineKeyboardButton("❌ Bekor qilish", callback_data="order:cancel")]
        ]),
        parse_mode=ParseMode.HTML
    )
    return True

async def order_confirm(update, context):
    if not is_admin(update.effective_user.id) and not await subscription_gate(update, context):
        return
    q = update.callback_query
    await q.answer()

    if q.data in ("order:no", "order:cancel"):
        clear_order_flow(context)
        await q.message.reply_text("❌ Buyurtma bekor qilindi.", reply_markup=main_menu())
        return

    sid = context.user_data.get("service_id")
    s = get_service(sid)
    link = context.user_data.get("link")
    qty = context.user_data.get("quantity")
    amount = context.user_data.get("amount")

    # 0 so'mlik (Tekin Xizmatlar) buyurtmalar ham haqiqiy buyurtma bo'la oladi.
    if s is None or not link or qty is None or amount is None:
        clear_order_flow(context)
        await q.message.reply_text("❌ Buyurtma ma'lumotlari yo'qoldi.")
        return

    if not take_balance(q.from_user.id, amount):
        await q.message.reply_text("❌ Balans yetarli emas.")
        return

    try:
        result = provider_add(s, link, qty)
        provider_id = str(result.get("order", "")) if isinstance(result, dict) else ""

        if not provider_id:
            raise RuntimeError(str(result))

        con = db()
        cur = con.execute(
            "INSERT INTO orders(user_id,service_id,link,quantity,amount,provider_order_id,status,created_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (
                q.from_user.id, sid, link, qty, amount,
                provider_id, "processing", datetime.now().isoformat()
            )
        )
        oid = cur.lastrowid
        con.commit()
        con.close()

        clear_order_flow(context)
        await q.message.reply_text(
            f"✅ <b>Buyurtma qabul qilindi!</b>\n\n"
            f"🆔 Buyurtma: #{oid}\n"
            f"📦 Miqdor: {qty}\n"
            f"💵 Narx: {money(amount)}\n"
            f"🔑 Provider ID: {provider_id}",
            parse_mode=ParseMode.HTML,
            reply_markup=main_menu()
        )
    except Exception as e:
        log.exception("Order error")
        add_balance(q.from_user.id, amount)
        clear_order_flow(context)
        await q.message.reply_text(
            "❌ Provider buyurtmani qabul qilmadi.\n"
            "Pul balansingizga qaytarildi.\n\n"
            f"Texnik sabab: {str(e)[:300]}"
        )

# ======================= DONATE =============================

async def donate(update, context):
    if not await subscription_gate(update, context):
        return
    con = db()
    rows = con.execute(
        "SELECT * FROM products WHERE category='donate' AND enabled=1 ORDER BY id"
    ).fetchall()
    con.close()

    buttons = [
        [InlineKeyboardButton(
            f"{r['name']} — {money(r['price'])}",
            callback_data=f"don:{r['id']}"
        )] for r in rows
    ]
    buttons.append([InlineKeyboardButton("⬅️ Menyu", callback_data="menu")])

    await update.message.reply_text(
        "🎮 <b>Donat</b>\n\nFree Fire / PUBG xizmatini tanlang:",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode=ParseMode.HTML
    )

async def donate_callback(update, context):
    if not is_admin(update.effective_user.id) and not await subscription_gate(update, context):
        return
    q = update.callback_query
    await q.answer()
    pid = int(q.data.split(":")[1])
    con = db()
    row = con.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
    con.close()
    if not row:
        await q.message.reply_text("❌ Mahsulot topilmadi.")
        return

    context.user_data["donate_id"] = pid
    context.user_data["donate_step"] = "player"
    await q.message.reply_text(
        f"🎮 {row['name']}\n"
        f"💵 {money(row['price'])}\n\n"
        "🆔 Player ID / UID yuboring:"
    )

async def donate_text(update, context):
    if context.user_data.get("donate_step") != "player":
        return False

    pid = context.user_data["donate_id"]
    con = db()
    row = con.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
    con.close()

    if not row:
        context.user_data.clear()
        return True

    if not take_balance(update.effective_user.id, row["price"]):
        await update.message.reply_text("❌ Balansingiz yetarli emas.")
        context.user_data.clear()
        return True

    # External Donate API is intentionally generic because each provider
    # has different request fields. If configured, this sends a common form.
    if DONATE_API_URL and DONATE_API_KEY:
        try:
            resp = requests.post(
                DONATE_API_URL,
                data={
                    "key": DONATE_API_KEY,
                    "product": row["provider_code"],
                    "player_id": update.message.text.strip()
                },
                timeout=30
            )
            data = resp.json()
            ok = bool(data.get("success") or data.get("status") in ("success", "ok"))
            if not ok:
                add_balance(update.effective_user.id, row["price"])
                await update.message.reply_text("❌ Donat provider buyurtmani qabul qilmadi. Pul qaytarildi.")
                context.user_data.clear()
                return True
        except Exception as e:
            add_balance(update.effective_user.id, row["price"])
            await update.message.reply_text("❌ Donat API xatosi. Pul qaytarildi.")
            context.user_data.clear()
            return True
    else:
        # Without provider documentation, do not pretend that a real top-up happened.
        add_balance(update.effective_user.id, row["price"])
        await update.message.reply_text(
            "⚠️ Donat API hali sozlanmagan. Admin DONATE_API_URL va DONATE_API_KEY ni qo'shishi kerak.\n"
            "Pul balansingizga qaytarildi."
        )
        context.user_data.clear()
        return True

    context.user_data.clear()
    await update.message.reply_text("✅ Donat buyurtmasi qabul qilindi.", reply_markup=main_menu())
    return True

# =================== NUMBER / STARS =========================

def admin_profile_html():
    # Open the admin's Telegram profile directly. Keep the configured support
    # username as the visible label, while using the numeric admin ID for a
    # reliable tg:// profile link.
    support = get_setting("support", "@admin").strip() or "@admin"
    admin_id = next(iter(ADMIN_IDS), 0)
    if admin_id:
        return f'<a href="tg://user?id={admin_id}">{support}</a>'
    return support

async def numbers(update, context):
    if not await subscription_gate(update, context): return
    text = (
        "📱 <b>Raqam olish</b>\n\n"
        "Raqamlar admin orqali beriladi.\n"
        f"👨‍💻 Admin: {admin_profile_html()}\n\n"
        "Kerakli raqam uchun adminga yozing."
    )
    await update.message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        entities=premium_entities(text),
        reply_markup=main_menu()
    )

async def stars(update, context):
    text = (
        "⭐ <b>Stars | Premium</b>\n\n"
        "Stars va Premium admin orqali beriladi.\n"
        f"👨‍💻 Admin: {admin_profile_html()}\n\n"
        "Kerakli Stars yoki Premium muddatini adminga yozing."
    )
    await update.message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        entities=premium_entities(text),
        reply_markup=main_menu()
    )

# ======================== ADMIN =============================

async def admin(update, context):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Siz admin emassiz.")
        return
    await update.message.reply_text("🛠 <b>Admin panel</b>", reply_markup=admin_menu(), parse_mode=ParseMode.HTML)

async def send_price_page(q, page=0):
    PAGE_SIZE = 20
    con = db()
    total = con.execute("SELECT COUNT(*) AS c FROM services WHERE enabled=1").fetchone()["c"]
    last_page = max(0, (int(total) - 1) // PAGE_SIZE)
    page = min(max(0, int(page)), last_page)
    rows = con.execute(
        "SELECT id,name,price,category FROM services WHERE enabled=1 ORDER BY id LIMIT ? OFFSET ?",
        (PAGE_SIZE, page * PAGE_SIZE),
    ).fetchall()
    con.close()

    buttons = []
    for r in rows:
        platform = PLATFORM_LABELS.get(r["category"], r["category"].title())
        buttons.append([InlineKeyboardButton(
            f"#{r['id']} {platform} · {r['name'][:24]} — {money(r['price'])}",
            callback_data=f"priceedit:{r['id']}",
            api_kwargs={"style": "primary"},
        )])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Oldingi", callback_data=f"adm:prices:{page-1}", api_kwargs={"style":"primary"}))
    if page < last_page:
        nav.append(InlineKeyboardButton("Keyingi ➡️", callback_data=f"adm:prices:{page+1}", api_kwargs={"style":"primary"}))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton("⬅️ Admin panel", callback_data="adm:home", api_kwargs={"style":"primary"})])

    await q.message.reply_text(
        f"💰 <b>Xizmat narxlarini sozlash</b>\n\nJami: <b>{total} ta</b>\nSahifa: <b>{page + 1}/{last_page + 1}</b>\n\nXizmatni tanlang:",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode=ParseMode.HTML,
    )


async def admin_callback(update, context):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        return
    action = q.data.split(":", 1)[1]

    if action == "home":
        await q.message.reply_text("🛠 <b>Admin panel</b>", reply_markup=admin_menu(), parse_mode=ParseMode.HTML)
        return

    if action == "stats":
        con = db()
        users = con.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
        orders_n = con.execute("SELECT COUNT(*) c FROM orders").fetchone()["c"]
        volume = con.execute("SELECT COALESCE(SUM(amount),0) s FROM orders").fetchone()["s"]
        paid = con.execute("SELECT COALESCE(SUM(amount),0) s FROM payments WHERE status='paid'").fetchone()["s"]
        pending = con.execute("SELECT COUNT(*) c FROM payment_requests WHERE status='pending'").fetchone()["c"]
        services = con.execute("SELECT COUNT(*) c FROM services WHERE enabled=1").fetchone()["c"]
        con.close()
        api_bal = "N/A"
        try:
            b = provider_balance(); api_bal = str(b.get("balance", b))
        except Exception: pass
        await q.message.reply_text(
            f"📊 <b>Statistika</b>\n\n👥 Foydalanuvchilar: {users}\n"
            f"🛒 Buyurtmalar: {orders_n}\n💵 Aylanma: {money(volume)}\n"
            f"💳 Tasdiqlangan to'lovlar: {money(paid)}\n⏳ Kutilayotgan to'lovlar: {pending}\n"
            f"🛍 Aktiv xizmatlar: {services}\n🔓 SMM API balansi: {api_bal}",
            parse_mode=ParseMode.HTML, reply_markup=admin_back())
        return

    if action == "users":
        con=db(); rows=con.execute("SELECT id,username,balance,created_at FROM users ORDER BY id DESC LIMIT 30").fetchall(); con.close()
        text=["👥 <b>Foydalanuvchilar</b>"]
        for r in rows:
            name = f"@{r['username']}" if r['username'] else str(r['id'])
            text.append(f'<a href="tg://user?id={r["id"]}">{name}</a> | ID: <code>{r["id"]}</code> | {money(r["balance"])}')
        await q.message.reply_text("\n".join(text), parse_mode=ParseMode.HTML, reply_markup=admin_back()); return

    if action == "user_search":
        context.user_data["admin_state"]="user_search"
        await q.message.reply_text("🔎 Foydalanuvchi Telegram ID sini yuboring:"); return

    if action == "balance":
        context.user_data["admin_state"]="balance_user"
        await q.message.reply_text("💳 Balans boshqaruvi uchun foydalanuvchi ID sini yuboring:"); return

    if action == "payments":
        con=db(); rows=con.execute("SELECT * FROM payment_requests WHERE status='pending' ORDER BY id DESC LIMIT 20").fetchall(); con.close()
        if not rows:
            await q.message.reply_text("💵 Kutilayotgan to'lovlar yo'q.",reply_markup=admin_back()); return
        for r in rows:
            u=await context.bot.get_chat(r['user_id'])
            username=f"@{u.username}" if getattr(u,'username',None) else str(r['user_id'])
            kb=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Tasdiqlash",callback_data=f"pay:approve:{r['id']}",api_kwargs={"style":"success"}),InlineKeyboardButton("❌ Bekor qilish",callback_data=f"pay:reject:{r['id']}",api_kwargs={"style":"danger"})]])
            cap=f"💵 <b>To'lov #{r['id']}</b>\n👤 {username}\n🆔 <code>{r['user_id']}</code>\n💰 <b>{money(r['amount'])}</b>\n\nTasdiqlash yoki bekor qilishni tanlang."
            try:
                if r['receipt_type']=='photo': await context.bot.send_photo(q.from_user.id,r['receipt_file_id'],caption=cap,parse_mode=ParseMode.HTML,reply_markup=kb)
                else: await context.bot.send_document(q.from_user.id,r['receipt_file_id'],caption=cap,parse_mode=ParseMode.HTML,reply_markup=kb)
            except Exception: await q.message.reply_text(cap,parse_mode=ParseMode.HTML,reply_markup=kb)
        return

    if action == "card":
        await q.message.reply_text(
            f"💳 <b>Karta sozlamalari</b>\n\nKarta: <code>{get_setting('payment_card')}</code>\nEgasi: <b>{get_setting('payment_owner')}</b>\n\nYangi karta raqamini yuboring.",
            parse_mode=ParseMode.HTML)
        context.user_data["admin_state"]="payment_card"
        return

    if action == "numbers":
        con=db(); rows=con.execute("SELECT * FROM number_inventory ORDER BY id DESC LIMIT 30").fetchall(); con.close()
        text=["📱 <b>Raqamlarni boshqarish</b>","🔒 Faqat admin boshqaradi.",""]
        for r in rows: text.append(f"#{r['id']} | <code>{r['phone']}</code> | {money(r['price'])} | {'🟢 mavjud' if r['status']=='available' else '🔴 '+r['status']}")
        kb=[[InlineKeyboardButton("➕ Raqam qo'shish",callback_data="num:admin_add")],[InlineKeyboardButton("🗑 Raqamni o'chirish",callback_data="num:admin_delete")],[InlineKeyboardButton("⬅️ Admin panel",callback_data="adm:home")]]
        await q.message.reply_text("\n".join(text) if rows else "📱 Raqamlar hali qo'shilmagan.",parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup(kb)); return

    if action == "freeadd":
        context.user_data["admin_state"]="free_category"
        await q.message.reply_text("🔥 Tekin xizmat qaysi platformaga? telegram / instagram / tiktok / youtube"); return

    if action == "referral":
        context.user_data["admin_state"] = "ref_reward"
        current = get_setting("ref_reward", "25")
        await q.message.reply_text(
            f"👥 <b>Do'st uchun bonus</b>\n\n"
            f"Hozirgi narx: <b>{money(current)}</b>\n\n"
            f"1 ta yangi do'st uchun beriladigan bonusni so'mda yuboring:",
            parse_mode=ParseMode.HTML
        )
        return

    if action == "promos":
        con = db()
        rows = con.execute(
            "SELECT id,code,reward,usage_limit,used_count,active "
            "FROM promocodes ORDER BY id DESC LIMIT 15"
        ).fetchall()
        con.close()
        text = ["🎟 <b>Promokodlar</b>\n"]
        buttons = []
        for row in rows:
            status = "🟢 faol" if row["active"] else "🔴 o'chirilgan"
            text.append(
                f"<code>{row['code']}</code> · {money(row['reward'])} · "
                f"{row['used_count']}/{row['usage_limit']} · {status}"
            )
            buttons.append([InlineKeyboardButton(
                f"{'⏸ O‘chirish' if row['active'] else '▶️ Yoqish'}: {row['code']}",
                callback_data=f"promo:toggle:{row['id']}"
            )])
        if len(text) == 1:
            text.append("Hozircha promokod yaratilmagan.")
        buttons.append([InlineKeyboardButton("➕ Promokod yaratish", callback_data="promo:create")])
        buttons.append([InlineKeyboardButton("⬅️ Admin panel", callback_data="adm:home")])
        await q.message.reply_text(
            "\n".join(text),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(buttons)
        )
        return

    if action == "services":
        con=db(); rows=con.execute("SELECT id,name,category,price,enabled FROM services ORDER BY id DESC LIMIT 40").fetchall(); con.close()
        if not rows:
            await q.message.reply_text("Xizmatlar yo'q. Avval SMM xizmatlarini yuklang.", reply_markup=admin_back()); return
        text=["🛍 <b>Xizmatlar</b>\n"]
        for r in rows: text.append(f"#{r['id']} | {r['category']} | {r['name'][:40]} | {money(r['price'])} | {'ON' if r['enabled'] else 'OFF'}")
        await q.message.reply_text("\n".join(text), parse_mode=ParseMode.HTML, reply_markup=admin_back()); return

    if action == "prices":
        await send_price_page(q, 0)
        return

    if action.startswith("prices:"):
        try:
            page = max(0, int(action.split(":", 1)[1]))
        except ValueError:
            page = 0
        await send_price_page(q, page)
        return

    if action == "donate":
        con=db(); rows=con.execute("SELECT id,name,price FROM products WHERE category='donate' ORDER BY id").fetchall(); con.close()
        buttons=[[InlineKeyboardButton(f"#{r['id']} {r['name'][:30]} — {money(r['price'])}", callback_data=f"donprice:{r['id']}")] for r in rows]
        buttons.append([InlineKeyboardButton("⬅️ Admin panel", callback_data="adm:home")])
        await q.message.reply_text("🎮 <b>Donat narxini tanlang:</b>", reply_markup=InlineKeyboardMarkup(buttons), parse_mode=ParseMode.HTML); return


    if action == "force":
        channels=get_force_channels()
        limit=get_setting("force_limit", "0")
        buttons=[[InlineKeyboardButton(f"❌ {ch}", callback_data=f"force:remove:{ch}")] for ch in channels]
        buttons += [
            [InlineKeyboardButton("➕ Kanal qo'shish", callback_data="force:add"), InlineKeyboardButton("🔢 Limit", callback_data="force:limit")],
            [InlineKeyboardButton("🗑 Barchasini tozalash", callback_data="force:clear")],
            [InlineKeyboardButton("⬅️ Admin panel", callback_data="adm:home")]
        ]
        txt=("\n".join(channels) if channels else "Kanal yo'q.")
        txt += f"\n\n📌 Limit: <b>{limit if limit != '0' else 'Cheklanmagan'}</b>"
        await q.message.reply_text("📢 <b>Majburiy obuna</b>\n\n" + txt, reply_markup=InlineKeyboardMarkup(buttons), parse_mode=ParseMode.HTML); return

    if action == "broadcast":
        context.user_data["admin_state"]="broadcast"
        await q.message.reply_text("📣 Endi yuborgan xabaringiz barcha foydalanuvchilarga yuboriladi. Bekor qilish: /cancel", reply_markup=admin_back()); return

    if action == "emoji":
        con=db(); rows=con.execute("SELECT id,unicode_emoji,custom_emoji_id,active FROM premium_emojis ORDER BY id").fetchall(); con.close()
        text=["✨ <b>Premium emoji</b>\n"]
        for r in rows: text.append(f"#{r['id']} {r['unicode_emoji']} → {r['custom_emoji_id']} | {'ON' if r['active'] else 'OFF'}")
        kb=[[InlineKeyboardButton("➕ Emoji qo'shish", callback_data="premium:add")], [InlineKeyboardButton("🔄 Yoqish/O'chirish", callback_data="premium:toggle")], [InlineKeyboardButton("⬅️ Admin panel", callback_data="adm:home")]]
        await q.message.reply_text("\n".join(text), reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.HTML); return

    if action == "settings":
        await q.message.reply_text(
            "⚙️ <b>Sozlamalar</b>\n\n"
            f"👋 Salomlashish: {get_setting('welcome')}\n"
            f"👨‍💻 Support: {get_setting('support')}\n"
            f"💰 Minimal to'lov: {get_setting('min_payment')}\n"
            f"💳 To'lov: karta + admin tasdiqlashi\n"
            f"📢 Majburiy kanallar: {len(get_force_channels())}\n"
            f"🔢 Majburiy obuna limiti: {get_setting('force_limit','0')}\n"
            f"✨ Premium emoji: {'ON' if get_setting('premium_emoji_enabled','1')=='1' else 'OFF'}",
            parse_mode=ParseMode.HTML, reply_markup=admin_back()); return

    if action == "database":
        await q.message.reply_text(database_info_text(), parse_mode=ParseMode.HTML, reply_markup=database_menu_markup())
        return

    if action == "sync":
        try: n=sync_services_from_provider(); msg=f"✅ SMM API dan {n} ta xizmat yuklandi."
        except Exception as e: msg=f"❌ Sync xatosi:\n{str(e)[:800]}"
        await q.message.reply_text(msg, reply_markup=admin_back()); return

async def price_command(update, context):
    if not is_admin(update.effective_user.id):
        return
    if len(context.args) != 2:
        await update.message.reply_text("Format: /price SERVICE_ID YANGI_NARX")
        return
    try:
        sid = int(context.args[0])
        price = float(context.args[1])
    except ValueError:
        await update.message.reply_text("❌ ID va narxni to'g'ri kiriting.")
        return

    con = db()
    cur = con.execute("UPDATE services SET price=? WHERE id=?", (price, sid))
    con.commit()
    con.close()
    await update.message.reply_text(
        "✅ Narx yangilandi." if cur.rowcount else "❌ Xizmat topilmadi."
    )

async def admin_text(update, context):
    if not is_admin(update.effective_user.id):
        return False

    if context.user_data.get("admin_action") == "broadcast":
        text = update.message.text
        con = db()
        cur = con.execute(
            "INSERT INTO broadcasts(admin_id,text,created_at) VALUES(?,?,?)",
            (update.effective_user.id, text, datetime.now().isoformat())
        )
        broadcast_id = cur.lastrowid
        users = con.execute("SELECT id FROM users").fetchall()
        con.commit()
        con.close()

        sent = failed = 0
        for row in users:
            try:
                await context.bot.send_message(row["id"], text)
                sent += 1
            except Exception:
                failed += 1

        con = db()
        con.execute(
            "UPDATE broadcasts SET sent=?,failed=? WHERE id=?",
            (sent, failed, broadcast_id)
        )
        con.commit()
        con.close()

        context.user_data.pop("admin_action", None)
        await update.message.reply_text(
            f"📣 Xabar yuborildi.\n✅ {sent}\n❌ {failed}"
        )
        return True

    return False

# ======================= CALLBACK MENU ======================

async def admin_action_callback(update, context):
    q=update.callback_query
    if not is_admin(q.from_user.id):
        await q.answer("⛔ Ruxsat yo'q"); return
    data=q.data
    await q.answer()

    if data.startswith("pay:approve:") or data.startswith("pay:reject:"):
        parts=data.split(":"); rid=int(parts[2]); approve=parts[1]=="approve"
        con=db(); r=con.execute("SELECT * FROM payment_requests WHERE id=?",(rid,)).fetchone()
        if not r or r['status']!="pending":
            await q.message.reply_text("⚠️ Bu to'lov allaqachon ko'rib chiqilgan."); return
        if approve:
            con.execute("UPDATE users SET balance=balance+? WHERE id=?",(r['amount'],r['user_id']))
            con.execute("INSERT INTO payments(user_id,amount,method,status,created_at) VALUES(?,?,?,?,?)",(r['user_id'],r['amount'],"manual", "paid",datetime.now().isoformat()))
            con.execute("UPDATE payment_requests SET status='approved',reviewed_at=? WHERE id=?",(datetime.now().isoformat(),rid)); con.commit(); con.close()
            await context.bot.send_message(r['user_id'],f"✅ To'lovingiz tasdiqlandi!\n💰 Balansga {money(r['amount'])} qo'shildi.")
            await q.edit_message_reply_markup(reply_markup=None); await q.message.reply_text(f"✅ To'lov #{rid} tasdiqlandi.",reply_markup=admin_menu())
        else:
            con.execute("UPDATE payment_requests SET status='rejected',reviewed_at=? WHERE id=?",(datetime.now().isoformat(),rid)); con.commit(); con.close()
            await context.bot.send_message(r['user_id'],"❌ To'lovingiz admin tomonidan bekor qilindi. Agar xato bo'lsa, qayta chek yuboring.")
            await q.edit_message_reply_markup(reply_markup=None); await q.message.reply_text(f"❌ To'lov #{rid} bekor qilindi.",reply_markup=admin_menu())
        return

    if data == "num:admin_add":
        context.user_data["admin_state"]="number_add_phone"
        await q.message.reply_text("📱 Raqamni yuboring (masalan +998901234567):\n/cancel — bekor qilish"); return
    if data == "num:admin_delete":
        context.user_data["admin_state"]="number_delete"
        await q.message.reply_text("🗑 O'chiriladigan raqam ID sini yuboring:"); return
    if data == "num:orders":
        con=db(); rows=con.execute("SELECT no.*,ni.phone FROM number_orders no JOIN number_inventory ni ON ni.id=no.number_id WHERE no.status='pending' ORDER BY no.id DESC").fetchall(); con.close()
        if not rows: await q.message.reply_text("📋 Kutilayotgan raqam buyurtmalari yo'q.",reply_markup=admin_back()); return
        for r in rows:
            kb=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Tasdiqlash",callback_data=f"num:approve:{r['id']}",api_kwargs={"style":"success"}),InlineKeyboardButton("❌ Bekor qilish",callback_data=f"num:reject:{r['id']}",api_kwargs={"style":"danger"})]])
            contract=get_setting('number_contract','')
            text=(f"📱 <b>Raqam buyurtmasi #{r['id']}</b>\n\n👤 User ID: <code>{r['user_id']}</code>\n📞 Raqam: <code>{r['phone']}</code>\n💰 Narx: <b>{money(r['price'])}</b>\n\n<b>Shartlar:</b>\n{contract}\n\n<b>Admin nima qiladi:</b> to'lovni tekshiradi, raqamni tasdiqlasa foydalanuvchiga yuboradi. Raqam 10 daqiqa amal qiladi.")
            await q.message.reply_text(text,parse_mode=ParseMode.HTML,reply_markup=kb); return
    if data.startswith("num:approve:") or data.startswith("num:reject:"):
        await q.answer("Raqam buyurtmalari hozircha o'chirilgan", show_alert=True)
        return

    if data == "db:send":
        path = database_path()
        if not os.path.exists(path):
            init_db()
        await context.bot.send_document(
            chat_id=q.from_user.id,
            document=path,
            caption=f"🗄 Joriy baza fayli: {os.path.basename(path)}\n📦 Hajmi: {database_size_text(path)}"
        )
        return

    if data == "db:backup":
        try:
            path = backup_current_database("backup")
            await context.bot.send_document(
                chat_id=q.from_user.id,
                document=path,
                caption=f"✅ Backup yaratildi: {os.path.basename(path)}"
            )
            await q.message.reply_text(database_info_text(), parse_mode=ParseMode.HTML, reply_markup=database_menu_markup())
        except Exception as exc:
            await q.message.reply_text(f"❌ Backup xatosi: {str(exc)[:500]}", reply_markup=database_menu_markup())
        return

    if data == "db:upload":
        context.user_data["admin_state"] = "db_upload"
        await q.message.reply_text(
            "➕ <b>Baza faylini yuboring</b>\n\n"
            "Faqat SQLite fayl qabul qilinadi: .db, .sqlite yoki .sqlite3\n"
            "Yuborilgan baza avval saqlanadi, joriy baza avtomatik almashtirilmaydi.\n\n"
            "Bekor qilish: /cancel",
            parse_mode=ParseMode.HTML,
            reply_markup=admin_back()
        )
        return

    if data == "db:list":
        backups = database_backups()
        if not backups:
            await q.message.reply_text(
                "📂 Hozircha saqlangan baza fayllari yo'q.",
                reply_markup=database_menu_markup()
            )
            return
        buttons = []
        lines = ["📂 <b>Saqlangan baza fayllari</b>\n"]
        for path in backups[:20]:
            name = os.path.basename(path)
            lines.append(f"• <code>{name}</code> — {database_size_text(path)}")
            buttons.append([
                InlineKeyboardButton(f"♻️ Tiklash: {name[:24]}", callback_data=f"db:restore:{name}"),
                InlineKeyboardButton("🗑", callback_data=f"db:delete:{name}", api_kwargs={"style": "danger"})
            ])
        buttons.append([InlineKeyboardButton("⬅️ Baza menyusi", callback_data="adm:database")])
        await q.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(buttons))
        return

    if data.startswith("db:delete:"):
        name = data.split(":", 2)[2]
        path = os.path.join(database_backup_dir(), safe_database_name(name))
        if os.path.commonpath([os.path.abspath(path), database_backup_dir()]) != database_backup_dir():
            await q.message.reply_text("❌ Fayl nomi noto'g'ri.", reply_markup=database_menu_markup()); return
        if not os.path.exists(path):
            await q.message.reply_text("❌ Fayl topilmadi.", reply_markup=database_menu_markup()); return
        try:
            os.remove(path)
            await q.message.reply_text("✅ Baza fayli o'chirildi.", reply_markup=database_menu_markup())
        except Exception as exc:
            await q.message.reply_text(f"❌ O'chirish xatosi: {str(exc)[:300]}", reply_markup=database_menu_markup())
        return

    if data.startswith("db:restore:"):
        name = data.split(":", 2)[2]
        path = os.path.join(database_backup_dir(), safe_database_name(name))
        if not os.path.exists(path) or not is_valid_database(path):
            await q.message.reply_text("❌ Bu fayl yaroqli Prime SMM SQLite bazasi emas.", reply_markup=database_menu_markup())
            return
        try:
            backup_current_database("before_restore")
            target = database_path()
            tmp = target + ".restoretmp"
            shutil.copy2(path, tmp)
            os.replace(tmp, target)
            init_db()
            await q.message.reply_text(
                f"✅ Baza tiklandi: <code>{os.path.basename(path)}</code>\n\nBot keyingi so'rovdan yangi bazadan foydalanadi.",
                parse_mode=ParseMode.HTML,
                reply_markup=database_menu_markup()
            )
        except Exception as exc:
            try:
                if os.path.exists(target + ".restoretmp"):
                    os.remove(target + ".restoretmp")
            except Exception:
                pass
            await q.message.reply_text(f"❌ Tiklash xatosi: {str(exc)[:500]}", reply_markup=database_menu_markup())
        return

    if data.startswith("priceedit:"):
        sid=int(data.split(":")[1]); s=get_service(sid)
        if not s: return
        context.user_data["admin_state"]="service_price"; context.user_data["edit_id"]=sid
        await q.message.reply_text(f"💰 {s['name']}\nHozirgi narx: {money(s['price'])} / 1000\n\nYangi narxni yuboring:"); return

    if data.startswith("donprice:"):
        pid=int(data.split(":")[1]); con=db(); r=con.execute("SELECT * FROM products WHERE id=?",(pid,)).fetchone(); con.close()
        if not r: return
        context.user_data["admin_state"]="donate_price"; context.user_data["edit_id"]=pid
        await q.message.reply_text(f"🎮 {r['name']}\nHozirgi narx: {money(r['price'])}\n\nYangi narxni yuboring:"); return

    if data == "force:add":
        channels=get_force_channels(); limit=int(get_setting("force_limit", "0") or 0)
        if limit > 0 and len(channels) >= limit:
            await q.message.reply_text(f"❌ Majburiy kanal limiti ({limit}) to'ldi.", reply_markup=admin_menu()); return
        context.user_data["admin_state"]="force_add"
        await q.message.reply_text("📢 Kanal username yuboring, masalan: @mychannel\n\nBot kanalga admin bo'lishi va a'zolarni ko'ra olishi kerak."); return

    if data == "force:limit":
        context.user_data["admin_state"]="force_limit"
        await q.message.reply_text("🔢 Majburiy obuna kanallari limitini yuboring. 0 = cheklanmagan."); return

    if data == "force:clear":
        set_setting("force_channels", "")
        await q.message.reply_text("✅ Barcha majburiy kanallar olib tashlandi.", reply_markup=admin_menu()); return

    if data.startswith("force:remove:"):
        ch=data.split(":",2)[2]
        current=[x for x in get_force_channels() if x != ch]
        set_setting("force_channels", ",".join(current))
        await q.message.reply_text(f"✅ O'chirildi: {ch}", reply_markup=admin_menu()); return

    if data == "premium:add":
        context.user_data["admin_state"]="premium_symbol"
        await q.message.reply_text("✨ Oddiy emoji belgisini yuboring, masalan: ⭐"); return

    if data == "premium:toggle":
        cur=get_setting("premium_emoji_enabled","1")=="1"; set_setting("premium_emoji_enabled","0" if cur else "1")
        await q.message.reply_text(f"✨ Premium emoji: {'🔴 OFF' if cur else '🟢 ON'}", reply_markup=admin_menu()); return


async def promo_callback(update, context):
    if not is_admin(update.effective_user.id) and not await subscription_gate(update, context):
        return
    q = update.callback_query
    await q.answer()
    data = q.data

    if data == "promo:enter":
        if not await subscription_gate(update, context):
            return
        context.user_data["promo_step"] = "code"
        await q.message.reply_text(
            "🎟 Promokod matnini yuboring:",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Bekor qilish", callback_data="promo:cancel")
            ]])
        )
        return

    if data == "promo:create":
        if not is_admin(q.from_user.id):
            await q.message.reply_text("⛔ Ruxsat yo'q.")
            return
        context.user_data["admin_state"] = "promo_code"
        await q.message.reply_text(
            "Yangi promokod nomini yuboring (3–24 ta harf yoki raqam):",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Bekor qilish", callback_data="promo:cancel")
            ]])
        )
        return

    if data.startswith("promo:toggle:"):
        if not is_admin(q.from_user.id):
            await q.message.reply_text("⛔ Ruxsat yo'q.")
            return
        try:
            promo_id = int(data.rsplit(":", 1)[1])
        except ValueError:
            await q.message.reply_text("❌ Promokod topilmadi.", reply_markup=admin_menu())
            return
        if toggle_promocode(promo_id):
            await q.message.reply_text(
                "✅ Promokod holati o'zgartirildi.",
                reply_markup=admin_menu()
            )
        else:
            await q.message.reply_text("❌ Promokod topilmadi.", reply_markup=admin_menu())
        return

    if data == "promo:cancel":
        context.user_data.pop("promo_step", None)
        if str(context.user_data.get("admin_state", "")).startswith("promo_"):
            context.user_data.pop("admin_state", None)
            for key in ("promo_code_pending", "promo_reward_pending"):
                context.user_data.pop(key, None)
        await q.message.reply_text(
            "❌ Amal bekor qilindi.",
            reply_markup=admin_menu() if is_admin(q.from_user.id) else main_menu()
        )

async def cancel_command(update, context):
    clear_order_flow(context)
    context.user_data.pop("promo_step", None)
    context.user_data.pop("admin_state", None)
    for key in (
        "promo_code_pending", "promo_reward_pending", "edit_id", "premium_symbol",
        "payment_step", "payment_amount", "donate_step", "donate_id", "admin_action"
    ):
        context.user_data.pop(key, None)
    await update.message.reply_text(
        "❌ Joriy amal bekor qilindi.",
        reply_markup=admin_menu() if is_admin(update.effective_user.id) else main_menu()
    )

async def free_services_callback(update, context):
    if not is_admin(update.effective_user.id) and not await subscription_gate(update, context):
        return
    q=update.callback_query; await q.answer()
    category=q.data.split(":",1)[1]
    con=db()
    if category == "all":
        rows=con.execute("SELECT * FROM services WHERE enabled=1 AND price=0 ORDER BY id DESC LIMIT 50").fetchall()
    else:
        rows=con.execute("SELECT * FROM services WHERE enabled=1 AND price=0 AND category=? ORDER BY id DESC LIMIT 50",(category,)).fetchall()
    con.close()
    if not rows:
        await q.message.reply_text("🔥 <b>Tekin Xizmatlar</b>\n\nHozircha tekin xizmat yo'q.",parse_mode=ParseMode.HTML,reply_markup=category_keyboard() if category=="all" else service_type_keyboard(category)); return
    buttons=[]
    for r in rows:
        buttons.append([InlineKeyboardButton(f"🔥 {r['name'][:55]} — BEPUL",callback_data=f"svc:{r['id']}",api_kwargs={"style":"primary","icon_custom_emoji_id":"5442939099906325301"})])
    buttons.append([InlineKeyboardButton("⬅️ Orqaga",callback_data="nakrutka")])
    await q.message.reply_text("🔥 <b>Tekin Xizmatlar</b>\n\nXizmatni tanlang:",parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup(buttons))

async def general_callback(update, context):
    if not is_admin(update.effective_user.id) and not await subscription_gate(update, context):
        await update.callback_query.answer()
        return
    q = update.callback_query
    data = q.data

    if data == "menu":
        await q.answer()
        await q.message.reply_text("🏠 Bosh menyu", reply_markup=main_menu())
        return

    if data == "nakrutka":
        await q.answer()
        await q.message.reply_text(
            "🛍 Platformani tanlang:",
            reply_markup=category_keyboard()
        )
        return

# ========================= MESSAGE ==========================

async def message_router(update, context):
    ensure_user(update.effective_user)

    # Admin states first
    if is_admin(update.effective_user.id):
        state=context.user_data.get("admin_state")
        text=(update.message.text or "").strip() if update.message else ""
        if state == "payment_card":
            card=re.sub(r"\D", "", text)
            if len(card) < 12: await update.message.reply_text("❌ Karta raqamini to'g'ri yuboring."); return
            set_setting("payment_card", card); context.user_data["admin_state"]="payment_owner"
            await update.message.reply_text("👤 Endi karta egasining ism/familiyasini yuboring:"); return
        if state == "payment_owner":
            if not text: return
            set_setting("payment_owner", text[:100]); context.user_data.clear(); await update.message.reply_text("✅ Karta ma'lumotlari saqlandi.",reply_markup=admin_menu()); return
        if state == "number_add_phone":
            if not text: return
            context.user_data["number_phone"]=text; context.user_data["admin_state"]="number_add_price"
            await update.message.reply_text("💰 Shu raqam narxini so'mda yuboring:"); return
        if state == "number_add_price":
            amount=parse_amount(text)
            if amount is None or amount <= 0: await update.message.reply_text("❌ Narxni to'g'ri yuboring."); return
            phone=context.user_data.get("number_phone")
            con=db()
            try:
                con.execute("INSERT INTO number_inventory(phone,price,status) VALUES(?,?,?)",(phone,float(amount),"available")); con.commit(); msg="✅ Raqam sotuvga qo'shildi."
            except sqlite3.IntegrityError: msg="❌ Bu raqam allaqachon mavjud."
            con.close(); context.user_data.clear(); await update.message.reply_text(msg,reply_markup=admin_menu()); return
        if state == "number_delete":
            try: nid=int(text)
            except: await update.message.reply_text("❌ Raqam ID sini yuboring."); return
            con=db(); cur=con.execute("DELETE FROM number_inventory WHERE id=? AND status='available'",(nid,)); con.commit(); con.close(); context.user_data.clear(); await update.message.reply_text("✅ Raqam o'chirildi." if cur.rowcount else "❌ Mavjud raqam topilmadi.",reply_markup=admin_menu()); return
        if state == "broadcast":
            con=db(); cur=con.execute("INSERT INTO broadcasts(admin_id,text,created_at) VALUES(?,?,?)",(update.effective_user.id,text,datetime.now().isoformat())); bid=cur.lastrowid; users=con.execute("SELECT id FROM users").fetchall(); con.commit(); con.close()
            sent=failed=0
            for row in users:
                try: await context.bot.send_message(row["id"], text); sent+=1
                except Exception: failed+=1
            con=db(); con.execute("UPDATE broadcasts SET sent=?,failed=? WHERE id=?",(sent,failed,bid)); con.commit(); con.close(); context.user_data.pop("admin_state",None)
            await update.message.reply_text(f"📣 Xabar yuborildi.\n✅ {sent}\n❌ {failed}", reply_markup=admin_menu()); return
        if state == "force_limit":
            try: limit=int(text)
            except ValueError: limit=-1
            if limit < 0:
                await update.message.reply_text("❌ 0 yoki musbat butun son yuboring."); return
            if limit and len(get_force_channels()) > limit:
                await update.message.reply_text(f"❌ Hozir {len(get_force_channels())} ta kanal bor. Avval kanallarni {limit} tagacha kamaytiring."); return
            set_setting("force_limit", str(limit)); context.user_data.pop("admin_state",None)
            await update.message.reply_text(f"✅ Limit saqlandi: {limit if limit else 'cheklanmagan'}", reply_markup=admin_menu()); return

        if state == "user_search":
            try: uid=int(text)
            except ValueError: uid=0
            con=db(); row=con.execute("SELECT * FROM users WHERE id=?",(uid,)).fetchone(); con.close()
            context.user_data.pop("admin_state",None)
            if not row:
                await update.message.reply_text("❌ Foydalanuvchi topilmadi.", reply_markup=admin_menu()); return
            uname=f"@{row['username']}" if row['username'] else "-"
            await update.message.reply_text(f'👤 <b>Foydalanuvchi</b>\n\n🆔 <code>{row["id"]}</code>\n🔗 <a href="tg://user?id={row["id"]}">Lichkasini ochish</a>\n👤 {uname}\n💰 Balans: <b>{money(row["balance"])}</b>\n📅 {row["created_at"]}', parse_mode=ParseMode.HTML, reply_markup=admin_menu(), disable_web_page_preview=True); return

        if state == "balance_user":
            try: uid=int(text)
            except ValueError: uid=0
            con=db(); row=con.execute("SELECT id,balance FROM users WHERE id=?",(uid,)).fetchone(); con.close()
            if not row:
                await update.message.reply_text("❌ Foydalanuvchi topilmadi. ID ni qayta yuboring."); return
            context.user_data["balance_user_id"]=uid; context.user_data["admin_state"]="balance_amount"
            await update.message.reply_text(f"👤 ID <code>{uid}</code>\n💰 Hozirgi balans: {money(row['balance'])}\n\nQo'shish uchun musbat, kamaytirish uchun manfiy summa yuboring. Masalan: 5000 yoki -3000", parse_mode=ParseMode.HTML); return

        if state == "balance_amount":
            raw=text.strip().replace(" ","")
            try: amount=Decimal(raw)
            except Exception: amount=None
            if amount is None:
                await update.message.reply_text("❌ Summa noto'g'ri."); return
            uid=int(context.user_data.get("balance_user_id",0))
            con=db(); con.execute("UPDATE users SET balance=COALESCE(balance,0)+? WHERE id=?",(float(amount),uid)); row=con.execute("SELECT balance FROM users WHERE id=?",(uid,)).fetchone(); con.commit(); con.close()
            context.user_data.clear()
            await update.message.reply_text(f"✅ Balans o'zgartirildi.\n🆔 {uid}\n💵 O'zgarish: {money(amount)}\n💰 Yangi balans: {money(row['balance'])}", reply_markup=admin_menu())
            try: await context.bot.send_message(uid, f"💳 Balansingiz admin tomonidan {money(amount)} ga o'zgartirildi.\n💰 Yangi balans: {money(row['balance'])}")
            except Exception: pass
            return

        if state == "free_category":
            cat=text.casefold()
            if cat not in PLATFORM_LABELS:
                await update.message.reply_text("❌ Faqat: telegram / instagram / tiktok / youtube"); return
            context.user_data["free_category"]=cat; context.user_data["admin_state"]="free_name"
            await update.message.reply_text("🔥 Tekin xizmat nomini yuboring. Masalan: Telegram Like"); return

        if state == "free_name":
            context.user_data["free_name"]=text[:180]; context.user_data["admin_state"]="free_provider"
            await update.message.reply_text("🔑 Provider service ID sini yuboring (API dagi service ID). Agar faqat menyuda ko'rinsin desangiz: 0"); return

        if state == "free_provider":
            provider=text.strip()
            if not provider.isdigit() or int(provider) <= 0:
                await update.message.reply_text("❌ Provider service ID sini musbat son ko'rinishida yuboring."); return
            context.user_data["free_provider"]=provider; context.user_data["admin_state"]="free_min"
            await update.message.reply_text("📦 Minimal miqdorni yuboring (masalan 10):"); return

        if state == "free_min":
            try: mn=int(text)
            except ValueError: mn=0
            if mn<1: await update.message.reply_text("❌ 1 yoki undan katta son yuboring."); return
            context.user_data["free_min"]=mn; context.user_data["admin_state"]="free_max"
            await update.message.reply_text("📦 Maksimal miqdorni yuboring:"); return

        if state == "free_max":
            try: mx=int(text)
            except ValueError: mx=0
            mn=int(context.user_data.get("free_min",1))
            if mx<mn: await update.message.reply_text("❌ Maksimal miqdor minimaldan kichik bo'lmasin."); return
            cat=context.user_data["free_category"]; name=context.user_data["free_name"]; provider=context.user_data.get("free_provider","0")
            con=db(); con.execute("INSERT INTO services(provider_id,name,category,price,min_order,max_order,enabled) VALUES(?,?,?,?,?,?,1)",(provider,name,cat,0,mn,mx)); con.commit(); con.close(); context.user_data.clear()
            await update.message.reply_text(f"✅ Tekin xizmat qo'shildi: {PLATFORM_LABELS[cat]} — {name}", reply_markup=admin_menu()); return

        if state == "ref_reward":
            amount = parse_amount(text)
            if amount is None or amount <= 0:
                await update.message.reply_text("❌ Bonus 0 dan katta musbat summa bo'lishi kerak.")
                return
            set_setting("ref_reward", str(float(amount)))
            context.user_data.pop("admin_state", None)
            await update.message.reply_text(
                f"✅ 1 ta yangi do'st uchun bonus {money(amount)} qilib saqlandi.",
                reply_markup=admin_menu()
            )
            return

        if state == "promo_code":
            code = text.upper()
            if not re.fullmatch(r"[A-Z0-9_-]{3,24}", code):
                await update.message.reply_text(
                    "❌ Kod 3–24 ta lotin harfi, raqam, chiziqcha yoki pastki chiziqdan iborat bo'lsin."
                )
                return
            context.user_data["promo_code_pending"] = code
            context.user_data["admin_state"] = "promo_reward"
            await update.message.reply_text(
                "🎁 Foydalanuvchiga beriladigan bonus miqdorini so'mda yuboring:"
            )
            return
        if state == "promo_reward":
            reward = parse_amount(text)
            if reward is None:
                await update.message.reply_text("❌ Bonus musbat summa bo'lishi kerak.")
                return
            context.user_data["promo_reward_pending"] = float(reward)
            context.user_data["admin_state"] = "promo_limit"
            await update.message.reply_text(
                "👥 Promokodni necha kishi ishlata olishini yuboring (1–1 000 000):"
            )
            return
        if state == "promo_limit":
            try:
                usage_limit = int(text)
            except ValueError:
                usage_limit = 0
            if not 1 <= usage_limit <= 1_000_000:
                await update.message.reply_text("❌ Limit 1 dan 1 000 000 gacha butun son bo'lsin.")
                return
            created = create_promocode(
                context.user_data.get("promo_code_pending", ""),
                context.user_data.get("promo_reward_pending", 0),
                usage_limit,
                update.effective_user.id
            )
            code = context.user_data.pop("promo_code_pending", "")
            reward = context.user_data.pop("promo_reward_pending", 0)
            context.user_data.pop("admin_state", None)
            if created:
                await update.message.reply_text(
                    f"✅ Promokod yaratildi: <code>{code}</code>\n"
                    f"Bonus: {money(reward)} · Limit: {usage_limit} kishi",
                    parse_mode=ParseMode.HTML,
                    reply_markup=admin_menu()
                )
            else:
                await update.message.reply_text(
                    "❌ Bu promokod nomi oldin ishlatilgan. Boshqa nom tanlang.",
                    reply_markup=admin_menu()
                )
            return

        if state == "force_add":
            if not text.startswith("@"): await update.message.reply_text("❌ @kanal ko'rinishida yuboring."); return
            channels=get_force_channels(); limit=int(get_setting("force_limit", "0") or 0)
            if text not in channels:
                if limit > 0 and len(channels) >= limit:
                    await update.message.reply_text(f"❌ Limit ({limit}) to'lgan.", reply_markup=admin_menu()); return
                channels.append(text)
            set_setting("force_channels", ",".join(channels))
            context.user_data.pop("admin_state",None); await update.message.reply_text(f"✅ Majburiy kanal qo'shildi: {text}", reply_markup=admin_menu()); return
        if state == "service_price":
            amount=parse_amount(text)
            if amount is None: await update.message.reply_text("❌ Faqat musbat narx yuboring."); return
            sid=int(context.user_data["edit_id"]); con=db(); con.execute("UPDATE services SET price=? WHERE id=?",(float(amount),sid)); con.commit(); con.close(); context.user_data.clear(); await update.message.reply_text("✅ Xizmat narxi yangilandi.", reply_markup=admin_menu()); return
        if state == "donate_price":
            amount=parse_amount(text)
            if amount is None: await update.message.reply_text("❌ Faqat musbat narx yuboring."); return
            pid=int(context.user_data["edit_id"]); con=db(); con.execute("UPDATE products SET price=? WHERE id=?",(float(amount),pid)); con.commit(); con.close(); context.user_data.clear(); await update.message.reply_text("✅ Donat narxi yangilandi.", reply_markup=admin_menu()); return
        if state == "premium_symbol":
            context.user_data["premium_symbol"]=text[:8]; context.user_data["admin_state"]="premium_custom"
            await update.message.reply_text("✨ Endi shu emoji uchun Telegram Premium custom emoji'ni yuboring."); return
        if state == "premium_custom":
            custom_id=None
            for ent in (update.message.entities or []):
                if ent.type == "custom_emoji": custom_id=ent.custom_emoji_id; break
            if not custom_id: await update.message.reply_text("❌ Premium custom emoji topilmadi. Premium emoji yuboring."); return
            symbol=context.user_data.get("premium_symbol", "✨")
            con=db(); con.execute("INSERT INTO premium_emojis(unicode_emoji,custom_emoji_id,active) VALUES(?,?,1) ON CONFLICT(unicode_emoji) DO UPDATE SET custom_emoji_id=excluded.custom_emoji_id,active=1",(symbol,custom_id)); con.commit(); con.close(); context.user_data.clear(); await update.message.reply_text("✅ Premium emoji saqlandi.", reply_markup=admin_menu()); return

    if context.user_data.get("promo_step") == "code":
        code = (update.message.text or "").strip()
        context.user_data.pop("promo_step", None)
        status, reward = redeem_promocode(update.effective_user.id, code)
        messages = {
            "invalid": "❌ Bunday promokod topilmadi.",
            "inactive": "❌ Bu promokod hozir faol emas.",
            "limit": "❌ Promokoddan foydalanish limiti tugagan.",
            "used": "⚠️ Bu promokodni avval ishlatgansiz.",
            "ok": f"✅ Promokod qabul qilindi. Balansingizga {money(reward)} qo'shildi."
        }
        await update.message.reply_text(messages[status], reply_markup=main_menu())
        return

    # Hard global gate: every normal text interaction is blocked until all
    # required channels are joined. Admins remain exempt.
    if not is_admin(update.effective_user.id):
        if not await subscription_gate(update, context):
            return

    # Payment amount flow
    if await payment_amount_text(update, context): return

    # Active order/donate flows
    if await order_text(update, context): return
    if await quantity_text(update, context): return
    if await donate_text(update, context): return

    text=(update.message.text or "").strip()
    # Accept both the old emoji-prefixed labels and the current plain button text.
    normalized = re.sub(r"^[^A-Za-z0-9А-Яа-яЎўҚқҒғҲҳ'| ]+\s*", "", text).strip()
    if text in ("Nakrutka", "🛍 Nakrutka", "🛍️ Nakrutka") or normalized == "Nakrutka": await nakrutka(update, context)
    elif text in ("Donat", "🎮 Donat") or normalized == "Donat": await donate(update, context)
    elif text in ("Stars | Premium", "⭐ Stars | Premium") or normalized == "Stars | Premium": await stars(update, context)
    elif text in ("Raqam olish", "📱 Raqam olish") or normalized == "Raqam olish": await numbers(update, context)
    elif text in ("To'lov qilish", "💳 To'lov qilish") or normalized == "To'lov qilish": await topup(update, context)
    elif text in ("Hisobim", "💼 Hisobim") or normalized == "Hisobim": await account(update, context)
    elif text in ("Buyurtmalarim", "🛒 Buyurtmalarim") or normalized == "Buyurtmalarim": await orders(update, context)
    elif text in ("Pul ishlash", "💰 Pul ishlash") or normalized == "Pul ishlash": await earnings(update, context)
    elif text in ("Bot qoidalari", "📚 Bot qoidalari") or normalized == "Bot qoidalari": await rules(update, context)
    elif text in ("Adminga murojaat", "👨‍💻 Adminga murojaat") or normalized == "Adminga murojaat": await support(update, context)

async def media_router(update, context):
    if is_admin(update.effective_user.id) and context.user_data.get("admin_state") == "db_upload" and update.message and update.message.document:
        document = update.message.document
        original = document.file_name or "database.db"
        if not original.lower().endswith((".db", ".sqlite", ".sqlite3")):
            await update.message.reply_text("❌ Faqat .db, .sqlite yoki .sqlite3 fayl yuboring.", reply_markup=database_menu_markup())
            return
        folder = database_backup_dir()
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"uploaded_{stamp}_{safe_database_name(original)}"
        path = os.path.join(folder, filename)
        try:
            tg_file = await document.get_file()
            await tg_file.download_to_drive(path)
            if not is_valid_database(path):
                os.remove(path)
                await update.message.reply_text(
                    "❌ Fayl SQLite bo'lsa ham, bu botning kerakli jadvallari topilmadi. Baza qo'shilmadi.",
                    reply_markup=database_menu_markup()
                )
                return
            context.user_data.pop("admin_state", None)
            await update.message.reply_text(
                f"✅ Baza fayli saqlandi.\n📄 {filename}\n📦 {database_size_text(path)}\n\nUni 'Saqlangan bazalar' bo'limidan tiklash mumkin.",
                reply_markup=database_menu_markup()
            )
        except Exception as exc:
            try:
                if os.path.exists(path): os.remove(path)
            except Exception: pass
            await update.message.reply_text(f"❌ Baza yuklash xatosi: {str(exc)[:500]}", reply_markup=database_menu_markup())
        return

    if await payment_media(update, context):
        return
    if is_admin(update.effective_user.id) and context.user_data.get("admin_state") == "broadcast":
        con=db(); cur=con.execute("INSERT INTO broadcasts(admin_id,text,created_at) VALUES(?,?,?)",(update.effective_user.id,"[media]",datetime.now().isoformat())); bid=cur.lastrowid; users=con.execute("SELECT id FROM users").fetchall(); con.commit(); con.close()
        sent=failed=0
        for row in users:
            try:
                await context.bot.copy_message(chat_id=row["id"],from_chat_id=update.effective_chat.id,message_id=update.message.message_id); sent+=1
            except Exception: failed+=1
        con=db(); con.execute("UPDATE broadcasts SET sent=?,failed=? WHERE id=?",(sent,failed,bid)); con.commit(); con.close(); context.user_data.pop("admin_state",None)
        await update.message.reply_text(f"📣 Media xabari yuborildi.\n✅ {sent}\n❌ {failed}",reply_markup=admin_menu()); return
    # Manual receipt payments were removed; ordinary media is ignored here.

# ========================== MAIN =============================

async def error_handler(update, context):
    log.exception("Unhandled bot error", exc_info=context.error)


def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN Replit Secrets ichida sozlanmagan.")
    if not SMM_API_KEY:
        log.warning("SMM_API_KEY sozlanmagan; xizmatlarni yuklash va buyurtma berish ishlamaydi.")

    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin))
    app.add_handler(CommandHandler("price", price_command))
    app.add_handler(CommandHandler("cancel", cancel_command))

    app.add_handler(CallbackQueryHandler(check_sub_callback, pattern=r"^check_sub$"))
    app.add_handler(CallbackQueryHandler(promo_callback, pattern=r"^promo:"))
    app.add_handler(CallbackQueryHandler(admin_action_callback, pattern=r"^(pay:|num:|priceedit:|prices:|donprice:|force:|premium:)"))
    app.add_handler(CallbackQueryHandler(free_services_callback, pattern=r"^free:"))
    app.add_handler(CallbackQueryHandler(category_callback, pattern=r"^cat:"))
    app.add_handler(CallbackQueryHandler(service_callback, pattern=r"^svc:"))
    app.add_handler(CallbackQueryHandler(order_confirm, pattern=r"^order:"))
    app.add_handler(CallbackQueryHandler(donate_callback, pattern=r"^don:"))
    app.add_handler(CallbackQueryHandler(admin_callback, pattern=r"^adm:"))
    app.add_handler(CallbackQueryHandler(general_callback, pattern=r"^(menu|nakrutka)$"))

    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL, media_router))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_router))
    app.add_error_handler(error_handler)

    log.info("Prime SMM bot started.")
    # Python 3.14 da get_event_loop() avtomatik loop yaratmaydi;
    # PTB 22.5 run_polling() uchun asosiy thread'da loopni oldindan yaratamiz.
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
