"""
TeleFilter — Multi-user SaaS Panel
python panel.py  →  http://127.0.0.1:5000
"""
import json
import random
import threading
import asyncio
import subprocess
import sys
import os
import atexit
import hashlib
import hmac
import time
import sqlite3
import secrets
import webbrowser
from collections import deque
from functools import wraps
from flask import Flask, render_template, jsonify, request, session, redirect, url_for
from telethon import TelegramClient
from telethon.errors import (
    SessionPasswordNeededError, PhoneCodeInvalidError, PasswordHashInvalidError, FloodWaitError,
)
from telethon.tl.types import MessageService, Channel, MessageActionTopicCreate
from telethon.utils import get_peer_id

from config_util import (
    normalize_config, empty_config, find_group, new_group_id,
    config_stats, dashboard_stats, group_config_stats,
)

try:
    from telethon.tl.functions.messages import GetForumTopicsRequest, CreateForumTopicRequest
    from telethon.tl.functions.channels import CreateChannelRequest, GetFullChannelRequest
    from telethon.tl.types import InputChannel
    HAS_FORUM_API = True
    HAS_CREATE_CHANNEL = True
    HAS_FULL_CHANNEL = True
except ImportError:
    try:
        from telethon.tl.functions.channels import (
            GetForumTopicsRequest, CreateForumTopicRequest, CreateChannelRequest,
            GetFullChannelRequest,
        )
        from telethon.tl.types import InputChannel
        HAS_FORUM_API = True
        HAS_CREATE_CHANNEL = True
        HAS_FULL_CHANNEL = True
    except ImportError:
        HAS_FORUM_API = False
        HAS_CREATE_CHANNEL = False
        HAS_FULL_CHANNEL = False

# ══════════════════════════════════════════════════════════
#  Paths
# ══════════════════════════════════════════════════════════
BASE_DIR        = os.path.dirname(os.path.abspath(__file__))
MASTER_CFG_PATH = os.path.join(BASE_DIR, 'master_config.json')
MAIN_SCRIPT     = os.path.join(BASE_DIR, 'main.py')
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
            created_at  TEXT DEFAULT CURRENT_TIMESTAMP
        )''')
        cols = {r[1] for r in c.execute('PRAGMA table_info(users)').fetchall()}
        if 'phone' not in cols:
            c.execute('ALTER TABLE users ADD COLUMN phone TEXT DEFAULT ""')
        c.commit()

_init_db()

def db_save_phone(tg_id: int, phone: str):
    with _db() as c:
        c.execute('UPDATE users SET phone=? WHERE tg_id=?', (phone.strip(), tg_id))
        c.commit()

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

def user_session_path(tg_id: int, kind: str = 'telefilter') -> str:
    """مسیر پایه session (بدون پسوند) — Telethon خودش .session اضافه می‌کند."""
    return os.path.join(user_dir(tg_id), kind)

def _session_file(uid: int, kind: str = 'telefilter') -> str:
    return user_session_path(uid, kind) + '.session'

def _migrate_panel_session(uid: int):
    """نسخه‌های قدیم panel.session را به telefilter منتقل می‌کند."""
    tele = _session_file(uid, 'telefilter')
    panel = _session_file(uid, 'panel')
    if os.path.exists(tele):
        return
    if not os.path.exists(panel):
        return
    import shutil
    shutil.copy2(panel, tele)
    journal = panel + '-journal'
    if os.path.exists(journal):
        shutil.copy2(journal, tele + '-journal')

def session_on_disk(uid: int) -> bool:
    _migrate_panel_session(uid)
    return os.path.exists(_session_file(uid))

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
#  Per-user Forwarder management
# ══════════════════════════════════════════════════════════
_fwd_lock  = threading.Lock()
_fwd_procs: dict[int, subprocess.Popen | None] = {}
_fwd_logs:  dict[int, deque]                   = {}

def _logs(uid: int) -> deque:
    if uid not in _fwd_logs:
        _fwd_logs[uid] = deque(maxlen=300)
    return _fwd_logs[uid]

def _log_reader(proc: subprocess.Popen, uid: int):
    try:
        for line in iter(proc.stdout.readline, ''):
            _logs(uid).append(line.rstrip())
    except Exception:
        pass

def fwd_status(uid: int) -> str:
    p = _fwd_procs.get(uid)
    if p is None:          return 'stopped'
    if p.poll() is None:   return 'running'
    return f'crashed ({p.returncode})' if p.returncode != 0 else 'stopped'

def _stop_fwd(uid: int):
    p = _fwd_procs.get(uid)
    if p and p.poll() is None:
        _logs(uid).append('— stopping forwarder —')
        p.terminate()
        try:   p.wait(timeout=6)
        except subprocess.TimeoutExpired: p.kill()
    _fwd_procs[uid] = None

def start_fwd(uid: int) -> bool:
    """فوروارد را اجرا می‌کند؛ اگر session وجود نداشته باشد False برمی‌گرداند."""
    if not session_on_disk(uid):
        _logs(uid).append('— forwarder skipped: no Telegram session (login in panel) —')
        return False
    with _fwd_lock:
        _stop_fwd(uid)
        cfg  = user_config_path(uid)
        sess = user_session_path(uid)
        _logs(uid).append('— starting forwarder —')
        proc = subprocess.Popen(
            [sys.executable, MAIN_SCRIPT, '--config', cfg, '--session', sess,
             '--user-id', str(uid)],
            cwd=BASE_DIR, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding='utf-8', errors='replace', bufsize=1
        )
        _fwd_procs[uid] = proc
        threading.Thread(target=_log_reader, args=(proc, uid), daemon=True).start()
    return True

def auto_start_fwd(uid: int):
    """فوروارد خودکار پس از داشتن session و API (بدون نیاز به کلیک دستی)."""
    user = db_get_user(uid)
    if not user or not user['is_approved']:
        return
    if not session_on_disk(uid):
        return
    api_id, api_hash = user_api_credentials(uid)
    if not api_id or not api_hash:
        return
    if fwd_status(uid) == 'running':
        return
    start_fwd(uid)

def needs_telethon_login(uid: int) -> bool:
    return not session_on_disk(uid)

def _auto_start_all():
    for u in db_all_users():
        if u['is_approved']:
            ensure_client(u['tg_id'])
            auto_start_fwd(u['tg_id'])

def _fwd_watchdog():
    while True:
        time.sleep(25)
        for u in db_all_users():
            if not u['is_approved']:
                continue
            uid = u['tg_id']
            if not session_on_disk(uid):
                continue
            st = fwd_status(uid)
            if st != 'running':
                auto_start_fwd(uid)

threading.Thread(
    target=lambda: (time.sleep(3), _auto_start_all()),
    daemon=True, name='fwd-boot'
).start()
threading.Thread(target=_fwd_watchdog, daemon=True, name='fwd-watchdog').start()

def stop_fwd(uid: int):
    with _fwd_lock: _stop_fwd(uid)

def stop_all():
    for uid in list(_fwd_procs): _stop_fwd(uid)

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
        try: asyncio.run_coroutine_threadsafe(old.disconnect(), _tg_loop).result(timeout=5)
        except Exception: pass
    _migrate_panel_session(uid)
    client = TelegramClient(user_session_path(uid), int(api_id), api_hash, loop=_tg_loop)
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

def reset_tg_client(uid: int) -> TelegramClient | None:
    old = _tg_clients.pop(uid, None)
    if old:
        try:
            asyncio.run_coroutine_threadsafe(old.disconnect(), _tg_loop).result(timeout=5)
        except Exception:
            pass
    _tg_connected[uid] = False
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
    ensure_client(uid)
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
    was_running = fwd_status(uid) == 'running'
    if was_running or tg_ok(uid):
        auto_start_fwd(uid)
    return jsonify({'ok': True, 'restarted': was_running})

@app.route('/api/status')
@login_required
def get_status():
    uid = cur_uid()
    ensure_client(uid)
    user = db_get_user(uid)
    return jsonify({
        'connected':        tg_ok(uid),
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
    start_fwd(uid)
    return jsonify({'ok': True, 'status': fwd_status(uid)})

@app.route('/api/forwarder/stop', methods=['POST'])
@login_required
def fwd_stop():
    uid = cur_uid()
    stop_fwd(uid)
    return jsonify({'ok': True, 'status': fwd_status(uid)})

@app.route('/api/forwarder/restart', methods=['POST'])
@login_required
def fwd_restart():
    uid = cur_uid()
    start_fwd(uid)
    return jsonify({'ok': True, 'status': fwd_status(uid)})

@app.route('/api/forwarder/logs')
@login_required
def fwd_logs():
    uid = cur_uid()
    n   = int(request.args.get('n', 80))
    return jsonify({'logs': list(_logs(uid))[-n:]})

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
        async for d in c.iter_dialogs(limit=200):
            ent = d.entity
            if not isinstance(ent, Channel):
                continue
            if not (getattr(ent, 'megagroup', False) or getattr(ent, 'gigagroup', False)):
                continue
            tid = str(d.id)
            items.append({
                'id': d.id,
                'title': d.name or tid,
                'is_forum': bool(getattr(ent, 'forum', False)),
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
    cfg = load_user_config(uid)
    for g in cfg.get('groups') or []:
        if str(g.get('telegram_id')) == str(telegram_id):
            return jsonify({'ok': True, 'group': g, 'exists': True})
    g = {
        'id': new_group_id(),
        'title': title,
        'telegram_id': str(telegram_id),
        'origin': 'linked',
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
    title = (request.get_json() or {}).get('title', '').strip()
    if not title:
        return jsonify({'error': 'no_title'}), 400

    async def _create():
        return await c(CreateChannelRequest(
            title=title,
            about='TeleFilter',
            megagroup=True,
            forum=True,
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
        result = _fetch_forum_topics(c, tg_gid)
        return jsonify({'topics': [{'id': t.id, 'title': t.title} for t in result.topics]})
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
