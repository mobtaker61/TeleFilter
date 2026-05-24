"""
TeleFilter — Multi-user SaaS Panel
python panel.py  →  http://127.0.0.1:5000
"""
import json
import random
import threading
import asyncio
import os
import atexit
import hashlib
import hmac
import logging
import time
import sqlite3
import secrets
import webbrowser
from collections import deque
from functools import wraps
from flask import Flask, render_template, render_template_string, jsonify, request, session, redirect, url_for
from telethon import TelegramClient
from telethon.errors import (
    SessionPasswordNeededError, PhoneCodeInvalidError, PasswordHashInvalidError, FloodWaitError,
)
from telethon.tl.types import MessageService, Channel, Chat, MessageActionTopicCreate
from telethon.utils import get_peer_id

from config_util import (
    normalize_config, empty_config, find_group, new_group_id,
    config_stats, dashboard_stats, group_config_stats,
    parse_value, get_rates, get_rates_smart, latest_rate, delete_rate, delete_rates_range,
    clean_text as _clean_text,
    aggregate_rate_daily, list_days_in_range,
    latest_two_rates, compute_change,
)
import forwarder as fwd

try:
    from telethon.tl.functions.messages import GetForumTopicsRequest, CreateForumTopicRequest
    from telethon.tl.functions.channels import (
        CreateChannelRequest, GetFullChannelRequest, InviteToChannelRequest,
    )
    HAS_INVITE_API = True
    from telethon.tl.types import InputChannel
    HAS_FORUM_API = True
    HAS_CREATE_CHANNEL = True
    HAS_FULL_CHANNEL = True
    HAS_INVITE_API = True
except ImportError:
    try:
        from telethon.tl.functions.channels import (
            GetForumTopicsRequest, CreateForumTopicRequest, CreateChannelRequest,
            GetFullChannelRequest, InviteToChannelRequest,
        )
        from telethon.tl.types import InputChannel
        HAS_FORUM_API = True
        HAS_CREATE_CHANNEL = True
        HAS_FULL_CHANNEL = True
        HAS_INVITE_API = True
    except ImportError:
        HAS_FORUM_API = False
        HAS_CREATE_CHANNEL = False
        HAS_FULL_CHANNEL = False
        HAS_INVITE_API = False

# ══════════════════════════════════════════════════════════
#  Paths
# ══════════════════════════════════════════════════════════
BASE_DIR        = os.path.dirname(os.path.abspath(__file__))
MASTER_CFG_PATH = os.path.join(BASE_DIR, 'master_config.json')
USERS_DIR       = os.path.join(BASE_DIR, 'data', 'users')
DB_PATH         = os.path.join(BASE_DIR, 'data', 'users.db')

os.makedirs(USERS_DIR, exist_ok=True)

# ══════════════════════════════════════════════════════════
#  Master config  (api_id, api_hash, bot_token, ...)
# ══════════════════════════════════════════════════════════
def load_master() -> dict:
    try:
        with open(MASTER_CFG_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}

def save_master(data: dict):
    with open(MASTER_CFG_PATH, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def master_ready() -> bool:
    m = load_master()
    return bool(m.get('bot_token') and m.get('bot_username'))

def master_api_ready() -> bool:
    m = load_master()
    return bool(m.get('api_id') and m.get('api_hash'))

# ══════════════════════════════════════════════════════════
#  Flask app  (secret_key از master config می‌آید)
# ══════════════════════════════════════════════════════════
app = Flask(__name__, static_folder='static', static_url_path='/static')

def _get_secret_key() -> str:
    m = load_master()
    if not m.get('secret_key'):
        m['secret_key'] = secrets.token_hex(32)
        save_master(m)
    return m['secret_key']

app.secret_key = _get_secret_key()

# ══════════════════════════════════════════════════════════
#  Database
# ══════════════════════════════════════════════════════════
def _db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def _init_db():
    with _db() as c:
        c.execute('''CREATE TABLE IF NOT EXISTS users (
            tg_id       INTEGER PRIMARY KEY,
            username    TEXT DEFAULT '',
            first_name  TEXT DEFAULT '',
            last_name   TEXT DEFAULT '',
            photo_url   TEXT DEFAULT '',
            phone       TEXT DEFAULT '',
            is_approved INTEGER DEFAULT 0,
            is_admin    INTEGER DEFAULT 0,
            public_token TEXT DEFAULT '',
            created_at  TEXT DEFAULT CURRENT_TIMESTAMP
        )''')
        cols = {r[1] for r in c.execute('PRAGMA table_info(users)').fetchall()}
        if 'phone' not in cols:
            c.execute('ALTER TABLE users ADD COLUMN phone TEXT DEFAULT ""')
        if 'public_token' not in cols:
            c.execute('ALTER TABLE users ADD COLUMN public_token TEXT DEFAULT ""')
        c.execute('CREATE INDEX IF NOT EXISTS idx_users_public_token ON users(public_token)')
        c.commit()

_init_db()

def db_save_phone(tg_id: int, phone: str):
    with _db() as c:
        c.execute('UPDATE users SET phone=? WHERE tg_id=?', (phone.strip(), tg_id))
        c.commit()


def db_get_or_create_public_token(tg_id: int) -> str:
    """توکن پابلیک کاربر — اگر نباشد، یکی تولید و ذخیره می‌کند."""
    with _db() as c:
        row = c.execute('SELECT public_token FROM users WHERE tg_id=?', (tg_id,)).fetchone()
        if row and row['public_token']:
            return row['public_token']
        import secrets
        tok = secrets.token_urlsafe(16)
        c.execute('UPDATE users SET public_token=? WHERE tg_id=?', (tok, tg_id))
        c.commit()
        return tok


def db_rotate_public_token(tg_id: int) -> str:
    """ساخت توکن جدید — لینک قبلی نامعتبر می‌شود."""
    import secrets
    tok = secrets.token_urlsafe(16)
    with _db() as c:
        c.execute('UPDATE users SET public_token=? WHERE tg_id=?', (tok, tg_id))
        c.commit()
    return tok


def db_user_by_public_token(token: str) -> dict | None:
    if not token:
        return None
    with _db() as c:
        row = c.execute('SELECT * FROM users WHERE public_token=?', (token,)).fetchone()
        return dict(row) if row else None

def _mask_phone(phone: str) -> str:
    p = phone.strip()
    if len(p) < 6:
        return '***'
    return p[:3] + '***' + p[-4:]

def db_get_user(tg_id: int) -> dict | None:
    with _db() as c:
        row = c.execute('SELECT * FROM users WHERE tg_id=?', (tg_id,)).fetchone()
        return dict(row) if row else None

def db_upsert_user(d: dict):
    with _db() as c:
        c.execute('''INSERT INTO users (tg_id,username,first_name,last_name,photo_url)
            VALUES (:tg_id,:username,:first_name,:last_name,:photo_url)
            ON CONFLICT(tg_id) DO UPDATE SET
                username=excluded.username, first_name=excluded.first_name,
                last_name=excluded.last_name, photo_url=excluded.photo_url''', d)
        c.commit()

def db_count_users() -> int:
    with _db() as c:
        return c.execute('SELECT COUNT(*) FROM users').fetchone()[0]

def db_all_users() -> list:
    with _db() as c:
        return [dict(r) for r in c.execute('SELECT * FROM users ORDER BY created_at').fetchall()]

def db_set_approval(tg_id: int, approved: bool, admin: bool = False):
    with _db() as c:
        c.execute('UPDATE users SET is_approved=?, is_admin=? WHERE tg_id=?',
                  (int(approved), int(admin), tg_id))
        c.commit()

def db_delete_user(tg_id: int):
    with _db() as c:
        c.execute('DELETE FROM users WHERE tg_id=?', (tg_id,))
        c.commit()

# ══════════════════════════════════════════════════════════
#  Per-user paths & config
# ══════════════════════════════════════════════════════════
def user_dir(tg_id: int) -> str:
    d = os.path.join(USERS_DIR, str(tg_id))
    os.makedirs(d, exist_ok=True)
    return d

def user_config_path(tg_id: int) -> str:
    return os.path.join(user_dir(tg_id), 'config.json')

PANEL_SESSION = 'panel'
LEGACY_SESSIONS = ('telefilter', 'fwd')

def user_session_path(tg_id: int, kind: str = PANEL_SESSION) -> str:
    """مسیر پایه session (بدون پسوند) — Telethon خودش .session اضافه می‌کند."""
    return os.path.join(user_dir(tg_id), kind)

def _session_file(uid: int, kind: str = PANEL_SESSION) -> str:
    return user_session_path(uid, kind) + '.session'

def _migrate_legacy_session(uid: int):
    """اگر فقط session قدیمی موجود است، آن را به panel.session منتقل می‌کند."""
    import shutil
    panel = _session_file(uid, PANEL_SESSION)
    if os.path.exists(panel):
        return
    for kind in LEGACY_SESSIONS:
        legacy = _session_file(uid, kind)
        if os.path.exists(legacy):
            try:
                shutil.copy2(legacy, panel)
            except Exception:
                pass
            return

def session_on_disk(uid: int) -> bool:
    _migrate_legacy_session(uid)
    return os.path.exists(_session_file(uid, PANEL_SESSION))

def _apply_master_api(cfg: dict) -> dict:
    m = load_master()
    cfg['api_id']   = str(m.get('api_id',   cfg.get('api_id', '')))
    cfg['api_hash'] = str(m.get('api_hash', cfg.get('api_hash', '')))
    return cfg

def load_user_config(tg_id: int) -> dict:
    try:
        with open(user_config_path(tg_id), 'r', encoding='utf-8') as f:
            cfg = normalize_config(json.load(f))
    except Exception:
        cfg = empty_config()
    return _apply_master_api(cfg)

def save_user_config(tg_id: int, data: dict):
    cfg = _apply_master_api(normalize_config(data))
    with open(user_config_path(tg_id), 'w', encoding='utf-8') as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)

def initialize_user_config(tg_id: int) -> dict:
    user_dir(tg_id)
    cfg = load_user_config(tg_id)
    save_user_config(tg_id, cfg)
    return cfg

def user_api_credentials(uid: int) -> tuple[str, str]:
    m = load_master()
    return str(m.get('api_id', '')), str(m.get('api_hash', ''))

# ══════════════════════════════════════════════════════════
#  Telegram Login Widget verification
# ══════════════════════════════════════════════════════════
def verify_tg_auth(data: dict) -> bool:
    bot_token = load_master().get('bot_token', '')
    if not bot_token:
        return False
    received = data.get('hash', '')
    check    = {k: v for k, v in data.items() if k != 'hash'}
    string   = '\n'.join(f'{k}={v}' for k, v in sorted(check.items()))
    key      = hashlib.sha256(bot_token.encode()).digest()
    expected = hmac.new(key, string.encode(), hashlib.sha256).hexdigest()
    if expected != received:
        return False
    if time.time() - int(data.get('auth_date', 0)) > 86400:
        return False
    return True

# ══════════════════════════════════════════════════════════
#  Auth helpers
# ══════════════════════════════════════════════════════════
def cur_uid() -> int | None:
    return session.get('tg_id')

def login_required(f):
    @wraps(f)
    def dec(*a, **kw):
        if not cur_uid():
            return (jsonify({'error': 'not_logged_in'}), 401) if request.is_json \
                   else redirect(url_for('login_page'))
        return f(*a, **kw)
    return dec

def admin_required(f):
    @wraps(f)
    def dec(*a, **kw):
        uid = cur_uid()
        if not uid:
            return redirect(url_for('login_page'))
        u = db_get_user(uid)
        if not u or not u['is_admin']:
            return jsonify({'error': 'admin only'}), 403
        return f(*a, **kw)
    return dec

# ══════════════════════════════════════════════════════════
#  Forwarder (in-process via forwarder.py)
# ══════════════════════════════════════════════════════════
_fwd_logs: dict[int, deque] = {}
_fwd_active: dict[int, bool] = {}


def _logs(uid: int) -> deque:
    if uid not in _fwd_logs:
        _fwd_logs[uid] = deque(maxlen=300)
    return _fwd_logs[uid]


def _log_msg(uid: int, msg: str):
    _logs(uid).append(f"{time.strftime('%H:%M:%S')} {msg}")


class _PanelLogHandler(logging.Handler):
    def emit(self, record):
        try:
            msg = self.format(record)
        except Exception:
            return
        # محدود می‌کنیم به لاگ‌های مربوط به یک کاربر [uid]
        import re
        m = re.match(r'^\[(\d+)\]', record.getMessage())
        if not m:
            return
        try:
            uid = int(m.group(1))
        except ValueError:
            return
        _logs(uid).append(f"{time.strftime('%H:%M:%S')} {record.getMessage()}")


_panel_log = logging.getLogger('telefilter.forwarder')
_panel_log.setLevel(logging.INFO)
_panel_log.addHandler(_PanelLogHandler())
# لاگ‌های ماژول chart (خطا/هشدار) هم به لاگ خود کاربر منتقل می‌شود
_charts_log = logging.getLogger('telefilter.charts')
_charts_log.setLevel(logging.INFO)
_charts_log.addHandler(_PanelLogHandler())


def fwd_status(uid: int) -> str:
    if not _fwd_active.get(uid):
        return 'stopped'
    return fwd.status(uid)


def needs_telethon_login(uid: int) -> bool:
    return not session_on_disk(uid)


def rebuild_forwarder(uid: int) -> bool:
    """routes را از روی config می‌سازد و handler را نصب می‌کند."""
    if not session_on_disk(uid):
        _log_msg(uid, '— forwarder skipped: no session —')
        return False
    api_id, api_hash = user_api_credentials(uid)
    if not api_id or not api_hash:
        _log_msg(uid, '— forwarder skipped: master API not set —')
        return False
    c = ensure_client(uid)
    if not c:
        _log_msg(uid, '— forwarder skipped: client not connected —')
        return False
    cfg = load_user_config(uid)
    try:
        tg_run(fwd.rebuild_and_install(uid, c, cfg), timeout=60)
        _fwd_active[uid] = True
        st = fwd.stats(uid)
        _log_msg(uid, f"routes built: {st['routes']} routes, {st['targets']} targets")
        return True
    except Exception as e:
        _log_msg(uid, f"rebuild failed: {e}")
        return False


def stop_forwarder(uid: int):
    c = _tg_clients.get(uid)
    if c:
        try:
            fwd.uninstall_handler(uid, c)
        except Exception:
            pass
    fwd.clear_routes(uid)
    _fwd_active[uid] = False
    _log_msg(uid, '— forwarder stopped —')


def auto_start_fwd(uid: int):
    """به‌صورت idempotent: routes را بازسازی و handler را نصب می‌کند."""
    user = db_get_user(uid)
    if not user or not user['is_approved']:
        return
    # اگر forwarder در حال اجراست، فقط مطمئن شو client وصل است
    if _fwd_active.get(uid):
        c = _tg_clients.get(uid)
        if c is not None:
            try:
                if c.is_connected():
                    return
            except Exception:
                pass
    rebuild_forwarder(uid)


def _auto_start_all():
    for u in db_all_users():
        if u['is_approved']:
            try:
                auto_start_fwd(u['tg_id'])
            except Exception as e:
                print(f'[boot:{u["tg_id"]}] {e}')


def _fwd_watchdog():
    """هر ۳۰ ثانیه چک می‌کند client وصل است و handler نصب است؛ در غیر این‌صورت reconnect/reinstall."""
    while True:
        time.sleep(30)
        for u in db_all_users():
            if not u['is_approved']:
                continue
            uid = u['tg_id']
            if not session_on_disk(uid):
                continue
            try:
                c = _tg_clients.get(uid)
                connected = False
                if c is not None:
                    try:
                        connected = bool(c.is_connected())
                    except Exception:
                        connected = False
                if not connected or fwd.status(uid) == 'idle' or not _fwd_active.get(uid):
                    auto_start_fwd(uid)
            except Exception as e:
                print(f'[watchdog:{uid}] {e}')


threading.Thread(
    target=lambda: (time.sleep(4), _auto_start_all()),
    daemon=True, name='fwd-boot'
).start()
threading.Thread(target=_fwd_watchdog, daemon=True, name='fwd-watchdog').start()


def stop_fwd(uid: int):
    stop_forwarder(uid)


def stop_all():
    for uid in list(_fwd_active):
        stop_forwarder(uid)


atexit.register(stop_all)

# ══════════════════════════════════════════════════════════
#  Per-user Telethon clients
# ══════════════════════════════════════════════════════════
_tg_loop     = asyncio.new_event_loop()
_tg_clients:   dict[int, TelegramClient | None] = {}
_tg_connected: dict[int, bool]                  = {}
_tg_auth:      dict[int, dict]                  = {}
_qr_state:     dict[int, dict]                  = {}

threading.Thread(target=_tg_loop.run_forever, daemon=True, name='tg-loop').start()

def tg_run(coro, timeout: int = 30):
    return asyncio.run_coroutine_threadsafe(coro, _tg_loop).result(timeout=timeout)

def _make_client(uid: int) -> TelegramClient | None:
    api_id, api_hash = user_api_credentials(uid)
    if not api_id or not api_hash:
        return None
    old = _tg_clients.get(uid)
    if old:
        try:
            fwd.uninstall_handler(uid, old)
        except Exception:
            pass
        try: asyncio.run_coroutine_threadsafe(old.disconnect(), _tg_loop).result(timeout=5)
        except Exception: pass
    _migrate_legacy_session(uid)
    client = TelegramClient(user_session_path(uid, PANEL_SESSION), int(api_id), api_hash, loop=_tg_loop)
    _tg_clients[uid]   = client
    _tg_connected[uid] = False
    if uid not in _tg_auth:
        _tg_auth[uid] = {'phase': 'idle', 'phone': None, 'phone_code_hash': None}
    return client

async def _client_connect(uid: int) -> TelegramClient | None:
    if not master_api_ready():
        return None
    if uid not in _tg_clients:
        _make_client(uid)
    c = _tg_clients.get(uid)
    if not c:
        return None
    if not c.is_connected():
        await c.connect()
    return c

async def _connect(uid: int):
    c = _tg_clients.get(uid)
    if not c:
        return
    try:
        if not c.is_connected():
            await c.connect()
        ok = await c.is_user_authorized()
        _tg_connected[uid] = ok
        if ok:
            _tg_auth[uid]['phase'] = 'done'
            if not _fwd_active.get(uid):
                try:
                    cfg = load_user_config(uid)
                    await fwd.rebuild_and_install(uid, c, cfg)
                    _fwd_active[uid] = True
                    _log_msg(uid, f"forwarder online ({fwd.stats(uid)['routes']} routes)")
                except Exception as e:
                    _log_msg(uid, f"forwarder install failed: {e}")
                    print(f'[TG:{uid}] forwarder install: {e}')
    except Exception as e:
        _tg_connected[uid] = False
        print(f'[TG:{uid}] {e}')

async def _telegram_ready_async(uid: int) -> bool:
    if not master_api_ready() or not session_on_disk(uid):
        _tg_connected[uid] = False
        return False
    if uid not in _tg_clients:
        _make_client(uid)
    c = _tg_clients.get(uid)
    if not c:
        return False
    await _connect(uid)
    return _tg_connected.get(uid, False)

def disconnect_tg_client(uid: int):
    old = _tg_clients.pop(uid, None)
    if old:
        try:
            fwd.uninstall_handler(uid, old)
        except Exception:
            pass
        try:
            asyncio.run_coroutine_threadsafe(old.disconnect(), _tg_loop).result(timeout=8)
        except Exception:
            pass
    _tg_connected[uid] = False
    _fwd_active[uid] = False


def reset_tg_client(uid: int) -> TelegramClient | None:
    disconnect_tg_client(uid)
    return ensure_client(uid)

def ensure_client(uid: int) -> TelegramClient | None:
    if not master_api_ready() or not session_on_disk(uid):
        return None
    try:
        if tg_run(_telegram_ready_async(uid), timeout=25):
            return _tg_clients.get(uid)
    except Exception as e:
        print(f'[TG:{uid}] ensure_client: {e}')
    return None

def tg_ok(uid: int) -> bool:
    try:
        return tg_run(_telegram_ready_async(uid), timeout=25)
    except Exception:
        return False

# ══════════════════════════════════════════════════════════
#  Routes — Setup (اولین اجرا)
# ══════════════════════════════════════════════════════════
@app.route('/setup', methods=['GET'])
def setup_page():
    if master_ready():
        return redirect(url_for('login_page'))
    return render_template('setup.html')

@app.route('/setup', methods=['POST'])
def do_setup():
    d = request.get_json() or {}
    required = ('api_id', 'api_hash', 'bot_token', 'bot_username')
    if not all(d.get(k) for k in required):
        return jsonify({'error': 'API و Bot الزامی‌اند'}), 400
    m = load_master()
    m.update({k: d[k] for k in required})
    if not m.get('secret_key'):
        m['secret_key'] = secrets.token_hex(32)
    save_master(m)
    app.secret_key = m['secret_key']
    return jsonify({'ok': True})

# ══════════════════════════════════════════════════════════
#  Routes — Login
# ══════════════════════════════════════════════════════════
@app.route('/login')
def login_page():
    if not master_ready():
        return redirect(url_for('setup_page'))
    if cur_uid():
        return redirect(url_for('index'))
    m = load_master()
    return render_template('login.html', bot_username=m.get('bot_username',''))

@app.route('/auth/telegram', methods=['POST'])
def auth_telegram():
    if not master_ready():
        return jsonify({'error': 'setup not done'}), 503
    data = request.get_json() or {}
    if not verify_tg_auth(data):
        return jsonify({'error': 'تأیید هویت ناموفق — داده‌های تلگرام معتبر نیست'}), 401

    uid  = int(data['id'])
    db_upsert_user({
        'tg_id':      uid,
        'username':   data.get('username',  ''),
        'first_name': data.get('first_name',''),
        'last_name':  data.get('last_name', ''),
        'photo_url':  data.get('photo_url', ''),
    })

    # اولین کاربر → ادمین و تأیید شده
    if db_count_users() == 1:
        db_set_approval(uid, approved=True, admin=True)

    user = db_get_user(uid)
    session['tg_id']      = uid
    session['first_name'] = user['first_name']
    session['photo_url']  = user['photo_url']

    # اگر کاربر approved است، client را lazy init کن
    if user['is_approved']:
        initialize_user_config(uid)
        ensure_client(uid)
        auto_start_fwd(uid)

    return jsonify({
        'ok': True,
        'approved': bool(user['is_approved']),
        'needs_api': not master_api_ready(),
        'needs_telethon': needs_telethon_login(uid),
    })

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login_page'))

# ══════════════════════════════════════════════════════════
#  Routes — Main App
# ══════════════════════════════════════════════════════════
@app.route('/')
@login_required
def index():
    if not master_ready():
        return redirect(url_for('setup_page'))
    user = db_get_user(cur_uid())
    if not user or not user['is_approved']:
        return render_template('pending.html', user=user)
    uid = cur_uid()
    initialize_user_config(uid)
    auto_start_fwd(uid)
    return render_template('index.html', user=user, master_api=master_api_ready())

# ══════════════════════════════════════════════════════════
#  Routes — API: Config
# ══════════════════════════════════════════════════════════
@app.route('/api/config', methods=['GET'])
@login_required
def get_config():
    return jsonify(load_user_config(cur_uid()))

@app.route('/api/config', methods=['POST'])
@login_required
def update_config():
    uid  = cur_uid()
    data = request.get_json()
    save_user_config(uid, data)
    # rebuild حتماً (حتی اگر فعال است) — چون sources عوض شده‌اند
    rebuild_forwarder(uid)
    return jsonify({
        'ok': True,
        'restarted': True,
        'status': fwd_status(uid),
        **fwd.stats(uid),
    })

@app.route('/api/status')
@login_required
def get_status():
    uid = cur_uid()
    ensure_client(uid)
    user = db_get_user(uid)
    cfg = load_user_config(uid)
    cstats = config_stats(cfg)
    return jsonify({
        'connected':        tg_ok(uid),
        'sources_configured': cstats.get('sources', 0),
        'panel_ready':      tg_ok(uid),
        'has_client':       _tg_clients.get(uid) is not None,
        'has_forum_api':    HAS_FORUM_API,
        'auth_phase':       _tg_auth.get(uid, {}).get('phase', 'idle'),
        'bot':              fwd_status(uid),
        'auto_forwarder':   True,
        'has_session':      session_on_disk(uid),
        'needs_api':        not master_api_ready(),
        'needs_telethon':   needs_telethon_login(uid),
        'is_admin':         bool(user and user.get('is_admin')),
        'onboarding_done':  master_api_ready() and session_on_disk(uid),
    })

# ══════════════════════════════════════════════════════════
#  Routes — API: Forwarder
# ══════════════════════════════════════════════════════════
@app.route('/api/forwarder/start', methods=['POST'])
@login_required
def fwd_start():
    uid = cur_uid()
    rebuild_forwarder(uid)
    return jsonify({'ok': True, 'status': fwd_status(uid), **fwd.stats(uid)})

@app.route('/api/forwarder/stop', methods=['POST'])
@login_required
def fwd_stop():
    uid = cur_uid()
    stop_forwarder(uid)
    return jsonify({'ok': True, 'status': fwd_status(uid)})

@app.route('/api/forwarder/restart', methods=['POST'])
@login_required
def fwd_restart():
    uid = cur_uid()
    rebuild_forwarder(uid)
    return jsonify({'ok': True, 'status': fwd_status(uid), **fwd.stats(uid)})

@app.route('/api/forwarder/logs')
@login_required
def fwd_logs():
    uid = cur_uid()
    n   = int(request.args.get('n', 80))
    return jsonify({
        'logs': list(_logs(uid))[-n:],
        'status': fwd_status(uid),
        **fwd.stats(uid),
    })

@app.route('/api/parse_value/test', methods=['POST'])
@login_required
def api_parse_value_test():
    """تست regex استخراج عدد روی متن نمونه؛ پشتیبانی از clean_text."""
    data = request.get_json() or {}
    text = data.get('text', '') or ''
    regex = data.get('regex', '') or ''
    do_clean = bool(data.get('clean', False))
    cleaned = _clean_text(text) if do_clean else text
    value, raw = parse_value(cleaned, regex or None)
    resp = {'cleaned': cleaned if do_clean else None}
    if value is None:
        resp.update({'ok': False, 'msg': 'عددی استخراج نشد'})
    else:
        resp.update({'ok': True, 'value': value, 'raw': raw})
    return jsonify(resp)


@app.route('/api/charts/<group_id>/<int:topic_id>/data')
@login_required
def api_chart_data(group_id: str, topic_id: int):
    """تاریخچهٔ نرخ‌ها برای نمودار interactive در پنل.
    تا ۷ روز: ردیف‌های خام (نقاط ریز).
    بیش از ۷ روز: aggregation روزانه + روز جاری ریز.
    """
    uid = cur_uid()
    hours = int(request.args.get('hours', 168))
    hours = max(1, min(hours, 24 * 180))
    result = get_rates_smart(uid, group_id, topic_id, since_hours=hours)
    cfg = load_user_config(uid)
    g = find_group(cfg, group_id) or {}
    topic_name = ''
    chart_label = ''
    for t in g.get('topics') or []:
        if int(t.get('topic_id') or 0) == int(topic_id):
            topic_name = t.get('name', '') or ''
            chart_label = t.get('chart_label', '') or ''
            break
    last = latest_rate(uid, group_id, topic_id)
    return jsonify({
        'ok': True,
        'group_id': group_id,
        'topic_id': topic_id,
        'topic_name': topic_name,
        'chart_label': chart_label,
        'group_title': g.get('title', ''),
        'rates': result['rates'],
        'mode': result['mode'],
        'latest': last,
        'count': result['count'],
        'hours': hours,
    })


@app.route('/api/charts/<group_id>/<int:topic_id>/rate/<int:rate_id>', methods=['DELETE'])
@login_required
def api_chart_delete_rate(group_id: str, topic_id: int, rate_id: int):
    uid = cur_uid()
    ok = delete_rate(uid, group_id, topic_id, rate_id)
    if not ok:
        return jsonify({'ok': False, 'msg': 'ردیف یافت نشد'}), 404
    return jsonify({'ok': True})


@app.route('/api/charts/<group_id>/<int:topic_id>/rates_bulk', methods=['POST'])
@login_required
def api_chart_bulk_delete_rates(group_id: str, topic_id: int):
    """حذف outlier ها: مقادیر کوچک‌تر از min یا بزرگ‌تر از max."""
    uid = cur_uid()
    data = request.get_json() or {}
    min_v = data.get('min')
    max_v = data.get('max')
    try:
        min_v = float(min_v) if min_v not in (None, '') else None
        max_v = float(max_v) if max_v not in (None, '') else None
    except (TypeError, ValueError):
        return jsonify({'ok': False, 'msg': 'مقدار نامعتبر'}), 400
    n = delete_rates_range(uid, group_id, topic_id, min_v, max_v)
    return jsonify({'ok': True, 'deleted': n})


@app.route('/chart/<group_id>/<int:topic_id>')
@login_required
def chart_page(group_id: str, topic_id: int):
    return render_template('chart.html', group_id=group_id, topic_id=topic_id)


# ══════════════════════════════════════════════════════════
#  Public chart sharing (no login required)
# ══════════════════════════════════════════════════════════
@app.route('/api/me/public_token')
@login_required
def api_me_public_token():
    uid = cur_uid()
    tok = db_get_or_create_public_token(uid)
    return jsonify({'token': tok, 'url': url_for('public_charts_page', token=tok, _external=False)})


@app.route('/api/me/public_token/rotate', methods=['POST'])
@login_required
def api_me_rotate_public_token():
    uid = cur_uid()
    tok = db_rotate_public_token(uid)
    return jsonify({'token': tok, 'url': url_for('public_charts_page', token=tok, _external=False)})


@app.route('/p/<token>')
def public_charts_page(token: str):
    u = db_user_by_public_token(token)
    if not u:
        return render_template_string(
            '<h2 style="font-family:sans-serif;text-align:center;margin-top:4rem;color:#64748b">'
            'لینک نامعتبر یا منقضی شده است</h2>'
        ), 404
    return render_template('public_charts.html', token=token, owner_name=(
        (u.get('first_name') or '') + ' ' + (u.get('last_name') or '')
    ).strip() or (u.get('username') or 'کاربر'))


@app.route('/api/public/<token>/charts')
def api_public_charts_list(token: str):
    """لیست تاپیک‌هایی که چارت فعال دارند برای این کاربر — بدون لاگین."""
    u = db_user_by_public_token(token)
    if not u:
        return jsonify({'ok': False, 'msg': 'invalid token'}), 404
    uid = int(u['tg_id'])
    cfg = load_user_config(uid)
    items = []
    for g in cfg.get('groups') or []:
        for t in g.get('topics') or []:
            if not t.get('chart_enabled') or not t.get('public_chart_enabled', t.get('chart_enabled')):
                continue
            last, prev = latest_two_rates(uid, g.get('id'), int(t.get('topic_id') or 0))
            change = compute_change(
                last.get('value') if last else None,
                prev.get('value') if prev else None,
            )
            items.append({
                'group_id': g.get('id'),
                'group_title': g.get('title', ''),
                'topic_id': int(t.get('topic_id') or 0),
                'name': t.get('name', ''),
                'chart_label': t.get('chart_label', '') or t.get('name', ''),
                'chart_days': int(t.get('chart_days') or 7),
                'chart_order': int(t.get('chart_order') or 0),
                'last_value': last.get('value') if last else None,
                'last_time': last.get('created_at') if last else None,
                'previous_value': prev.get('value') if prev else None,
                'change': change,
            })
    # اولویت بالاتر (عدد کوچک‌تر) اول؛ سپس بر اساس نام برای ثبات
    items.sort(key=lambda x: (x['chart_order'], x['chart_label']))
    return jsonify({
        'ok': True,
        'owner': ((u.get('first_name') or '') + ' ' + (u.get('last_name') or '')).strip()
                 or (u.get('username') or 'کاربر'),
        'charts': items,
    })


@app.route('/api/public/<token>/charts/<group_id>/<int:topic_id>/data')
def api_public_chart_data(token: str, group_id: str, topic_id: int):
    u = db_user_by_public_token(token)
    if not u:
        return jsonify({'ok': False, 'msg': 'invalid token'}), 404
    uid = int(u['tg_id'])
    cfg = load_user_config(uid)
    g = find_group(cfg, group_id) or {}
    topic = None
    for t in g.get('topics') or []:
        if int(t.get('topic_id') or 0) == int(topic_id):
            topic = t
            break
    if (
        not topic
        or not topic.get('chart_enabled')
        or not topic.get('public_chart_enabled', topic.get('chart_enabled'))
    ):
        return jsonify({'ok': False, 'msg': 'chart not public'}), 404
    hours = int(request.args.get('hours', 168))
    hours = max(1, min(hours, 24 * 180))
    result = get_rates_smart(uid, group_id, topic_id, since_hours=hours)
    last = latest_rate(uid, group_id, topic_id)
    return jsonify({
        'ok': True,
        'topic_id': topic_id,
        'group_id': group_id,
        'topic_name': topic.get('name', ''),
        'chart_label': topic.get('chart_label', '') or topic.get('name', ''),
        'group_title': g.get('title', ''),
        'rates': result['rates'],
        'mode': result['mode'],
        'latest': last,
        'count': result['count'],
        'hours': hours,
    })


@app.route('/api/forwarder/diag')
@login_required
def fwd_diag():
    """تشخیص لحظه‌ای: routes، اتصال، وضعیت — برای دیباگ کاربر."""
    uid = cur_uid()
    c = _tg_clients.get(uid)
    connected = False
    if c is not None:
        try:
            connected = bool(c.is_connected())
        except Exception:
            connected = False
    data = fwd._routes.get(uid) or {'source_map': {}, 'targets': {}, 'forum': {}}
    sources = []
    for peer_id, entries in data['source_map'].items():
        for r in entries:
            sources.append({
                'peer_id': peer_id,
                'group_id': r.get('gid'),
                'topic_id': r.get('topic_id') if r.get('is_forum') else None,
                'filters': r.get('filters') or [],
                'chart_enabled': bool(r.get('chart_enabled')),
                'forward_enabled': bool(r.get('forward_enabled', True)),
                'chart_message_enabled': bool(r.get('chart_message_enabled', True)),
                'value_regex': r.get('value_regex') or '',
                'chart_label': r.get('chart_label') or '',
            })
    targets = [
        {'group_id': gid, 'title': getattr(ent, 'title', '') or getattr(ent, 'first_name', '') or str(gid)}
        for gid, ent in data['targets'].items()
    ]
    return jsonify({
        'uid': uid,
        'session_on_disk': session_on_disk(uid),
        'client_connected': connected,
        'forwarder_active': bool(_fwd_active.get(uid)),
        'status': fwd_status(uid),
        'stats': fwd.stats(uid),
        'sources': sources,
        'targets': targets,
    })


@app.route('/api/charts/status')
@login_required
def api_chart_status():
    """وضعیت matplotlib + لیست تاپیک‌هایی که چارت فعال دارند.
    با ?retry=1 یک تلاش مجدد برای لود matplotlib (پس از pip install) می‌کند.
    """
    try:
        import charts as _charts
        if request.args.get('retry') == '1':
            _charts.reload()
        ok = _charts.is_available()
        err = _charts.load_error() if not ok else ''
    except Exception as e:
        ok, err = False, f'charts module load failed: {type(e).__name__}: {e}'

    uid = cur_uid()
    cfg = load_user_config(uid)
    enabled_topics = []
    for g in cfg.get('groups') or []:
        for t in g.get('topics') or []:
            if not t.get('chart_enabled'):
                continue
            srcs = []
            for s in t.get('sources') or []:
                srcs.append({
                    'chat': s.get('chat', ''),
                    'value_regex': s.get('value_regex', '') or '',
                })
            enabled_topics.append({
                'group_id': g.get('id'),
                'group_title': g.get('title', ''),
                'topic_id': t.get('topic_id'),
                'name': t.get('name', ''),
                'chart_label': t.get('chart_label', '') or '',
                'chart_message_enabled': bool(t.get('chart_message_enabled', True)),
                'public_chart_enabled': bool(t.get('public_chart_enabled', t.get('chart_enabled'))),
                'sources': srcs,
            })
    return jsonify({
        'matplotlib_available': ok,
        'error': err,
        'install_hint': 'pip install matplotlib و sudo apt install -y libgl1 libglib2.0-0',
        'enabled_topics': enabled_topics,
    })


@app.route('/api/charts/<group_id>/<int:topic_id>/test_send', methods=['POST'])
@login_required
def api_chart_test_send(group_id: str, topic_id: int):
    """ارسال دستی چارت آزمایشی به گروه/تاپیک برای دیباگ.
    اول با ApexCharts (Playwright) سعی می‌کند، در صورت ناموفقی به matplotlib برمی‌گردد.
    """
    uid = cur_uid()
    c = ensure_client(uid)
    if not c:
        return jsonify({'ok': False, 'msg': 'اتصال تلگرام برقرار نیست'}), 503

    cfg = load_user_config(uid)
    g = find_group(cfg, group_id)
    if not g:
        return jsonify({'ok': False, 'msg': 'گروه یافت نشد'}), 404
    is_forum = bool(g.get('is_forum'))
    chart_label = ''
    chart_days = 7
    for t in g.get('topics') or []:
        if int(t.get('topic_id') or 0) == int(topic_id):
            chart_label = t.get('chart_label') or t.get('name') or ''
            chart_days = int(t.get('chart_days') or 7)
            break

    data = get_rates_smart(uid, group_id, topic_id, since_hours=24 * chart_days)
    rates = data['rates']
    mode = data['mode']

    render_engine = 'matplotlib'
    png: bytes | None = None

    # 1) سعی با apex
    try:
        from apex_chart import render_chart_png as _apex_render
        async def _do_apex():
            return await _apex_render(rates, title=chart_label or f"Topic {topic_id}",
                                      mode=mode, days=chart_days)
        png = tg_run(_do_apex(), timeout=20)
        if png:
            render_engine = 'apex'
    except Exception as e:
        _charts_log.warning("apex render failed in test_send: %s", e)
        png = None

    # 2) fallback به matplotlib
    if not png:
        try:
            import charts as _charts
            if not _charts.is_available():
                return jsonify({
                    'ok': False,
                    'msg': 'هیچ‌یک از Apex/matplotlib در دسترس نیستند',
                    'error': _charts.load_error(),
                    'hint': 'playwright install --with-deps chromium  یا  apt install libgl1 libglib2.0-0',
                }), 500
            png = _charts.render_rate_chart(
                rates,
                title=chart_label or f"Topic {topic_id} (test)",
                y_label=chart_label or '',
            )
        except Exception as e:
            return jsonify({'ok': False, 'stage': 'render', 'msg': str(e)}), 500

    if not png:
        return jsonify({'ok': False, 'stage': 'render', 'msg': 'render returned empty'}), 500

    async def _send():
        target = await c.get_entity(int(g['telegram_id']))
        from config_util import get_last_chart_msg as _glcm, save_last_chart_msg as _slcm
        old = _glcm(uid, group_id, topic_id)
        if old:
            try:
                await c.delete_messages(target, [int(old)])
            except Exception:
                pass
        named = fwd._named_png(chart_label or f'topic_{topic_id}', png)
        kwargs = {'caption': f'📊 {chart_label} (test · {render_engine})', 'force_document': False}
        if is_forum and topic_id and topic_id > 0:
            kwargs['reply_to'] = int(topic_id)
        sent = await c.send_file(target, file=named, **kwargs)
        if sent and hasattr(sent, 'id'):
            _slcm(uid, group_id, topic_id, int(sent.id))
            return int(sent.id)
        return None

    try:
        msg_id = tg_run(_send(), timeout=45)
        return jsonify({
            'ok': True, 'message_id': msg_id,
            'rates_count': len(rates), 'mode': mode, 'engine': render_engine,
        })
    except Exception as e:
        return jsonify({'ok': False, 'stage': 'send', 'msg': str(e)}), 500

# ══════════════════════════════════════════════════════════
#  Backfill — واکشی تاریخچه‌ی نرخ‌ها از یک سورس
# ══════════════════════════════════════════════════════════
# هر job در حافظه: {key: {status, progress, stats, error, future, cancel_flag}}
# key = (uid, gid, tid, source_chat)
_backfill_jobs: dict[tuple, dict] = {}


def _bf_key(uid: int, gid: str, tid: int, src: str) -> tuple:
    return (int(uid), str(gid), int(tid), str(src))


@app.route('/api/backfill/start', methods=['POST'])
@login_required
def api_backfill_start():
    """شروع backfill برای یک سورس مشخص از یک تاپیک.

    body: { gid, tid, source_chat, days?, max_messages? }
    """
    uid = cur_uid()
    data = request.get_json() or {}
    gid = (data.get('gid') or '').strip()
    try:
        tid = int(data.get('tid') if data.get('tid') is not None else 0)
    except (TypeError, ValueError):
        return jsonify({'ok': False, 'msg': 'tid نامعتبر'}), 400
    src = (data.get('source_chat') or '').strip()
    days = int(data.get('days') or 90)
    max_msgs = int(data.get('max_messages') or 50000)
    if not gid or not src:
        return jsonify({'ok': False, 'msg': 'gid و source_chat الزامی است'}), 400

    c = ensure_client(uid)
    if not c:
        return jsonify({'ok': False, 'msg': 'اتصال تلگرام برقرار نیست'}), 503

    cfg = load_user_config(uid)
    g = find_group(cfg, gid)
    if not g:
        return jsonify({'ok': False, 'msg': 'گروه یافت نشد'}), 404
    src_obj = None
    topic_obj = None
    for t in (g.get('topics') or []):
        if int(t.get('topic_id') or 0) == tid:
            topic_obj = t
            for s in (t.get('sources') or []):
                if (s.get('chat') or '').strip() == src:
                    src_obj = s
                    break
            break
    if not src_obj or not topic_obj:
        return jsonify({'ok': False, 'msg': 'سورس در این تاپیک یافت نشد'}), 404
    # توجه: value_regex اختیاری است — اگر خالی باشد، parse_value از heuristic
    # داخلی (اولین عدد ≥ ۲ رقم) استفاده می‌کند.

    key = _bf_key(uid, gid, tid, src)
    existing = _backfill_jobs.get(key)
    if existing and existing.get('status') == 'running':
        return jsonify({'ok': False, 'msg': 'این job در حال اجراست'}), 409

    cancel_flag = {'stop': False}
    state = {
        'status': 'running',
        'started_at': time.time(),
        'progress': {},
        'stats': None,
        'error': None,
        'days': days,
        'max_messages': max_msgs,
        'source_chat': src,
        'gid': gid, 'tid': tid,
        'cancel_flag': cancel_flag,
    }
    _backfill_jobs[key] = state

    filters = src_obj.get('filters') or []
    value_regex = src_obj.get('value_regex') or ''
    clean = bool(src_obj.get('clean_text'))

    async def _runner():
        try:
            stats = await fwd.backfill_source(
                uid, c, src, gid, tid,
                value_regex=value_regex,
                filters=filters,
                clean_text=clean,
                since_days=days,
                max_messages=max_msgs,
                progress_cb=lambda p: state.update({'progress': p}),
                is_cancelled=lambda: cancel_flag.get('stop', False),
            )
            state['stats'] = stats
            state['status'] = 'cancelled' if stats.get('cancelled') else 'done'
            state['progress'] = stats
        except Exception as e:
            state['status'] = 'error'
            state['error'] = str(e)
            _charts_log.error("[%s] backfill failed: %s", uid, e, exc_info=True)
        finally:
            state['finished_at'] = time.time()

    fut = asyncio.run_coroutine_threadsafe(_runner(), _tg_loop)
    state['future'] = fut
    return jsonify({'ok': True, 'key': list(key)})


def _bf_public(state: dict) -> dict:
    """نسخه‌ی قابل serial دیکشنری state (بدون future / cancel_flag)."""
    return {
        'status': state.get('status'),
        'progress': state.get('progress') or {},
        'stats': state.get('stats'),
        'error': state.get('error'),
        'days': state.get('days'),
        'max_messages': state.get('max_messages'),
        'source_chat': state.get('source_chat'),
        'started_at': state.get('started_at'),
        'finished_at': state.get('finished_at'),
    }


@app.route('/api/backfill/status', methods=['GET'])
@login_required
def api_backfill_status():
    """وضعیت یک job مشخص یا همه‌ی job های کاربر.

    query: gid, tid, source_chat (همگی اختیاری — اگر باشد یکی برمی‌گرداند)
    """
    uid = cur_uid()
    gid = (request.args.get('gid') or '').strip()
    src = (request.args.get('source_chat') or '').strip()
    tid_raw = request.args.get('tid')
    if gid and src and tid_raw is not None:
        try:
            tid = int(tid_raw)
        except ValueError:
            return jsonify({'ok': False, 'msg': 'tid نامعتبر'}), 400
        key = _bf_key(uid, gid, tid, src)
        st = _backfill_jobs.get(key)
        if not st:
            return jsonify({'ok': True, 'state': None})
        return jsonify({'ok': True, 'state': _bf_public(st)})
    jobs = {}
    for k, st in _backfill_jobs.items():
        if k[0] != uid:
            continue
        jobs[f'{k[1]}|{k[2]}|{k[3]}'] = _bf_public(st)
    return jsonify({'ok': True, 'jobs': jobs})


@app.route('/api/charts/<group_id>/<int:topic_id>/reaggregate', methods=['POST'])
@login_required
def api_chart_reaggregate(group_id: str, topic_id: int):
    """محاسبه‌ی مجدد aggregation روزانه — برای مواردی که قبلاً backfill شده ولی aggregate نشده."""
    uid = cur_uid()
    days_back = int((request.get_json() or {}).get('days', 180))
    try:
        days = list_days_in_range(uid, group_id, topic_id, since_days=days_back)
        n = aggregate_rate_daily(uid, group_id, topic_id, days=days)
        return jsonify({'ok': True, 'aggregated_days': n})
    except Exception as e:
        return jsonify({'ok': False, 'msg': str(e)}), 500


@app.route('/api/backfill/cancel', methods=['POST'])
@login_required
def api_backfill_cancel():
    uid = cur_uid()
    data = request.get_json() or {}
    gid = (data.get('gid') or '').strip()
    src = (data.get('source_chat') or '').strip()
    try:
        tid = int(data.get('tid') if data.get('tid') is not None else 0)
    except (TypeError, ValueError):
        return jsonify({'ok': False, 'msg': 'tid نامعتبر'}), 400
    key = _bf_key(uid, gid, tid, src)
    st = _backfill_jobs.get(key)
    if not st:
        return jsonify({'ok': False, 'msg': 'job یافت نشد'}), 404
    if st.get('status') != 'running':
        return jsonify({'ok': False, 'msg': 'این job در حال اجرا نیست'}), 409
    flag = st.get('cancel_flag')
    if flag:
        flag['stop'] = True
    return jsonify({'ok': True})


# ══════════════════════════════════════════════════════════
#  Routes — API: Auth (Telegram login from panel)
# ══════════════════════════════════════════════════════════
def _finish_telethon_login(uid: int):
    initialize_user_config(uid)
    reset_tg_client(uid)
    auto_start_fwd(uid)

def _qr_wait_thread(uid: int):
    st = _qr_state.get(uid)
    if not st:
        return
    try:
        tg_run(st['qr'].wait(), timeout=120)
        _tg_connected[uid] = True
        _tg_auth.setdefault(uid, {})['phase'] = 'done'
        st['done'] = True
        _finish_telethon_login(uid)
    except Exception as e:
        st['error'] = str(e)

@app.route('/api/auth/prepare', methods=['POST'])
@login_required
def auth_prepare():
    """
    آماده‌سازی اتصال Telethon پس از لاگین ویجت:
    - اگر session هست → already
    - اگر شماره ذخیره شده → ارسال خودکار کد (بدون تایپ شماره)
    - وگرنه → QR با همان اکانت تلگرام (بدون شماره)
    """
    uid = cur_uid()
    user = db_get_user(uid) or {}
    if session_on_disk(uid):
        try:
            if tg_run(_telegram_ready_async(uid), timeout=25):
                return jsonify({
                    'ok': True, 'already': True,
                    'first_name': user.get('first_name', ''),
                })
        except Exception:
            pass

    try:
        c = tg_run(_client_connect(uid), timeout=20)
    except Exception:
        c = None
    if not c:
        return jsonify({'error': 'API سرور تنظیم نشده'}), 503

    phone = (user.get('phone') or '').strip()
    if phone:
        try:
            sent = tg_run(c.send_code_request(phone))
            _tg_auth.setdefault(uid, {}).update(
                phase='code_sent', phone=phone,
                phone_code_hash=sent.phone_code_hash,
            )
            return jsonify({
                'ok': True,
                'step': 'code',
                'masked_phone': _mask_phone(phone),
                'first_name': user.get('first_name', ''),
                'username': user.get('username', ''),
                'auto_code': True,
            })
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    async def _start_qr():
        if await c.is_user_authorized():
            return {'already': True}
        qr = await c.qr_login()
        return qr

    try:
        qr = tg_run(_start_qr(), timeout=30)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

    if isinstance(qr, dict) and qr.get('already'):
        _tg_connected[uid] = True
        _finish_telethon_login(uid)
        return jsonify({'ok': True, 'already': True})

    _qr_state[uid] = {'qr': qr, 'done': False, 'error': None}
    threading.Thread(target=_qr_wait_thread, args=(uid,), daemon=True).start()
    return jsonify({
        'ok': True,
        'step': 'qr',
        'qr_url': qr.url,
        'first_name': user.get('first_name', ''),
        'username': user.get('username', ''),
    })

@app.route('/api/auth/qr_status')
@login_required
def auth_qr_status():
    uid = cur_uid()
    st = _qr_state.get(uid)
    if not st:
        return jsonify({'pending': True})
    if st.get('error'):
        err = st['error']
        _qr_state.pop(uid, None)
        return jsonify({'error': str(err)}), 400
    if st.get('done'):
        _qr_state.pop(uid, None)
        return jsonify({'ok': True, 'done': True})
    return jsonify({'pending': True})

@app.route('/api/auth/send_code', methods=['POST'])
@login_required
def auth_send_code():
    uid = cur_uid()
    try:
        c = tg_run(_client_connect(uid), timeout=15)
    except Exception:
        c = None
    if not c:
        return jsonify({'error': 'API سرور تنظیم نشده یا اتصال برقرار نشد'}), 503
    phone = (request.get_json() or {}).get('phone', '').strip()
    if not phone:
        return jsonify({'error': 'شماره تلفن وارد نشده'}), 400
    try:
        sent = tg_run(c.send_code_request(phone))
        _tg_auth[uid].update(phase='code_sent', phone=phone,
                             phone_code_hash=sent.phone_code_hash)
        db_save_phone(uid, phone)
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/auth/verify_code', methods=['POST'])
@login_required
def auth_verify_code():
    uid  = cur_uid()
    c    = _tg_clients.get(uid)
    if not c:
        return jsonify({'error': 'ابتدا کد را درخواست کن'}), 400
    code = (request.get_json() or {}).get('code', '').strip()
    if not code: return jsonify({'error': 'کد وارد نشده'}), 400
    try:
        tg_run(c.sign_in(_tg_auth[uid]['phone'], code,
                         phone_code_hash=_tg_auth[uid]['phone_code_hash']))
        _tg_connected[uid] = True
        _tg_auth[uid]['phase'] = 'done'
        db_save_phone(uid, _tg_auth[uid].get('phone', ''))
        _finish_telethon_login(uid)
        return jsonify({'ok': True})
    except PhoneCodeInvalidError:
        return jsonify({'error': 'کد اشتباه است'}), 400
    except SessionPasswordNeededError:
        _tg_auth[uid]['phase'] = 'need_2fa'
        return jsonify({'ok': False, 'need_2fa': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/auth/verify_2fa', methods=['POST'])
@login_required
def auth_verify_2fa():
    uid = cur_uid()
    c   = _tg_clients.get(uid)
    if not c:
        return jsonify({'error': 'ابتدا کد را درخواست کن'}), 400
    pw  = (request.get_json() or {}).get('password', '')
    try:
        tg_run(c.sign_in(password=pw))
        _tg_connected[uid] = True
        _tg_auth[uid]['phase'] = 'done'
        _finish_telethon_login(uid)
        return jsonify({'ok': True})
    except PasswordHashInvalidError:
        return jsonify({'error': 'رمز اشتباه است'}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ══════════════════════════════════════════════════════════
#  Routes — API: Telegram Topics
# ══════════════════════════════════════════════════════════
def _group_telegram_id(cfg: dict, group_id: str) -> int | None:
    g = find_group(cfg, group_id)
    if not g or not g.get('telegram_id'):
        return None
    try:
        return int(g['telegram_id'])
    except (TypeError, ValueError):
        return None


def _fetch_forum_topics(c, tg_gid: int):
    return tg_run(c(GetForumTopicsRequest(
        peer=tg_gid, offset_date=None, offset_id=0, offset_topic=0, limit=100, q=None
    )))


def _peer_is_forum(c, tg_gid: int, cfg: dict, group_id: str) -> bool:
    g = find_group(cfg, group_id) if group_id else None
    if g and 'is_forum' in g:
        return bool(g['is_forum'])
    try:
        ent = tg_run(c.get_entity(tg_gid))
        return bool(getattr(ent, 'forum', False))
    except Exception:
        return False


def _resolve_member(c, ref: str):
    ref = (ref or '').strip()
    if not ref:
        raise ValueError('شناسه عضو خالی است')
    if ref.startswith('@'):
        ref = ref[1:]
    try:
        uid = int(ref)
        return tg_run(c.get_entity(uid))
    except (TypeError, ValueError):
        return tg_run(c.get_entity(ref))


def _topic_id_from_create_result(result, c, tg_gid: int, title: str) -> int | None:
    for upd in getattr(result, 'updates', []) or []:
        msg = getattr(upd, 'message', None)
        if not msg:
            continue
        act = getattr(msg, 'action', None)
        if isinstance(act, MessageActionTopicCreate):
            return msg.id
    for attempt in range(5):
        if attempt:
            time.sleep(0.4)
        try:
            res = _fetch_forum_topics(c, tg_gid)
            matches = [t for t in res.topics if getattr(t, 'title', None) == title]
            if matches:
                return matches[-1].id
        except Exception:
            pass
    return None


@app.route('/api/dashboard/stats')
@login_required
def api_dashboard_stats():
    uid = cur_uid()
    cfg = load_user_config(uid)
    group_id = request.args.get('group_id', '').strip() or None
    if group_id:
        g = find_group(cfg, group_id)
        if not g:
            return jsonify({'error': 'not_found'}), 404
        stats = group_config_stats(cfg, group_id)
        stats.update(dashboard_stats(uid, group_id))
    else:
        stats = config_stats(cfg)
        stats.update(dashboard_stats(uid))
    stats['bot'] = fwd_status(uid)
    stats['connected'] = session_on_disk(uid)
    return jsonify(stats)


@app.route('/api/groups/<group_id>/info')
@login_required
def group_telegram_info(group_id: str):
    uid = cur_uid()
    c = ensure_client(uid)
    if not c:
        return jsonify({'error': 'not_connected', 'msg': 'ابتدا با شماره تلفن وارد تلگرام شو'}), 503
    cfg = load_user_config(uid)
    tg_gid = _group_telegram_id(cfg, group_id)
    if tg_gid is None:
        return jsonify({'error': 'no_group'}), 400
    g = find_group(cfg, group_id)

    async def _fetch():
        entity = await c.get_entity(tg_gid)
        about = ''
        members_count = getattr(entity, 'participants_count', None)
        if HAS_FULL_CHANNEL and hasattr(entity, 'id'):
            try:
                inp = InputChannel(entity.id, entity.access_hash)
                full = await c(GetFullChannelRequest(channel=inp))
                about = getattr(full.full_chat, 'about', None) or ''
                members_count = getattr(full.full_chat, 'participants_count', members_count)
            except Exception:
                pass
        participants = []
        try:
            async for p in c.iter_participants(entity, limit=40):
                name = ' '.join(filter(None, [
                    getattr(p, 'first_name', '') or '',
                    getattr(p, 'last_name', '') or '',
                ])).strip() or getattr(p, 'username', '') or str(p.id)
                participants.append({
                    'id': p.id,
                    'name': name,
                    'username': getattr(p, 'username', '') or '',
                    'is_bot': bool(getattr(p, 'bot', False)),
                })
        except Exception:
            pass
        return {
            'title': getattr(entity, 'title', None) or (g.get('title') if g else ''),
            'about': about,
            'members_count': members_count,
            'telegram_id': str(tg_gid),
            'is_forum': bool(getattr(entity, 'forum', False)),
            'username': getattr(entity, 'username', '') or '',
            'participants': participants,
        }

    try:
        info = tg_run(_fetch(), timeout=45)
        return jsonify({'ok': True, **info})
    except Exception as e:
        return jsonify({'error': 'api', 'msg': str(e)}), 500


@app.route('/api/telegram/dialogs')
@login_required
def list_tg_dialogs():
    uid = cur_uid()
    c = ensure_client(uid)
    if not c:
        return jsonify({'error': 'not_connected', 'msg': 'ابتدا با شماره تلفن وارد تلگرام شو'}), 503
    cfg = load_user_config(uid)
    linked = {str(g.get('telegram_id')) for g in cfg.get('groups') or []}

    async def _fetch():
        items = []
        async for d in c.iter_dialogs(limit=300):
            ent = d.entity
            tid = str(d.id)
            if isinstance(ent, Channel):
                if not (
                    getattr(ent, 'megagroup', False)
                    or getattr(ent, 'gigagroup', False)
                    or getattr(ent, 'broadcast', False)
                ):
                    continue
                kind = 'channel' if getattr(ent, 'broadcast', False) else 'supergroup'
                items.append({
                    'id': d.id,
                    'title': d.name or tid,
                    'is_forum': bool(getattr(ent, 'forum', False)),
                    'kind': kind,
                    'already_linked': tid in linked,
                })
            elif isinstance(ent, Chat):
                items.append({
                    'id': d.id,
                    'title': d.name or tid,
                    'is_forum': False,
                    'kind': 'group',
                    'already_linked': tid in linked,
                })
        return items

    try:
        dialogs = tg_run(_fetch(), timeout=60)
        return jsonify({'dialogs': dialogs})
    except Exception as e:
        return jsonify({'error': 'api', 'msg': str(e)}), 500


@app.route('/api/groups/link', methods=['POST'])
@login_required
def link_group():
    uid = cur_uid()
    data = request.get_json() or {}
    try:
        telegram_id = int(data.get('telegram_id'))
    except (TypeError, ValueError):
        return jsonify({'error': 'invalid_id'}), 400
    title = (data.get('title') or '').strip() or str(telegram_id)
    is_forum = bool(data.get('is_forum', False))
    cfg = load_user_config(uid)
    for g in cfg.get('groups') or []:
        if str(g.get('telegram_id')) == str(telegram_id):
            return jsonify({'ok': True, 'group': g, 'exists': True})
    g = {
        'id': new_group_id(),
        'title': title,
        'telegram_id': str(telegram_id),
        'origin': 'linked',
        'is_forum': is_forum,
        'topics': [],
    }
    cfg.setdefault('groups', []).append(g)
    save_user_config(uid, cfg)
    auto_start_fwd(uid)
    return jsonify({'ok': True, 'group': g})


@app.route('/api/groups/create', methods=['POST'])
@login_required
def create_group():
    uid = cur_uid()
    c = ensure_client(uid)
    if not c:
        return jsonify({'error': 'not_connected'}), 503
    if not HAS_CREATE_CHANNEL:
        return jsonify({'error': 'no_api', 'msg': 'telethon upgrade required'}), 501
    data = request.get_json() or {}
    title = data.get('title', '').strip()
    use_forum = bool(data.get('forum', True))
    if not title:
        return jsonify({'error': 'no_title'}), 400

    async def _create():
        return await c(CreateChannelRequest(
            title=title,
            about='TeleFilter',
            megagroup=True,
            forum=use_forum,
        ))

    try:
        result = tg_run(_create())
        ch = result.chats[0]
        telegram_id = get_peer_id(ch)
        cfg = load_user_config(uid)
        g = {
            'id': new_group_id(),
            'title': title,
            'telegram_id': str(telegram_id),
            'origin': 'created',
            'is_forum': use_forum,
            'topics': [],
        }
        cfg.setdefault('groups', []).append(g)
        save_user_config(uid, cfg)
        auto_start_fwd(uid)
        return jsonify({'ok': True, 'group': g})
    except Exception as e:
        return jsonify({'error': 'api', 'msg': str(e)}), 500


@app.route('/api/groups/<group_id>', methods=['DELETE'])
@login_required
def delete_group(group_id: str):
    uid = cur_uid()
    cfg = load_user_config(uid)
    before = len(cfg.get('groups') or [])
    cfg['groups'] = [g for g in cfg.get('groups') or [] if g.get('id') != group_id]
    if len(cfg['groups']) == before:
        return jsonify({'error': 'not_found'}), 404
    save_user_config(uid, cfg)
    auto_start_fwd(uid)
    return jsonify({'ok': True})


@app.route('/api/groups/<group_id>/members', methods=['POST'])
@login_required
def group_add_member(group_id: str):
    uid = cur_uid()
    c = ensure_client(uid)
    if not c:
        return jsonify({'error': 'not_connected'}), 503
    if not HAS_INVITE_API:
        return jsonify({'error': 'no_api'}), 501
    cfg = load_user_config(uid)
    tg_gid = _group_telegram_id(cfg, group_id)
    if tg_gid is None:
        return jsonify({'error': 'no_group'}), 404
    ref = (request.get_json() or {}).get('user', '').strip()
    try:
        user_ent = _resolve_member(c, ref)
        channel = tg_run(c.get_entity(tg_gid))

        async def _invite():
            await c(InviteToChannelRequest(channel=channel, users=[user_ent]))

        tg_run(_invite())
        return jsonify({'ok': True, 'msg': 'دعوت ارسال شد'})
    except Exception as e:
        return jsonify({'error': 'api', 'msg': str(e)}), 500


@app.route('/api/groups/<group_id>/members/<int:member_id>', methods=['DELETE'])
@login_required
def group_remove_member(group_id: str, member_id: int):
    uid = cur_uid()
    c = ensure_client(uid)
    if not c:
        return jsonify({'error': 'not_connected'}), 503
    cfg = load_user_config(uid)
    tg_gid = _group_telegram_id(cfg, group_id)
    if tg_gid is None:
        return jsonify({'error': 'no_group'}), 404
    try:
        channel = tg_run(c.get_entity(tg_gid))

        async def _kick():
            await c.kick_participant(channel, member_id)

        tg_run(_kick())
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': 'api', 'msg': str(e)}), 500


@app.route('/api/telegram/topics', methods=['GET'])
@login_required
def get_tg_topics():
    uid = cur_uid()
    c = ensure_client(uid)
    if not c:
        return jsonify({'error': 'not_connected', 'msg': 'ابتدا با شماره تلفن وارد تلگرام شو'}), 503
    if not HAS_FORUM_API:
        return jsonify({'error': 'no_api', 'msg': 'pip install --upgrade telethon'}), 501
    group_id = request.args.get('group_id', '').strip()
    cfg = load_user_config(uid)
    tg_gid = _group_telegram_id(cfg, group_id) if group_id else None
    if tg_gid is None and cfg.get('groups'):
        tg_gid = _group_telegram_id(cfg, cfg['groups'][0]['id'])
    if tg_gid is None:
        return jsonify({'error': 'no_group', 'msg': 'ابتدا یک گروه اضافه کن'}), 400
    try:
        is_forum = _peer_is_forum(c, tg_gid, cfg, group_id)
        if not is_forum:
            return jsonify({
                'topics': [{'id': 0, 'title': 'چت اصلی'}],
                'is_forum': False,
            })
        result = _fetch_forum_topics(c, tg_gid)
        return jsonify({
            'topics': [{'id': t.id, 'title': t.title} for t in result.topics],
            'is_forum': True,
        })
    except Exception as e:
        return jsonify({'error': 'api', 'msg': str(e)}), 500

@app.route('/api/telegram/topics', methods=['POST'])
@login_required
def create_tg_topic():
    uid = cur_uid()
    c = ensure_client(uid)
    if not c:
        return jsonify({'error': 'not_connected', 'msg': 'ابتدا با شماره تلفن وارد تلگرام شو'}), 503
    if not HAS_FORUM_API:
        return jsonify({'error': 'no_api', 'msg': 'pip install --upgrade telethon'}), 501
    data  = request.get_json() or {}
    title = data.get('title', '').strip()
    group_id = data.get('group_id', '').strip()
    if not title:
        return jsonify({'error': 'no_title', 'msg': 'عنوان Topic الزامی است'}), 400
    cfg = load_user_config(uid)
    tg_gid = _group_telegram_id(cfg, group_id)
    if tg_gid is None:
        return jsonify({'error': 'no_group', 'msg': 'گروه انتخاب نشده'}), 400
    if not _peer_is_forum(c, tg_gid, cfg, group_id):
        return jsonify({'error': 'not_forum', 'msg': 'این گروه Forum نیست — از «چت اصلی» برای فوروارد استفاده کن'}), 400
    try:
        result = tg_run(c(CreateForumTopicRequest(
            peer=tg_gid, title=title, random_id=secrets.randbits(63),
        )))
        topic_id = _topic_id_from_create_result(result, c, tg_gid, title)
        return jsonify({'ok': True, 'topic_id': topic_id, 'title': title, 'group_id': group_id})
    except FloodWaitError as e:
        sec = int(getattr(e, 'seconds', 30) or 30)
        return jsonify({'error': 'flood', 'msg': f'تلگرام محدودیت گذاشته — حدود {sec} ثانیه صبر کن'}), 429
    except Exception as e:
        return jsonify({'error': 'api', 'msg': str(e)}), 500

# ══════════════════════════════════════════════════════════
#  Routes — Admin
# ══════════════════════════════════════════════════════════
@app.route('/api/admin/users', methods=['GET'])
@admin_required
def admin_list_users():
    users = db_all_users()
    # وضعیت real-time اضافه کن
    for u in users:
        uid = u['tg_id']
        u['bot_status'] = fwd_status(uid)
        u['tg_connected'] = tg_ok(uid)
    return jsonify({'users': users})

@app.route('/api/admin/users/<int:uid>/approve', methods=['POST'])
@admin_required
def admin_approve(uid: int):
    is_admin = (request.get_json() or {}).get('admin', False)
    db_set_approval(uid, approved=True, admin=is_admin)
    initialize_user_config(uid)
    ensure_client(uid)
    auto_start_fwd(uid)
    return jsonify({'ok': True})

@app.route('/api/admin/users/<int:uid>/revoke', methods=['POST'])
@admin_required
def admin_revoke(uid: int):
    # نمی‌توانی دسترسی خودت را لغو کنی
    if uid == cur_uid():
        return jsonify({'error': 'نمی‌توانی دسترسی خودت را لغو کنی'}), 400
    stop_fwd(uid)
    db_set_approval(uid, approved=False, admin=False)
    return jsonify({'ok': True})

@app.route('/api/admin/users/<int:uid>', methods=['DELETE'])
@admin_required
def admin_delete_user(uid: int):
    if uid == cur_uid():
        return jsonify({'error': 'نمی‌توانی خودت را حذف کنی'}), 400
    stop_fwd(uid)
    db_delete_user(uid)
    return jsonify({'ok': True})

@app.route('/api/admin/master', methods=['GET'])
@admin_required
def admin_get_master():
    m = load_master()
    # api_hash و bot_token را mask کن
    safe = {
        'api_id':       m.get('api_id', ''),
        'api_hash':     ('*' * 28 + m.get('api_hash','')[-4:]) if m.get('api_hash') else '',
        'bot_token':    ('*' * 40 + m.get('bot_token','')[-6:]) if m.get('bot_token') else '',
        'bot_username': m.get('bot_username', ''),
    }
    return jsonify(safe)

# ══════════════════════════════════════════════════════════
if __name__ == '__main__':
    if not master_ready():
        print('[TeleFilter] First run — opening setup wizard...')
    webbrowser.open('http://127.0.0.1:5000')
    app.run(debug=False, port=5000, threaded=True)
