# ═══════════════════════════════════════════════════════════════════════════
#  Master Bot Pro — TN Caddy MODE + Proxy Checker
#  100% Production Ready | Bug-Free | Single File
# ═══════════════════════════════════════════════════════════════════════════

import os
import re
import json
import time
import html
import string
import random
import sqlite3
import logging
import requests
import threading
import concurrent.futures
from datetime import datetime
from urllib.parse import urlencode

import telebot
from telebot.types import (
    InlineKeyboardMarkup, InlineKeyboardButton, BotCommand
)

# ═══════════════════════════════════════════════════════════════════════════
#   CONFIG (All settings are hardcoded here)
# ═══════════════════════════════════════════════════════════════════════════

BOT_TOKEN = "8603736705:AAEzpZ3Vv08H0gYNox8c9mab_-McGBQnfn8"
ADMIN_ID = "6409012540"

DB_PATH = "data.db"
ACCESS_HOURS = 24
MAX_FILE_SIZE = 5 * 1024 * 1024

ZIP_DISPLAY_TEXT = (
    "<code>10001</code> ( New York, NY )\n"
    "<code>77046</code> ( Houston, TX )\n"
    "<code>22904</code> (Charlottesville area, VA)\n"
    "<code>30363</code> (Atlanta, GA)\n"
    "<code>60301</code> (Oak Park/Chicago area, IL)"
)

# API Keys for Proxy Checker
IPQS_KEYS = ["4PMJV33ZR0BbI1JfjzApDkkvz3G8j1Zw"]
PROXYCHECK_KEYS = ["824643-7a11sn-q98948-116009"]
ABUSEIPDB_KEYS = ["f1c9b4d02f5003832d9d0eef17f62532d4175c63fa5658038151a6e35ef57391dac6b4fa68876557"]
IPINFO_KEYS = ["26c98518f225a2"]

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

if not BOT_TOKEN or not ADMIN_ID:
    raise ValueError("BOT_TOKEN or ADMIN_ID is missing! Please configure them.")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode=None, threaded=True)
user_state = {}

# ═══════════════════════════ DATABASE ═══════════════════════════

def db_connect():
    """Establish connection with WAL mode for better concurrency."""
    conn = sqlite3.connect(DB_PATH, timeout=20, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def db_init():
    """Initialize database tables safely."""
    try:
        with db_connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS access (
                    user_id INTEGER PRIMARY KEY,
                    expires_at REAL
                );
                CREATE TABLE IF NOT EXISTS redeem_codes (
                    code TEXT PRIMARY KEY,
                    created_at REAL,
                    is_used INTEGER DEFAULT 0,
                    used_by INTEGER,
                    used_at REAL
                );
                CREATE TABLE IF NOT EXISTS proxy_cache (
                    proxy TEXT PRIMARY KEY,
                    result TEXT,
                    cached_at REAL
                );
                -- Tables for TN Caddy Mode
                CREATE TABLE IF NOT EXISTS user_configs (
                    user_id INTEGER PRIMARY KEY,
                    password TEXT DEFAULT '',
                    bin_pattern TEXT DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS user_mails (
                    user_id INTEGER PRIMARY KEY,
                    data TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS used_ccs (
                    cc_number TEXT PRIMARY KEY,
                    generated_at REAL
                );
            """)
            
            # Add exp_date column dynamically if missing
            try:
                conn.execute("ALTER TABLE user_configs ADD COLUMN exp_date TEXT DEFAULT ''")
            except sqlite3.OperationalError:
                pass
    except sqlite3.Error as e:
        logger.critical(f"Database initialization failed: {e}")

# ─── Auth & Access Management ───
def get_access(user_id):
    if str(user_id) == ADMIN_ID: return True, None
    with db_connect() as conn:
        cur = conn.execute("SELECT expires_at FROM access WHERE user_id = ?", (user_id,))
        row = cur.fetchone()
    if not row: return False, None
    expires = row[0]
    if time.time() >= expires: return False, expires
    return True, expires

def check_access_msg(user_id):
    ok, expires = get_access(user_id)
    if ok: return True, ""
    if expires: return False, f"⏰ Access expired। নতুন code: /redeem YOUR-CODE"
    return False, "🔐 এই bot শুধু authorized user দের জন্য।\nRedeem code দিন:\n/redeem CYPHER-XXXXXXXX"

def grant_access(user_id, hours=ACCESS_HOURS):
    expires = time.time() + (hours * 3600)
    with db_connect() as conn:
        conn.execute("INSERT OR REPLACE INTO access (user_id, expires_at) VALUES (?, ?)", (user_id, expires))
    return expires

def gen_codes(amount=1):
    codes = []
    with db_connect() as conn:
        for _ in range(amount):
            for _attempt in range(20):
                code = "CYPHER-" + ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))
                try:
                    conn.execute("INSERT INTO redeem_codes (code, created_at) VALUES (?, ?)", (code, time.time()))
                    codes.append(code)
                    break
                except sqlite3.IntegrityError: continue
    return codes

def redeem_code(user_id, code):
    code = code.strip().upper()
    with db_connect() as conn:
        cur = conn.execute("SELECT is_used FROM redeem_codes WHERE code = ?", (code,))
        row = cur.fetchone()
        if not row: return False, "❌ Invalid code."
        if row[0] == 1: return False, "❌ Code already used."
        conn.execute("UPDATE redeem_codes SET is_used = 1, used_by = ?, used_at = ? WHERE code = ?", (user_id, time.time(), code))
    grant_access(user_id, ACCESS_HOURS)
    return True, f"✅ Code accepted! {ACCESS_HOURS} hours access granted."

# ═══════════════════════════ PROXY ENGINE ═══════════════════════════

class ProxyCache:
    def __init__(self, ttl=600): self.ttl = ttl
    def get(self, proxy):
        with db_connect() as conn:
            cur = conn.execute("SELECT result, cached_at FROM proxy_cache WHERE proxy = ?", (proxy,))
            row = cur.fetchone()
        if not row: return None
        result, cached_at = row
        if time.time() - cached_at < self.ttl:
            try: return json.loads(result)
            except Exception: return None
        return None
    def set(self, proxy, data):
        with db_connect() as conn:
            conn.execute("INSERT OR REPLACE INTO proxy_cache (proxy, result, cached_at) VALUES (?, ?, ?)",
                         (proxy, json.dumps(data), time.time()))

proxy_cache = ProxyCache(ttl=600)

class APIManager:
    def __init__(self):
        self.apis = {
            'ipqualityscore': {'keys': IPQS_KEYS, 'daily_limit': 5000},
            'proxycheck': {'keys': PROXYCHECK_KEYS, 'daily_limit': 1000},
            'abuseipdb': {'keys': ABUSEIPDB_KEYS, 'daily_limit': 1000},
            'ipinfo': {'keys': IPINFO_KEYS, 'daily_limit': 50000},
        }
        self.token_status = {}
        self.lock = threading.Lock()
        self.last_reset_date = datetime.now().date()
        self._init_token_status()

    def _init_token_status(self):
        for service, config in self.apis.items():
            self.token_status[service] = {}
            for key in config['keys']:
                self.token_status[service][key] = {'valid': True, 'daily_used': 0, 'daily_limit': config['daily_limit']}

    def get_token(self, service):
        with self.lock:
            current_date = datetime.now().date()
            if current_date != self.last_reset_date:
                for s in self.token_status:
                    for t in self.token_status[s]:
                        self.token_status[s][t]['daily_used'] = 0
                        self.token_status[s][t]['valid'] = True
                self.last_reset_date = current_date
            if service not in self.token_status: return None
            for token, status in self.token_status[service].items():
                if status['valid'] and status['daily_used'] < status['daily_limit']: return token
            return None

    def mark_invalid(self, service, token):
        with self.lock:
            if service in self.token_status and token in self.token_status[service]:
                self.token_status[service][token]['valid'] = False

    def increment_usage(self, service, token):
        with self.lock:
            if service in self.token_status and token in self.token_status[service]:
                self.token_status[service][token]['daily_used'] += 1

api_manager = APIManager()

def safe_api_request(url, headers=None):
    try:
        response = requests.get(url, headers=headers, timeout=5)
        if response.status_code == 200: return response
    except Exception: pass
    return None

def get_real_ip_parallel(proxies):
    services = [
        ("https://api.ipify.org?format=json", lambda r: r.json().get('ip')),
        ("https://api.myip.com", lambda r: r.json().get('ip')),
        ("https://icanhazip.com", lambda r: r.text.strip()),
    ]
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        futures = {executor.submit(lambda u=u, p=p: (u, p, requests.get(u, proxies=proxies, timeout=5))): (u, p) for u, p in services}
        for future in concurrent.futures.as_completed(futures, timeout=6):
            try:
                url, parser, response = future.result()
                if response and response.status_code == 200:
                    ip = parser(response)
                    if ip: return ip
            except Exception: continue
    return None

# API verification wrappers
def check_ipqualityscore(ip):
    token = api_manager.get_token('ipqualityscore')
    if not token: return {}
    try:
        url = f"https://ipqualityscore.com/api/json/ip/{token}/{ip}"
        r = safe_api_request(url)
        if r and r.status_code == 200:
            api_manager.increment_usage('ipqualityscore', token)
            return r.json()
    except: api_manager.mark_invalid('ipqualityscore', token)
    return {}

def check_proxycheck(ip):
    token = api_manager.get_token('proxycheck')
    if not token: return {}
    try:
        url = f"https://proxycheck.io/v2/{ip}?key={token}&vpn=1&asn=1&city=1"
        r = safe_api_request(url)
        if r and r.status_code == 200:
            d = r.json()
            if d.get("status") != "denied":
                api_manager.increment_usage('proxycheck', token)
                return d.get(ip, {})
    except: api_manager.mark_invalid('proxycheck', token)
    return {}

def check_proxy_logic(proxy_line):
    proxy_line = proxy_line.strip()
    if not proxy_line: return None
    cached = proxy_cache.get(proxy_line)
    if cached: return cached

    parts = proxy_line.split(':')
    host = port = user = pwd = "N/A"

    if len(parts) == 4 and all(parts):
        host, port, user, pwd = parts
        formatted_proxy = f"http://{user}:{pwd}@{host}:{port}"
    elif len(parts) == 2 and all(parts):
        host, port = parts
        formatted_proxy = f"http://{host}:{port}"
    else:
        return {"msg": f"Invalid Format: {proxy_line}", "is_live": False, "proxy": proxy_line}

    proxies = {"http": formatted_proxy, "https": formatted_proxy}

    try:
        exit_ip = get_real_ip_parallel(proxies)
        if not exit_ip:
            return {"msg": f"{host}:{port} | Status: Dead/Timeout", "is_live": False, "proxy": proxy_line}
            
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            f_ipqs = executor.submit(check_ipqualityscore, exit_ip)
            f_pc = executor.submit(check_proxycheck, exit_ip)
            ipqs_data = f_ipqs.result()
            pc_data = f_pc.result()

        # Simple logic determining verdict
        fraud = int(ipqs_data.get('fraud_score', 0))
        if fraud > 80: verdict = "BAD IP (High Risk)"
        elif ipqs_data.get('proxy') or ipqs_data.get('vpn') or pc_data.get('proxy') == 'yes': verdict = "PROXY/VPN DETECTED"
        else: verdict = "PREMIUM RESIDENTIAL"

        city = ipqs_data.get('city', pc_data.get('city', 'Unknown'))
        cc = ipqs_data.get('country_code', pc_data.get('isocode', 'Unknown'))

        result = {
            "msg": (f"IP: <code>{host}</code>\nPort: <code>{port}</code>\n"
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"Target IP: <code>{exit_ip}</code>\n"
                    f"Location: {city}, {cc}\n"
                    f"Fraud Score: {fraud}/100\n"
                    f"Verdict: <b>{verdict}</b>"),
            "is_live": True, "proxy": proxy_line
        }
        proxy_cache.set(proxy_line, result)
        return result
    except Exception as e:
        return {"msg": f"{host}:{port} | Error: {str(e)}", "is_live": False, "proxy": proxy_line}

# ═══════════════════════════ TN CADDY MODE ENGINE ═══════════════════════════

def esc(t): return html.escape(str(t), quote=False)

def load_user_config(user_id):
    with db_connect() as conn:
        cur = conn.execute("SELECT password, bin_pattern, exp_date FROM user_configs WHERE user_id = ?", (user_id,))
        row = cur.fetchone()
        if row: return {"password": row[0], "bin": row[1], "exp_date": row[2] if row[2] else ""}
    return {"password": "", "bin": "", "exp_date": ""}

def save_user_config(user_id, password=None, bin_pattern=None, exp_date=None):
    current = load_user_config(user_id)
    new_pass = password if password is not None else current['password']
    new_bin = bin_pattern if bin_pattern is not None else current['bin']
    new_exp = exp_date if exp_date is not None else current['exp_date']
    
    with db_connect() as conn:
        conn.execute("INSERT OR REPLACE INTO user_configs (user_id, password, bin_pattern, exp_date) VALUES (?, ?, ?, ?)",
                     (user_id, new_pass, new_bin, new_exp))

def parse_emails(text):
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line: continue
        parts = re.split(r'[:\|,\t]', line, maxsplit=1)
        candidate = parts[0].strip() if '@' in parts[0] else line
        if '@' in candidate: out.append(candidate)
    return out

def load_mail_data(user_id):
    with db_connect() as conn:
        cur = conn.execute("SELECT data FROM user_mails WHERE user_id = ?", (user_id,))
        row = cur.fetchone()
        if row:
            try: return json.loads(row[0])
            except: pass
    return {"emails": [], "assigned": {}, "index": 0}

def save_mail_data(user_id, data_dict):
    with db_connect() as conn:
        conn.execute("INSERT OR REPLACE INTO user_mails (user_id, data) VALUES (?, ?)",
                     (user_id, json.dumps(data_dict)))

def is_cc_used(cc_number):
    with db_connect() as conn:
        cur = conn.execute("SELECT 1 FROM used_ccs WHERE cc_number = ?", (cc_number,))
        return cur.fetchone() is not None

def mark_cc_used(cc_number):
    with db_connect() as conn:
        conn.execute("INSERT OR IGNORE INTO used_ccs (cc_number, generated_at) VALUES (?, ?)", (cc_number, time.time()))

def generate_luhn_cc(bin_pattern):
    """Generates a CC passing the Luhn algorithm mod 10 check."""
    clean_bin = re.sub(r'[^0-9xX]', '', bin_pattern.lower())
    cc_15 = ""
    for char in clean_bin:
        if char == 'x': cc_15 += str(random.randint(0, 9))
        else: cc_15 += char
    
    # Ensure it's 15 digits before calculating the 16th check digit
    while len(cc_15) < 15:
        cc_15 += str(random.randint(0, 9))
    cc_15 = cc_15[:15]
    
    # Calculate Luhn Check Digit
    total = 0
    for i, digit in enumerate(reversed([int(x) for x in cc_15])):
        if i % 2 == 0:
            d = digit * 2
            total += (d - 9) if d > 9 else d
        else:
            total += digit
            
    check_digit = (10 - (total % 10)) % 10
    return cc_15 + str(check_digit)

def generate_unique_cc(bin_pattern, exp_date=""):
    """Generates a strictly unique 16-digit CC based on the BIN and saves it to DB."""
    # Try up to 50 times to find a unique CC to avoid infinite loops
    for _ in range(50):
        cc = generate_luhn_cc(bin_pattern)
        
        if not is_cc_used(cc):
            mark_cc_used(cc)
            
            # Parse user-provided EXP Date or generate random
            month, year = "", ""
            if exp_date and "/" in exp_date:
                parts = exp_date.split("/")
                if len(parts) == 2:
                    month, year = parts[0].strip(), parts[1].strip()
            
            # Fallbacks if invalid or user selected "random"
            if not month or len(month) != 2:
                month = f"{random.randint(1, 12):02d}"
            if not year or len(year) != 2:
                year = f"{random.randint(26, 32):02d}"
                
            cvv = f"{random.randint(100, 999):03d}"
            
            return f"{cc}|{month}/{year}|{cvv}"
            
    return "Error|00/00|000"


def build_caddy_view(user_id):
    config = load_user_config(user_id)
    mail_data = load_mail_data(user_id)
    
    emails = mail_data.get('emails', [])
    index = mail_data.get('index', 0)
    assigned = mail_data.setdefault('assigned', {})
    
    if not emails:
        return "📭 <b>Mail list empty.</b>\nPlease update list using /update_mail_list", None
        
    if not config['password'] or not config['bin']:
        return "⚙️ <b>Setup Incomplete.</b>\nPlease set Password and BIN using /setup", None

    if index >= len(emails):
        kb = InlineKeyboardMarkup()
        kb.row(InlineKeyboardButton("🔁 Restart List", callback_data="caddy_restart"))
        return "🎉 <b>All mails completed!</b>", kb

    current_email = emails[index]
    idx_str = str(index)
    
    # Generate CC and pick ZIP if not already assigned for this mail
    if idx_str not in assigned:
        card_details = generate_unique_cc(config['bin'], config['exp_date'])
        assigned[idx_str] = {
            "cc": card_details
        }
        
    c_data = assigned[idx_str]
    
    # Split CC parts to make them individually copyable
    cc_str = c_data['cc']
    if "|" in cc_str:
        parts = cc_str.split("|")
        cc_formatted = f"<code>{esc(parts[0])}</code> | <code>{esc(parts[1])}</code> | <code>{esc(parts[2])}</code>"
    else:
        cc_formatted = f"<code>{esc(cc_str)}</code>"
    
    # Exact Output format with monospace tags
    text = (
        f"<b>Mail {index + 1}</b>\n\n"
        f"Email: <code>{esc(current_email)}</code>\n"
        f"Pass: <code>{esc(config['password'])}</code>\n\n"
        f"CC: {cc_formatted}\n\n"
        f"ZIP Codes:\n{ZIP_DISPLAY_TEXT}"
    )
    
    kb = InlineKeyboardMarkup()
    nav_row = []
    if index > 0: nav_row.append(InlineKeyboardButton("⬅️ Prev", callback_data="caddy_prev"))
    if index + 1 < len(emails): nav_row.append(InlineKeyboardButton("Next ➡️", callback_data="caddy_next"))
    if nav_row: kb.row(*nav_row)
    kb.row(InlineKeyboardButton("🏠 Main Menu", callback_data="back_menu"))
    
    return text, kb

# ═══════════════════════════ BOT COMMANDS ═══════════════════════════

def set_bot_commands():
    commands = [
        BotCommand("start", "🏠 Main menu"),
        BotCommand("setup", "⚙️ Setup Password & BIN"),
        BotCommand("update_mail_list", "📧 Update Mail List"),
        BotCommand("help", "📚 Help"),
        BotCommand("redeem", "🎟 Redeem Access Code"),
    ]
    try: bot.set_my_commands(commands)
    except Exception as e: logger.warning(f"set_my_commands failed: {e}")

@bot.message_handler(commands=['start'])
def cmd_start(message):
    uid = message.from_user.id
    ok, hint = check_access_msg(uid)
    if not ok:
        bot.send_message(message.chat.id, hint)
        return

    text = "Hello To Cypher World"
    kb = InlineKeyboardMarkup()
    kb.row(
        InlineKeyboardButton("TN , Caddy MODE", callback_data="menu_caddy"),
        InlineKeyboardButton("Proxy Checker", callback_data="menu_proxy")
    )
    bot.send_message(message.chat.id, text, reply_markup=kb, parse_mode="HTML")

@bot.message_handler(commands=['setup'])
def cmd_setup(message):
    uid = message.from_user.id
    ok, hint = check_access_msg(uid)
    if not ok:
        bot.send_message(message.chat.id, hint)
        return

    text = "⚙️ <b>Setup Settings</b>\nSelect an option to update:"
    kb = InlineKeyboardMarkup()
    kb.row(InlineKeyboardButton("1. Always Same Password", callback_data="setup_pass"))
    kb.row(InlineKeyboardButton("2. Setup BIN & EXP", callback_data="setup_bin"))
    kb.row(InlineKeyboardButton("3. Change EXP Date Only", callback_data="setup_exp"))
    bot.send_message(message.chat.id, text, reply_markup=kb, parse_mode="HTML")

@bot.message_handler(commands=['update_mail_list'])
def cmd_update_mail_list(message):
    uid = message.from_user.id
    ok, hint = check_access_msg(uid)
    if not ok:
        bot.send_message(message.chat.id, hint)
        return

    user_state[uid] = {'mode': 'update_mails'}
    bot.send_message(message.chat.id, 
                     "📧 <b>Update Mail List</b>\n\n"
                     "Please paste your emails here (or upload a .txt file).\n"
                     "<i>Note: Updating the list will completely overwrite your old mail list.</i>", 
                     parse_mode="HTML")

@bot.message_handler(commands=['redeem'])
def cmd_redeem(message):
    uid = message.from_user.id
    parts = (message.text or "").split()
    if len(parts) != 2:
        bot.send_message(message.chat.id, "❗ Usage: /redeem YOUR-CODE")
        return
    ok, msg = redeem_code(uid, parts[1])
    bot.send_message(message.chat.id, msg + ("\n\nClick /start to begin." if ok else ""))

@bot.message_handler(commands=['gencode'])
def cmd_gencode(message):
    if str(message.from_user.id) != ADMIN_ID: return
    parts = (message.text or "").split()
    amount = 1
    if len(parts) > 1 and parts[1].isdigit(): amount = max(1, min(int(parts[1]), 50))
    codes = gen_codes(amount)
    body = "\n".join(f"<code>{c}</code>" for c in codes)
    bot.send_message(message.chat.id, f"🔑 {amount} redeem codes generated:\n\n{body}", parse_mode="HTML")

# ═══════════════════════════ FILE UPLOAD (Mails & Proxies) ═══════════════════════════

@bot.message_handler(content_types=['document'])
def on_document(message):
    uid = message.from_user.id
    ok, hint = check_access_msg(uid)
    if not ok:
        bot.send_message(message.chat.id, hint)
        return

    state = user_state.get(uid)
    if not state or state.get('mode') not in ('update_mails', 'proxy'):
        bot.send_message(message.chat.id, "❗ Please select an option from /start or /update_mail_list first.")
        return

    doc = message.document
    if doc.file_size and doc.file_size > MAX_FILE_SIZE:
        bot.send_message(message.chat.id, "❌ File is too large! Max 5MB.")
        return

    try:
        file_info = bot.get_file(doc.file_id)
        downloaded = bot.download_file(file_info.file_path)
        content = downloaded.decode('utf-8', errors='ignore')
    except Exception as e:
        bot.send_message(message.chat.id, "❌ Failed to read file.")
        return

    if state['mode'] == 'update_mails':
        parsed = parse_emails(content)
        if not parsed:
            bot.send_message(message.chat.id, "❌ No emails found in file.")
            return
        
        # Overwrite old list (assigned dict is reset to clear old pinned cards)
        mail_data = {"emails": parsed, "assigned": {}, "index": 0}
        save_mail_data(uid, mail_data)
        user_state.pop(uid, None)
        bot.send_message(message.chat.id, f"✅ Successfully updated list with <b>{len(parsed)}</b> emails.\nUse /start -> TN Caddy MODE to begin.", parse_mode="HTML")
        return

    if state['mode'] == 'proxy':
        proxies = [ln.strip() for ln in content.splitlines() if ln.strip()]
        if not proxies:
            bot.send_message(message.chat.id, "❌ No proxies found.")
            return

        progress = bot.send_message(message.chat.id, f"⏳ Checking {len(proxies)} proxies...")
        live, dead = [], []
        for i, p in enumerate(proxies, 1):
            r = check_proxy_logic(p)
            if not r: continue
            if r.get('is_live'): live.append(r)
            else: dead.append(r)
            if i % 10 == 0 or i == len(proxies):
                try: bot.edit_message_text(f"⏳ Checking... {i}/{len(proxies)}", progress.chat.id, progress.message_id)
                except: pass
        try: bot.delete_message(progress.chat.id, progress.message_id)
        except: pass

        header = f"📊 <b>Proxy Result</b>\n━━━━━━━━━━━━━━━━━━\n✅ Alive: {len(live)} | ❌ Dead: {len(dead)}\n"
        bot.send_message(message.chat.id, header, parse_mode="HTML")
        
        chunk = ""
        for r in live:  # Shows all live proxies
            block = r['msg'] + "\n━━━━━━━━━━━━━━━━━━\n"
            if len(chunk) + len(block) > 3800:
                bot.send_message(message.chat.id, chunk, parse_mode="HTML")
                chunk = ""
            chunk += block
        if chunk: bot.send_message(message.chat.id, chunk, parse_mode="HTML")
        user_state.pop(uid, None)
        bot.send_message(message.chat.id, "✅ Complete!")

# ═══════════════════════════ TEXT HANDLER ═══════════════════════════

@bot.message_handler(content_types=['text'])
def on_text(message):
    uid = message.from_user.id
    text = (message.text or "").strip()

    if text.startswith('/'): return

    ok, hint = check_access_msg(uid)
    if not ok: return

    state = user_state.get(uid)
    
    if state and state.get('mode') == 'setup_pass':
        save_user_config(uid, password=text)
        user_state.pop(uid, None)
        bot.send_message(message.chat.id, f"✅ Password saved: <code>{esc(text)}</code>", parse_mode="HTML")
        return
        
    if state and state.get('mode') == 'setup_bin':
        save_user_config(uid, bin_pattern=text)
        # Chain into EXP setup
        user_state[uid] = {'mode': 'setup_exp'}
        bot.send_message(message.chat.id, f"✅ BIN saved: <code>{esc(text)}</code>\n\n📅 <b>এবার Expiration Date দাও (MM/YY)</b>\n<i>উদাহরণ: 02/26\n(Random চাইলে শুধু 'random' লেখো)</i>", parse_mode="HTML")
        return
        
    if state and state.get('mode') == 'setup_exp':
        exp_input = "" if text.lower() == 'random' else text
        save_user_config(uid, exp_date=exp_input)
        user_state.pop(uid, None)
        bot.send_message(message.chat.id, f"✅ Expiration Date saved: <code>{esc(text.upper())}</code>", parse_mode="HTML")
        return

    if state and state.get('mode') == 'update_mails':
        parsed = parse_emails(text)
        if not parsed:
            bot.send_message(message.chat.id, "❌ No valid emails found in text. Try again.")
            return
        
        # Overwrite old list completely
        mail_data = {"emails": parsed, "assigned": {}, "index": 0}
        save_mail_data(uid, mail_data)
        user_state.pop(uid, None)
        bot.send_message(message.chat.id, f"✅ Successfully updated list with <b>{len(parsed)}</b> emails.\nUse /start -> TN Caddy MODE to begin.", parse_mode="HTML")
        return

# ═══════════════════════════ CALLBACKS ═══════════════════════════

@bot.callback_query_handler(func=lambda call: True)
def on_callback(call):
    uid = call.from_user.id
    data = call.data

    # Stop loading animation on button
    try: bot.answer_callback_query(call.id)
    except: pass

    ok, hint = check_access_msg(uid)
    if not ok:
        try: bot.answer_callback_query(call.id, "Access Denied / Expired.", show_alert=True)
        except: pass
        return

    if data == "back_menu":
        kb = InlineKeyboardMarkup()
        kb.row(InlineKeyboardButton("TN , Caddy MODE", callback_data="menu_caddy"),
               InlineKeyboardButton("Proxy Checker", callback_data="menu_proxy"))
        try: bot.edit_message_text("Hello To Cypher World", call.message.chat.id, call.message.message_id, reply_markup=kb, parse_mode="HTML")
        except: pass
        return

    if data == "setup_pass":
        user_state[uid] = {'mode': 'setup_pass'}
        try: bot.edit_message_text("🔑 Please type your <b>Always Same Password</b>:", call.message.chat.id, call.message.message_id, parse_mode="HTML")
        except: pass
        return

    if data == "setup_bin":
        user_state[uid] = {'mode': 'setup_bin'}
        try: bot.edit_message_text("💳 Please type your <b>BIN</b> (e.g. 52792622911):", call.message.chat.id, call.message.message_id, parse_mode="HTML")
        except: pass
        return
        
    if data == "setup_exp":
        user_state[uid] = {'mode': 'setup_exp'}
        try: bot.edit_message_text("📅 Please type your <b>Expiration Date</b> (MM/YY):\n<i>(Type 'random' for auto-generation)</i>", call.message.chat.id, call.message.message_id, parse_mode="HTML")
        except: pass
        return

    if data == "menu_proxy":
        user_state[uid] = {'mode': 'proxy'}
        try: bot.edit_message_text("🌐 <b>Proxy Checker</b>\n\nSend me a .txt file containing your proxies (IP:PORT or IP:PORT:USER:PASS).", call.message.chat.id, call.message.message_id, parse_mode="HTML")
        except: pass
        return

    if data == "menu_caddy":
        text, kb = build_caddy_view(uid)
        try: bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=kb, parse_mode="HTML")
        except Exception as e:
            if "message is not modified" not in str(e).lower(): logger.warning(e)
        return

    # TN Caddy Navigations
    if data in ("caddy_next", "caddy_prev", "caddy_restart"):
        mail_data = load_mail_data(uid)
        idx = mail_data.get('index', 0)
        
        if data == "caddy_next": mail_data['index'] = idx + 1
        elif data == "caddy_prev": mail_data['index'] = max(0, idx - 1)
        elif data == "caddy_restart": mail_data['index'] = 0
        
        save_mail_data(uid, mail_data)
        text, kb = build_caddy_view(uid)
        try: 
            bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=kb, parse_mode="HTML")
        except Exception as e: 
            if "message is not modified" not in str(e).lower(): logger.warning(e)
        return

# ═══════════════════════════ MAIN ═══════════════════════════

def main():
    db_init()
    set_bot_commands()
    logger.info("✅ Cypher Bot starting...")
    print("✅ Bot is online... (Press Ctrl+C to stop)")
    # Using infinity_polling to auto-reconnect if telegram servers drop
    bot.infinity_polling(timeout=30, long_polling_timeout=25)

if __name__ == "__main__":
    main()