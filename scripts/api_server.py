#!/usr/bin/env python3
"""
PhoneInventory API Server
Minimal REST API for cross-device data sync (sales, transfers, etc.)
Runs alongside the Telegram bot.
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import sys
import threading
import time
import urllib.request
from datetime import datetime, timezone, timedelta
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, quote

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import env_loader
import export_inventory
import mailer

DEPLOY_DIR = env_loader.DEPLOY_DIR
DB_PATH = os.path.join(DEPLOY_DIR, "data", "inventory.db")
API_PORT = int(os.environ.get("API_PORT", "8580"))
API_HOST = os.environ.get("API_HOST", "127.0.0.1")  # loopback: only nginx reaches it
PST = timezone(timedelta(hours=-7))

# ─── Auth config ───
AUTH_SECRET = env_loader.require_env("AUTH_SECRET").encode()
TOKEN_TTL = 30 * 86400          # login token lifetime: 30 days
DEFAULT_PIN = "888888"
IMEI_API_KEY = os.environ.get("IMEI_API_KEY", "")
ALLOWED_ORIGIN = os.environ.get("ALLOWED_ORIGIN", "https://phonesinventory.com")

# HK team Telegram bot — notified on new defect/repair reports (env-gated).
HK_BOT_TOKEN = os.environ.get("HK_BOT_TOKEN", "")
HK_CHAT_ID = os.environ.get("HK_CHAT_ID", "")


def _decode_data_url(durl):
    try:
        b64 = durl.split(',', 1)[1] if ',' in durl else durl
        return base64.b64decode(b64)
    except Exception:
        return None


def notify_hk_defect(defect):
    """Push a defect report (caption + photos) to the HK team Telegram.
    No-op until HK_BOT_TOKEN and HK_CHAT_ID are configured. Runs in a thread."""
    if not HK_BOT_TOKEN or not HK_CHAT_ID:
        return
    try:
        import requests
        issues = defect.get('issues') or []
        if isinstance(issues, str):
            try: issues = json.loads(issues)
            except Exception: issues = [issues]
        sev = {'minor': '轻微', 'moderate': '中等', 'severe': '严重', 'dead': '无法使用'}.get(
            defect.get('severity', ''), defect.get('severity', ''))
        caption = (
            "🔧 报修通知 / Repair report\n"
            "📱 " + str(defect.get('phone_name') or '') + "\n"
            "IMEI: " + str(defect.get('imei', '')) + "\n"
            "📍 " + str(defect.get('store', '')) + "\n"
            "⚠️ 故障: " + ("、".join(issues) if issues else '-') + (("  (" + sev + ")") if sev else "") + "\n"
            "📝 " + (str(defect.get('description') or '') or '-') + "\n"
            "👤 " + str(defect.get('reported_by') or '')
        )
        photos = defect.get('photos') or []
        if isinstance(photos, str):
            try: photos = json.loads(photos)
            except Exception: photos = []
        imgs = [b for b in (_decode_data_url(p) for p in photos) if b]
        base = "https://api.telegram.org/bot" + HK_BOT_TOKEN
        if not imgs:
            requests.post(base + "/sendMessage", data={'chat_id': HK_CHAT_ID, 'text': caption}, timeout=20)
        elif len(imgs) == 1:
            requests.post(base + "/sendPhoto", data={'chat_id': HK_CHAT_ID, 'caption': caption},
                          files={'photo': ('defect.jpg', imgs[0], 'image/jpeg')}, timeout=30)
        else:
            media, files = [], {}
            for i, b in enumerate(imgs[:10]):
                key = 'photo%d' % i
                files[key] = (key + '.jpg', b, 'image/jpeg')
                item = {'type': 'photo', 'media': 'attach://' + key}
                if i == 0:
                    item['caption'] = caption
                media.append(item)
            requests.post(base + "/sendMediaGroup",
                          data={'chat_id': HK_CHAT_ID, 'media': json.dumps(media)}, files=files, timeout=40)
    except Exception as e:
        print("[HK-notify] failed:", e)

# Endpoints reachable without a token
PUBLIC_PATHS = {
    ('POST', '/api/login'),
    ('POST', '/api/pin-reset/request'),
    ('POST', '/api/pin-reset/confirm'),
    ('GET', '/api/health'),
}

# Login accounts (seeded into users table; PINs live in user_pins as salted hashes)
SEED_USERS = [
    ('anderson@ifixforu.com', 'Andy Wang', 'admin', 'all'),
    ('jayden@ifixforu.com', 'Jayden Sun', 'staff', 'Alhambra'),
    ('steve@ifixforu.com', 'Steve Shen', 'staff', 'Monterey Park'),
    ('harry@ifixforu.com', 'Harry Zou', 'staff', 'San Gabriel'),
    ('bill@ifixforu.com', 'Bill Han', 'staff', 'Rowland Heights'),
    ('will@ifixforu.com', 'Will Jiang', 'staff', 'Arcadia 1'),
    ('kelvin@ifixforu.com', 'Kelvin Liu', 'staff', 'Arcadia 2'),
    ('cici@ifixforu.com', 'Cici', 'staff', 'Irvine'),
    ('feiyang@ifixforu.com', 'Feiyang Zhao', 'staff', 'Rancho Cucamonga'),
    ('jason@ifixforu.com', 'Jason Zeng', 'staff', 'Las Vegas'),
    ('chester@ifixforu.com', 'Chester', 'staff', 'Alhambra'),
    ('grace@ifixforu.com', 'Grace', 'staff', 'Las Vegas'),
    ('bobby@ifixforu.com', 'Bobby', 'staff', 'Monterey Park'),
    ('hk@ifixforu.com', 'HK Team', 'hk', '香港仓'),
]

db_lock = threading.Lock()

# ─── PIN hashing (sha256$<salt>$<hex>) ───

def hash_pin(pin, salt=None):
    salt = salt or secrets.token_hex(8)
    digest = hashlib.sha256((salt + pin).encode()).hexdigest()
    return f"sha256${salt}${digest}"


def verify_pin(pin, stored):
    if not stored:
        return False
    stored = str(stored)
    if stored.startswith("sha256$"):
        try:
            _, salt, digest = stored.split("$", 2)
        except ValueError:
            return False
        return hmac.compare_digest(hashlib.sha256((salt + pin).encode()).hexdigest(), digest)
    # Legacy plaintext row (pre-migration safety net)
    return hmac.compare_digest(stored, pin)


# ─── Auth tokens (HMAC-signed, 30-day expiry) ───

def make_token(email):
    exp = int(time.time()) + TOKEN_TTL
    payload = f"{email}|{exp}"
    sig = hmac.new(AUTH_SECRET, payload.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{payload}|{sig}".encode()).decode()


def verify_token(token):
    """Return email if token is valid and unexpired, else None."""
    try:
        payload = base64.urlsafe_b64decode(token.encode()).decode()
        email, exp, sig = payload.rsplit("|", 2)
        expected = hmac.new(AUTH_SECRET, f"{email}|{exp}".encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        if int(exp) < time.time():
            return None
        return email
    except Exception:
        return None


# ─── In-memory rate limiting (single process) ───

_rate_lock = threading.Lock()
_login_fails = {}   # "email|ip" -> [timestamps]
_reset_sends = {}   # email -> [timestamps]


def _too_many(bucket, key, limit, window):
    now = time.time()
    with _rate_lock:
        recent = [t for t in bucket.get(key, []) if now - t < window]
        bucket[key] = recent
        return len(recent) >= limit


def _record(bucket, key):
    with _rate_lock:
        bucket.setdefault(key, []).append(time.time())


def _notify_pin_changed(email, name):
    """Best-effort PIN-change notice (background thread)."""
    if not mailer.is_configured():
        return
    try:
        mailer.send_pin_changed_notice(email, name)
    except Exception as e:
        print(f"[API] Failed to send PIN change notice to {email}: {e}")

# ─── Store Name Normalization ───
# Slug key → display name mapping (must match STORE_DATA in index.html)
STORE_KEY_TO_NAME = {
    'alhambra': 'Alhambra',
    'monterey-park': 'Monterey Park',
    'san-gabriel': 'San Gabriel',
    'rowland-heights': 'Rowland Heights',
    'arcadia-1': 'Arcadia 1',
    'arcadia-2': 'Arcadia 2',
    'irvine': 'Irvine',
    'rancho-cucamonga': 'Rancho Cucamonga',
    'las-vegas': 'Las Vegas',
    'hq-warehouse': 'HQ 总仓',
    'hk-warehouse': '香港仓',
}

def normalize_store_name(raw):
    """Convert slug key or any variant to canonical display name."""
    if not raw:
        return raw
    # Already a display name?
    for name in STORE_KEY_TO_NAME.values():
        if raw == name:
            return name
    # Try slug lookup
    key = raw.lower().strip().replace(' ', '-')
    if key in STORE_KEY_TO_NAME:
        return STORE_KEY_TO_NAME[key]
    # Try direct lowercase match
    name_lower = {v.lower(): v for v in STORE_KEY_TO_NAME.values()}
    if raw.lower().strip() in name_lower:
        return name_lower[raw.lower().strip()]
    return raw


def get_db():
    # timeout: how long to wait for a locked DB before raising (cross-process WAL writes)
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


def get_user(conn, email):
    return conn.execute(
        "SELECT email, name, role, store FROM users WHERE email = ?", (email,)
    ).fetchone()


def check_user_pin(conn, email, pin):
    """True if pin matches the stored (or default) PIN for email."""
    row = conn.execute("SELECT pin FROM user_pins WHERE email = ?", (email,)).fetchone()
    if row:
        return verify_pin(pin, row['pin'])
    return pin == DEFAULT_PIN


def pin_is_default(conn, email):
    row = conn.execute("SELECT pin FROM user_pins WHERE email = ?", (email,)).fetchone()
    if row:
        return verify_pin(DEFAULT_PIN, row['pin'])
    return True


def init_api_tables():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sales (
            id TEXT PRIMARY KEY,
            imei TEXT NOT NULL,
            phone_name TEXT,
            storage TEXT,
            color TEXT,
            color_en TEXT,
            cond TEXT,
            region TEXT,
            cost REAL DEFAULT 0,
            msrp REAL DEFAULT 0,
            price REAL DEFAULT 0,
            tax REAL DEFAULT 0,
            total REAL DEFAULT 0,
            profit REAL DEFAULT 0,
            tax_applied INTEGER DEFAULT 0,
            tax_rate REAL DEFAULT 0,
            customer TEXT,
            customer_phone TEXT,
            customer_email TEXT,
            payment_methods TEXT,
            store TEXT,
            store_key TEXT,
            seller TEXT,
            assistants TEXT,
            status TEXT DEFAULT 'completed',
            created_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_sales_imei ON sales(imei);
        CREATE INDEX IF NOT EXISTS idx_sales_status ON sales(status);

        CREATE TABLE IF NOT EXISTS transfers (
            id TEXT PRIMARY KEY,
            imei TEXT NOT NULL,
            phone_name TEXT,
            from_store TEXT,
            to_store TEXT,
            requested_by TEXT,
            approved_by TEXT,
            rejected_by TEXT,
            status TEXT DEFAULT 'pending',
            notes TEXT,
            created_at TEXT,
            updated_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_transfers_status ON transfers(status);

        CREATE TABLE IF NOT EXISTS deposits (
            id TEXT PRIMARY KEY,
            store TEXT,
            store_key TEXT,
            amount REAL DEFAULT 0,
            method TEXT,
            note TEXT,
            deposited_by TEXT,
            created_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_deposits_store ON deposits(store_key);

        CREATE TABLE IF NOT EXISTS shipments (
            id TEXT PRIMARY KEY,
            tracking_no TEXT,
            carrier TEXT,
            from_store TEXT,
            to_store TEXT,
            imeis TEXT,
            qty INTEGER DEFAULT 0,
            received_imeis TEXT,
            status TEXT DEFAULT 'draft',
            notes TEXT,
            created_by TEXT,
            received_by TEXT,
            shipped_at TEXT,
            received_at TEXT,
            created_at TEXT,
            updated_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_shipments_status ON shipments(status);
        CREATE INDEX IF NOT EXISTS idx_shipments_tracking ON shipments(tracking_no);

        CREATE TABLE IF NOT EXISTS defects (
            id TEXT PRIMARY KEY,
            imei TEXT NOT NULL,
            phone_name TEXT,
            storage TEXT,
            store TEXT,
            issues TEXT,
            severity TEXT,
            description TEXT,
            photos TEXT,
            status TEXT DEFAULT 'open',
            reported_by TEXT,
            resolved_by TEXT,
            created_at TEXT,
            updated_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_defects_status ON defects(status);
        CREATE INDEX IF NOT EXISTS idx_defects_imei ON defects(imei);

        CREATE TABLE IF NOT EXISTS tradeins (
            id TEXT PRIMARY KEY,
            imei TEXT,
            model TEXT,
            storage TEXT,
            color TEXT,
            region TEXT,
            grade TEXT,
            battery TEXT,
            ref_price REAL,
            coefficient REAL,
            deductions REAL,
            final_price REAL,
            checklist TEXT,
            deduction_items TEXT,
            seller_name TEXT,
            seller_id TEXT,
            seller_source TEXT,
            store TEXT,
            staff TEXT,
            status TEXT DEFAULT 'acquired',
            created_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_tradeins_created ON tradeins(created_at);

        CREATE TABLE IF NOT EXISTS stock_requests (
            id TEXT PRIMARY KEY,
            requested_by TEXT,
            store TEXT,
            model TEXT,
            items TEXT,
            qty INTEGER DEFAULT 1,
            note TEXT,
            status TEXT DEFAULT 'pending',
            approved_by TEXT,
            fulfilled_by TEXT,
            created_at TEXT,
            updated_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_sr_status ON stock_requests(status);

        CREATE TABLE IF NOT EXISTS user_pins (
            email TEXT PRIMARY KEY,
            pin TEXT NOT NULL,
            changed_at TEXT
        );

        CREATE TABLE IF NOT EXISTS users (
            email TEXT PRIMARY KEY,
            name TEXT,
            role TEXT DEFAULT 'staff',
            store TEXT
        );

        CREATE TABLE IF NOT EXISTS pin_reset_codes (
            email TEXT PRIMARY KEY,
            code_hash TEXT,
            expires_at INTEGER,
            attempts INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT,
            actor TEXT,
            role TEXT,
            action TEXT,
            entity_type TEXT,
            entity_id TEXT,
            detail TEXT,
            ip TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(id DESC);
    """)
    # inventory table is created by inventory_bot.py; ensure its hot-path indexes
    # exist regardless (imei is looked up on nearly every write). Guarded so a
    # not-yet-created inventory table (fresh setup) doesn't break API startup.
    try:
        conn.executescript("""
            CREATE INDEX IF NOT EXISTS idx_inv_imei ON inventory(imei);
            CREATE INDEX IF NOT EXISTS idx_inv_store_status ON inventory(store, status);
            CREATE INDEX IF NOT EXISTS idx_inv_status ON inventory(status);
        """)
    except Exception:
        pass
    # Seed login accounts (never overwrites existing rows)
    conn.executemany(
        "INSERT OR IGNORE INTO users (email, name, role, store) VALUES (?,?,?,?)",
        SEED_USERS
    )
    # Migrate legacy plaintext PINs to salted hashes
    for r in conn.execute("SELECT email, pin FROM user_pins").fetchall():
        if not str(r['pin']).startswith('sha256$'):
            conn.execute("UPDATE user_pins SET pin = ? WHERE email = ?",
                         (hash_pin(str(r['pin'])), r['email']))
    # Additive column migrations for existing DBs (idempotent)
    _ensure_columns(conn, "sales", {"assistants": "TEXT"})
    conn.commit()
    conn.close()


def _ensure_columns(conn, table, cols):
    """Add missing columns to an existing table. cols = {name: sqltype}."""
    existing = {r["name"] for r in conn.execute("PRAGMA table_info(%s)" % table).fetchall()}
    for name, sqltype in cols.items():
        if name not in existing:
            try:
                conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, name, sqltype))
            except Exception:
                pass


_CORS_ALLOWED = {
    ALLOWED_ORIGIN, 'https://phonesinventory.com', 'https://www.phonesinventory.com',
}
_CORS_DEV_HOSTS = ('localhost', '127.0.0.1')


def cors_origin(handler):
    """Locks CORS to an exact allowlist; localhost/127.0.0.1 (any port) for dev.
    Substring matching was unsafe (https://localhost.evil.com would pass)."""
    origin = handler.headers.get('Origin', '')
    if origin in _CORS_ALLOWED:
        return origin
    try:
        host = origin.split('://', 1)[1].split('/', 1)[0].split(':', 1)[0]
    except (IndexError, AttributeError):
        host = ''
    if host in _CORS_DEV_HOSTS:
        return origin
    return ALLOWED_ORIGIN


def json_response(handler, data, status=200):
    body = json.dumps(data, ensure_ascii=False).encode('utf-8')
    handler.send_response(status)
    handler.send_header('Content-Type', 'application/json; charset=utf-8')
    handler.send_header('Access-Control-Allow-Origin', cors_origin(handler))
    handler.send_header('Access-Control-Allow-Methods', 'GET, POST, PUT, DELETE, OPTIONS')
    handler.send_header('Access-Control-Allow-Headers', 'Content-Type, X-Auth-Token')
    handler.send_header('Content-Length', len(body))
    handler.end_headers()
    handler.wfile.write(body)


class APIHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        print(f"[API] {self.client_address[0]} - {format % args}")

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header('Access-Control-Allow-Origin', cors_origin(self))
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, PUT, DELETE, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, X-Auth-Token')
        self.end_headers()

    MAX_BODY = 8 * 1024 * 1024  # 8 MB cap (Excel intake can be a few hundred KB)

    def read_body(self):
        try:
            length = int(self.headers.get('Content-Length', 0))
        except (TypeError, ValueError):
            length = 0
        if length <= 0:
            return {}
        if length > self.MAX_BODY:
            raise ValueError('request body too large')
        return json.loads(self.rfile.read(length))

    def check_auth(self, method, path):
        """Token gate for every endpoint except PUBLIC_PATHS.
        Returns the authenticated email ('' for public paths), or None
        after sending a 401 response."""
        if (method, path) in PUBLIC_PATHS:
            self._auth_email = None
            return ''
        token = self.headers.get('X-Auth-Token', '')
        email = verify_token(token) if token else None
        if not email:
            json_response(self, {'error': 'unauthorized'}, 401)
            return None
        self._auth_email = email
        return email

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)

        if self.check_auth('GET', path) is None:
            return

        if path == '/api/inventory':
            return self.get_inventory(params)
        elif path == '/api/phones':
            return self.get_phones()
        elif path == '/api/sales':
            return self.get_sales(params)
        elif path == '/api/transfers':
            return self.get_transfers(params)
        elif path == '/api/shipments':
            return self.get_shipments(params)
        elif path == '/api/deposits':
            return self.get_deposits(params)
        elif path == '/api/defects':
            return self.get_defects(params)
        elif path == '/api/tradeins':
            return self.get_tradeins(params)
        elif path == '/api/audit-log':
            return self.get_audit_log(params)
        elif path == '/api/stock-requests':
            return self.get_stock_requests()
        elif path == '/api/sold-imeis':
            return self.get_sold_imeis()
        elif path == '/api/imei-check':
            return self.imei_check(params)
        elif path == '/api/user-pins':
            return self.get_user_pins()
        elif path == '/api/health':
            return self.health_check()
        else:
            json_response(self, {'error': 'Not found'}, 404)

    def do_POST(self):
        path = urlparse(self.path).path

        if self.check_auth('POST', path) is None:
            return

        if path == '/api/login':
            return self.login()
        elif path == '/api/pin-reset/request':
            return self.pin_reset_request()
        elif path == '/api/pin-reset/confirm':
            return self.pin_reset_confirm()
        elif path == '/api/sale':
            return self.create_sale()
        elif path == '/api/inventory/batch':
            return self.create_inventory_batch()
        elif path == '/api/shipments':
            return self.create_shipment()
        elif path == '/api/deposit':
            return self.create_deposit()
        elif path == '/api/defects':
            return self.create_defect()
        elif path == '/api/tradein':
            return self.create_tradein()
        elif path == '/api/transfer/batch':
            return self.create_transfer_batch()
        elif path == '/api/transfer':
            return self.create_transfer()
        elif path == '/api/stock-requests':
            return self.handle_stock_request()
        elif path == '/api/change-pin':
            return self.change_pin()
        else:
            json_response(self, {'error': 'Not found'}, 404)

    def do_PUT(self):
        path = urlparse(self.path).path

        if self.check_auth('PUT', path) is None:
            return

        if path.startswith('/api/shipments/'):
            shipment_id = path.split('/')[-1]
            return self.update_shipment(shipment_id)
        elif path.startswith('/api/defects/'):
            defect_id = path.split('/')[-1]
            return self.update_defect(defect_id)
        elif path.startswith('/api/transfer/'):
            transfer_id = path.split('/')[-1]
            return self.update_transfer(transfer_id)
        elif path.startswith('/api/sale/'):
            sale_id = path.split('/')[-1]
            return self.update_sale(sale_id)
        elif path.startswith('/api/inventory/'):
            old_imei = path.split('/')[-1]
            return self.update_inventory(old_imei)
        else:
            json_response(self, {'error': 'Not found'}, 404)

    def do_DELETE(self):
        path = urlparse(self.path).path

        if self.check_auth('DELETE', path) is None:
            return

        if path.startswith('/api/inventory/'):
            imei = path.split('/')[-1]
            return self.delete_inventory(imei)
        else:
            json_response(self, {'error': 'Not found'}, 404)

    # ─── Auth: login / PIN reset ───

    def login(self):
        try:
            data = self.read_body()
            email = str(data.get('email', '')).strip().lower()
            pin = str(data.get('pin', '')).strip()
            key = f"{email}|{self.client_address[0]}"
            if _too_many(_login_fails, key, 10, 900):
                return json_response(self, {
                    'error': '尝试次数过多，请15分钟后再试 / Too many attempts, try again in 15 minutes'
                }, 429)
            if not email or not pin:
                return json_response(self, {'error': '邮箱或密码错误 / Incorrect email or PIN'}, 401)

            conn = get_db()
            try:
                user = get_user(conn, email)
                if not user or not check_user_pin(conn, email, pin):
                    _record(_login_fails, key)
                    return json_response(self, {'error': '邮箱或密码错误 / Incorrect email or PIN'}, 401)
                must_change = pin_is_default(conn, email)
            finally:
                conn.close()

            return json_response(self, {
                'ok': True,
                'token': make_token(email),
                'account': {'email': user['email'], 'name': user['name'],
                            'role': user['role'], 'store': user['store']},
                'mustChangePin': must_change,
            })
        except Exception as e:
            print(f"[API] Error in login: {e}")
            return json_response(self, {'error': str(e)}, 500)

    def pin_reset_request(self):
        """Email a 6-digit reset code (10 min validity, 3 sends/hour/email)."""
        try:
            if not mailer.is_configured():
                return json_response(self, {
                    'error': '邮件服务未配置，请联系管理员 / Email service not configured'
                }, 503)
            data = self.read_body()
            email = str(data.get('email', '')).strip().lower()
            if not email:
                return json_response(self, {'error': 'Email required'}, 400)
            if _too_many(_reset_sends, email, 3, 3600):
                return json_response(self, {
                    'error': '请求过于频繁，请稍后再试 / Too many requests, try again later'
                }, 429)

            conn = get_db()
            try:
                user = get_user(conn, email)
            finally:
                conn.close()
            if not user:
                # Don't reveal whether the account exists
                return json_response(self, {'ok': True})

            code = f"{secrets.randbelow(1000000):06d}"
            expires = int(time.time()) + 600
            with db_lock:
                conn = get_db()
                try:
                    conn.execute(
                        "INSERT OR REPLACE INTO pin_reset_codes (email, code_hash, expires_at, attempts) VALUES (?,?,?,0)",
                        (email, hash_pin(code), expires)
                    )
                    conn.commit()
                finally:
                    conn.close()

            try:
                mailer.send_pin_reset_code(email, user['name'], code)
            except Exception as e:
                print(f"[API] Failed to send reset code to {email}: {e}")
                return json_response(self, {
                    'error': '邮件发送失败，请稍后再试 / Failed to send email'
                }, 502)
            _record(_reset_sends, email)
            return json_response(self, {'ok': True})
        except Exception as e:
            print(f"[API] Error in pin_reset_request: {e}")
            return json_response(self, {'error': str(e)}, 500)

    def pin_reset_confirm(self):
        """Verify reset code, set new PIN, auto-login (same payload as /api/login)."""
        try:
            data = self.read_body()
            email = str(data.get('email', '')).strip().lower()
            code = str(data.get('code', '')).strip()
            new_pin = str(data.get('newPin', '')).strip()
            if len(new_pin) != 6 or not new_pin.isdigit():
                return json_response(self, {'error': 'PIN must be 6 digits'}, 400)
            if new_pin == DEFAULT_PIN:
                return json_response(self, {'error': 'Cannot use default PIN'}, 400)

            now_ts = int(time.time())
            now = datetime.now(PST).strftime("%Y-%m-%d %H:%M:%S")
            with db_lock:
                conn = get_db()
                try:
                    row = conn.execute(
                        "SELECT code_hash, expires_at, attempts FROM pin_reset_codes WHERE email = ?",
                        (email,)
                    ).fetchone()
                    if not row or row['expires_at'] < now_ts or row['attempts'] >= 5:
                        conn.execute("DELETE FROM pin_reset_codes WHERE email = ? AND expires_at < ?",
                                     (email, now_ts))
                        conn.commit()
                        return json_response(self, {
                            'error': '验证码无效或已过期 / Invalid or expired code'
                        }, 401)
                    if not verify_pin(code, row['code_hash']):
                        conn.execute("UPDATE pin_reset_codes SET attempts = attempts + 1 WHERE email = ?",
                                     (email,))
                        conn.commit()
                        return json_response(self, {'error': '验证码错误 / Incorrect code'}, 401)

                    user = get_user(conn, email)
                    if not user:
                        return json_response(self, {'error': 'Account not found'}, 404)
                    conn.execute(
                        "INSERT OR REPLACE INTO user_pins (email, pin, changed_at) VALUES (?,?,?)",
                        (email, hash_pin(new_pin), now)
                    )
                    conn.execute("DELETE FROM pin_reset_codes WHERE email = ?", (email,))
                    conn.commit()
                finally:
                    conn.close()

            threading.Thread(target=_notify_pin_changed, args=(email, user['name']), daemon=True).start()
            return json_response(self, {
                'ok': True,
                'token': make_token(email),
                'account': {'email': user['email'], 'name': user['name'],
                            'role': user['role'], 'store': user['store']},
                'mustChangePin': False,
            })
        except Exception as e:
            print(f"[API] Error in pin_reset_confirm: {e}")
            return json_response(self, {'error': str(e)}, 500)

    # ─── IMEI lookup proxy (keeps the third-party API key server-side) ───

    def imei_check(self, params):
        try:
            imei = params.get('imei', [''])[0].strip()
            srv = params.get('srv', ['1013'])[0].strip()
            digits = ''.join(c for c in imei if c.isdigit())
            if len(digits) != 15:
                return json_response(self, {'error': 'IMEI must be 15 digits'}, 400)
            if srv not in ('1013', '1010'):
                return json_response(self, {'error': 'Invalid srv'}, 400)
            if not IMEI_API_KEY:
                return json_response(self, {'error': 'IMEI API key not configured'}, 503)
            url = (f"https://us.gsxunlocking.com/api/uapi?format=json"
                   f"&key={quote(IMEI_API_KEY)}&srv={srv}&imei={digits}")
            req = urllib.request.Request(url, headers={'User-Agent': 'PhonesInventory/1.0'})
            with urllib.request.urlopen(req, timeout=120) as resp:
                body = resp.read().decode('utf-8', 'replace')
            try:
                return json_response(self, json.loads(body))
            except ValueError:
                return json_response(self, {'error': 'Upstream returned non-JSON'}, 502)
        except Exception as e:
            print(f"[API] IMEI check failed: {e}")
            return json_response(self, {'error': '查询失败，请稍后再试 / Lookup failed'}, 502)

    # ─── Enriched phones list (replaces the public data/phones.js export) ───

    def _auth_user(self, conn):
        """Return the authenticated user's row (email/name/role/store) or None."""
        email = getattr(self, '_auth_email', None)
        if not email:
            return None
        return get_user(conn, email)

    def _is_admin(self, conn):
        """True if the authenticated user is an admin. Used to gate owner-only
        fields (e.g. cost) so staff devices never receive them."""
        u = self._auth_user(conn)
        return bool(u and u['role'] == 'admin')

    def _audit(self, conn, action, entity_type, entity_id='', detail=None):
        """Append-only activity log. Call inside a handler's db_lock, before commit."""
        try:
            u = self._auth_user(conn)
            now = datetime.now(PST).strftime("%Y-%m-%d %H:%M:%S")
            actor = (u['name'] if u else '') or (getattr(self, '_auth_email', '') or '')
            ip = self.client_address[0] if getattr(self, 'client_address', None) else ''
            conn.execute(
                "INSERT INTO audit_log (ts,actor,role,action,entity_type,entity_id,detail,ip) VALUES (?,?,?,?,?,?,?,?)",
                (now, actor, (u['role'] if u else ''), action, entity_type, str(entity_id),
                 json.dumps(detail, ensure_ascii=False) if detail else '', ip))
        except Exception:
            pass

    def get_audit_log(self, params):
        conn = None
        try:
            conn = get_db()
            user = self._auth_user(conn)
            if not (user and user['role'] == 'admin'):
                return json_response(self, {'error': '仅管理员可见 / Admin only'}, 403)
            try:
                limit = min(500, max(1, int(params.get('limit', ['200'])[0])))
            except (TypeError, ValueError):
                limit = 200
            rows = conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                try: d['detail'] = json.loads(d['detail']) if d.get('detail') else None
                except Exception: d['detail'] = d.get('detail')
                out.append(d)
            return json_response(self, {'log': out})
        except Exception as e:
            return json_response(self, {'error': str(e)}, 500)
        finally:
            if conn is not None:
                conn.close()

    def get_phones(self):
        conn = None
        try:
            conn = get_db()
            user = self._auth_user(conn)
            role = user['role'] if user else None
            # HK warehouse role sees only HK warehouse + HQ warehouse stock.
            if role == 'hk':
                rows = conn.execute(
                    "SELECT * FROM inventory WHERE store IN ('香港仓','HQ 总仓') ORDER BY id DESC"
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM inventory ORDER BY id DESC").fetchall()
            phones = export_inventory.build_phones(rows)
            # cost is owner-only. admin: all. HK: only its own 香港仓 cost (HQ masked).
            # staff: never.
            if role == 'hk':
                for p in phones:
                    if p.get('store') != '香港仓':
                        p['cost'] = 0
            elif role != 'admin':
                for p in phones:
                    p['cost'] = 0
            return json_response(self, {'phones': phones})
        except Exception as e:
            return json_response(self, {'error': str(e)}, 500)
        finally:
            if conn is not None:
                conn.close()

    # ─── Inventory ───

    def get_inventory(self, params):
        """List inventory with optional filters."""
        conn = None
        try:
            conn = get_db()
            store = params.get('store', [None])[0]
            status = params.get('status', [None])[0]

            query = "SELECT * FROM inventory"
            conditions = []
            args = []

            user = self._auth_user(conn)
            role = user['role'] if user else None
            if role == 'hk':
                # HK warehouse role is limited to HK + HQ warehouses.
                norm = normalize_store_name(store) if (store and store != 'all') else None
                if norm in ('香港仓', 'HQ 总仓'):
                    conditions.append("store = ?")
                    args.append(norm)
                else:
                    conditions.append("store IN ('香港仓','HQ 总仓')")
            elif store and store != 'all':
                conditions.append("store = ?")
                args.append(normalize_store_name(store))
            if status:
                conditions.append("status = ?")
                args.append(status)

            if conditions:
                query += " WHERE " + " AND ".join(conditions)
            query += " ORDER BY id DESC"

            rows = conn.execute(query, args).fetchall()

            items = []
            for r in rows:
                item = dict(r)
                item['batteryHealth'] = item.pop('battery_health', '')
                item['colorEn'] = item.pop('color_en', '')
                item['scannedBy'] = item.pop('scanned_by', '')
                item['scannedAt'] = item.pop('scanned_at', '')
                item['createdAt'] = item.pop('created_at', '')
                item.pop('raw_ocr', None)  # Don't send raw OCR data
                # cost is owner-only: admin all; HK only own 香港仓; staff none.
                if role == 'admin':
                    pass
                elif role == 'hk':
                    if item.get('store') != '香港仓':
                        item['cost'] = 0
                else:
                    item['cost'] = 0
                items.append(item)

            return json_response(self, {'inventory': items, 'total': len(items)})
        except Exception as e:
            return json_response(self, {'error': str(e)}, 500)
        finally:
            if conn is not None:
                conn.close()

    def delete_inventory(self, imei):
        """Delete an inventory record by IMEI."""
        try:
            with db_lock:
                conn = get_db()
                try:
                    user = self._auth_user(conn)
                    if not (user and user['role'] == 'admin'):
                        return json_response(self, {'error': '仅管理员可删除库存 / Admin only'}, 403)
                    row = conn.execute("SELECT id, status FROM inventory WHERE imei = ?", (imei,)).fetchone()
                    if not row:
                        return json_response(self, {'error': 'Record not found'}, 404)
                    # Prevent deleting sold phones with active sales
                    if row['status'] == 'sold':
                        sale = conn.execute(
                            "SELECT id FROM sales WHERE imei = ? AND status = 'completed'", (imei,)
                        ).fetchone()
                        if sale:
                            return json_response(self, {
                                'error': f'Cannot delete: phone has active sale ({sale["id"]})'
                            }, 400)
                    conn.execute("DELETE FROM inventory WHERE imei = ?", (imei,))
                    self._audit(conn, 'inventory.delete', 'inventory', imei, {'status': row['status']})
                    conn.commit()
                finally:
                    conn.close()
            return json_response(self, {'ok': True})
        except Exception as e:
            return json_response(self, {'error': str(e)}, 500)

    # ─── Sales ───

    def create_sale(self):
        try:
            data = self.read_body()
            imei = data.get('imei', '').strip()
            if not imei:
                return json_response(self, {'error': 'IMEI required'}, 400)
            # Validate IMEI format
            digits = ''.join(c for c in imei if c.isdigit())
            if len(digits) != 15:
                return json_response(self, {'error': 'IMEI must be 15 digits'}, 400)
            imei = digits

            with db_lock:
                conn = get_db()
                try:
                    # Check duplicate sale
                    existing = conn.execute(
                        "SELECT id FROM sales WHERE imei = ? AND status = 'completed'", (imei,)
                    ).fetchone()
                    if existing:
                        return json_response(self, {
                            'error': f'此手机已售出 (订单: {existing["id"]})',
                            'duplicate': True,
                            'existing_id': existing['id']
                        }, 409)

                    # Verify phone exists + is sellable; pull authoritative cost/store from inventory.
                    inv = conn.execute(
                        "SELECT id, status, cost, store FROM inventory WHERE imei = ?", (imei,)
                    ).fetchone()
                    if not inv:
                        return json_response(self, {'error': 'IMEI not found in inventory'}, 404)
                    if inv['status'] != 'available':
                        msg = {'sold': '已售出', 'transit': '正在调拨中', 'reserved': '已被预留',
                               'defect': '问题机(报修中)'}.get(inv['status'], '当前不可销售')
                        return json_response(self, {'error': msg + ' / Not sellable'}, 409)

                    # Money math is server-authoritative: cost comes from inventory (never the
                    # client, which sees cost=0 for staff), profit is computed here. Seller is
                    # the authenticated user, not a client-supplied string.
                    user = self._auth_user(conn)
                    seller = (user['name'] if user else '') or data.get('seller', '')
                    real_cost = inv['cost'] or 0
                    try: price = float(data.get('price', 0) or 0)
                    except (TypeError, ValueError): price = 0
                    try: tax = float(data.get('tax', 0) or 0)
                    except (TypeError, ValueError): tax = 0
                    total = round(price + tax, 2)
                    profit = round(price - real_cost, 2)

                    now = datetime.now(PST).strftime("%Y-%m-%d %H:%M:%S")
                    store_name = normalize_store_name(data.get('store', '')) or (inv['store'] or '')
                    conn.execute("""
                        INSERT INTO sales (id, imei, phone_name, storage, color, color_en, cond, region,
                            cost, msrp, price, tax, total, profit, tax_applied, tax_rate,
                            customer, customer_phone, customer_email, payment_methods,
                            store, store_key, seller, assistants, status, created_at)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """, (
                        data.get('id', ''), imei, data.get('phoneName', ''),
                        data.get('storage', ''), data.get('color', ''), data.get('colorEn', ''),
                        data.get('cond', ''), data.get('region', ''),
                        real_cost, data.get('msrp', 0), price,
                        tax, total, profit,
                        1 if data.get('taxApplied') else 0, data.get('taxRate', 0),
                        data.get('customer', ''), data.get('customerPhone', ''),
                        data.get('customerEmail', ''), json.dumps(data.get('paymentMethods', [])),
                        store_name, data.get('storeKey', ''),
                        seller, json.dumps(data.get('assistants', []) or []), 'completed', now
                    ))
                    # Mark phone as sold (guard: only if still available).
                    conn.execute("UPDATE inventory SET status = 'sold' WHERE imei = ? AND status = 'available'", (imei,))
                    self._audit(conn, 'sale.create', 'sale', data.get('id', ''),
                                {'imei': imei, 'price': price, 'profit': profit, 'store': store_name})
                    conn.commit()
                finally:
                    conn.close()

            return json_response(self, {'ok': True, 'id': data.get('id', '')})
        except Exception as e:
            print(f"[API] Error creating sale: {e}")
            return json_response(self, {'error': str(e)}, 500)

    def get_sales(self, params):
        conn = None
        try:
            conn = get_db()
            user = self._auth_user(conn)
            role = user['role'] if user else None
            store = params.get('store', [None])[0]
            conds, args = [], []
            # Staff only ever see their own store's sales; HK sees none (no US sales access).
            if role == 'staff':
                conds.append("store = ?"); args.append(user['store'])
            elif role == 'hk':
                conds.append("1 = 0")
            elif store and store != 'all':
                conds.append("store_key = ?"); args.append(store)
            q = "SELECT * FROM sales"
            if conds:
                q += " WHERE " + " AND ".join(conds)
            q += " ORDER BY created_at DESC"
            rows = conn.execute(q, args).fetchall()
            is_admin = (role == 'admin')
            sales = []
            for r in rows:
                sale = dict(r)
                sale['taxApplied'] = bool(sale.pop('tax_applied', 0))
                sale['taxRate'] = sale.pop('tax_rate', 0)
                sale['phoneName'] = sale.pop('phone_name', '')
                sale['colorEn'] = sale.pop('color_en', '')
                sale['customerPhone'] = sale.pop('customer_phone', '')
                sale['customerEmail'] = sale.pop('customer_email', '')
                sale['storeKey'] = sale.pop('store_key', '')
                sale['createdAt'] = sale.pop('created_at', '')
                pm = sale.pop('payment_methods', '[]')
                try:
                    sale['paymentMethods'] = json.loads(pm) if pm else []
                except Exception:
                    sale['paymentMethods'] = []
                asst = sale.pop('assistants', None)
                try:
                    sale['assistants'] = json.loads(asst) if asst else []
                except Exception:
                    sale['assistants'] = []
                # Owner-only: hide cost/profit from non-admins.
                if not is_admin:
                    sale['cost'] = 0
                    sale['profit'] = 0
                sales.append(sale)
            return json_response(self, {'sales': sales})
        except Exception as e:
            return json_response(self, {'error': str(e)}, 500)
        finally:
            if conn is not None:
                conn.close()

    def update_sale(self, sale_id):
        try:
            data = self.read_body()
            with db_lock:
                conn = get_db()
                try:
                    user = self._auth_user(conn)
                    role = user['role'] if user else None
                    if role not in ('admin', 'staff'):
                        return json_response(self, {'error': '无权限 / Not allowed'}, 403)
                    status = data.get('status', '')
                    if status != 'returned':
                        return json_response(self, {'error': '仅支持退货 / Only "returned" supported'}, 400)
                    # Query state FIRST, before updating.
                    row = conn.execute("SELECT imei, status, store FROM sales WHERE id = ?", (sale_id,)).fetchone()
                    if not row:
                        return json_response(self, {'error': 'Sale not found'}, 404)
                    if role == 'staff' and normalize_store_name(row['store'] or '') != user['store']:
                        return json_response(self, {'error': '只能处理本店销售 / Own store only'}, 403)
                    if row['status'] == 'returned':
                        return json_response(self, {'error': 'Already returned'}, 400)
                    conn.execute("UPDATE sales SET status = 'returned' WHERE id = ?", (sale_id,))
                    conn.execute("UPDATE inventory SET status = 'available' WHERE imei = ? AND status = 'sold'", (row['imei'],))
                    self._audit(conn, 'sale.return', 'sale', sale_id, {'imei': row['imei']})
                    conn.commit()
                finally:
                    conn.close()
            return json_response(self, {'ok': True})
        except Exception as e:
            return json_response(self, {'error': str(e)}, 500)

    def get_sold_imeis(self):
        conn = None
        try:
            conn = get_db()
            rows = conn.execute("SELECT imei FROM sales WHERE status = 'completed'").fetchall()
            return json_response(self, {'imeis': [r['imei'] for r in rows]})
        except Exception as e:
            return json_response(self, {'error': str(e)}, 500)
        finally:
            if conn is not None:
                conn.close()

    # ─── Transfers ───

    def create_transfer(self):
        try:
            data = self.read_body()
            imei = data.get('imei', '')
            with db_lock:
                conn = get_db()
                try:
                    now = datetime.now(PST).strftime("%Y-%m-%d %H:%M:%S")
                    # Validate the phone is actually transferable before creating a request.
                    inv = conn.execute("SELECT status FROM inventory WHERE imei=?", (imei,)).fetchone()
                    if not inv:
                        return json_response(self, {'error': 'IMEI 不在库存中 / Phone not found in inventory'}, 404)
                    if inv['status'] != 'available':
                        msg = {
                            'sold': '此手机已售出',
                            'transit': '此手机已在调拨中',
                            'reserved': '此手机已被预留',
                        }.get(inv['status'], '此手机当前不可调拨')
                        return json_response(self, {'error': msg + ' / Phone not available for transfer'}, 409)
                    conn.execute("""
                        INSERT INTO transfers (id, imei, phone_name, from_store, to_store,
                            requested_by, status, notes, created_at, updated_at)
                        VALUES (?,?,?,?,?,?,?,?,?,?)
                    """, (
                        data.get('id', ''), data.get('imei', ''), data.get('phoneName', ''),
                        data.get('fromStore', ''), data.get('toStore', ''),
                        data.get('requestedBy', ''), 'pending',
                        data.get('notes', ''), now, now
                    ))
                    # Lock the phone at the origin store as in-transit:
                    # it stays in from_store (visible) but is no longer sellable.
                    conn.execute(
                        "UPDATE inventory SET status='transit' WHERE imei=? AND status='available'",
                        (data.get('imei', ''),)
                    )
                    conn.commit()
                finally:
                    conn.close()
            return json_response(self, {'ok': True, 'id': data.get('id', '')})
        except Exception as e:
            return json_response(self, {'error': str(e)}, 500)

    def create_inventory_batch(self):
        """Bulk add units to inventory (HK warehouse intake / admin restock).
        HK role is locked to 香港仓; admin may target any store. One shared
        spec (brand/model/storage/condition/region/color/cost/price) applied to
        a pasted list of IMEIs. Duplicates and malformed IMEIs are skipped."""
        try:
            data = self.read_body()
            with db_lock:
                conn = get_db()
                try:
                    user = self._auth_user(conn)
                    role = user['role'] if user else None
                    if role not in ('hk', 'admin'):
                        return json_response(self, {'error': '无权限入库 / Not allowed to add stock'}, 403)

                    store = '香港仓' if role == 'hk' else normalize_store_name(data.get('store') or '香港仓')
                    who = (user['name'] if user else '') or (getattr(self, '_auth_email', '') or '')
                    intake_note = 'HK intake' if role == 'hk' else 'admin intake'

                    # ── Per-row mode (Excel/CSV upload): each row carries its own spec ──
                    rows = data.get('rows')
                    if isinstance(rows, list) and rows:
                        now = datetime.now(PST).strftime("%Y-%m-%d %H:%M:%S")
                        existing = {r[0] for r in conn.execute("SELECT imei FROM inventory")}
                        created, skipped, seen = [], [], set()
                        for idx, row in enumerate(rows):
                            imei = str(row.get('imei', '')).strip().replace(' ', '')
                            r_model = (row.get('model') or '').strip()
                            r_storage = (row.get('storage') or '').strip()
                            if not imei:
                                skipped.append({'row': idx + 1, 'imei': '', 'reason': '缺 IMEI / Missing IMEI'}); continue
                            if len(imei) != 15 or not imei.isdigit():
                                skipped.append({'row': idx + 1, 'imei': imei, 'reason': '格式错误 / Invalid IMEI'}); continue
                            if imei in existing or imei in seen:
                                skipped.append({'row': idx + 1, 'imei': imei, 'reason': '已存在 / Duplicate'}); continue
                            if not r_model or not r_storage:
                                skipped.append({'row': idx + 1, 'imei': imei, 'reason': '缺型号或容量 / Missing model/storage'}); continue
                            seen.add(imei)
                            r_cond = (row.get('condition') or 'used').strip().lower()
                            if r_cond not in ('new', 'used'):
                                r_cond = 'used'
                            r_region = (row.get('region') or ('hk' if role == 'hk' else 'us')).strip().lower()
                            if r_region not in ('us', 'cn', 'hk'):
                                r_region = 'us'
                            try: r_cost = float(row.get('cost') or 0)
                            except (TypeError, ValueError): r_cost = 0
                            try: r_price = float(row.get('price') or 0)
                            except (TypeError, ValueError): r_price = 0
                            r_battery = str(row.get('battery') or '').strip()
                            r_color = (row.get('color') or '').strip()
                            r_store = store if role == 'hk' else normalize_store_name(row.get('store') or store)
                            conn.execute("""
                                INSERT INTO inventory
                                (imei,imei2,serial,brand,model,storage,color,color_en,
                                 condition,battery_health,region,store,
                                 cost,price,status,scanned_by,scanned_at,raw_ocr,notes)
                                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                            """, (imei, '', '', (row.get('brand') or 'Apple').strip(), r_model, r_storage,
                                  r_color, '', r_cond, r_battery, r_region, r_store,
                                  r_cost, r_price, 'available', who, now, '', intake_note))
                            created.append(imei)
                        conn.commit()
                        return json_response(self, {
                            'ok': True, 'store': store, 'mode': 'rows',
                            'created': created, 'skipped': skipped,
                            'createdCount': len(created), 'skippedCount': len(skipped),
                        })

                    brand = (data.get('brand') or 'Apple').strip()
                    model = (data.get('model') or '').strip()
                    storage = (data.get('storage') or '').strip()
                    color = (data.get('color') or '').strip()
                    condition = (data.get('condition') or 'used').strip().lower()
                    if condition not in ('new', 'used'):
                        condition = 'used'
                    region = (data.get('region') or ('hk' if role == 'hk' else 'us')).strip().lower()
                    if region not in ('us', 'cn', 'hk'):
                        region = 'hk'
                    try: cost = float(data.get('cost') or 0)
                    except (TypeError, ValueError): cost = 0
                    try: price = float(data.get('price') or 0)
                    except (TypeError, ValueError): price = 0
                    imeis = data.get('imeis', [])
                    who = (user['name'] if user else '') or (getattr(self, '_auth_email', '') or '')

                    if not model or not storage:
                        return json_response(self, {'error': '型号和容量必填 / Model & storage required'}, 400)
                    if not imeis:
                        return json_response(self, {'error': 'IMEI 为空 / No IMEIs provided'}, 400)

                    now = datetime.now(PST).strftime("%Y-%m-%d %H:%M:%S")
                    existing = {r[0] for r in conn.execute("SELECT imei FROM inventory")}
                    created, skipped, seen = [], [], set()
                    for raw in imeis:
                        imei = str(raw).strip().replace(' ', '')
                        if not imei:
                            continue
                        if len(imei) != 15 or not imei.isdigit():
                            skipped.append({'imei': imei, 'reason': '格式错误 / Invalid IMEI'}); continue
                        if imei in existing or imei in seen:
                            skipped.append({'imei': imei, 'reason': '已存在 / Duplicate'}); continue
                        seen.add(imei)
                        conn.execute("""
                            INSERT INTO inventory
                            (imei,imei2,serial,brand,model,storage,color,color_en,
                             condition,battery_health,region,store,
                             cost,price,status,scanned_by,scanned_at,raw_ocr,notes)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        """, (imei, '', '', brand, model, storage, color, '',
                              condition, '', region, store,
                              cost, price, 'available', who, now, '',
                              'HK intake' if role == 'hk' else 'admin intake'))
                        created.append(imei)
                    conn.commit()
                    return json_response(self, {
                        'ok': True, 'store': store,
                        'created': created, 'skipped': skipped,
                        'createdCount': len(created), 'skippedCount': len(skipped),
                    })
                finally:
                    conn.close()
        except Exception as e:
            return json_response(self, {'error': str(e)}, 500)

    # ─── Cross-border shipments (香港仓 → HQ) ───

    def create_deposit(self):
        """Record a cash deposit handed to HQ/bank. Cash-on-hand for a store is
        computed client-side as Σ(cash sales) − Σ(deposits). Staff can only
        deposit for their own store; admin may specify the store."""
        try:
            data = self.read_body()
            with db_lock:
                conn = get_db()
                try:
                    user = self._auth_user(conn)
                    role = user['role'] if user else None
                    if role not in ('staff', 'admin'):
                        return json_response(self, {'error': '无权限 / Not allowed'}, 403)
                    try:
                        amount = round(float(data.get('amount', 0)), 2)
                    except (TypeError, ValueError):
                        amount = 0
                    if amount <= 0:
                        return json_response(self, {'error': '金额无效 / Invalid amount'}, 400)
                    if role == 'staff':
                        store_name = user['store'] or ''
                    else:
                        store_name = normalize_store_name(data.get('store', '')) or (data.get('store', '') or '')
                    store_key = (data.get('storeKey') or '').strip()
                    did = (data.get('id') or '').strip() or ('DP-' + datetime.now(PST).strftime('%Y%m%d%H%M%S'))
                    now = datetime.now(PST).strftime("%Y-%m-%d %H:%M:%S")
                    conn.execute("""
                        INSERT INTO deposits (id, store, store_key, amount, method, note, deposited_by, created_at)
                        VALUES (?,?,?,?,?,?,?,?)
                    """, (did, store_name, store_key, amount, (data.get('method') or 'cash'),
                          (data.get('note') or '').strip(), (user['name'] if user else ''), now))
                    self._audit(conn, 'deposit.create', 'deposit', did, {'store': store_name, 'amount': amount})
                    conn.commit()
                    return json_response(self, {'ok': True, 'id': did, 'amount': amount, 'store': store_name, 'createdAt': now})
                finally:
                    conn.close()
        except Exception as e:
            return json_response(self, {'error': str(e)}, 500)

    def get_deposits(self, params):
        conn = None
        try:
            conn = get_db()
            user = self._auth_user(conn)
            role = user['role'] if user else None
            conds, args = [], []
            if role == 'staff':
                conds.append("store = ?"); args.append(user['store'])
            elif role == 'hk':
                conds.append("1 = 0")
            else:
                store = params.get('store', [None])[0]
                if store and store != 'all':
                    conds.append("store_key = ?"); args.append(store)
            q = "SELECT * FROM deposits"
            if conds:
                q += " WHERE " + " AND ".join(conds)
            q += " ORDER BY created_at DESC"
            rows = conn.execute(q, args).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                d['storeKey'] = d.pop('store_key', '')
                d['depositedBy'] = d.pop('deposited_by', '')
                d['createdAt'] = d.pop('created_at', '')
                out.append(d)
            return json_response(self, {'deposits': out})
        except Exception as e:
            return json_response(self, {'error': str(e)}, 500)
        finally:
            if conn:
                conn.close()

    def create_shipment(self):
        """HK (or admin) creates a shipment from 香港仓. IMEIs must be 香港仓
        stock and available. With a tracking_no it goes straight to 'shipped'
        (locking those units as transit); otherwise it stays 'draft'."""
        try:
            data = self.read_body()
            with db_lock:
                conn = get_db()
                try:
                    user = self._auth_user(conn)
                    role = user['role'] if user else None
                    if role not in ('hk', 'admin'):
                        return json_response(self, {'error': '无权限发货 / Not allowed to ship'}, 403)
                    sid = (data.get('id') or '').strip()
                    if not sid:
                        return json_response(self, {'error': 'shipment id required'}, 400)
                    from_store = '香港仓' if role == 'hk' else normalize_store_name(data.get('fromStore') or '香港仓')
                    to_store = normalize_store_name(data.get('toStore') or 'HQ 总仓')
                    carrier = (data.get('carrier') or '').strip()
                    tracking = (data.get('trackingNo') or '').strip()
                    notes = (data.get('notes') or '').strip()

                    valid, skipped, seen = [], [], set()
                    for r in data.get('imeis', []):
                        imei = str(r).strip().replace(' ', '')
                        if not imei:
                            continue
                        if imei in seen:
                            skipped.append({'imei': imei, 'reason': '重复 / Duplicate'}); continue
                        seen.add(imei)
                        inv = conn.execute("SELECT status, store FROM inventory WHERE imei=?", (imei,)).fetchone()
                        if not inv:
                            skipped.append({'imei': imei, 'reason': '不在库存 / Not found'}); continue
                        if normalize_store_name(inv['store']) != from_store:
                            skipped.append({'imei': imei, 'reason': '不在' + from_store + ' / Not in warehouse'}); continue
                        if inv['status'] != 'available':
                            skipped.append({'imei': imei, 'reason': '不可发货(' + inv['status'] + ') / Not available'}); continue
                        valid.append(imei)

                    if not valid:
                        return json_response(self, {'error': '没有可发货的 IMEI / No shippable IMEIs', 'skipped': skipped}, 400)

                    now = datetime.now(PST).strftime("%Y-%m-%d %H:%M:%S")
                    status = 'shipped' if tracking else 'draft'
                    shipped_at = now if tracking else ''
                    conn.execute("""
                        INSERT INTO shipments
                        (id,tracking_no,carrier,from_store,to_store,imeis,qty,received_imeis,
                         status,notes,created_by,shipped_at,created_at,updated_at)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """, (sid, tracking, carrier, from_store, to_store, json.dumps(valid), len(valid), '[]',
                          status, notes, (user['name'] if user else ''), shipped_at, now, now))
                    if status == 'shipped':
                        for imei in valid:
                            conn.execute("UPDATE inventory SET status='transit' WHERE imei=? AND status='available'", (imei,))
                    conn.commit()
                    return json_response(self, {'ok': True, 'id': sid, 'status': status,
                                                'qty': len(valid), 'shippedImeis': valid, 'skipped': skipped})
                finally:
                    conn.close()
        except Exception as e:
            return json_response(self, {'error': str(e)}, 500)

    def get_shipments(self, params):
        conn = None
        try:
            conn = get_db()
            rows = conn.execute("SELECT * FROM shipments ORDER BY created_at DESC").fetchall()
            out = []
            for r in rows:
                d = dict(r)
                try: d['imeis'] = json.loads(d.get('imeis') or '[]')
                except Exception: d['imeis'] = []
                try: d['receivedImeis'] = json.loads(d.pop('received_imeis', None) or '[]')
                except Exception: d['receivedImeis'] = []
                d['trackingNo'] = d.pop('tracking_no', '')
                d['fromStore'] = d.pop('from_store', '')
                d['toStore'] = d.pop('to_store', '')
                d['createdBy'] = d.pop('created_by', '')
                d['receivedBy'] = d.pop('received_by', '')
                d['shippedAt'] = d.pop('shipped_at', '')
                d['receivedAt'] = d.pop('received_at', '')
                d['createdAt'] = d.pop('created_at', '')
                d['updatedAt'] = d.pop('updated_at', '')
                out.append(d)
            return json_response(self, {'shipments': out})
        except Exception as e:
            return json_response(self, {'error': str(e)}, 500)
        finally:
            if conn is not None:
                conn.close()

    def update_shipment(self, sid):
        """Ship a draft (HK), or receive/清点 a shipment (US HQ). Receiving moves
        the box's IMEIs from 香港仓 to the destination store and makes them
        available. Cancelling unlocks any in-transit units."""
        try:
            data = self.read_body()
            new_status = data.get('status', '')
            with db_lock:
                conn = get_db()
                try:
                    user = self._auth_user(conn)
                    role = user['role'] if user else None
                    row = conn.execute("SELECT * FROM shipments WHERE id=?", (sid,)).fetchone()
                    if not row:
                        return json_response(self, {'error': 'Shipment not found'}, 404)
                    now = datetime.now(PST).strftime("%Y-%m-%d %H:%M:%S")
                    imeis = json.loads(row['imeis'] or '[]')

                    if new_status == 'shipped':
                        if role not in ('hk', 'admin'):
                            return json_response(self, {'error': '无权限 / Not allowed'}, 403)
                        tracking = (data.get('trackingNo') or row['tracking_no'] or '').strip()
                        carrier = (data.get('carrier') or row['carrier'] or '').strip()
                        conn.execute("UPDATE shipments SET status='shipped', tracking_no=?, carrier=?, shipped_at=?, updated_at=? WHERE id=?",
                                     (tracking, carrier, now, now, sid))
                        for imei in imeis:
                            conn.execute("UPDATE inventory SET status='transit' WHERE imei=? AND status='available'", (imei,))

                    elif new_status == 'received':
                        if role not in ('admin', 'staff'):
                            return json_response(self, {'error': '仅美国 HQ 可清点收货 / Only US side can receive'}, 403)
                        to_store = normalize_store_name(row['to_store'] or 'HQ 总仓')
                        verified = data.get('receivedImeis')
                        target = verified if (isinstance(verified, list) and verified) else imeis
                        moved = []
                        for imei in target:
                            conn.execute("UPDATE inventory SET store=?, status='available' WHERE imei=? AND status != 'sold'",
                                         (to_store, imei))
                            moved.append(imei)
                        conn.execute("UPDATE shipments SET status='received', received_by=?, received_imeis=?, received_at=?, updated_at=? WHERE id=?",
                                     ((user['name'] if user else ''), json.dumps(moved), now, now, sid))

                    elif new_status == 'cancelled':
                        if role not in ('hk', 'admin'):
                            return json_response(self, {'error': '无权限 / Not allowed'}, 403)
                        for imei in imeis:
                            conn.execute("UPDATE inventory SET status='available' WHERE imei=? AND status='transit'", (imei,))
                        conn.execute("UPDATE shipments SET status='cancelled', updated_at=? WHERE id=?", (now, sid))

                    else:
                        return json_response(self, {'error': 'Invalid status: ' + str(new_status)}, 400)

                    conn.commit()
                    return json_response(self, {'ok': True, 'id': sid, 'status': new_status})
                finally:
                    conn.close()
        except Exception as e:
            return json_response(self, {'error': str(e)}, 500)

    # ─── Defects / repair tickets (US reports, HK can see) ───

    def create_defect(self):
        """US side (admin/staff) flags a specific phone as defective. Creates a
        repair ticket and marks the unit 'defect' (not sellable). HK can then
        see exactly which IMEI has a problem via GET /api/defects."""
        try:
            data = self.read_body()
            imei = str(data.get('imei', '')).strip().replace(' ', '')
            with db_lock:
                conn = get_db()
                try:
                    user = self._auth_user(conn)
                    role = user['role'] if user else None
                    if role not in ('admin', 'staff'):
                        return json_response(self, {'error': '无权限报修 / Not allowed to report'}, 403)
                    if not imei:
                        return json_response(self, {'error': 'IMEI 必填 / IMEI required'}, 400)
                    inv = conn.execute("SELECT model, storage, store, status FROM inventory WHERE imei=?", (imei,)).fetchone()
                    if not inv:
                        return json_response(self, {'error': 'IMEI 不在库存 / Not in inventory'}, 404)
                    now = datetime.now(PST).strftime("%Y-%m-%d %H:%M:%S")
                    did = (data.get('id') or ('DEF-' + imei[-6:] + '-' + now.replace('-', '').replace(':', '').replace(' ', '')[-6:])).strip()
                    phone_name = data.get('phoneName') or ((inv['model'] or '') + (' ' + inv['storage'] if inv['storage'] else '')).strip()
                    issues = data.get('issues') or []
                    photos = data.get('photos') or []
                    conn.execute("""
                        INSERT INTO defects
                        (id,imei,phone_name,storage,store,issues,severity,description,photos,
                         status,reported_by,created_at,updated_at)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """, (did, imei, phone_name, inv['storage'] or '', inv['store'] or '',
                          json.dumps(issues, ensure_ascii=False), (data.get('severity') or '').strip(),
                          (data.get('description') or '').strip(), json.dumps(photos, ensure_ascii=False),
                          'open', (user['name'] if user else ''), now, now))
                    # Take the unit off the sales floor (skip if already sold).
                    conn.execute("UPDATE inventory SET status='defect' WHERE imei=? AND status != 'sold'", (imei,))
                    conn.commit()
                    # Notify the HK team on Telegram (photos + details), non-blocking.
                    threading.Thread(target=notify_hk_defect, args=({
                        'imei': imei, 'phone_name': phone_name, 'store': inv['store'] or '',
                        'issues': issues, 'severity': (data.get('severity') or '').strip(),
                        'description': (data.get('description') or '').strip(),
                        'reported_by': (user['name'] if user else ''), 'photos': photos,
                    },), daemon=True).start()
                    return json_response(self, {'ok': True, 'id': did, 'imei': imei})
                finally:
                    conn.close()
        except Exception as e:
            return json_response(self, {'error': str(e)}, 500)

    def get_defects(self, params):
        conn = None
        try:
            conn = get_db()
            user = self._auth_user(conn)
            role = user['role'] if user else None
            if role == 'staff':
                rows = conn.execute("SELECT * FROM defects WHERE store=? ORDER BY created_at DESC",
                                    (user['store'],)).fetchall()
            else:
                # admin + hk see all (HK must see every defective unit it supplied)
                rows = conn.execute("SELECT * FROM defects ORDER BY created_at DESC").fetchall()
            out = []
            for r in rows:
                d = dict(r)
                try: d['issues'] = json.loads(d.get('issues') or '[]')
                except Exception: d['issues'] = []
                try: d['photos'] = json.loads(d.get('photos') or '[]')
                except Exception: d['photos'] = []
                d['phoneName'] = d.pop('phone_name', '')
                d['reportedBy'] = d.pop('reported_by', '')
                d['resolvedBy'] = d.pop('resolved_by', '')
                d['createdAt'] = d.pop('created_at', '')
                d['updatedAt'] = d.pop('updated_at', '')
                out.append(d)
            return json_response(self, {'defects': out})
        except Exception as e:
            return json_response(self, {'error': str(e)}, 500)
        finally:
            if conn is not None:
                conn.close()

    def update_defect(self, did):
        """Resolve or reopen a repair ticket (US side). Resolving puts the unit
        back on sale; returning leaves it off-sale for return to HK."""
        try:
            data = self.read_body()
            new_status = data.get('status', '')
            if new_status not in ('resolved', 'returned', 'reopen'):
                return json_response(self, {'error': 'Invalid status: ' + str(new_status)}, 400)
            with db_lock:
                conn = get_db()
                try:
                    user = self._auth_user(conn)
                    role = user['role'] if user else None
                    if role not in ('admin', 'staff'):
                        return json_response(self, {'error': '无权限 / Not allowed'}, 403)
                    row = conn.execute("SELECT imei, status FROM defects WHERE id=?", (did,)).fetchone()
                    if not row:
                        return json_response(self, {'error': 'Defect not found'}, 404)
                    now = datetime.now(PST).strftime("%Y-%m-%d %H:%M:%S")
                    if new_status == 'resolved':
                        conn.execute("UPDATE defects SET status='resolved', resolved_by=?, updated_at=? WHERE id=?",
                                     ((user['name'] if user else ''), now, did))
                        conn.execute("UPDATE inventory SET status='available' WHERE imei=? AND status='defect'", (row['imei'],))
                    elif new_status == 'returned':
                        conn.execute("UPDATE defects SET status='returned', resolved_by=?, updated_at=? WHERE id=?",
                                     ((user['name'] if user else ''), now, did))
                        # stays off-sale (status 'defect') pending physical return to HK
                    elif new_status == 'reopen':
                        conn.execute("UPDATE defects SET status='open', updated_at=? WHERE id=?", (now, did))
                        conn.execute("UPDATE inventory SET status='defect' WHERE imei=? AND status='available'", (row['imei'],))
                    conn.commit()
                    return json_response(self, {'ok': True, 'id': did, 'status': new_status})
                finally:
                    conn.close()
        except Exception as e:
            return json_response(self, {'error': str(e)}, 500)

    # ─── Trade-in acquisition (buy used phone from a customer) ───

    def create_tradein(self):
        """Record a trade-in acquisition (staff/admin) and put the unit straight
        into inventory as used stock (cost = agreed buy price)."""
        try:
            data = self.read_body()
            imei = str(data.get('imei', '')).strip().replace(' ', '')
            with db_lock:
                conn = get_db()
                try:
                    user = self._auth_user(conn)
                    role = user['role'] if user else None
                    if role not in ('admin', 'staff'):
                        return json_response(self, {'error': '无权限回收 / Not allowed'}, 403)
                    model = (data.get('model') or '').strip()
                    storage = (data.get('storage') or '').strip()
                    if len(imei) != 15 or not imei.isdigit():
                        return json_response(self, {'error': 'IMEI 格式错误 / Invalid IMEI'}, 400)
                    if not model or not storage:
                        return json_response(self, {'error': '型号和容量必填 / Model & storage required'}, 400)
                    if conn.execute("SELECT 1 FROM inventory WHERE imei=?", (imei,)).fetchone():
                        return json_response(self, {'error': '此 IMEI 已在库存 / Already in inventory'}, 409)

                    # Staff acquire into their own store; admin may pass one.
                    store = normalize_store_name(data.get('store') or (user['store'] if user else '')) or (user['store'] if user else '')
                    if role == 'staff':
                        store = user['store']
                    region = (data.get('region') or 'us').strip().lower()
                    if region not in ('us', 'cn', 'hk'):
                        region = 'us'
                    color = (data.get('color') or '').strip()
                    battery = str(data.get('battery') or '').strip()
                    grade = (data.get('grade') or '').strip()
                    try: final_price = float(data.get('finalPrice') or 0)
                    except (TypeError, ValueError): final_price = 0
                    who = (user['name'] if user else '')
                    now = datetime.now(PST).strftime("%Y-%m-%d %H:%M:%S")
                    tid = (data.get('id') or ('TI-' + imei[-6:] + '-' + now.replace('-', '').replace(':', '').replace(' ', '')[-6:])).strip()

                    conn.execute("""
                        INSERT INTO tradeins
                        (id,imei,model,storage,color,region,grade,battery,ref_price,coefficient,
                         deductions,final_price,checklist,deduction_items,seller_name,seller_id,
                         seller_source,store,staff,status,created_at)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """, (tid, imei, model, storage, color, region, grade, battery,
                          float(data.get('refPrice') or 0), float(data.get('coefficient') or 0),
                          float(data.get('deductions') or 0), final_price,
                          json.dumps(data.get('checklist') or {}, ensure_ascii=False),
                          json.dumps(data.get('deductionItems') or [], ensure_ascii=False),
                          (data.get('sellerName') or '').strip(), (data.get('sellerId') or '').strip(),
                          (data.get('sellerSource') or '').strip(), store, who, 'acquired', now))
                    # Land the unit in inventory as used stock; cost = buy price.
                    conn.execute("""
                        INSERT INTO inventory
                        (imei,imei2,serial,brand,model,storage,color,color_en,condition,battery_health,
                         region,store,cost,price,status,scanned_by,scanned_at,raw_ocr,notes)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """, (imei, '', '', 'Apple', model, storage, color, '', 'used', battery,
                          region, store, final_price, 0.0, 'available', who, now, '',
                          'trade-in ' + (grade or '')))
                    self._audit(conn, 'tradein.create', 'tradein', tid,
                                {'imei': imei, 'model': model, 'grade': grade, 'buyPrice': final_price, 'store': store})
                    conn.commit()
                    return json_response(self, {'ok': True, 'id': tid, 'imei': imei, 'store': store})
                finally:
                    conn.close()
        except Exception as e:
            return json_response(self, {'error': str(e)}, 500)

    def get_tradeins(self, params):
        conn = None
        try:
            conn = get_db()
            user = self._auth_user(conn)
            role = user['role'] if user else None
            if role == 'staff':
                rows = conn.execute("SELECT * FROM tradeins WHERE store=? ORDER BY created_at DESC",
                                    (user['store'],)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM tradeins ORDER BY created_at DESC").fetchall()
            out = []
            for r in rows:
                d = dict(r)
                for k in ('checklist', 'deduction_items'):
                    try: d[k] = json.loads(d.get(k) or ('{}' if k == 'checklist' else '[]'))
                    except Exception: d[k] = {} if k == 'checklist' else []
                out.append(d)
            return json_response(self, {'tradeins': out})
        except Exception as e:
            return json_response(self, {'error': str(e)}, 500)
        finally:
            if conn is not None:
                conn.close()

    def create_transfer_batch(self):
        """Bulk transfer: create many pending transfers to one target store in a
        single transaction. Each IMEI is validated independently; un-transferable
        ones are skipped (with a reason) instead of failing the whole batch."""
        try:
            data = self.read_body()
            items = data.get('items', [])
            to_store = normalize_store_name(data.get('toStore', ''))
            requested_by = data.get('requestedBy', '')
            reason = data.get('reason', '')
            if not items:
                return json_response(self, {'error': 'items 为空 / No IMEIs provided'}, 400)
            if not to_store:
                return json_response(self, {'error': '目标门店必填 / Target store required'}, 400)

            created, skipped = [], []
            seen = set()
            with db_lock:
                conn = get_db()
                try:
                    now = datetime.now(PST).strftime("%Y-%m-%d %H:%M:%S")
                    for item in items:
                        imei = str(item.get('imei', '')).strip().replace(' ', '')
                        tid = item.get('id', '')
                        if not imei:
                            continue
                        if imei in seen:
                            skipped.append({'imei': imei, 'reason': '重复 / Duplicate in list'})
                            continue
                        seen.add(imei)
                        inv = conn.execute(
                            "SELECT status, model, storage, store FROM inventory WHERE imei=?",
                            (imei,)
                        ).fetchone()
                        if not inv:
                            skipped.append({'imei': imei, 'reason': '不在库存 / Not in inventory'})
                            continue
                        if inv['status'] != 'available':
                            msg = {
                                'sold': '已售出 / Sold',
                                'transit': '已在调拨中 / In transit',
                                'reserved': '已预留 / Reserved',
                            }.get(inv['status'], '当前不可调拨 / Not available')
                            skipped.append({'imei': imei, 'reason': msg})
                            continue
                        from_store = inv['store'] or ''
                        if from_store and normalize_store_name(from_store) == to_store:
                            skipped.append({'imei': imei, 'reason': '已在目标门店 / Already at target store'})
                            continue
                        phone_name = ((inv['model'] or '') + (' ' + inv['storage'] if inv['storage'] else '')).strip()
                        conn.execute("""
                            INSERT INTO transfers (id, imei, phone_name, from_store, to_store,
                                requested_by, status, notes, created_at, updated_at)
                            VALUES (?,?,?,?,?,?,?,?,?,?)
                        """, (tid, imei, phone_name, from_store, to_store,
                              requested_by, 'pending', reason, now, now))
                        conn.execute(
                            "UPDATE inventory SET status='transit' WHERE imei=? AND status='available'",
                            (imei,)
                        )
                        created.append({
                            'id': tid, 'imei': imei, 'phoneName': phone_name,
                            'fromStore': from_store, 'toStore': to_store,
                        })
                    conn.commit()
                finally:
                    conn.close()
            return json_response(self, {
                'ok': True,
                'created': created,
                'skipped': skipped,
                'createdCount': len(created),
                'skippedCount': len(skipped),
            })
        except Exception as e:
            return json_response(self, {'error': str(e)}, 500)

    def get_transfers(self, params):
        conn = None
        try:
            conn = get_db()
            rows = conn.execute("SELECT * FROM transfers ORDER BY created_at DESC").fetchall()
            transfers = []
            for r in rows:
                t = dict(r)
                t['fromStore'] = t.pop('from_store', '')
                t['toStore'] = t.pop('to_store', '')
                t['requestedBy'] = t.pop('requested_by', '')
                t['approvedBy'] = t.pop('approved_by', '')
                t['rejectedBy'] = t.pop('rejected_by', '')
                t['phoneName'] = t.pop('phone_name', '')
                t['createdAt'] = t.pop('created_at', '')
                t['updatedAt'] = t.pop('updated_at', '')
                transfers.append(t)
            return json_response(self, {'transfers': transfers})
        except Exception as e:
            return json_response(self, {'error': str(e)}, 500)
        finally:
            if conn is not None:
                conn.close()

    def update_transfer(self, transfer_id):
        # Valid state transitions
        VALID_TRANSITIONS = {
            'approved': ('pending',),
            'rejected': ('pending',),
            # One-step model: receiving store confirms a pending request directly.
            # 'approved' kept for backward compatibility (admin two-step flow).
            'completed': ('pending', 'approved'),
            'cancelled': ('pending',),
            'returned': ('approved', 'completed'),
        }
        try:
            data = self.read_body()
            new_status = data.get('status', '')
            if new_status not in VALID_TRANSITIONS:
                return json_response(self, {'error': f'Invalid status: {new_status}'}, 400)

            with db_lock:
                conn = get_db()
                try:
                    now = datetime.now(PST).strftime("%Y-%m-%d %H:%M:%S")
                    # Always fetch current state first
                    row = conn.execute(
                        "SELECT imei, from_store, to_store, status FROM transfers WHERE id=?",
                        (transfer_id,)
                    ).fetchone()
                    if not row:
                        return json_response(self, {'error': 'Transfer not found'}, 404)

                    current_status = row['status']
                    if current_status not in VALID_TRANSITIONS[new_status]:
                        return json_response(self, {
                            'error': f'Cannot change from {current_status} to {new_status}'
                        }, 400)

                    # Guard: never resurrect a phone that was sold while the transfer
                    # was pending. Moves that make a phone 'available' must not run on sold stock.
                    if new_status in ('approved', 'completed', 'returned'):
                        inv_row = conn.execute(
                            "SELECT status FROM inventory WHERE imei=?", (row['imei'],)
                        ).fetchone()
                        if inv_row and inv_row['status'] == 'sold':
                            return json_response(self, {
                                'error': '此手机已售出，无法完成调拨 / Phone already sold, cannot complete transfer'
                            }, 409)

                    if new_status == 'approved':
                        conn.execute(
                            "UPDATE transfers SET status='approved', approved_by=?, updated_at=? WHERE id=?",
                            (data.get('approvedBy', ''), now, transfer_id)
                        )
                        target_store = normalize_store_name(row['to_store'])
                        conn.execute("UPDATE inventory SET store=?, status='available' WHERE imei=?",
                                     (target_store, row['imei']))

                    elif new_status == 'rejected':
                        # Receiving store rejected: unlock phone, it stays in origin store.
                        conn.execute(
                            "UPDATE transfers SET status='rejected', rejected_by=?, updated_at=? WHERE id=?",
                            (data.get('rejectedBy', ''), now, transfer_id)
                        )
                        conn.execute("UPDATE inventory SET status='available' WHERE imei=? AND status IN ('transit','reserved')",
                                     (row['imei'],))

                    elif new_status == 'completed':
                        # Receiving store confirmed: move phone to destination store, make it sellable.
                        conn.execute(
                            "UPDATE transfers SET status='completed', updated_at=? WHERE id=?",
                            (now, transfer_id)
                        )
                        target_store = normalize_store_name(row['to_store'])
                        conn.execute("UPDATE inventory SET store=?, status='available' WHERE imei=?",
                                     (target_store, row['imei']))

                    elif new_status == 'cancelled':
                        # Origin store withdrew the request: unlock phone in origin store.
                        conn.execute(
                            "UPDATE transfers SET status='cancelled', updated_at=? WHERE id=?",
                            (now, transfer_id)
                        )
                        conn.execute("UPDATE inventory SET status='available' WHERE imei=? AND status IN ('transit','reserved')",
                                     (row['imei'],))

                    elif new_status == 'returned':
                        original_store = normalize_store_name(row['from_store'])
                        conn.execute(
                            "UPDATE transfers SET status='returned', updated_at=? WHERE id=?",
                            (now, transfer_id)
                        )
                        conn.execute("UPDATE inventory SET store=?, status='available' WHERE imei=?",
                                     (original_store, row['imei']))

                    conn.commit()
                finally:
                    conn.close()
            return json_response(self, {'ok': True})
        except Exception as e:
            return json_response(self, {'error': str(e)}, 500)


    # ─── Stock Requests ───

    def get_stock_requests(self):
        conn = None
        try:
            conn = get_db()
            rows = conn.execute("SELECT * FROM stock_requests ORDER BY created_at DESC").fetchall()
            result = []
            for r in rows:
                sr = dict(r)
                sr['requestedBy'] = sr.pop('requested_by', '')
                sr['approvedBy'] = sr.pop('approved_by', '')
                sr['fulfilledBy'] = sr.pop('fulfilled_by', '')
                sr['createdAt'] = sr.pop('created_at', '')
                sr['updatedAt'] = sr.pop('updated_at', '')
                try:
                    sr['items'] = json.loads(sr.get('items') or '[]')
                except:
                    sr['items'] = []
                result.append(sr)
            return json_response(self, {'ok': True, 'data': result})
        except Exception as e:
            return json_response(self, {'error': str(e)}, 500)
        finally:
            if conn is not None:
                conn.close()

    def handle_stock_request(self):
        try:
            data = self.read_body()
            action = data.get('action', '')
            now = datetime.now(PST).strftime("%Y-%m-%d %H:%M:%S")

            if action == 'create':
                sr_id = 'SR-' + datetime.now(PST).strftime('%Y%m%d%H%M%S')
                with db_lock:
                    conn = get_db()
                    try:
                        conn.execute("""
                            INSERT INTO stock_requests (id, requested_by, store, model, items, qty, note, status, created_at, updated_at)
                            VALUES (?,?,?,?,?,?,?,'pending',?,?)
                        """, (
                            sr_id, data.get('requestedBy', ''), data.get('store', ''),
                            data.get('model', ''), json.dumps(data.get('items', [])),
                            data.get('qty', 1), data.get('note', ''), now, now
                        ))
                        conn.commit()
                    finally:
                        conn.close()
                return json_response(self, {'ok': True, 'data': {'id': sr_id}})

            elif action in ('approve', 'reject', 'fulfill'):
                sr_id = data.get('id', '')
                by = data.get('by', '')
                status_map = {'approve': 'approved', 'reject': 'rejected', 'fulfill': 'fulfilled'}
                new_status = status_map[action]
                with db_lock:
                    conn = get_db()
                    try:
                        if action == 'approve':
                            conn.execute(
                                "UPDATE stock_requests SET status=?, approved_by=?, updated_at=? WHERE id=?",
                                (new_status, by, now, sr_id)
                            )
                        elif action == 'reject':
                            conn.execute(
                                "UPDATE stock_requests SET status=?, approved_by=?, updated_at=? WHERE id=?",
                                (new_status, by, now, sr_id)
                            )
                        elif action == 'fulfill':
                            conn.execute(
                                "UPDATE stock_requests SET status=?, fulfilled_by=?, updated_at=? WHERE id=?",
                                (new_status, by, now, sr_id)
                            )
                        conn.commit()
                    finally:
                        conn.close()
                return json_response(self, {'ok': True})

            else:
                return json_response(self, {'error': 'Unknown action'}, 400)
        except Exception as e:
            return json_response(self, {'error': str(e)}, 500)

    # ─── Inventory Update ───

    def update_inventory(self, old_imei):
        """Update inventory record fields (IMEI, store, etc.)"""
        try:
            data = self.read_body()
            new_imei = data.get('imei', '').strip()

            if new_imei:
                # Validate new IMEI
                digits = ''.join(c for c in new_imei if c.isdigit())
                if len(digits) != 15:
                    return json_response(self, {'error': 'IMEI must be 15 digits'}, 400)
                new_imei = digits

            with db_lock:
                conn = get_db()
                try:
                    user = self._auth_user(conn)
                    if not (user and user['role'] == 'admin'):
                        return json_response(self, {'error': '仅管理员可编辑库存 / Admin only'}, 403)
                    # Check old record exists
                    row = conn.execute("SELECT id FROM inventory WHERE imei = ?", (old_imei,)).fetchone()
                    if not row:
                        return json_response(self, {'error': 'Record not found'}, 404)

                    if new_imei and new_imei != old_imei:
                        # Check new IMEI not duplicate
                        dup = conn.execute("SELECT id FROM inventory WHERE imei = ? AND imei != ?",
                                           (new_imei, old_imei)).fetchone()
                        if dup:
                            return json_response(self, {'error': 'New IMEI already exists'}, 409)
                        conn.execute("UPDATE inventory SET imei = ? WHERE imei = ?", (new_imei, old_imei))

                    target_imei = new_imei or old_imei

                    # Update other fields if provided
                    if data.get('store'):
                        conn.execute("UPDATE inventory SET store = ? WHERE imei = ?",
                                     (normalize_store_name(data['store']), target_imei))
                    if data.get('model'):
                        conn.execute("UPDATE inventory SET model = ? WHERE imei = ?",
                                     (data['model'], target_imei))
                    if data.get('color'):
                        conn.execute("UPDATE inventory SET color = ? WHERE imei = ?",
                                     (data['color'], target_imei))
                    if data.get('color_en'):
                        conn.execute("UPDATE inventory SET color_en = ? WHERE imei = ?",
                                     (data['color_en'], target_imei))
                    if data.get('condition'):
                        conn.execute("UPDATE inventory SET condition = ? WHERE imei = ?",
                                     (data['condition'], target_imei))
                    if data.get('region'):
                        conn.execute("UPDATE inventory SET region = ? WHERE imei = ?",
                                     (data['region'], target_imei))
                    if data.get('storage'):
                        conn.execute("UPDATE inventory SET storage = ? WHERE imei = ?",
                                     (data['storage'], target_imei))
                    # 'sold' must go through create_sale (ledger); block it here.
                    if data.get('status') and data['status'] in ('available', 'reserved'):
                        conn.execute("UPDATE inventory SET status = ? WHERE imei = ?",
                                     (data['status'], target_imei))
                    if data.get('cost') is not None:
                        try:
                            conn.execute("UPDATE inventory SET cost = ? WHERE imei = ?",
                                         (float(data['cost']), target_imei))
                        except (TypeError, ValueError):
                            pass
                    if data.get('price') is not None:
                        try:
                            conn.execute("UPDATE inventory SET price = ? WHERE imei = ?",
                                         (float(data['price']), target_imei))
                        except (TypeError, ValueError):
                            pass

                    self._audit(conn, 'inventory.update', 'inventory', target_imei,
                                {k: data.get(k) for k in ('store', 'model', 'condition', 'region', 'cost', 'price', 'status') if data.get(k) is not None})
                    conn.commit()
                finally:
                    conn.close()

            return json_response(self, {'ok': True, 'imei': target_imei})
        except Exception as e:
            print(f"[API] Error updating inventory: {e}")
            return json_response(self, {'error': str(e)}, 500)

    # ─── Health Check ───

    def health_check(self):
        """Functional health check — verifies DB is accessible and data is consistent."""
        checks = {}
        try:
            conn = get_db()
            conn.execute("SELECT 1")
            checks['db'] = 'ok'
            # Quick data quality checks
            row = conn.execute("SELECT COUNT(*) as c FROM inventory WHERE imei != '' AND length(imei) != 15").fetchone()
            checks['imei_valid'] = 'ok' if row['c'] == 0 else f'{row["c"]} invalid'
            row = conn.execute("SELECT COUNT(*) as c FROM inventory WHERE store IS NULL OR store = ''").fetchone()
            checks['store_valid'] = 'ok' if row['c'] == 0 else f'{row["c"]} missing'
            row = conn.execute("SELECT COUNT(*) as c FROM inventory WHERE region NOT IN ('us','hk','cn')").fetchone()
            checks['region_valid'] = 'ok' if row['c'] == 0 else f'{row["c"]} invalid'
            # Store name consistency
            row = conn.execute("""SELECT COUNT(*) as c FROM inventory
                WHERE store NOT IN ('Alhambra','Monterey Park','San Gabriel','Rowland Heights',
                    'Arcadia 1','Arcadia 2','Irvine','Rancho Cucamonga','Las Vegas','HQ 总仓','香港仓','')
                AND store IS NOT NULL AND store != ''""").fetchone()
            checks['store_names'] = 'ok' if row['c'] == 0 else f'{row["c"]} non-standard'
            conn.close()
            all_ok = all(v == 'ok' for v in checks.values())
            return json_response(self, {'status': 'ok' if all_ok else 'degraded', 'checks': checks})
        except Exception as e:
            checks['db'] = str(e)
            return json_response(self, {'status': 'error', 'checks': checks}, 500)

    # ─── User PINs ───

    def get_user_pins(self):
        """Retired: PINs are never broadcast anymore (login moved server-side)."""
        return json_response(self, {'error': 'gone'}, 410)

    def change_pin(self):
        """Change own PIN — requires valid token + correct current PIN."""
        try:
            data = self.read_body()
            email = str(data.get('email', '')).strip().lower()
            current_pin = str(data.get('currentPin', '')).strip()
            pin = str(data.get('pin', '')).strip()
            if not email or not pin:
                return json_response(self, {'error': 'Email and PIN required'}, 400)
            if len(pin) != 6 or not pin.isdigit():
                return json_response(self, {'error': 'PIN must be 6 digits'}, 400)
            if pin == DEFAULT_PIN:
                return json_response(self, {'error': 'Cannot use default PIN'}, 400)
            if email != getattr(self, '_auth_email', None):
                return json_response(self, {'error': 'Can only change your own PIN'}, 403)

            conn = get_db()
            try:
                user = get_user(conn, email)
                if not user:
                    return json_response(self, {'error': 'Account not found'}, 404)
                if not check_user_pin(conn, email, current_pin):
                    return json_response(self, {'error': '当前密码错误 / Incorrect current PIN'}, 401)
            finally:
                conn.close()

            now = datetime.now(PST).strftime("%Y-%m-%d %H:%M:%S")
            with db_lock:
                conn = get_db()
                try:
                    conn.execute(
                        "INSERT OR REPLACE INTO user_pins (email, pin, changed_at) VALUES (?, ?, ?)",
                        (email, hash_pin(pin), now)
                    )
                    conn.commit()
                finally:
                    conn.close()

            threading.Thread(target=_notify_pin_changed, args=(email, user['name']), daemon=True).start()
            return json_response(self, {'ok': True})
        except Exception as e:
            print(f"[API] Error changing PIN: {e}")
            return json_response(self, {'error': str(e)}, 500)


def main():
    init_api_tables()
    server = ThreadingHTTPServer((API_HOST, API_PORT), APIHandler)
    server.daemon_threads = True
    print(f"API Server running on port {API_PORT} (threaded)")
    print(f"Database: {DB_PATH}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nAPI Server stopped.")
        server.server_close()


if __name__ == "__main__":
    main()
