"""
پنل مدیریت TeleFilter
اجرا: python panel.py  →  http://127.0.0.1:5000
"""
import json
import random
import threading
import asyncio
import webbrowser
from flask import Flask, render_template, jsonify, request
from telethon import TelegramClient
from telethon.tl.types import UpdateNewChannelMessage, MessageService

try:
    # در Telethon این توابع در messages هستند نه channels
    from telethon.tl.functions.messages import GetForumTopicsRequest, CreateForumTopicRequest
    HAS_FORUM_API = True
except ImportError:
    try:
        from telethon.tl.functions.channels import GetForumTopicsRequest, CreateForumTopicRequest
        HAS_FORUM_API = True
    except ImportError:
        HAS_FORUM_API = False
        print("[!] Forum Topics API در این نسخه Telethon یافت نشد.")

CONFIG_PATH = 'config.json'
app = Flask(__name__)

# ── Config helpers ───────────────────────────────────────
def load_config() -> dict:
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {"api_id": "", "api_hash": "", "target_group_id": "", "topics": []}

def save_config(data: dict):
    with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# ── Telethon — background thread + event loop ────────────
TG_CONNECTED = False
_cfg = load_config()

_loop = asyncio.new_event_loop()
_tg = TelegramClient(
    'telefilter_session',
    _cfg.get('api_id', ''),
    _cfg.get('api_hash', ''),
    loop=_loop
)

threading.Thread(target=_loop.run_forever, daemon=True, name='tg-loop').start()

def tg_run(coro, timeout: int = 20):
    """یک coroutine تلتون را از thread سینک فلاسک اجرا می‌کند."""
    return asyncio.run_coroutine_threadsafe(coro, _loop).result(timeout=timeout)

async def _connect():
    global TG_CONNECTED
    try:
        await _tg.connect()
        TG_CONNECTED = await _tg.is_user_authorized()
    except Exception as e:
        TG_CONNECTED = False
        print(f"[Telegram] {e}")

try:
    tg_run(_connect(), timeout=15)
    print(f"[Telegram] {'Connected ✓' if TG_CONNECTED else 'Not authorized — run: python helper.py'}")
except Exception as e:
    print(f"[Telegram] Could not connect: {e}")

# ── Flask routes ─────────────────────────────────────────
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/config', methods=['GET'])
def get_config():
    return jsonify(load_config())

@app.route('/api/config', methods=['POST'])
def update_config():
    save_config(request.get_json())
    return jsonify({'ok': True})

@app.route('/api/status')
def get_status():
    return jsonify({'connected': TG_CONNECTED, 'has_forum_api': HAS_FORUM_API})

# ── Telegram: دریافت Topics گروه ─────────────────────────
@app.route('/api/telegram/topics', methods=['GET'])
def get_tg_topics():
    if not TG_CONNECTED:
        return jsonify({'error': 'not_connected',
                        'msg': 'ابتدا python helper.py را اجرا کن تا وارد تلگرام شوی'}), 503
    if not HAS_FORUM_API:
        return jsonify({'error': 'no_api',
                        'msg': 'pip install --upgrade telethon'}), 501

    cfg = load_config()
    try:
        gid = int(cfg['target_group_id'])
    except (KeyError, ValueError, TypeError):
        return jsonify({'error': 'no_group',
                        'msg': 'target_group_id در تنظیمات وارد نشده'}), 400
    try:
        result = tg_run(_tg(GetForumTopicsRequest(
            channel=gid, offset_date=0, offset_id=0,
            offset_topic=0, limit=100, q=''
        )))
        topics = [{'id': t.id, 'title': t.title} for t in result.topics]
        return jsonify({'topics': topics})
    except Exception as e:
        return jsonify({'error': 'api', 'msg': str(e)}), 500

# ── Telegram: ساخت Topic جدید در گروه ────────────────────
@app.route('/api/telegram/topics', methods=['POST'])
def create_tg_topic():
    if not TG_CONNECTED:
        return jsonify({'error': 'not_connected', 'msg': 'ابتدا python helper.py را اجرا کن'}), 503
    if not HAS_FORUM_API:
        return jsonify({'error': 'no_api', 'msg': 'pip install --upgrade telethon'}), 501

    data = request.get_json()
    title = (data.get('title') or '').strip()
    if not title:
        return jsonify({'error': 'no_title', 'msg': 'عنوان Topic الزامی است'}), 400

    cfg = load_config()
    try:
        gid = int(cfg['target_group_id'])
    except (KeyError, ValueError, TypeError):
        return jsonify({'error': 'no_group', 'msg': 'target_group_id وارد نشده'}), 400
    try:
        result = tg_run(_tg(CreateForumTopicRequest(
            channel=gid,
            title=title,
            random_id=random.randint(1, 2**31)
        )))
        # topic_id = شناسه پیام service که تلگرام برمی‌گرداند
        topic_id = None
        for upd in result.updates:
            msg = getattr(upd, 'message', None)
            if isinstance(msg, MessageService):
                topic_id = msg.id
                break
        # fallback
        if topic_id is None:
            for upd in result.updates:
                if hasattr(upd, 'id'):
                    topic_id = upd.id
                    break

        return jsonify({'ok': True, 'topic_id': topic_id, 'title': title})
    except Exception as e:
        return jsonify({'error': 'api', 'msg': str(e)}), 500

if __name__ == '__main__':
    webbrowser.open('http://127.0.0.1:5000')
    app.run(debug=False, port=5000, threaded=True)
