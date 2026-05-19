"""
TeleFilter — پنل مدیریت + مدیریت Bot
یک دستور برای همه چیز:  python panel.py
مرورگر:  http://127.0.0.1:5000
"""
import json
import random
import threading
import asyncio
import subprocess
import sys
import os
import atexit
import webbrowser
from collections import deque
from flask import Flask, render_template, jsonify, request
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError, PhoneCodeInvalidError, PasswordHashInvalidError
from telethon.tl.types import UpdateNewChannelMessage, MessageService

try:
    from telethon.tl.functions.messages import GetForumTopicsRequest, CreateForumTopicRequest
    HAS_FORUM_API = True
except ImportError:
    try:
        from telethon.tl.functions.channels import GetForumTopicsRequest, CreateForumTopicRequest
        HAS_FORUM_API = True
    except ImportError:
        HAS_FORUM_API = False

BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, 'config.json')
MAIN_SCRIPT = os.path.join(BASE_DIR, 'main.py')
app = Flask(__name__)

# ══════════════════════════════════════════════════════════
#  Config helpers
# ══════════════════════════════════════════════════════════
def load_config() -> dict:
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {"api_id": "", "api_hash": "", "target_group_id": "", "topics": []}

def save_config(data: dict):
    with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# ══════════════════════════════════════════════════════════
#  Forwarder (main.py) process management
# ══════════════════════════════════════════════════════════
_fwd_proc : subprocess.Popen | None = None
_fwd_lock  = threading.Lock()
_fwd_logs  : deque[str] = deque(maxlen=300)

def _log_reader(proc: subprocess.Popen):
    """لاگ‌های main.py را در پس‌زمینه می‌خواند."""
    try:
        for line in iter(proc.stdout.readline, ''):
            _fwd_logs.append(line.rstrip())
    except Exception:
        pass

def _fwd_status() -> str:
    if _fwd_proc is None:
        return 'stopped'
    if _fwd_proc.poll() is None:
        return 'running'
    code = _fwd_proc.returncode
    return f'crashed ({code})' if code != 0 else 'stopped'

def _stop_unsafe():
    global _fwd_proc
    if _fwd_proc and _fwd_proc.poll() is None:
        _fwd_logs.append('— stopping forwarder —')
        _fwd_proc.terminate()
        try:
            _fwd_proc.wait(timeout=6)
        except subprocess.TimeoutExpired:
            _fwd_proc.kill()
    _fwd_proc = None

def start_forwarder():
    global _fwd_proc
    with _fwd_lock:
        _stop_unsafe()
        _fwd_logs.append('— starting forwarder —')
        proc = subprocess.Popen(
            [sys.executable, MAIN_SCRIPT],
            cwd=BASE_DIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True, encoding='utf-8', errors='replace',
            bufsize=1
        )
        _fwd_proc = proc
        threading.Thread(target=_log_reader, args=(proc,), daemon=True).start()

def stop_forwarder():
    with _fwd_lock:
        _stop_unsafe()

def restart_forwarder():
    start_forwarder()   # start_forwarder خودش اول stop می‌کند

atexit.register(stop_forwarder)   # وقتی panel بسته می‌شود bot هم متوقف شود

# ══════════════════════════════════════════════════════════
#  Telethon — background loop
# ══════════════════════════════════════════════════════════
TG_CONNECTED = False
_auth = {'phase': 'idle', 'phone': None, 'phone_code_hash': None}

_cfg  = load_config()
_loop = asyncio.new_event_loop()
_tg   = TelegramClient(
    os.path.join(BASE_DIR, 'telefilter_panel_session'),
    _cfg.get('api_id', ''),
    _cfg.get('api_hash', ''),
    loop=_loop
)
threading.Thread(target=_loop.run_forever, daemon=True, name='tg-loop').start()

def tg_run(coro, timeout: int = 30):
    return asyncio.run_coroutine_threadsafe(coro, _loop).result(timeout=timeout)

async def _connect():
    global TG_CONNECTED
    try:
        await _tg.connect()
        TG_CONNECTED = await _tg.is_user_authorized()
        if TG_CONNECTED:
            _auth['phase'] = 'done'
    except Exception as e:
        TG_CONNECTED = False
        print(f"[Telegram] {e}")

try:
    tg_run(_connect(), timeout=15)
    print(f"[Telegram] {'Connected ✓' if TG_CONNECTED else 'Not authorized — login from the panel'}")
except Exception as e:
    print(f"[Telegram] Could not connect: {e}")

# ══════════════════════════════════════════════════════════
#  Flask routes — صفحه اصلی + Config
# ══════════════════════════════════════════════════════════
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/config', methods=['GET'])
def get_config():
    return jsonify(load_config())

@app.route('/api/config', methods=['POST'])
def update_config():
    save_config(request.get_json())
    was_running = _fwd_status() == 'running'
    if was_running:
        restart_forwarder()
    return jsonify({'ok': True, 'restarted': was_running})

@app.route('/api/status')
def get_status():
    return jsonify({
        'connected':    TG_CONNECTED,
        'has_forum_api': HAS_FORUM_API,
        'auth_phase':   _auth['phase'],
        'bot':          _fwd_status(),
    })

# ══════════════════════════════════════════════════════════
#  Flask routes — Forwarder control
# ══════════════════════════════════════════════════════════
@app.route('/api/forwarder/start', methods=['POST'])
def fwd_start():
    start_forwarder()
    return jsonify({'ok': True, 'status': _fwd_status()})

@app.route('/api/forwarder/stop', methods=['POST'])
def fwd_stop():
    stop_forwarder()
    return jsonify({'ok': True, 'status': _fwd_status()})

@app.route('/api/forwarder/restart', methods=['POST'])
def fwd_restart():
    restart_forwarder()
    return jsonify({'ok': True, 'status': _fwd_status()})

@app.route('/api/forwarder/logs')
def fwd_logs():
    n = int(request.args.get('n', 80))
    return jsonify({'logs': list(_fwd_logs)[-n:]})

# ══════════════════════════════════════════════════════════
#  Flask routes — Auth
# ══════════════════════════════════════════════════════════
@app.route('/api/auth/send_code', methods=['POST'])
def auth_send_code():
    phone = (request.get_json() or {}).get('phone', '').strip()
    if not phone:
        return jsonify({'error': 'شماره تلفن وارد نشده'}), 400
    try:
        sent = tg_run(_tg.send_code_request(phone))
        _auth.update(phase='code_sent', phone=phone, phone_code_hash=sent.phone_code_hash)
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/auth/verify_code', methods=['POST'])
def auth_verify_code():
    global TG_CONNECTED
    code = (request.get_json() or {}).get('code', '').strip()
    if not code:
        return jsonify({'error': 'کد وارد نشده'}), 400
    try:
        tg_run(_tg.sign_in(_auth['phone'], code, phone_code_hash=_auth['phone_code_hash']))
        TG_CONNECTED = True
        _auth['phase'] = 'done'
        return jsonify({'ok': True})
    except PhoneCodeInvalidError:
        return jsonify({'error': 'کد اشتباه است'}), 400
    except SessionPasswordNeededError:
        _auth['phase'] = 'need_2fa'
        return jsonify({'ok': False, 'need_2fa': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/auth/verify_2fa', methods=['POST'])
def auth_verify_2fa():
    global TG_CONNECTED
    pw = (request.get_json() or {}).get('password', '')
    try:
        tg_run(_tg.sign_in(password=pw))
        TG_CONNECTED = True
        _auth['phase'] = 'done'
        return jsonify({'ok': True})
    except PasswordHashInvalidError:
        return jsonify({'error': 'رمز اشتباه است'}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ══════════════════════════════════════════════════════════
#  Flask routes — Telegram Topics
# ══════════════════════════════════════════════════════════
@app.route('/api/telegram/topics', methods=['GET'])
def get_tg_topics():
    if not TG_CONNECTED:
        return jsonify({'error': 'not_connected', 'msg': 'ابتدا وارد تلگرام شو'}), 503
    if not HAS_FORUM_API:
        return jsonify({'error': 'no_api', 'msg': 'pip install --upgrade telethon'}), 501
    cfg = load_config()
    try:
        gid = int(cfg['target_group_id'])
    except (KeyError, ValueError, TypeError):
        return jsonify({'error': 'no_group', 'msg': 'target_group_id در تنظیمات وارد نشده'}), 400
    try:
        result = tg_run(_tg(GetForumTopicsRequest(
            peer=gid, offset_date=None, offset_id=0, offset_topic=0, limit=100, q=None
        )))
        return jsonify({'topics': [{'id': t.id, 'title': t.title} for t in result.topics]})
    except Exception as e:
        return jsonify({'error': 'api', 'msg': str(e)}), 500

@app.route('/api/telegram/topics', methods=['POST'])
def create_tg_topic():
    if not TG_CONNECTED:
        return jsonify({'error': 'not_connected', 'msg': 'ابتدا وارد تلگرام شو'}), 503
    if not HAS_FORUM_API:
        return jsonify({'error': 'no_api', 'msg': 'pip install --upgrade telethon'}), 501
    data  = request.get_json() or {}
    title = data.get('title', '').strip()
    if not title:
        return jsonify({'error': 'no_title', 'msg': 'عنوان Topic الزامی است'}), 400
    cfg = load_config()
    try:
        gid = int(cfg['target_group_id'])
    except (KeyError, ValueError, TypeError):
        return jsonify({'error': 'no_group', 'msg': 'target_group_id وارد نشده'}), 400
    try:
        result = tg_run(_tg(CreateForumTopicRequest(
            peer=gid, title=title, random_id=random.randint(1, 2**31)
        )))
        topic_id = None
        for upd in result.updates:
            msg = getattr(upd, 'message', None)
            if isinstance(msg, MessageService):
                topic_id = msg.id
                break
        if topic_id is None:
            for upd in result.updates:
                if hasattr(upd, 'id'):
                    topic_id = upd.id; break
        return jsonify({'ok': True, 'topic_id': topic_id, 'title': title})
    except Exception as e:
        return jsonify({'error': 'api', 'msg': str(e)}), 500

# ══════════════════════════════════════════════════════════
if __name__ == '__main__':
    webbrowser.open('http://127.0.0.1:5000')
    app.run(debug=False, port=5000, threaded=True)
