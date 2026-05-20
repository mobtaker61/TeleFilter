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
from telethon.errors import SessionPasswordNeededError, PhoneCodeInvalidError, PasswordHashInvalidError
from telethon.tl.types import MessageService

try:
    from telethon.tl.functions.messages import GetForumTopicsRequest, CreateForumTopicRequest
    HAS_FORUM_API = True
except ImportError:
    try:
        from telethon.tl.functions.channels import GetForumTopicsRequest, CreateForumTopicRequest
        HAS_FORUM_API = True
    except ImportError:
        HAS_FORUM_API = False

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
    return bool(m.get('api_id') and m.get('api_hash') and m.get('bot_token') and m.get('bot_username'))

# ══════════════════════════════════════════════════════════
#  Flask app  (secret_key از master config می‌آید)
# ══════════════════════════════════════════════════════════
app = Flask(__name__)

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
            is_approved INTEGER DEFAULT 0,
            is_admin    INTEGER DEFAULT 0,
            created_at  TEXT DEFAULT CURRENT_TIMESTAMP
        )''')
        c.commit()

_init_db()

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

def user_session_path(tg_id: int, kind: str) -> str:
    return os.path.join(user_dir(tg_id), f'{kind}.session')

def load_user_config(tg_id: int) -> dict:
    try:
        with open(user_config_path(tg_id), 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        m = load_master()
        return {'api_id': m.get('api_id',''), 'api_hash': m.get('api_hash',''),
                'target_group_id': '', 'topics': []}

def save_user_config(tg_id: int, data: dict):
    # همیشه از api_id/api_hash مشترک استفاده کن
    m = load_master()
    data['api_id']   = m.get('api_id',   data.get('api_id', ''))
    data['api_hash'] = m.get('api_hash', data.get('api_hash', ''))
    with open(user_config_path(tg_id), 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

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

def start_fwd(uid: int):
    with _fwd_lock:
        _stop_fwd(uid)
        cfg  = user_config_path(uid)
        sess = user_session_path(uid, 'telefilter')
        _logs(uid).append('— starting forwarder —')
        proc = subprocess.Popen(
            [sys.executable, MAIN_SCRIPT, '--config', cfg, '--session', sess],
            cwd=BASE_DIR, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding='utf-8', errors='replace', bufsize=1
        )
        _fwd_procs[uid] = proc
        threading.Thread(target=_log_reader, args=(proc, uid), daemon=True).start()

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

threading.Thread(target=_tg_loop.run_forever, daemon=True, name='tg-loop').start()

def tg_run(coro, timeout: int = 30):
    return asyncio.run_coroutine_threadsafe(coro, _tg_loop).result(timeout=timeout)

def _make_client(uid: int) -> TelegramClient | None:
    m = load_master()
    if not m.get('api_id') or not m.get('api_hash'):
        return None
    old = _tg_clients.get(uid)
    if old:
        try: asyncio.run_coroutine_threadsafe(old.disconnect(), _tg_loop).result(timeout=5)
        except Exception: pass
    client = TelegramClient(user_session_path(uid, 'panel'), m['api_id'], m['api_hash'], loop=_tg_loop)
    _tg_clients[uid]   = client
    _tg_connected[uid] = False
    if uid not in _tg_auth:
        _tg_auth[uid] = {'phase': 'idle', 'phone': None, 'phone_code_hash': None}
    return client

async def _connect(uid: int):
    c = _tg_clients.get(uid)
    if not c: return
    try:
        await c.connect()
        ok = await c.is_user_authorized()
        _tg_connected[uid] = ok
        if ok: _tg_auth[uid]['phase'] = 'done'
    except Exception as e:
        _tg_connected[uid] = False
        print(f'[TG:{uid}] {e}')

def ensure_client(uid: int) -> TelegramClient | None:
    if uid not in _tg_clients:
        c = _make_client(uid)
        if c:
            try: tg_run(_connect(uid), timeout=15)
            except Exception: pass
    return _tg_clients.get(uid)

def tg_ok(uid: int) -> bool:
    return _tg_connected.get(uid, False)

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
        return jsonify({'error': 'همه فیلدها الزامی‌اند'}), 400
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
        ensure_client(uid)

    return jsonify({'ok': True, 'approved': bool(user['is_approved'])})

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
    return render_template('index.html', user=user)

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
    if was_running:
        start_fwd(uid)
    return jsonify({'ok': True, 'restarted': was_running})

@app.route('/api/status')
@login_required
def get_status():
    uid = cur_uid()
    ensure_client(uid)
    return jsonify({
        'connected':     tg_ok(uid),
        'has_client':    _tg_clients.get(uid) is not None,
        'has_forum_api': HAS_FORUM_API,
        'auth_phase':    _tg_auth.get(uid, {}).get('phase', 'idle'),
        'bot':           fwd_status(uid),
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
@app.route('/api/auth/send_code', methods=['POST'])
@login_required
def auth_send_code():
    uid = cur_uid()
    c   = ensure_client(uid)
    if not c:
        return jsonify({'error': 'سرور هنوز آماده نشده'}), 503
    phone = (request.get_json() or {}).get('phone', '').strip()
    if not phone:
        return jsonify({'error': 'شماره تلفن وارد نشده'}), 400
    try:
        sent = tg_run(c.send_code_request(phone))
        _tg_auth[uid].update(phase='code_sent', phone=phone,
                             phone_code_hash=sent.phone_code_hash)
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/auth/verify_code', methods=['POST'])
@login_required
def auth_verify_code():
    uid  = cur_uid()
    c    = _tg_clients.get(uid)
    code = (request.get_json() or {}).get('code', '').strip()
    if not code: return jsonify({'error': 'کد وارد نشده'}), 400
    try:
        tg_run(c.sign_in(_tg_auth[uid]['phone'], code,
                         phone_code_hash=_tg_auth[uid]['phone_code_hash']))
        _tg_connected[uid] = True
        _tg_auth[uid]['phase'] = 'done'
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
    pw  = (request.get_json() or {}).get('password', '')
    try:
        tg_run(c.sign_in(password=pw))
        _tg_connected[uid] = True
        _tg_auth[uid]['phase'] = 'done'
        return jsonify({'ok': True})
    except PasswordHashInvalidError:
        return jsonify({'error': 'رمز اشتباه است'}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ══════════════════════════════════════════════════════════
#  Routes — API: Telegram Topics
# ══════════════════════════════════════════════════════════
@app.route('/api/telegram/topics', methods=['GET'])
@login_required
def get_tg_topics():
    uid = cur_uid()
    if not tg_ok(uid):
        return jsonify({'error': 'not_connected', 'msg': 'ابتدا وارد تلگرام شو'}), 503
    if not HAS_FORUM_API:
        return jsonify({'error': 'no_api', 'msg': 'pip install --upgrade telethon'}), 501
    cfg = load_user_config(uid)
    try:
        gid = int(cfg['target_group_id'])
    except (KeyError, ValueError, TypeError):
        return jsonify({'error': 'no_group', 'msg': 'target_group_id وارد نشده'}), 400
    try:
        result = tg_run(_tg_clients[uid](GetForumTopicsRequest(
            peer=gid, offset_date=None, offset_id=0, offset_topic=0, limit=100, q=None
        )))
        return jsonify({'topics': [{'id': t.id, 'title': t.title} for t in result.topics]})
    except Exception as e:
        return jsonify({'error': 'api', 'msg': str(e)}), 500

@app.route('/api/telegram/topics', methods=['POST'])
@login_required
def create_tg_topic():
    uid = cur_uid()
    if not tg_ok(uid):
        return jsonify({'error': 'not_connected', 'msg': 'ابتدا وارد تلگرام شو'}), 503
    if not HAS_FORUM_API:
        return jsonify({'error': 'no_api', 'msg': 'pip install --upgrade telethon'}), 501
    data  = request.get_json() or {}
    title = data.get('title', '').strip()
    if not title:
        return jsonify({'error': 'no_title', 'msg': 'عنوان Topic الزامی است'}), 400
    cfg = load_user_config(uid)
    try:
        gid = int(cfg['target_group_id'])
    except (KeyError, ValueError, TypeError):
        return jsonify({'error': 'no_group', 'msg': 'target_group_id وارد نشده'}), 400
    try:
        result = tg_run(_tg_clients[uid](CreateForumTopicRequest(
            peer=gid, title=title, random_id=random.randint(1, 2**31)
        )))
        topic_id = None
        for upd in result.updates:
            msg = getattr(upd, 'message', None)
            if isinstance(msg, MessageService):
                topic_id = msg.id; break
        if topic_id is None:
            for upd in result.updates:
                if hasattr(upd, 'id'): topic_id = upd.id; break
        return jsonify({'ok': True, 'topic_id': topic_id, 'title': title})
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
