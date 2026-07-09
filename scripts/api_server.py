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
PST = timezone(timedelta(hours=-7))

# ─── Auth config ───
AUTH_SECRET = env_loader.require_env("AUTH_SECRET").encode()
TOKEN_TTL = 30 * 86400          # login token lifetime: 30 days
DEFAULT_PIN = "888888"
IMEI_API_KEY = os.environ.get("IMEI_API_KEY", "")
ALLOWED_ORIGIN = os.environ.get("ALLOWED_ORIGIN", "https://phonesinventory.com")

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
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
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
    """)
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
    conn.commit()
    conn.close()


def cors_origin(handler):
    """Production locks CORS to the site origin; localhost allowed for dev."""
    origin = handler.headers.get('Origin', '')
    if 'localhost' in origin or '127.0.0.1' in origin:
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

    def read_body(self):
        length = int(self.headers.get('Content-Length', 0))
        if length == 0:
            return {}
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

        if path.startswith('/api/transfer/'):
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

    def get_phones(self):
        try:
            conn = get_db()
            rows = conn.execute("SELECT * FROM inventory ORDER BY id DESC").fetchall()
            conn.close()
            return json_response(self, {'phones': export_inventory.build_phones(rows)})
        except Exception as e:
            return json_response(self, {'error': str(e)}, 500)

    # ─── Inventory ───

    def get_inventory(self, params):
        """List inventory with optional filters."""
        try:
            conn = get_db()
            store = params.get('store', [None])[0]
            status = params.get('status', [None])[0]

            query = "SELECT * FROM inventory"
            conditions = []
            args = []

            if store and store != 'all':
                conditions.append("store = ?")
                args.append(normalize_store_name(store))
            if status:
                conditions.append("status = ?")
                args.append(status)

            if conditions:
                query += " WHERE " + " AND ".join(conditions)
            query += " ORDER BY id DESC"

            rows = conn.execute(query, args).fetchall()
            conn.close()

            items = []
            for r in rows:
                item = dict(r)
                item['batteryHealth'] = item.pop('battery_health', '')
                item['colorEn'] = item.pop('color_en', '')
                item['scannedBy'] = item.pop('scanned_by', '')
                item['scannedAt'] = item.pop('scanned_at', '')
                item['createdAt'] = item.pop('created_at', '')
                item.pop('raw_ocr', None)  # Don't send raw OCR data
                items.append(item)

            return json_response(self, {'inventory': items, 'total': len(items)})
        except Exception as e:
            return json_response(self, {'error': str(e)}, 500)

    def delete_inventory(self, imei):
        """Delete an inventory record by IMEI."""
        try:
            with db_lock:
                conn = get_db()
                try:
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

                    # Verify phone exists in inventory
                    inv = conn.execute("SELECT id, status FROM inventory WHERE imei = ?", (imei,)).fetchone()
                    if not inv:
                        return json_response(self, {'error': 'IMEI not found in inventory'}, 404)
                    # Block selling a phone that is currently being transferred between stores
                    if inv['status'] == 'transit':
                        return json_response(self, {
                            'error': '此手机正在调拨中，无法销售 / Phone is in transit, cannot sell'
                        }, 409)

                    now = datetime.now(PST).strftime("%Y-%m-%d %H:%M:%S")
                    store_name = normalize_store_name(data.get('store', ''))
                    conn.execute("""
                        INSERT INTO sales (id, imei, phone_name, storage, color, color_en, cond, region,
                            cost, msrp, price, tax, total, profit, tax_applied, tax_rate,
                            customer, customer_phone, customer_email, payment_methods,
                            store, store_key, seller, status, created_at)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """, (
                        data.get('id', ''), imei, data.get('phoneName', ''),
                        data.get('storage', ''), data.get('color', ''), data.get('colorEn', ''),
                        data.get('cond', ''), data.get('region', ''),
                        data.get('cost', 0), data.get('msrp', 0), data.get('price', 0),
                        data.get('tax', 0), data.get('total', 0), data.get('profit', 0),
                        1 if data.get('taxApplied') else 0, data.get('taxRate', 0),
                        data.get('customer', ''), data.get('customerPhone', ''),
                        data.get('customerEmail', ''), json.dumps(data.get('paymentMethods', [])),
                        store_name, data.get('storeKey', ''),
                        data.get('seller', ''), 'completed', now
                    ))
                    # Mark phone as sold in inventory
                    conn.execute("UPDATE inventory SET status = 'sold' WHERE imei = ?", (imei,))
                    conn.commit()
                finally:
                    conn.close()

            return json_response(self, {'ok': True, 'id': data.get('id', '')})
        except Exception as e:
            print(f"[API] Error creating sale: {e}")
            return json_response(self, {'error': str(e)}, 500)

    def get_sales(self, params):
        try:
            conn = get_db()
            store = params.get('store', [None])[0]
            if store and store != 'all':
                rows = conn.execute(
                    "SELECT * FROM sales WHERE store_key = ? ORDER BY created_at DESC", (store,)
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM sales ORDER BY created_at DESC").fetchall()
            conn.close()
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
                except:
                    sale['paymentMethods'] = []
                sales.append(sale)
            return json_response(self, {'sales': sales})
        except Exception as e:
            return json_response(self, {'error': str(e)}, 500)

    def update_sale(self, sale_id):
        try:
            data = self.read_body()
            with db_lock:
                conn = get_db()
                try:
                    status = data.get('status', '')
                    if status == 'returned':
                        # Query IMEI FIRST, before updating status
                        row = conn.execute("SELECT imei, status FROM sales WHERE id = ?", (sale_id,)).fetchone()
                        if not row:
                            return json_response(self, {'error': 'Sale not found'}, 404)
                        if row['status'] == 'returned':
                            return json_response(self, {'error': 'Already returned'}, 400)
                        conn.execute("UPDATE sales SET status = 'returned' WHERE id = ?", (sale_id,))
                        conn.execute("UPDATE inventory SET status = 'available' WHERE imei = ?", (row['imei'],))
                    conn.commit()
                finally:
                    conn.close()
            return json_response(self, {'ok': True})
        except Exception as e:
            return json_response(self, {'error': str(e)}, 500)

    def get_sold_imeis(self):
        try:
            conn = get_db()
            rows = conn.execute("SELECT imei FROM sales WHERE status = 'completed'").fetchall()
            conn.close()
            return json_response(self, {'imeis': [r['imei'] for r in rows]})
        except Exception as e:
            return json_response(self, {'error': str(e)}, 500)

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

    def get_transfers(self, params):
        try:
            conn = get_db()
            rows = conn.execute("SELECT * FROM transfers ORDER BY created_at DESC").fetchall()
            conn.close()
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
        try:
            conn = get_db()
            rows = conn.execute("SELECT * FROM stock_requests ORDER BY created_at DESC").fetchall()
            conn.close()
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
                    if data.get('status') and data['status'] in ('available', 'reserved', 'sold'):
                        conn.execute("UPDATE inventory SET status = ? WHERE imei = ?",
                                     (data['status'], target_imei))

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
            row = conn.execute("SELECT COUNT(*) as c FROM inventory WHERE region NOT IN ('us','hk','cn','jp','kr')").fetchone()
            checks['region_valid'] = 'ok' if row['c'] == 0 else f'{row["c"]} invalid'
            # Store name consistency
            row = conn.execute("""SELECT COUNT(*) as c FROM inventory
                WHERE store NOT IN ('Alhambra','Monterey Park','San Gabriel','Rowland Heights',
                    'Arcadia 1','Arcadia 2','Irvine','Rancho Cucamonga','Las Vegas','HQ 总仓','')
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
    server = ThreadingHTTPServer(('0.0.0.0', API_PORT), APIHandler)
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
