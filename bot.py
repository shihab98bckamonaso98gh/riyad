import asyncio
import html
import json
import logging
import os
import re
import sqlite3
import time
import traceback
import warnings
from collections import defaultdict, deque
from datetime import datetime, timedelta
from typing import Dict, List, Set, Optional, Tuple
from uuid import uuid4

import requests
from dotenv import load_dotenv
from telegram import (
    Bot,
    CopyTextButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    Update,
    InlineQueryResultArticle,
    InputTextMessageContent,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    InlineQueryHandler,
    filters,
    ContextTypes,
)

import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
warnings.filterwarnings("ignore", category=UserWarning)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram.ext").setLevel(logging.WARNING)

# Silent logs: only warnings & errors
logging.basicConfig(level=logging.WARNING, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("sms_otp_bot")

load_dotenv()

# ════════════════════════════════════════════════════════════════
#  CONFIGURATION
# ════════════════════════════════════════════════════════════════
TOKEN = os.getenv("BOT_TOKEN")
BOT_USERNAME = os.getenv("BOT_USERNAME")
ORBITX_SMS_FOOTER = os.getenv("ORBITX_SMS_FOOTER", "ORBIT X SMS")

admin_ids_str = os.getenv("ADMIN_CHAT_IDS", "")
ADMIN_CHAT_IDS = {int(x.strip()) for x in admin_ids_str.split(",") if x.strip().isdigit()}
if not ADMIN_CHAT_IDS:
    single_admin = os.getenv("ADMIN_CHAT_ID")
    if single_admin:
        ADMIN_CHAT_IDS = {int(single_admin)}

MAIN_CHANNEL_ID = os.getenv("MAIN_CHANNEL_ID", "")
MAIN_CHANNEL_INVITE_LINK = os.getenv("MAIN_CHANNEL_INVITE_LINK", "")
GROUP_CHAT_ID = os.getenv("GROUP_CHAT_ID", "")
GROUP_INVITE_LINK = os.getenv("GROUP_INVITE_LINK", "")

try: MAIN_CHANNEL_ID_INT = int(MAIN_CHANNEL_ID) if MAIN_CHANNEL_ID else None
except ValueError: MAIN_CHANNEL_ID_INT = None
try: GROUP_CHAT_ID_INT = int(GROUP_CHAT_ID) if GROUP_CHAT_ID else None
except ValueError: GROUP_CHAT_ID_INT = None

# ── Persistent data directory (Railway: /data, local: .) ──
DATA_DIR = os.getenv("DATA_DIR", ".")
os.makedirs(DATA_DIR, exist_ok=True)   # creates /data if missing

DB_FILE = os.path.join(DATA_DIR, "wallet.db")
MAIN_BUTTONS_FILE = os.path.join(DATA_DIR, "main_buttons.json")
SUB_BUTTONS_FILE = os.path.join(DATA_DIR, "sub_buttons.json")
POOLS_FILE = os.path.join(DATA_DIR, "pools.json")
ASSIGNED_FILE = os.path.join(DATA_DIR, "assigned.json")
USERS_FILE = os.path.join(DATA_DIR, "users.json")
SEEN_PAIRS_FILE = os.path.join(DATA_DIR, "seen_pairs_site8.txt")

SITE8_BASE_URL = os.getenv("SITE8_BASE_URL", "http://139.99.68.231/ints")
SITE8_USERNAME = os.getenv("SITE8_USERNAME", "")
SITE8_PASSWORD = os.getenv("SITE8_PASSWORD", "")
SITE8_CHECK_INTERVAL = int(os.getenv("SITE8_CHECK_INTERVAL", "10"))

INTERNAL_RETRIES = 3
RETRY_BACKOFF = 15
MAX_BACKOFF = 120
REQUEST_TIMEOUT = 60
RATE_LIMIT_WINDOW = 60
RATE_LIMIT_MAX_REQUESTS = 15
RATE_LIMIT_BAN_MINUTES = 10

session8 = requests.Session()
session8.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": f"{SITE8_BASE_URL}/agent/SMSCDRReports",
    "X-Requested-With": "XMLHttpRequest",
    "Connection": "close",
    "Cache-Control": "no-cache",
})

last_get_number: Dict[int, float] = {}
user_request_timestamps: Dict[int, deque] = defaultdict(lambda: deque(maxlen=RATE_LIMIT_MAX_REQUESTS))

# ════════════════════════════════════════════════════════════════
#  DATABASE
# ════════════════════════════════════════════════════════════════
def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        balance_bdt REAL DEFAULT 0.0,
        bkash TEXT,
        rocket TEXT,
        binance TEXT,
        today_otps INTEGER DEFAULT 0,
        today_earned REAL DEFAULT 0.0,
        total_earned REAL DEFAULT 0.0,
        last_reset_date TEXT DEFAULT '',
        referred_by INTEGER DEFAULT NULL,
        referral_earned REAL DEFAULT 0.0,
        total_referrals INTEGER DEFAULT 0
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS withdraw_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        amount_bdt REAL,
        method TEXT,
        wallet_detail TEXT,
        status TEXT DEFAULT 'pending',
        request_time TEXT,
        completed_time TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS banned_users (
        user_id INTEGER PRIMARY KEY,
        until REAL
    )''')
    c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('min_withdrawal_bdt', '20.0')")
    c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('per_otp_bdt', '0.30')")
    c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('refer_rate_bdt', '0.10')")
    c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('refer_levels', '2')")
    for col, typ in [('today_otps','INTEGER DEFAULT 0'), ('today_earned','REAL DEFAULT 0.0'), ('total_earned','REAL DEFAULT 0.0'),
                     ('last_reset_date',"TEXT DEFAULT ''"), ('referred_by','INTEGER DEFAULT NULL'), ('referral_earned','REAL DEFAULT 0.0'),
                     ('total_referrals','INTEGER DEFAULT 0')]:
        try: c.execute(f"ALTER TABLE users ADD COLUMN {col} {typ}")
        except sqlite3.OperationalError: pass
    conn.commit()
    conn.close()

init_db()

# ════════════════════════════════════════════════════════════════
#  HELPERS
# ════════════════════════════════════════════════════════════════
def is_admin(user_id): return user_id in ADMIN_CHAT_IDS
def is_banned(user_id):
    conn = sqlite3.connect(DB_FILE)
    row = conn.execute("SELECT until FROM banned_users WHERE user_id=?", (user_id,)).fetchone()
    conn.close()
    return bool(row and row[0] > time.time())
def ban_user(user_id, minutes=5):
    until = time.time() + minutes * 60
    conn = sqlite3.connect(DB_FILE)
    conn.execute("INSERT OR REPLACE INTO banned_users (user_id, until) VALUES (?, ?)", (user_id, until))
    conn.commit(); conn.close()
def check_global_rate_limit(user_id):
    if is_admin(user_id): return True
    now = time.time()
    ts = user_request_timestamps[user_id]
    while ts and ts[0] < now - RATE_LIMIT_WINDOW: ts.popleft()
    if len(ts) >= RATE_LIMIT_MAX_REQUESTS:
        ban_user(user_id, minutes=RATE_LIMIT_BAN_MINUTES)
        return False
    ts.append(now); return True

async def enforce_rate_limit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id if update.effective_user else update.callback_query.from_user.id if update.callback_query else None
    if not user_id: return True
    if not check_global_rate_limit(user_id):
        conn = sqlite3.connect(DB_FILE)
        row = conn.execute("SELECT until FROM banned_users WHERE user_id=?", (user_id,)).fetchone()
        conn.close()
        minutes_left = 0
        if row:
            remaining = int(row[0] - time.time())
            minutes_left = max(1, (remaining + 59) // 60)
        msg = f"🚫 <b>You have been blocked for spamming!</b>\n\nPlease wait {minutes_left} minute(s) before using the bot again." if minutes_left else "🚫 <b>You have been blocked for spamming!</b>\n\nPlease wait a few minutes."
        try:
            if update.message: await update.message.reply_text(msg, parse_mode=ParseMode.HTML)
            elif update.callback_query: await update.callback_query.edit_message_text(msg, parse_mode=ParseMode.HTML)
            else: await context.bot.send_message(chat_id=user_id, text=msg, parse_mode=ParseMode.HTML)
        except Exception: pass
        return False
    return True

def load_json(filename, default):
    if not os.path.exists(filename): return default
    try:
        with open(filename,'r') as f:
            content = f.read().strip()
            return json.loads(content) if content else default
    except: os.remove(filename); return default
def save_json(filename, data):
    with open(filename,'w') as f: json.dump(data, f, indent=2, ensure_ascii=False)
def load_main_buttons(): return load_json(MAIN_BUTTONS_FILE, ["Facebook","Instagram"])
def save_main_buttons(buttons): save_json(MAIN_BUTTONS_FILE, buttons)
def load_sub_buttons(): return load_json(SUB_BUTTONS_FILE, {"Facebook":["Peru"],"Instagram":["India"]})
def save_sub_buttons(data): save_json(SUB_BUTTONS_FILE, data)
def load_pools(): return load_json(POOLS_FILE, {})
def save_pools(data): save_json(POOLS_FILE, data)
def load_assigned():
    raw = load_json(ASSIGNED_FILE, {})
    normalized = {}
    for num, val in raw.items():
        if isinstance(val, int): normalized[num] = {"user_id": val, "main": "", "sub": None}
        elif isinstance(val, dict): normalized[num] = {"user_id": val.get("user_id",0), "main": val.get("main",""), "sub": val.get("sub")}
    return normalized
def save_assigned(data): save_json(ASSIGNED_FILE, data)
def load_users(): return set(load_json(USERS_FILE, []))
def save_users(users): save_json(USERS_FILE, list(users))
def ensure_user_exists(user_id):
    conn = sqlite3.connect(DB_FILE)
    conn.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
    conn.commit(); conn.close()
def get_user_balance(user_id):
    ensure_user_exists(user_id)
    conn = sqlite3.connect(DB_FILE)
    row = conn.execute("SELECT balance_bdt FROM users WHERE user_id=?", (user_id,)).fetchone()
    conn.close()
    return row[0] if row else 0.0
def credit_user(user_id, amount_bdt):
    ensure_user_exists(user_id)
    today_str = datetime.now().strftime("%Y-%m-%d")
    conn = sqlite3.connect(DB_FILE)
    row = conn.execute("SELECT last_reset_date FROM users WHERE user_id=?", (user_id,)).fetchone()
    last_reset = row[0] if row else ""
    if last_reset != today_str:
        conn.execute("UPDATE users SET today_otps=0, today_earned=0.0, last_reset_date=? WHERE user_id=?", (today_str, user_id))
    conn.execute("UPDATE users SET balance_bdt = balance_bdt + ?, today_otps = today_otps + 1, today_earned = today_earned + ?, total_earned = total_earned + ? WHERE user_id=?",
                 (amount_bdt, amount_bdt, amount_bdt, user_id))
    conn.commit(); conn.close()
def process_referral_commissions(user_id, per_otp):
    refer_rate = float(get_setting("refer_rate_bdt","0.10"))
    max_levels = int(get_setting("refer_levels","2"))
    conn = sqlite3.connect(DB_FILE)
    current = user_id
    for _ in range(max_levels):
        row = conn.execute("SELECT referred_by FROM users WHERE user_id=?", (current,)).fetchone()
        if not row or row[0] is None: break
        referrer = row[0]
        conn.execute("UPDATE users SET balance_bdt = balance_bdt + ?, referral_earned = referral_earned + ? WHERE user_id=?",
                     (refer_rate, refer_rate, referrer))
        current = referrer
    conn.commit(); conn.close()
def get_user_wallet(user_id):
    conn = sqlite3.connect(DB_FILE)
    row = conn.execute("SELECT bkash, rocket, binance FROM users WHERE user_id=?", (user_id,)).fetchone()
    conn.close()
    return {'bkash': row[0], 'rocket': row[1], 'binance': row[2]} if row else {'bkash':None,'rocket':None,'binance':None}
def set_wallet_detail(user_id, field, value):
    ensure_user_exists(user_id)
    conn = sqlite3.connect(DB_FILE)
    conn.execute(f"UPDATE users SET {field}=? WHERE user_id=?", (value, user_id))
    conn.commit(); conn.close()
def create_withdrawal(user_id, amount_bdt, method, wallet_detail):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect(DB_FILE)
    balance = conn.execute("SELECT balance_bdt FROM users WHERE user_id=?", (user_id,)).fetchone()[0]
    if balance < amount_bdt:
        conn.close(); return False, "Insufficient balance."
    conn.execute("UPDATE users SET balance_bdt = balance_bdt - ? WHERE user_id=?", (amount_bdt, user_id))
    conn.execute("INSERT INTO withdraw_requests (user_id, amount_bdt, method, wallet_detail, status, request_time) VALUES (?,?,?,?,'pending',?)",
                 (user_id, amount_bdt, method, wallet_detail, now))
    conn.commit(); conn.close()
    return True, None
def get_pending_requests():
    conn = sqlite3.connect(DB_FILE)
    rows = conn.execute("SELECT id, user_id, amount_bdt, method, wallet_detail, request_time FROM withdraw_requests WHERE status='pending' ORDER BY request_time").fetchall()
    conn.close()
    return [{'id':r[0], 'user_id':r[1], 'amount_bdt':r[2], 'method':r[3], 'wallet_detail':r[4], 'time':r[5]} for r in rows]
def complete_withdrawal(request_id, admin_id):
    conn = sqlite3.connect(DB_FILE)
    row = conn.execute("SELECT id, user_id, amount_bdt, method, wallet_detail FROM withdraw_requests WHERE id=? AND status='pending'", (request_id,)).fetchone()
    if not row: conn.close(); return None
    user_id, amount, method, wallet = row[1], row[2], row[3], row[4]
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("UPDATE withdraw_requests SET status='completed', completed_time=? WHERE id=?", (now, request_id))
    conn.commit(); conn.close()
    ex_rate = 125.0
    if method == 'binance':
        amount_display = f"${amount/ex_rate:.4f}"; wallet_label = "Binance UID"
    else:
        amount_display = f"{amount:.2f} BDT"; wallet_label = f"{method.capitalize()} Number" if method!='mobile' else "Mobile Number"
    msg = f"🎉 <b>Withdrawal Approved</b>\n\n💵 <b>Amount:</b> {amount_display}\n🏦 <b>Method:</b> {method}\n📞 <b>{wallet_label}:</b> {wallet}\n✅ <b>Status:</b> Complete\n\nWe appreciate your trust!"
    return user_id, msg
def get_withdrawal_history(user_id=None):
    conn = sqlite3.connect(DB_FILE)
    if user_id is None:
        rows = conn.execute("SELECT id, user_id, amount_bdt, method, wallet_detail, request_time, completed_time FROM withdraw_requests WHERE status='completed' ORDER BY completed_time DESC LIMIT 200").fetchall()
    else:
        rows = conn.execute("SELECT id, amount_bdt, method, wallet_detail, request_time, completed_time FROM withdraw_requests WHERE user_id=? AND status='completed' ORDER BY completed_time DESC", (user_id,)).fetchall()
    conn.close()
    return [{'id':r[0],'user_id':r[1] if len(r)>6 else user_id,'amount_bdt':r[2],'method':r[3],'wallet':r[4],'request_time':r[5],'completed_time':r[6]} for r in rows]
def get_setting(key, default=None):
    conn = sqlite3.connect(DB_FILE)
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    conn.close()
    return row[0] if row else default
def set_setting(key, value):
    conn = sqlite3.connect(DB_FILE)
    conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value)))
    conn.commit(); conn.close()
def get_user_stats(user_id):
    assigned = load_assigned()
    numbers_used = sum(1 for v in assigned.values() if isinstance(v, dict) and v.get("user_id") == user_id)
    conn = sqlite3.connect(DB_FILE)
    row = conn.execute("SELECT today_otps, today_earned, total_earned, referral_earned, total_referrals FROM users WHERE user_id=?", (user_id,)).fetchone()
    if row:
        today_otps = row[0] or 0; today_earned = row[1] or 0.0; total_earned = row[2] or 0.0; referral_earned = row[3] or 0.0; total_referrals = row[4] or 0
    else:
        today_otps=0; today_earned=0.0; total_earned=0.0; referral_earned=0.0; total_referrals=0
    total_withdrawn_row = conn.execute("SELECT COALESCE(SUM(amount_bdt),0) FROM withdraw_requests WHERE user_id=? AND status='completed'", (user_id,)).fetchone()
    total_withdrawn = total_withdrawn_row[0] if total_withdrawn_row else 0.0
    conn.close()
    return {"numbers_used":numbers_used,"today_otps":today_otps,"today_earned":today_earned,"total_earned":total_earned,"total_withdrawn":total_withdrawn,"referral_earned":referral_earned,"total_referrals":total_referrals}
def get_admin_stats():
    assigned = load_assigned()
    total_numbers = len(assigned)
    conn = sqlite3.connect(DB_FILE)
    row = conn.execute("SELECT COALESCE(SUM(today_otps),0), COALESCE(SUM(today_earned),0) FROM users").fetchone()
    today_otps = row[0] or 0; today_earned = row[1] or 0.0
    total_withdrawn_row = conn.execute("SELECT COALESCE(SUM(amount_bdt),0) FROM withdraw_requests WHERE status='completed'").fetchone()
    total_withdrawn = total_withdrawn_row[0] if total_withdrawn_row else 0.0
    conn.close()
    return {"numbers_used":total_numbers,"today_otps":today_otps,"today_earned":today_earned,"total_withdrawn":total_withdrawn}

def build_menu_buttons(buttons, header=None, footer=None):
    menu = []
    if header: menu.append(header)
    for i in range(0, len(buttons), 2): menu.append(buttons[i:i+2])
    if footer: menu.append(footer)
    return InlineKeyboardMarkup(menu)

# ════════════════════════════════════════════════════════════════
#  CHANNEL MEMBERSHIP
# ════════════════════════════════════════════════════════════════
async def check_channel_membership(bot, user_id, channel_id):
    if channel_id is None: return True
    try:
        member = await bot.get_chat_member(chat_id=channel_id, user_id=user_id)
        return member.status not in ['left','kicked','banned']
    except Exception:
        return False

async def require_membership(update, context):
    user_id = update.effective_user.id if update.effective_user else update.callback_query.from_user.id if update.callback_query else None
    if not user_id or is_admin(user_id): return True

    missing_main = MAIN_CHANNEL_ID_INT and not await check_channel_membership(context.bot, user_id, MAIN_CHANNEL_ID_INT)
    missing_group = GROUP_CHAT_ID_INT and not await check_channel_membership(context.bot, user_id, GROUP_CHAT_ID_INT)

    if not missing_main and not missing_group:
        return True

    keyboard_buttons = []
    if MAIN_CHANNEL_INVITE_LINK:
        keyboard_buttons.append([InlineKeyboardButton("🔗 Join Main Channel", url=MAIN_CHANNEL_INVITE_LINK)])
    if GROUP_INVITE_LINK:
        keyboard_buttons.append([InlineKeyboardButton("🔗 Join OTP Group", url=GROUP_INVITE_LINK)])
    keyboard_buttons.append([InlineKeyboardButton("✅ Verify", callback_data="verify_join")])

    channels_list = []
    if missing_main: channels_list.append("Main Channel")
    if missing_group: channels_list.append("OTP Group")
    text = f"🔒 <b>Access Restricted!</b>\n\nYou must join our channels: {', '.join(channels_list)}."
    if update.message: await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard_buttons), parse_mode=ParseMode.HTML)
    elif update.callback_query: await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard_buttons), parse_mode=ParseMode.HTML)
    else: await context.bot.send_message(chat_id=user_id, text=text, reply_markup=InlineKeyboardMarkup(keyboard_buttons), parse_mode=ParseMode.HTML)
    return False

async def verify_join_callback(update, context):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    ok_main = not MAIN_CHANNEL_ID_INT or await check_channel_membership(context.bot, user_id, MAIN_CHANNEL_ID_INT)
    ok_group = not GROUP_CHAT_ID_INT or await check_channel_membership(context.bot, user_id, GROUP_CHAT_ID_INT)
    if ok_main and ok_group:
        await query.edit_message_text("✅ Verification successful! Use /start to begin.", parse_mode=ParseMode.HTML)
    else:
        await query.answer("❌ You haven't joined all required channels. Please join and try again.", show_alert=True)

# ════════════════════════════════════════════════════════════════
#  SITE LOGIN & FETCH
# ════════════════════════════════════════════════════════════════
def site_login(session, base_url, username, password, retries=3):
    login_url = f"{base_url}/login"
    signin_url = f"{base_url}/signin"
    for attempt in range(1, retries+1):
        try:
            resp = session.get(login_url, timeout=REQUEST_TIMEOUT)
        except Exception:
            time.sleep(2); continue
        match = re.search(r"What is (\d+)\s*\+\s*(\d+)\s*=\s*\?\s*:", resp.text)
        if not match:
            time.sleep(2); continue
        a, b = int(match.group(1)), int(match.group(2))
        answer = a + b
        data = {"username": username, "password": password, "capt": str(answer)}
        try:
            resp = session.post(signin_url, data=data, allow_redirects=True, timeout=REQUEST_TIMEOUT)
        except Exception:
            time.sleep(2); continue
        if "Dashboard" in resp.text or "/agent/" in resp.url:
            try: session.get(f"{base_url}/agent/", timeout=REQUEST_TIMEOUT)
            except: pass
            return True
        else:
            time.sleep(2)
    return False

def fetch_data_sync_generic(session, base_url):
    today = datetime.now()
    fdate1 = (today - timedelta(days=30)).strftime("%Y-%m-%d 00:00:00")
    fdate2 = (today + timedelta(days=1)).strftime("%Y-%m-%d 23:59:59")
    data_url = f"{base_url}/agent/res/data_smscdr.php"
    params = {
        "fdate1": fdate1, "fdate2": fdate2, "frange": "", "fclient": "",
        "fnum": "", "fcli": "", "fgdate": "", "fgmonth": "", "fgrange": "",
        "fgclient": "", "fgnumber": "", "fgcli": "", "fg": "0",
        "sEcho": "1", "iDisplayStart": "0", "iDisplayLength": "-1",
        "iColumns": "9", "sColumns": "",
        **{f"mDataProp_{i}": str(i) for i in range(9)},
    }
    for _ in range(INTERNAL_RETRIES):
        try:
            resp = session.get(data_url, params=params, timeout=REQUEST_TIMEOUT)
        except Exception:
            time.sleep(2); continue
        if "login" in resp.url.lower():
            return None
        if resp.status_code != 200:
            time.sleep(2); continue
        try:
            json_data = resp.json()
        except Exception:
            if "login" in resp.text.lower() and "password" in resp.text.lower():
                return None
            time.sleep(2); continue
        rows = json_data.get("aaData")
        if rows is None:
            return []
        return rows
    return None

async def fetch_data_async_generic(session, base_url):
    return await asyncio.to_thread(fetch_data_sync_generic, session, base_url)

# ════════════════════════════════════════════════════════════════
#  OTP EXTRACTION & SENDING
# ════════════════════════════════════════════════════════════════
def extract_otp(sms_text: str) -> Optional[str]:
    if not isinstance(sms_text, str):
        return None
    s = sms_text.strip()
    if not s:
        return None
    m = re.search(r"#\s*((?:\d+\s*)+?)\s*is\s+your", s)
    if m: return re.sub(r"\s+", "", m.group(1))
    m = re.search(r"#\s*(\d[\d\s]+)", s)
    if m: return re.sub(r"\s+", "", m.group(1))
    keyword_patterns = [
        r"(?:cod[ée]?\s*(?:igo|e)?|code|otp|pin|password|verification|seguridad|código|kode|token)\s*(?:[:#-]?\s*)(\d{4,8})",
        r"(\d{4,8})\s*(?:is your|is het|es tu|je|is uw|es)\s*(?:code|otp|pin|password|verification)",
        r"code\s*[:#-]?\s*(\d{4,8})",
        r"otp\s*[:#-]?\s*(\d{4,8})",
        r"verification\s*code\s*[:#-]?\s*(\d{4,8})",
        r"security\s*code\s*[:#-]?\s*(\d{4,8})",
        r"2fa\s*code\s*[:#-]?\s*(\d{4,8})",
        r"(\d{4,8})\s*(?:コード|验证码|인증번호)",
        r"código\s*[:#-]?\s*(\d{4,8})",
        r"cod\s*de\s*seguridad\s*[:#-]?\s*(\d{4,8})",
        r"cod\s*de\s*seguridad\s*(\d{4,8})",
        r"tu\s*código\s*es\s*(\d{4,8})",
    ]
    for pat in keyword_patterns:
        m = re.search(pat, s, re.IGNORECASE)
        if m:
            return m.group(1).strip()
    if re.fullmatch(r"\d{4,8}", s):
        return s
    matches = re.findall(r"\b\d{4,8}\b", s)
    if matches:
        valid = [num for num in matches if not (num.startswith('0') and len(num) >= 10)]
        if valid:
            return valid[-1]
    return None

def mask_number(num):
    if not num or not num.strip(): return "Unknown"
    num = num.strip()
    if not num.startswith("+"): num = "+" + num
    if len(num) <= 7: return num[:3] + "***"
    return num[:4] + "*"*(len(num)-7) + num[-3:]

def h(s): return html.escape(str(s), quote=False)

def normalise_number(num):
    return num.strip().lstrip('+')

def load_seen_pairs(filename):
    if not os.path.exists(filename): return set()
    with open(filename, 'r') as f:
        return set(line.strip() for line in f if "|" in line)

def save_seen_pair(filename, number, otp):
    with open(filename, 'a') as f:
        f.write(f"{number}|{otp}\n")

async def send_otp_to_group(bot, row, otp, country=""):
    if not GROUP_CHAT_ID_INT: return
    number = str(row[2]).strip()
    cli = str(row[3]).strip() if len(row)>3 else ""
    sms = str(row[5]).strip() if len(row)>5 else ""
    masked = mask_number(number)
    country_part = f"{country} " if country else ""
    text = (f"✅ 📩 {country_part}Message Received!\n\n🏢 CLI : {h(cli)}\n📞 Number: {masked}\n\n🔑 OTP: {h(otp)}\n\n💬 Message:\n{h(sms)}")
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("Get Number", url=f"https://t.me/{BOT_USERNAME}?start=start")]])
    try:
        await bot.send_message(chat_id=GROUP_CHAT_ID_INT, text=text, reply_markup=keyboard)
    except Exception as e:
        logger.error(f"Group send failed: {e}")

async def send_otp_to_user(bot, user_id, row, otp, old_balance, new_balance, country=""):
    number = str(row[2]).strip()
    sms = str(row[5]).strip() if len(row)>5 else ""
    if not number.startswith("+"): number = "+" + number
    country_part = f"{country} " if country else ""
    text = (f"📩 <b>{country_part}Message Received!</b>\n\n📞 Number : <code>{h(number)}</code>\n\n🔑 OTP Code: <code>{h(otp)}</code>\n\n💬 Full Message:\n<code>{h(sms)}</code>\n\n💰 Balance : {old_balance:.2f} BDT ---> {new_balance:.2f} BDT")
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton(f"OTP: {otp}", copy_text=CopyTextButton(text=otp))]])
    try:
        await bot.send_message(user_id, text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.error(f"User send failed {user_id}: {e}")

# ════════════════════════════════════════════════════════════════
#  SITE 8 MONITOR (sends OTP to assigned user & group)
# ════════════════════════════════════════════════════════════════
async def safe_monitor_site8(application):
    while True:
        try: await monitor_site8(application)
        except Exception:
            logger.error(f"Monitor crashed: {traceback.format_exc()}")
            await asyncio.sleep(60)

async def monitor_site8(application):
    session = session8
    base_url = SITE8_BASE_URL
    username = SITE8_USERNAME
    password = SITE8_PASSWORD
    seen_file = SEEN_PAIRS_FILE
    bot = application.bot

    if not site_login(session, base_url, username, password):
        logger.error("Initial login failed for Site8")

    seen_pairs = load_seen_pairs(seen_file)
    rows = await fetch_data_async_generic(session, base_url)
    if rows:
        for row in rows:
            if len(row) < 9: continue
            sms_text = str(row[5])
            otp = extract_otp(sms_text)
            if not otp: continue
            number = str(row[2]).strip()
            pair = f"{number}|{otp}"
            if pair not in seen_pairs:
                seen_pairs.add(pair)
                save_seen_pair(seen_file, number, otp)

    consecutive_failures = 0
    while True:
        rows = await fetch_data_async_generic(session, base_url)
        if rows is None:
            if site_login(session, base_url, username, password):
                rows = await fetch_data_async_generic(session, base_url)
                if rows is not None: consecutive_failures = 0
                else: consecutive_failures += 1
            else: consecutive_failures += 1
            if rows is None:
                backoff = min(RETRY_BACKOFF * (consecutive_failures + 1), MAX_BACKOFF)
                await asyncio.sleep(backoff)
                continue
        else:
            consecutive_failures = 0

        assigned = load_assigned()
        normalised_assigned = {normalise_number(k): v for k, v in assigned.items()}
        per_otp = float(get_setting("per_otp_bdt", "0.30"))
        for row in rows:
            if len(row) < 9: continue
            sms_text = str(row[5])
            otp = extract_otp(sms_text)
            if not otp: continue
            number = str(row[2]).strip()
            pair = f"{number}|{otp}"
            if pair in seen_pairs: continue
            seen_pairs.add(pair)
            save_seen_pair(seen_file, number, otp)

            assign_data = normalised_assigned.get(normalise_number(number), {})
            user_id = assign_data.get("user_id") if isinstance(assign_data, dict) else assign_data
            country = assign_data.get("main", "") if isinstance(assign_data, dict) else ""

            tasks = [send_otp_to_group(bot, row, otp, country)]
            if user_id:
                old_bal = get_user_balance(user_id)
                credit_user(user_id, per_otp)
                process_referral_commissions(user_id, per_otp)
                new_bal = get_user_balance(user_id)
                tasks.append(send_otp_to_user(bot, user_id, row, otp, old_bal, new_bal, country))
            await asyncio.gather(*tasks)
        await asyncio.sleep(SITE8_CHECK_INTERVAL)

# ════════════════════════════════════════════════════════════════
#  MAIN HANDLERS
# ════════════════════════════════════════════════════════════════
def check_get_number_rate_limit(user_id):
    now = time.time()
    last = last_get_number.get(user_id, 0)
    if now - last < 5: return False, 5 - int(now - last)
    last_get_number[user_id] = now
    return True, 0

async def start(update, context):
    if not await require_membership(update, context): return
    if not await enforce_rate_limit(update, context): return
    user_id = update.effective_user.id
    users = load_users()
    users.add(user_id)
    save_users(users)
    if context.args and context.args[0].startswith("ref"):
        try:
            referrer_id = int(context.args[0][3:])
            if referrer_id != user_id:
                conn = sqlite3.connect(DB_FILE)
                existing = conn.execute("SELECT referred_by FROM users WHERE user_id=?", (user_id,)).fetchone()
                if existing and existing[0] is None:
                    conn.execute("UPDATE users SET referred_by=?, total_referrals = total_referrals + 1 WHERE user_id=?", (referrer_id, user_id))
                    conn.execute("UPDATE users SET total_referrals = total_referrals + 1 WHERE user_id=?", (referrer_id,))
                elif not existing:
                    ensure_user_exists(user_id)
                    conn.execute("UPDATE users SET referred_by=? WHERE user_id=?", (referrer_id, user_id))
                    conn.execute("UPDATE users SET total_referrals = total_referrals + 1 WHERE user_id=?", (referrer_id,))
                conn.commit(); conn.close()
        except Exception:
            pass
    keyboard = [["Get Number", "Balance"], ["Status", "Refer & Earn"]]
    if is_admin(user_id): keyboard.append(["Admin Panel"])
    await update.message.reply_text("Welcome! Choose an option:", reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True))

async def balance_main(update, context):
    if not await require_membership(update, context): return
    if not await enforce_rate_limit(update, context): return
    if is_banned(update.effective_user.id):
        await update.message.reply_text("🚫 You are temporarily banned."); return
    user_id = update.effective_user.id
    balance = get_user_balance(user_id)
    wallet = get_user_wallet(user_id)
    min_bdt = float(get_setting("min_withdrawal_bdt","20.0"))
    msg = (f"⚠️ Double‑check your wallet! Wrong details = no refund.\n\n🤑 Balance: {balance:.2f} BDT / ${balance/125:.4f}\n\n🌍 Bkash: {wallet['bkash'] or 'Not Set'}\n🌍 Rocket: {wallet['rocket'] or 'Not Set'}\n🌍 Binance: {wallet['binance'] or 'Not Set'}\n\n💳 Minimum Withdrawal: {min_bdt} BDT / ${min_bdt/125:.2f}")
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Set Wallet", callback_data="profile_set_wallet"),
         InlineKeyboardButton("Withdraw", callback_data="profile_withdraw")],
        [InlineKeyboardButton("Withdraw History", callback_data="balance_withdraw_history")]
    ])
    await update.message.reply_text(msg, reply_markup=keyboard, parse_mode=ParseMode.HTML)

async def status_main(update, context):
    if not await require_membership(update, context): return
    if not await enforce_rate_limit(update, context): return
    if is_banned(update.effective_user.id):
        await update.message.reply_text("🚫 You are temporarily banned."); return
    user_id = update.effective_user.id
    stats = get_user_stats(user_id)
    ex_rate = 125.0
    msg = (f"📊 <b>YOUR STATISTICS</b>\n━━━━━━━━━━━━━━━━━━━━\n📞 Numbers Used: {stats['numbers_used']}\n📩 Today's OTPs: {stats['today_otps']}\n💰 Today's Earned: {stats['today_earned']:.2f} BDT / ${stats['today_earned']/ex_rate:.4f} USDT\n💵 Total Earned: {stats['total_earned']:.2f} BDT / ${stats['total_earned']/ex_rate:.4f} USDT\n💳 Total Withdrawn: {stats['total_withdrawn']:.2f} BDT / ${stats['total_withdrawn']/ex_rate:.4f} USDT\n━━━━━━━━━━━━━━━━━━━━\n📢 <b>{ORBITX_SMS_FOOTER}</b>")
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)

async def refer_and_earn(update, context):
    if not await require_membership(update, context): return
    if not await enforce_rate_limit(update, context): return
    user_id = update.effective_user.id
    conn = sqlite3.connect(DB_FILE)
    row = conn.execute("SELECT referral_earned, total_referrals FROM users WHERE user_id=?", (user_id,)).fetchone()
    conn.close()
    ref_earned = row[0] if row else 0.0
    ref_count = row[1] if row else 0
    link = f"https://t.me/{BOT_USERNAME}?start=ref{user_id}"
    text = (f"👥 <b>Refer & Earn</b>\n\n📎 Your referral link:\n<code>{link}</code>\n\n📊 Total referrals: {ref_count}\n💰 Earned from referrals: {ref_earned:.2f} BDT\n\n<i>Tap the button below to share your link with a friend. When they receive an OTP, you get paid!</i>")
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("📤 Share", switch_inline_query=f"ref_{user_id}")]])
    await update.message.reply_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)

async def inline_query(update, context):
    query = update.inline_query.query
    if not query.startswith("ref_"): return
    try: referrer_id = int(query.split("_")[1])
    except: return
    link = f"https://t.me/{BOT_USERNAME}?start=ref{referrer_id}"
    message_text = f"🚀 <b>Earn money by receiving OTPs!</b>\n\nJoin the bot and start earning instantly.\n\n👉 <a href='{link}'>Start Bot</a>"
    input_content = InputTextMessageContent(message_text, parse_mode=ParseMode.HTML)
    result = InlineQueryResultArticle(
        id=uuid4().hex,
        title="Invite to earn OTP rewards",
        description="Share this referral link with your friend",
        input_message_content=input_content,
        thumb_url="https://i.imgur.com/...",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🚀 Start Bot", url=link)]])
    )
    await update.inline_query.answer([result], cache_time=0)

# ── Get Number flow ──
async def get_number_start(update, context):
    if not await require_membership(update, context): return
    if not await enforce_rate_limit(update, context): return
    if is_banned(update.effective_user.id):
        await update.message.reply_text("🚫 You are temporarily banned for spamming."); return
    mains = load_main_buttons()
    if not mains: await update.message.reply_text("No main buttons available."); return
    buttons = [InlineKeyboardButton(name, callback_data=f"get_main:{name}") for name in mains]
    await update.message.reply_text("Choose a service:", reply_markup=build_menu_buttons(buttons))

async def get_main_callback(update, context):
    if not await require_membership(update, context): return
    if not await enforce_rate_limit(update, context): return
    query = update.callback_query; await query.answer()
    main_name = query.data.split(":",1)[1]
    subs = load_sub_buttons().get(main_name, [])
    if not subs:
        pool_key = main_name
        pools = load_pools()
        numbers = pools.get(pool_key, [])
        if not numbers: await query.edit_message_text("No numbers available for this service."); return
        assigned_number = numbers.pop(0)
        pools[pool_key] = numbers
        save_pools(pools)
        assigned = load_assigned()
        assigned[assigned_number] = {"user_id": query.from_user.id, "main": main_name, "sub": None}
        save_assigned(assigned)
        context.user_data["last_main"] = main_name; context.user_data["last_sub"] = None
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("Copy Number", copy_text=CopyTextButton(text=assigned_number))],
            [InlineKeyboardButton("Change Number", callback_data=f"change_number:{main_name}:"),
             InlineKeyboardButton("OTP Group", url="https://t.me/otpservers")]
        ])
        await query.edit_message_text(f"New 𝗡𝘂𝗺𝗯𝗲𝗿 𝗔𝘀𝘀𝗶𝗴𝗻𝗲𝗱!\n\n{assigned_number}\n\nWaiting for OTP ...", parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
        return
    buttons = [InlineKeyboardButton(sub, callback_data=f"get_sub:{main_name}:{sub}") for sub in subs]
    await query.edit_message_text(f"Select a sub‑category for {main_name}:", reply_markup=build_menu_buttons(buttons))

async def get_sub_callback(update, context):
    if not await require_membership(update, context): return
    if not await enforce_rate_limit(update, context): return
    query = update.callback_query; await query.answer()
    user_id = query.from_user.id
    allowed, wait = check_get_number_rate_limit(user_id)
    if not allowed: await query.edit_message_text(f"⏳ Please wait {wait} seconds before requesting another number."); return
    _, main_name, sub_name = query.data.split(":",2)
    await assign_number_and_display(query, main_name, sub_name, user_id, context)

async def change_number_callback(update, context):
    if not await require_membership(update, context): return
    if not await enforce_rate_limit(update, context): return
    query = update.callback_query; await query.answer()
    user_id = query.from_user.id
    if is_banned(user_id): await query.edit_message_text("🚫 You are banned."); return
    allowed, wait = check_get_number_rate_limit(user_id)
    if not allowed: await query.edit_message_text(f"⏳ Wait {wait}s."); return
    parts = query.data.split(":",2)
    main_name = parts[1]; sub_name = parts[2] if len(parts)>2 else None
    if sub_name: await assign_number_and_display(query, main_name, sub_name, user_id, context)
    else:
        pool_key = main_name
        pools = load_pools()
        numbers = pools.get(pool_key, [])
        if not numbers: await query.edit_message_text("No numbers available for this service."); return
        assigned_number = numbers.pop(0)
        pools[pool_key] = numbers
        save_pools(pools)
        assigned = load_assigned()
        assigned[assigned_number] = {"user_id": user_id, "main": main_name, "sub": None}
        save_assigned(assigned)
        context.user_data["last_main"] = main_name; context.user_data["last_sub"] = None
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("Copy Number", copy_text=CopyTextButton(text=assigned_number))],
            [InlineKeyboardButton("Change Number", callback_data=f"change_number:{main_name}:"),
             InlineKeyboardButton("OTP Group", url="https://t.me/otpservers")]
        ])
        await query.edit_message_text(f"New 𝗡𝘂𝗺𝗯𝗲𝗿 𝗔𝘀𝘀𝗶𝗴𝗻𝗲𝗱!\n\n{assigned_number}\n\nWaiting for OTP ...", parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)

async def assign_number_and_display(query_or_update, main_name, sub_name, user_id, context=None):
    pool_key = f"{main_name}_{sub_name}"
    pools = load_pools()
    numbers = pools.get(pool_key, [])
    if not numbers:
        if hasattr(query_or_update, 'edit_message_text'): await query_or_update.edit_message_text("No numbers available in this category.")
        else: await query_or_update.message.reply_text("No numbers available in this category.")
        return
    assigned_number = numbers.pop(0)
    pools[pool_key] = numbers
    save_pools(pools)
    assigned = load_assigned()
    assigned[assigned_number] = {"user_id": user_id, "main": main_name, "sub": sub_name}
    save_assigned(assigned)
    if context:
        context.user_data["last_main"] = main_name
        context.user_data["last_sub"] = sub_name
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Copy Number", copy_text=CopyTextButton(text=assigned_number))],
        [InlineKeyboardButton("Change Number", callback_data=f"change_number:{main_name}:{sub_name}"),
         InlineKeyboardButton("OTP Group", url="https://t.me/otpservers")]
    ])
    text = f"New 𝗡𝘂𝗺𝗯𝗲𝗿 𝗔𝘀𝘀𝗶𝗴𝗻𝗲𝗱!\n\n{assigned_number}\n\nWaiting for OTP ..."
    if hasattr(query_or_update, 'edit_message_text'):
        await query_or_update.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
    else:
        await query_or_update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)

# ════════════════════════════════════════════════════════════════
#  ADMIN PANEL (unified conversation)
# ════════════════════════════════════════════════════════════════
(
    PROFILE_SELECT,
    SET_WALLET_METHOD, SET_WALLET_VALUE,
    WITHDRAW_METHOD, WITHDRAW_AMOUNT,
    EDIT_MENU, EDIT_PRICE, EDIT_RATE, EDIT_REFER_RATE,
    ADD_MAIN, REMOVE_MAIN_SELECT,
    UPLOAD_MAIN_SELECT, UPLOAD_SUB_OPTION, UPLOAD_FILE,
    BROADCAST_RECEIVE, BROADCAST_CONFIRM,
) = range(16)

def admin_profile_kb():
    return [
        ["💰 Balance", "📋 Pending"],
        ["✅ Approved", "✏️ Edit"],
        ["📢 Broadcast", "Upload"],
        ["Status", "Users status"],
        ["📊 Number Status", "Add/Remove Main Button"],
        ["⬅️ Back"]
    ]

async def back_to_profile(update, context):
    user_id = update.effective_user.id
    if is_admin(user_id):
        await update.message.reply_text("👤 Admin Panel", reply_markup=ReplyKeyboardMarkup(admin_profile_kb(), resize_keyboard=True))
    else:
        await start(update, context)
    return ConversationHandler.END

async def cancel(update, context):
    user_id = update.effective_user.id
    if is_admin(user_id):
        await update.message.reply_text("Cancelled.", reply_markup=ReplyKeyboardMarkup(admin_profile_kb(), resize_keyboard=True))
        return ConversationHandler.END
    else:
        await start(update, context)
        return ConversationHandler.END

async def number_status(update, context):
    if not await require_membership(update, context): return
    if not await enforce_rate_limit(update, context): return
    if not is_admin(update.effective_user.id): await update.message.reply_text("⛔ Access denied."); return
    pools = load_pools()
    assigned = load_assigned()
    total_assigned = len(assigned)
    total_pool = sum(len(nums) for nums in pools.values())
    lines = [f"📊 <b>NUMBER STATUS</b>\n📞 Total numbers in pools: {total_pool}\n🔒 Assigned numbers: {total_assigned}\n"]
    mains = load_main_buttons()
    sub_data = load_sub_buttons()
    for main in mains:
        main_count = len(pools.get(main, []))
        lines.append(f"\n<b>🔹 {main}</b>\n   ├── Main category: {main_count} numbers")
        for sub in sub_data.get(main, []):
            sub_count = len(pools.get(f"{main}_{sub}", []))
            lines.append(f"   ├── {sub}: {sub_count} numbers")
        lines.append("   └── " + "─"*20)
    lines.append(f"\n📢 <b>{ORBITX_SMS_FOOTER}</b>")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)
    return PROFILE_SELECT

# ── Add/Remove Main Button ──
async def add_remove_main(update, context):
    if not is_admin(update.effective_user.id): return ConversationHandler.END
    if not await enforce_rate_limit(update, context): return ConversationHandler.END
    keyboard = [["Add Main Button", "Remove Main Button"], ["⬅️ Back"]]
    await update.message.reply_text("Choose action:", reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True))
    return ADD_MAIN

async def add_main_prompt(update, context):
    if update.message.text == "⬅️ Back":
        await update.message.reply_text("👤 Admin Panel", reply_markup=ReplyKeyboardMarkup(admin_profile_kb(), resize_keyboard=True))
        return PROFILE_SELECT
    await update.message.reply_text("Send the name of the new main button:", reply_markup=ReplyKeyboardMarkup([["⬅️ Back"]], resize_keyboard=True))
    return ADD_MAIN

async def add_main_receive(update, context):
    if update.message.text == "⬅️ Back":
        await update.message.reply_text("👤 Admin Panel", reply_markup=ReplyKeyboardMarkup(admin_profile_kb(), resize_keyboard=True))
        return PROFILE_SELECT
    name = update.message.text.strip()
    mains = load_main_buttons()
    if name in mains:
        await update.message.reply_text("Already exists.", reply_markup=ReplyKeyboardMarkup(admin_profile_kb(), resize_keyboard=True))
    else:
        mains.append(name)
        save_main_buttons(mains)
        sub_buttons = load_sub_buttons()
        if name not in sub_buttons:
            sub_buttons[name] = []
            save_sub_buttons(sub_buttons)
        await update.message.reply_text(f"Main button '{name}' added.", reply_markup=ReplyKeyboardMarkup(admin_profile_kb(), resize_keyboard=True))
    return PROFILE_SELECT

async def remove_main_select(update, context):
    if update.message.text == "⬅️ Back":
        await update.message.reply_text("👤 Admin Panel", reply_markup=ReplyKeyboardMarkup(admin_profile_kb(), resize_keyboard=True))
        return PROFILE_SELECT
    mains = load_main_buttons()
    if not mains:
        await update.message.reply_text("No main buttons to remove.", reply_markup=ReplyKeyboardMarkup(admin_profile_kb(), resize_keyboard=True))
        return PROFILE_SELECT
    keyboard = [[InlineKeyboardButton(m, callback_data=f"remove_main:{m}")] for m in mains]
    keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data="cancel_remove_main")])
    await update.message.reply_text("Select main button to remove:", reply_markup=InlineKeyboardMarkup(keyboard))
    return REMOVE_MAIN_SELECT

async def remove_main_callback(update, context):
    query = update.callback_query
    await query.answer()
    if query.data == "cancel_remove_main":
        await query.edit_message_text("Cancelled.")
        await query.message.reply_text("👤 Admin Panel", reply_markup=ReplyKeyboardMarkup(admin_profile_kb(), resize_keyboard=True))
        return PROFILE_SELECT
    main_name = query.data.split(":",1)[1]
    mains = load_main_buttons()
    if main_name in mains:
        mains.remove(main_name)
        save_main_buttons(mains)
        sub_buttons = load_sub_buttons()
        if main_name in sub_buttons:
            subs = sub_buttons.pop(main_name)
            save_sub_buttons(sub_buttons)
            pools = load_pools()
            for sub in subs: pools.pop(f"{main_name}_{sub}", None)
            pools.pop(main_name, None)
            save_pools(pools)
        await query.edit_message_text(f"Main button '{main_name}' and its sub buttons removed.")
    else:
        await query.edit_message_text("Not found.")
    await query.message.reply_text("👤 Admin Panel", reply_markup=ReplyKeyboardMarkup(admin_profile_kb(), resize_keyboard=True))
    return PROFILE_SELECT

# ── Upload ──
async def upload_from_profile(update, context):
    if not is_admin(update.effective_user.id): return ConversationHandler.END
    if not await enforce_rate_limit(update, context): return ConversationHandler.END
    mains = load_main_buttons()
    if not mains:
        await update.message.reply_text("No main buttons.", reply_markup=ReplyKeyboardMarkup(admin_profile_kb(), resize_keyboard=True))
        return PROFILE_SELECT
    keyboard = [[InlineKeyboardButton(m, callback_data=f"upload_main:{m}")] for m in mains]
    keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data="cancel_upload")])
    await update.message.reply_text("Select main button for upload:", reply_markup=InlineKeyboardMarkup(keyboard))
    return UPLOAD_MAIN_SELECT

async def upload_main_callback(update, context):
    query = update.callback_query; await query.answer()
    if query.data == "cancel_upload":
        await query.edit_message_text("Cancelled.")
        await query.message.reply_text("👤 Admin Panel", reply_markup=ReplyKeyboardMarkup(admin_profile_kb(), resize_keyboard=True))
        return PROFILE_SELECT
    main_name = query.data.split(":",1)[1]
    context.user_data["upload_main"] = main_name
    subs = load_sub_buttons().get(main_name, [])
    if subs:
        buttons = [[InlineKeyboardButton("Upload to main directly", callback_data=f"upload_direct_main:{main_name}")]]
        for sub in subs: buttons.append([InlineKeyboardButton(f"Sub: {sub}", callback_data=f"upload_sub:{main_name}:{sub}")])
        buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="cancel_upload")])
        await query.edit_message_text(f"Where to upload numbers for '{main_name}'?", reply_markup=InlineKeyboardMarkup(buttons))
        return UPLOAD_SUB_OPTION
    else:
        context.user_data["upload_sub"] = None
        await query.edit_message_text(f"Send a .txt file with numbers (one per line) for '{main_name}'.")
        return UPLOAD_FILE

async def upload_sub_option_callback(update, context):
    query = update.callback_query; await query.answer()
    if query.data == "cancel_upload":
        await query.edit_message_text("Cancelled.")
        await query.message.reply_text("👤 Admin Panel", reply_markup=ReplyKeyboardMarkup(admin_profile_kb(), resize_keyboard=True))
        return PROFILE_SELECT
    data = query.data
    if data.startswith("upload_direct_main:"):
        main_name = data.split(":",2)[1]
        context.user_data["upload_main"] = main_name; context.user_data["upload_sub"] = None
        await query.edit_message_text(f"Send a .txt file with numbers (one per line) for '{main_name}'.")
        return UPLOAD_FILE
    elif data.startswith("upload_sub:"):
        _, main_name, sub_name = data.split(":",2)
        context.user_data["upload_main"] = main_name; context.user_data["upload_sub"] = sub_name
        await query.edit_message_text(f"Send a .txt file with numbers for {main_name} / {sub_name}.")
        return UPLOAD_FILE

async def upload_file_receive(update, context):
    if not update.message.document:
        await update.message.reply_text("Please send a .txt file.")
        return UPLOAD_FILE
    doc = update.message.document
    if not doc.file_name.endswith(".txt"):
        await update.message.reply_text("Only .txt files accepted.")
        return UPLOAD_FILE
    file = await doc.get_file()
    content = (await file.download_as_bytearray()).decode("utf-8")
    numbers = [line.strip() for line in content.splitlines() if line.strip()]
    main_name = context.user_data["upload_main"]
    sub_name = context.user_data.get("upload_sub")
    pool_key = f"{main_name}_{sub_name}" if sub_name else main_name
    pools = load_pools()
    pools[pool_key] = numbers
    save_pools(pools)
    desc = f"{main_name} / {sub_name}" if sub_name else main_name
    try:
        await update.message.bot.send_message(GROUP_CHAT_ID_INT, f"{desc}‑এ {len(numbers)} টি নাম্বার আপলোড হয়েছে (আগের নাম্বার মুছে ফেলা হয়েছে)।")
    except Exception as e:
        logger.error(f"Upload notification failed: {e}")
    await update.message.reply_text(f"Replaced numbers in {desc} with {len(numbers)} new numbers.",
                                    reply_markup=ReplyKeyboardMarkup(admin_profile_kb(), resize_keyboard=True))
    return PROFILE_SELECT

# ── Broadcast ──
async def broadcast_start(update, context):
    if not is_admin(update.effective_user.id): return ConversationHandler.END
    if not await enforce_rate_limit(update, context): return ConversationHandler.END
    await update.message.reply_text("Send the content you want to broadcast (text, photo, video, file).",
                                    reply_markup=ReplyKeyboardMarkup([["⬅️ Back"]], resize_keyboard=True))
    return BROADCAST_RECEIVE

async def broadcast_receive(update, context):
    if update.message.text and update.message.text == "⬅️ Back":
        await update.message.reply_text("👤 Admin Panel", reply_markup=ReplyKeyboardMarkup(admin_profile_kb(), resize_keyboard=True))
        return PROFILE_SELECT
    context.user_data["broadcast_msg"] = update.message
    keyboard = [[InlineKeyboardButton("Yes, send to all", callback_data="broadcast_confirm")],
                [InlineKeyboardButton("⬅️ Back", callback_data="broadcast_cancel")]]
    await update.message.reply_text("Confirm broadcast?", reply_markup=InlineKeyboardMarkup(keyboard))
    return BROADCAST_CONFIRM

async def broadcast_confirm(update, context):
    query = update.callback_query; await query.answer()
    if query.data == "broadcast_cancel":
        await query.edit_message_text("Cancelled.")
        await query.message.reply_text("👤 Admin Panel", reply_markup=ReplyKeyboardMarkup(admin_profile_kb(), resize_keyboard=True))
        return PROFILE_SELECT
    users = load_users()
    msg = context.user_data["broadcast_msg"]
    bot = context.bot
    success = 0
    for uid in users:
        try:
            await msg.copy(chat_id=uid)
            success += 1
            await asyncio.sleep(0.05)
        except Exception:
            pass
    await query.edit_message_text(f"Broadcast finished. Sent to {success}/{len(users)} users.")
    await query.message.reply_text("👤 Admin Panel", reply_markup=ReplyKeyboardMarkup(admin_profile_kb(), resize_keyboard=True))
    return PROFILE_SELECT

# ── Admin entry ──
async def profile_start(update, context):
    if not await require_membership(update, context): return ConversationHandler.END
    if not await enforce_rate_limit(update, context): return ConversationHandler.END
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("⛔ Access denied.")
        return ConversationHandler.END
    await update.message.reply_text("👤 Admin Panel", reply_markup=ReplyKeyboardMarkup(admin_profile_kb(), resize_keyboard=True))
    return PROFILE_SELECT

async def profile_select(update, context):
    if not await require_membership(update, context): return PROFILE_SELECT
    if not await enforce_rate_limit(update, context): return PROFILE_SELECT
    user_id = update.effective_user.id
    text = update.message.text
    if text == "⬅️ Back":
        await start(update, context)
        return ConversationHandler.END
    elif text == "💰 Balance":
        balance = get_user_balance(user_id); wallet = get_user_wallet(user_id); min_bdt = float(get_setting("min_withdrawal_bdt","20.0"))
        msg = (f"⚠️ Double‑check your wallet! Wrong details = no refund.\n\n🤑 Balance: {balance:.2f} BDT / ${balance/125:.4f}\n\n🌍 Bkash: {wallet['bkash'] or 'Not Set'}\n🌍 Rocket: {wallet['rocket'] or 'Not Set'}\n🌍 Binance: {wallet['binance'] or 'Not Set'}\n\n💳 Minimum Withdrawal: {min_bdt} BDT / ${min_bdt/125:.2f}")
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("Set Wallet", callback_data="profile_set_wallet"), InlineKeyboardButton("Withdraw", callback_data="profile_withdraw")],[InlineKeyboardButton("Withdraw History", callback_data="balance_withdraw_history")]])
        await update.message.reply_text(msg, reply_markup=keyboard, parse_mode=ParseMode.HTML)
        return PROFILE_SELECT
    elif text == "📋 Pending":
        pending = get_pending_requests()
        if not pending: await update.message.reply_text("No pending withdrawal requests.")
        else:
            lines = []; kb_buttons = []
            for p in pending:
                lines.append(f"🔹 ID: {p['id']} | User: {p['user_id']}\n   💵 {p['amount_bdt']} BDT via {p['method']} ({p['wallet_detail']})\n   🕒 {p['time']}")
                kb_buttons.append([InlineKeyboardButton(f"✅ Complete #{p['id']}", callback_data=f"admin_complete_{p['id']}")])
            await update.message.reply_text("📋 <b>Pending Withdrawals:</b>\n\n" + "\n\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(kb_buttons))
        return PROFILE_SELECT
    elif text == "✅ Approved":
        history = get_withdrawal_history(user_id=None)
        if not history: await update.message.reply_text("No approved withdrawals yet.")
        else:
            lines = [f"🔹 ID: {h['id']} | User: {h['user_id']}\n   💵 {h['amount_bdt']} BDT via {h['method']} ({h['wallet']})\n   📅 {h['completed_time']}" for h in history]
            await update.message.reply_text("✅ <b>Approved Withdrawals:</b>\n\n" + "\n\n".join(lines), parse_mode=ParseMode.HTML)
        return PROFILE_SELECT
    elif text == "✏️ Edit":
        kb = [["Withdraw price", "Rate", "Refer rate"], ["⬅️ Back"]]
        await update.message.reply_text("Edit Menu", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True))
        return EDIT_MENU
    elif text == "Add/Remove Main Button":
        return await add_remove_main(update, context)
    elif text == "Upload":
        return await upload_from_profile(update, context)
    elif text == "📢 Broadcast":
        return await broadcast_start(update, context)
    elif text == "📊 Number Status":
        await number_status(update, context)
        return PROFILE_SELECT
    elif text == "Status":
        stats = get_user_stats(user_id); ex_rate = 125.0
        msg = (f"📊 <b>YOUR STATISTICS</b>\n━━━━━━━━━━━━━━━━━━━━\n📞 Numbers Used: {stats['numbers_used']}\n📩 Today's OTPs: {stats['today_otps']}\n💰 Today's Earned: {stats['today_earned']:.2f} BDT / ${stats['today_earned']/ex_rate:.4f} USDT\n💵 Total Earned: {stats['total_earned']:.2f} BDT / ${stats['total_earned']/ex_rate:.4f} USDT\n💳 Total Withdrawn: {stats['total_withdrawn']:.2f} BDT / ${stats['total_withdrawn']/ex_rate:.4f} USDT\n━━━━━━━━━━━━━━━━━━━━\n📢 <b>{ORBITX_SMS_FOOTER}</b>")
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML)
        return PROFILE_SELECT
    elif text == "Users status":
        stats = get_admin_stats(); ex_rate = 125.0
        msg = (f"📊 <b>USERS STATISTICS</b>\n━━━━━━━━━━━━━━━━━━━━\n📞 Numbers Used: {stats['numbers_used']}\n📩 Today's OTPs: {stats['today_otps']}\n💰 Today's Cost: {stats['today_earned']:.2f} BDT / ${stats['today_earned']/ex_rate:.4f} USDT\n💳 Total Withdrawn: {stats['total_withdrawn']:.2f} BDT / ${stats['total_withdrawn']/ex_rate:.4f} USDT\n━━━━━━━━━━━━━━━━━━━━\n📢 <b>{ORBITX_SMS_FOOTER}</b>")
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML)
        return PROFILE_SELECT
    else:
        return PROFILE_SELECT

# ── Wallet / Withdraw / Edit callbacks ──
async def profile_callback_handler(update, context):
    if not await require_membership(update, context): return ConversationHandler.END
    if not await enforce_rate_limit(update, context): return ConversationHandler.END
    query = update.callback_query; await query.answer()
    data = query.data
    if data == "profile_set_wallet":
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("Bkash", callback_data="wallet_bkash"), InlineKeyboardButton("Rocket", callback_data="wallet_rocket"), InlineKeyboardButton("Binance", callback_data="wallet_binance")]])
        await query.edit_message_text("Select wallet to set:", reply_markup=keyboard); return SET_WALLET_METHOD
    elif data == "profile_withdraw":
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("Bkash", callback_data="withdraw_method_bkash"), InlineKeyboardButton("Rocket", callback_data="withdraw_method_rocket"), InlineKeyboardButton("Binance", callback_data="withdraw_method_binance"), InlineKeyboardButton("Mobile Recharge", callback_data="withdraw_method_mobile")]])
        await query.edit_message_text("Select withdrawal method:", reply_markup=keyboard); return WITHDRAW_METHOD
    elif data == "balance_withdraw_history":
        user_id = query.from_user.id
        history = get_withdrawal_history(user_id=user_id)
        if not history: await query.edit_message_text("No completed withdrawals yet.")
        else:
            lines = [f"🔹 ID: {h['id']}\n   💵 {h['amount_bdt']} BDT via {h['method']} ({h['wallet']})\n   📅 {h['completed_time']}" for h in history]
            await query.edit_message_text("📋 <b>Your Withdraw History:</b>\n\n" + "\n\n".join(lines), parse_mode=ParseMode.HTML)
        return ConversationHandler.END

async def wallet_method_select(update, context):
    if not await require_membership(update, context): return SET_WALLET_METHOD
    if not await enforce_rate_limit(update, context): return SET_WALLET_METHOD
    query = update.callback_query; await query.answer()
    method = query.data.split("_")[1]; context.user_data["wallet_method"] = method
    prompt = "Enter your Binance UID:" if method == "binance" else f"Enter your {method.capitalize()} number:"
    await query.edit_message_text(prompt); return SET_WALLET_VALUE

async def wallet_value_received(update, context):
    if not await require_membership(update, context): return SET_WALLET_VALUE
    if not await enforce_rate_limit(update, context): return SET_WALLET_VALUE
    user_id = update.effective_user.id; value = update.message.text.strip(); method = context.user_data["wallet_method"]
    if method in ("bkash", "rocket") and not re.fullmatch(r"\d{7,15}", value):
        await update.message.reply_text("Invalid phone number. Must be 7-15 digits. Try again or /cancel.", reply_markup=ReplyKeyboardMarkup([["⬅️ Back"]], resize_keyboard=True))
        return SET_WALLET_VALUE
    elif method == "binance" and not re.fullmatch(r"\d{6,}", value):
        await update.message.reply_text("Invalid Binance UID. Must be numeric. Try again or /cancel.", reply_markup=ReplyKeyboardMarkup([["⬅️ Back"]], resize_keyboard=True))
        return SET_WALLET_VALUE
    set_wallet_detail(user_id, method, value)
    await update.message.reply_text(f"{method.capitalize()} wallet set to: {value}", reply_markup=ReplyKeyboardMarkup(admin_profile_kb() if is_admin(user_id) else [["Get Number","Balance"],["Status","Refer & Earn"]], resize_keyboard=True))
    return ConversationHandler.END

async def withdraw_method_select(update, context):
    if not await require_membership(update, context): return WITHDRAW_METHOD
    if not await enforce_rate_limit(update, context): return WITHDRAW_METHOD
    query = update.callback_query; await query.answer()
    method = query.data.replace("withdraw_method_", ""); context.user_data["withdraw_method"] = method
    wallet = get_user_wallet(query.from_user.id)
    detail = wallet.get(method) if method in ("bkash","rocket","binance") else wallet.get("bkash")
    if not detail: await query.edit_message_text(f"Your {method} wallet is not set. Use 'Set Wallet' first."); return ConversationHandler.END
    context.user_data["withdraw_wallet_detail"] = detail
    balance = get_user_balance(query.from_user.id); min_bdt = float(get_setting("min_withdrawal_bdt","20.0"))
    msg = f"💰 Current Balance: {balance:.2f} BDT / ${balance/125:.4f}\n💳 Minimum Withdrawal: {min_bdt} BDT / ${min_bdt/125:.2f}\n\nEnter amount in BDT to withdraw:"
    await query.edit_message_text(msg); return WITHDRAW_AMOUNT

async def withdraw_amount_received(update, context):
    if not await require_membership(update, context): return WITHDRAW_AMOUNT
    if not await enforce_rate_limit(update, context): return WITHDRAW_AMOUNT
    user_id = update.effective_user.id; text = update.message.text.strip()
    try: amount = float(text)
    except ValueError:
        await update.message.reply_text("Invalid number. Try again or /cancel.", reply_markup=ReplyKeyboardMarkup([["⬅️ Back"]], resize_keyboard=True))
        return WITHDRAW_AMOUNT
    min_bdt = float(get_setting("min_withdrawal_bdt","20.0"))
    if amount < min_bdt:
        await update.message.reply_text(f"Minimum withdrawal is {min_bdt} BDT.", reply_markup=ReplyKeyboardMarkup([["⬅️ Back"]], resize_keyboard=True))
        return WITHDRAW_AMOUNT
    success, err = create_withdrawal(user_id, amount, context.user_data["withdraw_method"], context.user_data["withdraw_wallet_detail"])
    if success:
        await update.message.reply_text("✅ Withdrawal request submitted. Processing...",
                                        reply_markup=ReplyKeyboardMarkup(admin_profile_kb() if is_admin(user_id) else [["Get Number","Balance"],["Status","Refer & Earn"]], resize_keyboard=True))
    else:
        await update.message.reply_text(f"❌ {err}",
                                        reply_markup=ReplyKeyboardMarkup(admin_profile_kb() if is_admin(user_id) else [["Get Number","Balance"],["Status","Refer & Earn"]], resize_keyboard=True))
    return ConversationHandler.END

async def edit_menu(update, context):
    if not await enforce_rate_limit(update, context): return EDIT_MENU
    text = update.message.text
    if text == "Withdraw price":
        cur_min = get_setting("min_withdrawal_bdt","20.0")
        await update.message.reply_text(f"Current minimum withdrawal: {cur_min} BDT\nEnter new minimum amount in BDT:", reply_markup=ReplyKeyboardMarkup([["⬅️ Back"]], resize_keyboard=True))
        return EDIT_PRICE
    elif text == "Rate":
        cur_rate = get_setting("per_otp_bdt","0.30")
        await update.message.reply_text(f"Current OTP earning rate: {cur_rate} BDT per OTP\nEnter new rate in BDT:", reply_markup=ReplyKeyboardMarkup([["⬅️ Back"]], resize_keyboard=True))
        return EDIT_RATE
    elif text == "Refer rate":
        cur_ref = get_setting("refer_rate_bdt","0.10"); cur_lvl = get_setting("refer_levels","2")
        await update.message.reply_text(f"Current referral reward: {cur_ref} BDT per OTP, levels: {cur_lvl}\n\nSend new referral rate and levels separated by a space (e.g. 0.15 2):", reply_markup=ReplyKeyboardMarkup([["⬅️ Back"]], resize_keyboard=True))
        return EDIT_REFER_RATE
    elif text == "⬅️ Back":
        await update.message.reply_text("👤 Admin Panel", reply_markup=ReplyKeyboardMarkup(admin_profile_kb(), resize_keyboard=True))
        return PROFILE_SELECT
    else:
        return EDIT_MENU

async def edit_price_received(update, context):
    if not await enforce_rate_limit(update, context): return EDIT_PRICE
    text = update.message.text
    if text == "⬅️ Back":
        await update.message.reply_text("👤 Admin Panel", reply_markup=ReplyKeyboardMarkup(admin_profile_kb(), resize_keyboard=True))
        return PROFILE_SELECT
    try:
        new_min = float(text)
        if new_min <= 0: raise ValueError
    except ValueError:
        await update.message.reply_text("Invalid amount. Positive number only.")
        return EDIT_PRICE
    set_setting("min_withdrawal_bdt", new_min)
    await update.message.reply_text(f"Minimum withdrawal updated to {new_min} BDT.", reply_markup=ReplyKeyboardMarkup(admin_profile_kb(), resize_keyboard=True))
    return PROFILE_SELECT

async def edit_rate_received(update, context):
    if not await enforce_rate_limit(update, context): return EDIT_RATE
    text = update.message.text
    if text == "⬅️ Back":
        await update.message.reply_text("👤 Admin Panel", reply_markup=ReplyKeyboardMarkup(admin_profile_kb(), resize_keyboard=True))
        return PROFILE_SELECT
    try:
        new_rate = float(text)
        if new_rate <= 0: raise ValueError
    except ValueError:
        await update.message.reply_text("Invalid rate. Positive number only.")
        return EDIT_RATE
    set_setting("per_otp_bdt", new_rate)
    await update.message.reply_text(f"OTP earning rate updated to {new_rate} BDT.", reply_markup=ReplyKeyboardMarkup(admin_profile_kb(), resize_keyboard=True))
    return PROFILE_SELECT

async def edit_refer_rate_received(update, context):
    if not await enforce_rate_limit(update, context): return EDIT_REFER_RATE
    text = update.message.text
    if text == "⬅️ Back":
        await update.message.reply_text("👤 Admin Panel", reply_markup=ReplyKeyboardMarkup(admin_profile_kb(), resize_keyboard=True))
        return PROFILE_SELECT
    parts = text.strip().split()
    if len(parts) != 2:
        await update.message.reply_text("Invalid format. Please enter two numbers: rate and levels.")
        return EDIT_REFER_RATE
    try:
        rate = float(parts[0]); levels = int(parts[1])
        if rate <= 0 or levels < 1 or levels > 10: raise ValueError
    except ValueError:
        await update.message.reply_text("Invalid values. Rate must be >0, levels 1-10.")
        return EDIT_REFER_RATE
    set_setting("refer_rate_bdt", rate); set_setting("refer_levels", levels)
    await update.message.reply_text(f"Referral reward set to {rate} BDT per OTP, up to {levels} levels.", reply_markup=ReplyKeyboardMarkup(admin_profile_kb(), resize_keyboard=True))
    return PROFILE_SELECT

async def admin_complete_callback(update, context):
    query = update.callback_query; await query.answer()
    if not is_admin(query.from_user.id): return
    if not await enforce_rate_limit(update, context): return
    req_id = int(query.data.split("_")[-1])
    result = complete_withdrawal(req_id, query.from_user.id)
    if result is None:
        await query.edit_message_text("Request not found or already processed.")
        return
    user_id, msg = result
    await context.bot.send_message(user_id, msg, parse_mode=ParseMode.HTML)
    await query.edit_message_text(f"✅ Withdrawal #{req_id} approved and user notified.")
    await query.message.reply_text("👤 Admin Panel", reply_markup=ReplyKeyboardMarkup(admin_profile_kb(), resize_keyboard=True))
    return PROFILE_SELECT

# ════════════════════════════════════════════════════════════════
#  BUILD APPLICATION
# ════════════════════════════════════════════════════════════════
def main():
    application = Application.builder().token(TOKEN).build()

    application.add_handler(CallbackQueryHandler(verify_join_callback, pattern="^verify_join$"))
    application.add_handler(CommandHandler("start", start))

    application.add_handler(MessageHandler(filters.Regex("^Get Number$"), get_number_start))
    application.add_handler(MessageHandler(filters.Regex("^Balance$"), balance_main))
    application.add_handler(MessageHandler(filters.Regex("^Status$"), status_main))
    application.add_handler(MessageHandler(filters.Regex("^Refer & Earn$"), refer_and_earn))

    application.add_handler(CallbackQueryHandler(get_main_callback, pattern="^get_main:"))
    application.add_handler(CallbackQueryHandler(get_sub_callback, pattern="^get_sub:"))
    application.add_handler(CallbackQueryHandler(change_number_callback, pattern="^change_number:"))
    application.add_handler(CallbackQueryHandler(admin_complete_callback, pattern="^admin_complete_"))

    application.add_handler(InlineQueryHandler(inline_query))

    admin_conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex("^Admin Panel$"), profile_start),
            CallbackQueryHandler(profile_callback_handler, pattern="^(profile_set_wallet|profile_withdraw|balance_withdraw_history)$")
        ],
        states={
            PROFILE_SELECT: [
                MessageHandler(filters.Regex("^(💰 Balance|📋 Pending|✅ Approved|✏️ Edit|Upload|📢 Broadcast|Add/Remove Main Button|⬅️ Back|Status|Users status|📊 Number Status)$"), profile_select),
            ],
            SET_WALLET_METHOD: [CallbackQueryHandler(wallet_method_select, pattern="^wallet_(bkash|rocket|binance)$")],
            SET_WALLET_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, wallet_value_received), CommandHandler("cancel", cancel)],
            WITHDRAW_METHOD: [CallbackQueryHandler(withdraw_method_select, pattern="^withdraw_method_(bkash|rocket|binance|mobile)$")],
            WITHDRAW_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, withdraw_amount_received), CommandHandler("cancel", cancel)],
            EDIT_MENU: [MessageHandler(filters.Regex("^(Withdraw price|Rate|Refer rate|⬅️ Back)$"), edit_menu)],
            EDIT_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_price_received), CommandHandler("cancel", cancel)],
            EDIT_RATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_rate_received), CommandHandler("cancel", cancel)],
            EDIT_REFER_RATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_refer_rate_received), CommandHandler("cancel", cancel)],
            ADD_MAIN: [
                MessageHandler(filters.Regex("^Add Main Button$"), add_main_prompt),
                MessageHandler(filters.Regex("^Remove Main Button$"), remove_main_select),
                MessageHandler(filters.Regex("^⬅️ Back$"), lambda u,c: back_to_profile(u,c)),
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_main_receive),
            ],
            REMOVE_MAIN_SELECT: [
                CallbackQueryHandler(remove_main_callback, pattern="^remove_main:|^cancel_remove_main$")
            ],
            UPLOAD_MAIN_SELECT: [
                CallbackQueryHandler(upload_main_callback, pattern="^upload_main:|^cancel_upload$")
            ],
            UPLOAD_SUB_OPTION: [
                CallbackQueryHandler(upload_sub_option_callback, pattern="^(upload_direct_main:|upload_sub:|cancel_upload$)")
            ],
            UPLOAD_FILE: [
                MessageHandler(filters.Document.ALL, upload_file_receive),
                MessageHandler(filters.TEXT & ~filters.COMMAND, lambda u,c: upload_file_receive(u,c))
            ],
            BROADCAST_RECEIVE: [
                MessageHandler(filters.ALL & ~filters.COMMAND, broadcast_receive),
                MessageHandler(filters.Regex("^⬅️ Back$"), lambda u,c: back_to_profile(u,c)),
            ],
            BROADCAST_CONFIRM: [
                CallbackQueryHandler(broadcast_confirm, pattern="^broadcast_")
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    application.add_handler(admin_conv)

    async def post_init(app: Application):
        asyncio.create_task(safe_monitor_site8(app))

    application.post_init = post_init

    print("Bot started")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()