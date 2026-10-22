"""
In-process forwarder.

به جای اجرای subprocess جدا (main.py) که نیاز به session دوم و کپی فایل SQLite
داشت و باعث database is locked / readonly می‌شد، همان Telethon client پنل
رویدادهای NewMessage را مستقیم می‌گیرد و فوروارد می‌کند.
"""
from __future__ import annotations

import asyncio
import html
import io
import logging
import random
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from telethon import events
from telethon.errors import MessageNotModifiedError, MessageIdInvalidError, FloodWaitError
from telethon.tl.functions.messages import ForwardMessagesRequest
from telethon.utils import get_peer_id

from config_util import (
    normalize_config, record_forward,
    parse_value, record_rate, get_rates, get_rates_smart, latest_rate,
    get_last_chart_msg, save_last_chart_msg,
    get_forum_rate_msg, save_forum_rate_msg, clear_forum_rate_msg,
    clean_text as _clean_text,
    aggregate_rate_daily, list_days_in_range,
    norm_check_interval,
)
# charts را lazy لود می‌کنیم تا اگر matplotlib در سرور دچار خطا شد،
# فوروارد عادی همچنان کار کند.
try:
    from charts import render_rate_chart, is_available as charts_available
except Exception as _e:  # noqa: BLE001
    render_rate_chart = None  # type: ignore[assignment]

    def charts_available() -> bool:  # type: ignore[no-redef]
        return False
    logging.getLogger('telefilter.forwarder').error("charts module load failed: %s", _e)

# apex chart renderer (با Playwright) — اختیاری، fallback به matplotlib
try:
    from apex_chart import render_chart_png as _render_apex
except Exception as _e:  # noqa: BLE001
    _render_apex = None  # type: ignore[assignment]
    logging.getLogger('telefilter.forwarder').warning("apex_chart load failed: %s", _e)

logger = logging.getLogger('telefilter.forwarder')
GENERAL_TOPIC_ID = 1

# per-user state
_routes: dict[int, dict] = {}      # uid -> {'source_map', 'targets', 'forum'}
_handlers: dict[int, Any] = {}     # uid -> registered event handler callable
_last_error: dict[int, str] = {}   # uid -> last error string
_forum_rate_locks: dict[tuple, asyncio.Lock] = {}  # (uid,gid,tid) -> lock
_chart_locks: dict[tuple, asyncio.Lock] = {}
# شمارنده‌ی آخرین درخواست per topic — درخواست‌های قدیمی‌تر پشت lock کنار می‌روند (latest-wins)
_chart_seq: dict[tuple, int] = {}
_forum_rate_seq: dict[tuple, int] = {}

# صف پردازش و حالت زمان‌بندی‌شده
INTERVAL_TICK_SECONDS = 10
_tasks: dict[int, set] = {}                    # uid -> task های پردازش در صف/در حال اجرا
_interval_tasks: dict[int, asyncio.Task] = {}  # uid -> حلقه‌ی interval
_buffer: dict[int, dict] = {}                  # uid -> {(gid, tid): {source_peer: آخرین item}}
_last_run: dict[tuple, float] = {}             # (uid, gid, tid) -> زمان آخرین پردازش interval
_cursor: dict[tuple, int] = {}                 # (uid, gid, tid, source_peer) -> آخرین message_id پردازش‌شده


def _topic_lock(store: dict, uid: int, gid: str, tid: int) -> asyncio.Lock:
    key = (uid, gid, int(tid))
    lock = store.get(key)
    if lock is None:
        lock = asyncio.Lock()
        store[key] = lock
    return lock


def _forum_rate_lock(uid: int, gid: str, tid: int) -> asyncio.Lock:
    return _topic_lock(_forum_rate_locks, uid, gid, tid)


def _chart_lock(uid: int, gid: str, tid: int) -> asyncio.Lock:
    return _topic_lock(_chart_locks, uid, gid, tid)


def _msg_missing(exc: Exception) -> bool:
    if isinstance(exc, MessageIdInvalidError):
        return True
    s = str(exc).lower()
    return any(x in s for x in (
        'message_id_invalid', 'message not found', 'message to edit not found',
        'could not find', 'specified message id',
    ))


def _flood_wait_seconds(exc: Exception) -> int | None:
    if isinstance(exc, FloodWaitError):
        return int(exc.seconds)
    import re
    m = re.search(r'wait of (\d+) second', str(exc), re.I)
    return int(m.group(1)) if m else None


def _peer_keys(entity) -> set[int]:
    keys: set[int] = set()
    try:
        keys.add(int(get_peer_id(entity)))
    except Exception:
        pass
    cid = getattr(entity, 'id', None)
    if cid is None:
        return keys
    try:
        cid = int(cid)
    except (TypeError, ValueError):
        return keys
    keys.add(cid)
    keys.add(-cid)
    if cid > 0:
        keys.add(int(f'-100{cid}'))
    return keys


async def _resolve(client, identifier):
    try:
        return await client.get_entity(identifier)
    except (ValueError, KeyError):
        pass
    candidates: set[int] = set()
    try:
        raw = int(str(identifier).strip())
        candidates = {raw, abs(raw)}
        s = str(abs(raw))
        if s.startswith('100'):
            candidates.add(int(s[3:]))
    except (ValueError, TypeError):
        pass
    async for d in client.iter_dialogs():
        if candidates and d.id in candidates:
            return d.entity
        if not candidates and str(identifier) in (
            d.name or '', getattr(d.entity, 'username', '') or ''
        ):
            return d.entity
    raise ValueError(f"cannot resolve entity {identifier!r}")


async def build_routes(uid: int, client, cfg: dict) -> dict:
    cfg = normalize_config(cfg)
    source_map: dict[int, list] = {}
    targets: dict[str, Any] = {}
    forum: dict[str, bool] = {}

    for g in cfg.get('groups') or []:
        gid = g.get('id', '')
        tg_id = g.get('telegram_id', '')
        if not gid or not tg_id:
            continue
        try:
            ent = await _resolve(client, int(tg_id))
        except Exception as e:
            logger.error("[%s] target %s resolve failed: %s", uid, tg_id, e)
            continue
        targets[gid] = ent
        is_forum = g.get('is_forum')
        if is_forum is None:
            is_forum = bool(getattr(ent, 'forum', False))
        forum[gid] = bool(is_forum)

        topics = g.get('topics') or []
        if not topics and not is_forum:
            topics = [{'topic_id': 0, 'name': 'main', 'sources': []}]

        for t in topics:
            tid_raw = t.get('topic_id')
            if tid_raw is None:
                continue
            try:
                tid = int(tid_raw)
            except (TypeError, ValueError):
                continue
            chart_enabled = bool(t.get('chart_enabled', False))
            forward_enabled = bool(t.get('forward_enabled', True))
            chart_message_enabled = bool(t.get('chart_message_enabled', True))
            forum_rate_enabled = bool(t.get('forum_rate_enabled', False))
            chart_label = str(t.get('chart_label') or t.get('name') or '')
            skip_unchanged = bool(t.get('skip_unchanged', True))
            chart_days = int(t.get('chart_days') or 7)
            interval_minutes = norm_check_interval(t.get('check_interval'))
            try:
                max_change_pct = float(t.get('max_change_percent', 10) or 0)
            except (TypeError, ValueError):
                max_change_pct = 10.0
            for s in t.get('sources') or []:
                # سورس‌های غیرفعال کاملاً نادیده گرفته می‌شوند
                if s.get('enabled') is False:
                    continue
                raw_chat = s.get('chat')
                chat = raw_chat.strip() if isinstance(raw_chat, str) else raw_chat
                if not chat:
                    continue
                filters = s.get('filters') or []
                value_regex = str(s.get('value_regex') or '')
                try:
                    cv = chat
                    try:
                        cv = int(chat)
                    except (TypeError, ValueError):
                        pass
                    src_ent = await _resolve(client, cv)
                    route = {
                        'gid': gid,
                        'topic_id': tid,
                        'filters': filters,
                        'is_forum': bool(is_forum),
                        'chart_enabled': chart_enabled,
                        'forward_enabled': forward_enabled,
                        'chart_message_enabled': chart_message_enabled,
                        'forum_rate_enabled': forum_rate_enabled,
                        'chart_label': chart_label,
                        'value_regex': value_regex,
                        'skip_unchanged': skip_unchanged,
                        'chart_days': chart_days,
                        'max_change_percent': max_change_pct,
                        'clean_text': bool(s.get('clean_text', False)),
                        'interval_minutes': interval_minutes,
                        'source_chat': chat,
                        'source_peer': int(get_peer_id(src_ent)),
                        'source_entity': src_ent,
                    }
                    for k in _peer_keys(src_ent):
                        source_map.setdefault(k, []).append(route)
                except Exception as e:
                    logger.warning("[%s] source %r resolve failed: %s", uid, chat, e)

    routes = {'source_map': source_map, 'targets': targets, 'forum': forum}
    _routes[uid] = routes
    n = sum(len(v) for v in source_map.values())
    logger.info(
        "[%s] routes ready: %d routes across %d targets (peer ids: %s)",
        uid, n, len(targets), sorted(source_map.keys()),
    )
    return routes


def clear_routes(uid: int):
    _routes.pop(uid, None)


_SAFE_NAME_RE = re.compile(r'[^A-Za-z0-9_\-]+')


def _named_png(label: str, png_bytes: bytes) -> io.BytesIO:
    """
    PNG bytes را در یک BytesIO با اسم/پسوند مناسب می‌پیچد تا Telethon آن را
    به‌عنوان عکس (photo) ارسال کند، نه document.
    """
    safe = _SAFE_NAME_RE.sub('_', label or 'chart').strip('_')[:40] or 'chart'
    ts = time.strftime('%Y%m%d_%H%M%S')
    buf = io.BytesIO(png_bytes)
    buf.name = f"{safe}_{ts}.png"
    return buf


def _matches(text: str, filters: list) -> bool:
    if not filters:
        return True
    for r in filters:
        if isinstance(r, str):
            r = [r]
        if all(p.lower() in text for p in r):
            return True
    return False


def _values_equal(a: float | None, b: float | None) -> bool:
    """مقایسه‌ی دو float با tolerance بسیار کم (برای رفع خطای floating-point)."""
    if a is None or b is None:
        return False
    return abs(a - b) < 1e-9


def _fmt_value(v: float) -> str:
    """فرمت‌بندی عدد با جداکننده‌ی هزارگان."""
    if v == int(v):
        return f"{int(v):,}"
    return f"{v:,.4f}".rstrip('0').rstrip('.')


def _format_change(current: float, previous: float | None) -> str:
    """
    رشته‌ی تغییر برای caption:
      🟢 +1,500 (+2.50%)
      🔴 -800 (-1.20%)
      ⚪ بدون تغییر
      🆕 (بدون قبلی)
    """
    if previous is None:
        return '🆕 اولین نرخ'
    diff = current - previous
    if abs(diff) < 1e-9:
        return '⚪ بدون تغییر'
    if previous == 0:
        sign = '+' if diff > 0 else ''
        emoji = '🔝' if diff > 0 else '🔻'
        return f'{emoji} {sign}{_fmt_value(diff)}'
    pct = (diff / abs(previous)) * 100.0
    sign = '+' if diff > 0 else ''
    emoji = '🔝' if diff > 0 else '🔻'
    return f'{emoji} {sign}{_fmt_value(diff)} ({sign}{pct:.2f}%)'


LOCAL_TZ = timezone(timedelta(hours=4))   # Asia/Dubai — هم‌راستا با rate_daily


_FA_DIGITS = str.maketrans('0123456789', '۰۱۲۳۴۵۶۷۸۹')


def _to_jalali(gy: int, gm: int, gd: int) -> tuple[int, int, int]:
    """تبدیل میلادی به شمسی (الگوریتم استاندارد، بدون وابستگی)."""
    g_d_m = (0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334)
    gy2 = gy + 1 if gm > 2 else gy
    days = (355666 + 365 * gy + (gy2 + 3) // 4 - (gy2 + 99) // 100
            + (gy2 + 399) // 400 + gd + g_d_m[gm - 1])
    jy = -1595 + 33 * (days // 12053)
    days %= 12053
    jy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        jy += (days - 1) // 365
        days = (days - 1) % 365
    if days < 186:
        return jy, 1 + days // 31, 1 + days % 31
    return jy, 7 + (days - 186) // 30, 1 + (days - 186) % 30


def _forum_rate_text(route: dict, value: float, when: datetime | None = None) -> str:
    """متن HTML پیام نرخ: نرخ bold + تاریخ شمسی/میلادی و ساعت پیام سورس (وقت دبی)."""
    label = route.get('chart_label') or route.get('name') or f"Topic {route.get('topic_id')}"
    if when is None:
        when = datetime.now(timezone.utc)
    elif when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    local = when.astimezone(LOCAL_TZ)
    jy, jm, jd = _to_jalali(local.year, local.month, local.day)
    return (
        f"آخرین نرخ {html.escape(label)}:\n"
        f"<b>{_fmt_value(value)}</b>\n"
        f"📅 {f'{jy}/{jm:02d}/{jd:02d}'.translate(_FA_DIGITS)}  ({local:%Y-%m-%d})\n"
        f"🕒 {local:%H:%M}"
    )


async def _update_forum_rate_message(uid: int, client, route: dict, target, value: float,
                                     when: datetime | None = None):
    """یک پیام ثابت برای نرخ این تاپیک در General topic ایجاد یا ویرایش می‌کند."""
    gid = route['gid']
    tid = route['topic_id']
    text = _forum_rate_text(route, value, when)
    key = (uid, gid, int(tid))
    seq = _forum_rate_seq.get(key, 0) + 1
    _forum_rate_seq[key] = seq

    async with _forum_rate_lock(uid, gid, tid):
        # اگر در زمان انتظار برای lock مقدار جدیدتری رسیده، این ویرایش قدیمی است
        if _forum_rate_seq.get(key) != seq:
            logger.debug("[%s] forum rate skipped stale value topic=%s value=%s", uid, tid, value)
            return
        old_msg = get_forum_rate_msg(uid, gid, tid)

        if old_msg:
            try:
                await client.edit_message(target, int(old_msg), text, parse_mode='html')
                logger.info("[%s] forum rate updated topic=%s msg=%s", uid, tid, old_msg)
                return
            except MessageNotModifiedError:
                logger.debug("[%s] forum rate unchanged topic=%s msg=%s", uid, tid, old_msg)
                return
            except Exception as e:
                fw = _flood_wait_seconds(e)
                if fw:
                    logger.warning(
                        "[%s] forum rate flood wait %ss — skip edit topic=%s msg=%s",
                        uid, fw, tid, old_msg,
                    )
                    return
                if _msg_missing(e):
                    logger.warning("[%s] forum rate msg=%s missing, will recreate: %s", uid, old_msg, e)
                    clear_forum_rate_msg(uid, gid, tid)
                else:
                    logger.warning("[%s] forum rate edit failed topic=%s msg=%s: %s", uid, tid, old_msg, e)
                    return

        try:
            sent = await client.send_message(target, text, reply_to=GENERAL_TOPIC_ID,
                                             parse_mode='html')
            if sent and hasattr(sent, 'id'):
                save_forum_rate_msg(uid, gid, tid, int(sent.id))
                logger.info("[%s] forum rate created topic=%s msg=%s", uid, tid, sent.id)
            else:
                logger.warning("[%s] forum rate sent but no id returned: %r", uid, sent)
        except Exception as e:
            logger.error("[%s] forum rate send failed topic=%s: %s", uid, tid, e, exc_info=True)


async def _render_chart_png(uid: int, route: dict, gid: str, tid: int) -> bytes | None:
    """رندر تصویر چارت: ابتدا ApexCharts (Playwright)، در صورت شکست matplotlib."""
    days = int(route.get('chart_days') or 7)
    title = route.get('chart_label') or f"Topic {tid}"
    png: bytes | None = None

    # ابتدا تلاش با ApexCharts (Playwright headless chromium)
    if _render_apex is not None:
        try:
            data = get_rates_smart(uid, gid, tid, since_hours=24 * days)
            png = await _render_apex(
                data['rates'], title=title, mode=data['mode'], days=days,
            )
            if png:
                logger.info("[%s] chart: rendered via apex (mode=%s, points=%d)",
                            uid, data['mode'], data['count'])
        except Exception as e:
            logger.warning("[%s] apex render failed, falling back to matplotlib: %s", uid, e)
            png = None

    # fallback به matplotlib اگر apex در دسترس نباشد یا ناموفق
    if png is None:
        if render_rate_chart is None or not charts_available():
            logger.warning(
                "[%s] chart: نه ApexCharts نه matplotlib در دسترس است — تصویر ارسال نشد. "
                "روی سرور این را اجرا کنید: pip install playwright && "
                "playwright install --with-deps chromium   "
                "(یا) pip install matplotlib && sudo apt-get install -y libgl1 libglib2.0-0",
                uid,
            )
            return None
        try:
            rates = get_rates(uid, gid, tid, since_hours=24 * days)
            png = render_rate_chart(
                rates,
                title=title,
                y_label=route.get('chart_label') or '',
            )
        except Exception as e:
            logger.error("[%s] chart render failed: %s", uid, e, exc_info=True)
            return None
    if not png:
        logger.warning("[%s] chart render returned empty", uid)
        return None
    return png


async def _process_chart(
    uid: int, client, route: dict, target, raw_text: str,
    pre_parsed: tuple[float | None, str | None] | None = None,
    previous_value: float | None = None,
    record_value: bool = True,
):
    """
    اگر برای این سورس chart فعال است:
      1) عدد را با regex استخراج کن (اگر pre_parsed نداده شده)
      2) در DB ذخیره کن
      3) نمودار را رندر و ارسال کن؛ پس از موفقیت، پیام قبلی را حذف کن
    اگر در حین انتظار برای lock مقدار جدیدتری برای همین تاپیک برسد، این فراخوانی
    بدون رندر کنار می‌رود (فقط آخرین مقدار ارسال می‌شود → صف تشکیل نمی‌شود).
    """
    gid = route['gid']
    tid = route['topic_id']
    if pre_parsed is not None:
        value, raw_match = pre_parsed
    else:
        value_regex = route.get('value_regex') or None
        text_for_parse = _clean_text(raw_text) if route.get('clean_text') else raw_text
        value, raw_match = parse_value(text_for_parse, value_regex)
    if value is None:
        logger.warning(
            "[%s] chart: regex match failed topic=%s regex=%r sample=%r",
            uid, tid, route.get('value_regex'), (raw_text or '')[:120],
        )
        return
    if record_value:
        record_rate(uid, gid, tid, value, raw_match or '', None)
        logger.info("[%s] chart: value=%s recorded topic=%s", uid, value, tid)

    key = (uid, gid, int(tid))
    seq = _chart_seq.get(key, 0) + 1
    _chart_seq[key] = seq

    async with _chart_lock(uid, gid, tid):
        if _chart_seq.get(key) != seq:
            logger.info("[%s] chart: skipped stale value=%s topic=%s (newer pending)", uid, value, tid)
            return
        png = await _render_chart_png(uid, route, gid, tid)
        if not png:
            return

        last_str = _fmt_value(value)
        change_line = _format_change(value, previous_value)
        caption = (
            f"📊 {route.get('chart_label') or ''}\n"
            f"آخرین: {last_str}\n"
            f"{change_line}"
        ).strip()
        named = _named_png(route.get('chart_label') or f'topic_{tid}', png)

        old_msg = get_last_chart_msg(uid, gid, tid)
        # تلگرام روی EditMessageRequest برای تصویر محدودیت شدید دارد (FloodWait).
        # الگوی امن: ابتدا پیام جدید بفرست، سپس قبلی را حذف کن.
        try:
            kwargs = {'caption': caption, 'force_document': False}
            if route.get('is_forum') and tid and tid > 0:
                kwargs['reply_to'] = int(tid)
            sent = await client.send_file(target, file=named, **kwargs)
            if not sent or not hasattr(sent, 'id'):
                logger.warning("[%s] chart sent but no id returned: %r", uid, sent)
                return
            new_id = int(sent.id)
            save_last_chart_msg(uid, gid, tid, new_id)
            logger.info("[%s] chart sent topic=%s msg=%s value=%s", uid, tid, new_id, value)
            if old_msg and int(old_msg) != new_id:
                try:
                    await client.delete_messages(target, [int(old_msg)])
                    logger.info("[%s] chart: removed previous msg=%s", uid, old_msg)
                except Exception as de:
                    fw = _flood_wait_seconds(de)
                    if fw:
                        logger.warning(
                            "[%s] chart delete flood wait %ss — old msg=%s kept in chat",
                            uid, fw, old_msg,
                        )
                    else:
                        logger.warning("[%s] chart delete old msg=%s failed: %s", uid, old_msg, de)
        except Exception as e:
            fw = _flood_wait_seconds(e)
            if fw:
                logger.warning("[%s] chart send flood wait %ss topic=%s", uid, fw, tid)
            else:
                logger.error("[%s] chart send failed topic=%s: %s", uid, tid, e, exc_info=True)


# ══════════════════════════════════════════════════════════
#  پردازش پیام‌ها: لحظه‌ای / زمان‌بندی‌شده (interval) / ریفرش
# ══════════════════════════════════════════════════════════
def _spawn(uid: int, coro) -> asyncio.Task:
    """پردازش در task جدا تا handler تلگرام بلاک نشود و صف قابل لغو باشد."""
    task = asyncio.get_running_loop().create_task(coro)
    bucket = _tasks.setdefault(uid, set())
    bucket.add(task)
    task.add_done_callback(bucket.discard)
    return task


def _cancel_task(task) -> None:
    if task is not None and not task.done():
        # thread-safe: uninstall از thread های Flask هم صدا زده می‌شود
        task.get_loop().call_soon_threadsafe(task.cancel)


def cancel_pending(uid: int) -> int:
    """
    لغو همه‌ی پردازش‌های در صف/در حال اجرا و خالی کردن بافر interval.
    خروجی: تعداد موارد حذف‌شده (task + پیام بافرشده).
    """
    n = 0
    for t in _tasks.pop(uid, set()):
        if not t.done():
            _cancel_task(t)
            n += 1
    buf = _buffer.pop(uid, None) or {}
    n += sum(len(v) for v in buf.values())
    for k in [k for k in _last_run if k[0] == uid]:
        _last_run.pop(k, None)
    return n


async def _process_item(uid: int, client, route: dict, target, item: dict,
                        *, send_chart: bool = True) -> float | None:
    """
    پایپ‌لاین کامل یک پیام برای یک route:
    اعتبارسنجی نرخ → ثبت → آپدیت تالار → فوروارد → (اختیاری) چارت.
    خروجی: مقدار ثبت‌شده اگر پیام کامل پردازش شد، وگرنه None.
    """
    gid = route['gid']
    tid = route['topic_id']
    peer_id = item['peer_id']
    raw_text = item['raw_text']
    ckey = (uid, gid, tid, route.get('source_peer') or peer_id)
    _cursor[ckey] = max(_cursor.get(ckey, 0), int(item['msg_id']))

    use_topic = route.get('is_forum') and tid > 0
    wants_forward = route.get('forward_enabled', True)
    wants_chart_message = bool(route.get('chart_enabled')) and route.get('chart_message_enabled', True)
    wants_forum_rate = (
        bool(route.get('chart_enabled'))
        and bool(route.get('is_forum'))
        and bool(route.get('forum_rate_enabled'))
    )
    if (wants_forward or wants_chart_message or wants_forum_rate) and not target:
        logger.warning("[%s] no target for group=%s", uid, gid)
        return None

    # ── چک‌های اعتبارسنجی نرخ ──
    # اگر چارت فعال است، عدد را یک بار parse می‌کنیم (cache pre_parsed)
    # و سپس دو چک می‌کنیم:
    #   ۱) skip_unchanged: مقدار == آخرین → کامل skip (نه فوروارد، نه چارت)
    #   ۲) max_change_percent: تغییر بیش از حد → کامل skip (outlier ضد data corruption)
    pre_parsed = None
    prev_value_for_caption: float | None = None
    parsed_value: float | None = None
    parsed_raw_match: str | None = None
    if route.get('chart_enabled') and raw_text:
        value_regex = route.get('value_regex') or None
        text_for_parse = _clean_text(raw_text) if route.get('clean_text') else raw_text
        v, raw_match = parse_value(text_for_parse, value_regex)
        if v is not None:
            pre_parsed = (v, raw_match)
            parsed_value = v
            parsed_raw_match = raw_match
            last = latest_rate(uid, gid, tid)
            last_v = float(last.get('value') or 0) if last else None
            prev_value_for_caption = last_v

            # چک ۱: مقدار تکراری
            if route.get('skip_unchanged', True) and last_v is not None and _values_equal(last_v, v):
                logger.info(
                    "[%s] skip unchanged: chat=%s topic=%s value=%s (= last)",
                    uid, peer_id, tid, v,
                )
                return None

            # چک ۲: outlier (تغییر بیش از max_change_percent)
            max_pct = float(route.get('max_change_percent') or 0)
            if max_pct > 0 and last_v is not None and last_v != 0:
                change_pct = abs(v - last_v) / abs(last_v) * 100.0
                if change_pct > max_pct:
                    direction = '↑' if v > last_v else '↓'
                    logger.warning(
                        "[%s] skip outlier: chat=%s topic=%s value=%s %s %.2f%% "
                        "(last=%s, threshold=%.1f%%)",
                        uid, peer_id, tid, v, direction, change_pct, last_v, max_pct,
                    )
                    return None

    if route.get('chart_enabled') and parsed_value is not None:
        inserted = record_rate(
            uid, gid, tid, parsed_value,
            parsed_raw_match or '', None,
        )
        logger.info(
            "[%s] chart: value=%s %s topic=%s",
            uid, parsed_value, "recorded" if inserted else "duplicate/ignored", tid,
        )
        if wants_forum_rate:
            await _update_forum_rate_message(uid, client, route, target, parsed_value,
                                             item.get('date'))

    if wants_forward:
        try:
            kwargs = {
                'from_peer': item['chat'],
                'id': [item['msg_id']],
                'to_peer': target,
                'random_id': [random.randint(1, 2**63 - 1)],
            }
            if use_topic:
                kwargs['top_msg_id'] = tid
            await client(ForwardMessagesRequest(**kwargs))
            record_forward(uid, gid, tid if use_topic else 0, peer_id)
            logger.info(
                "[%s] forwarded chat=%s msg=%s → group=%s topic=%s",
                uid, peer_id, item['msg_id'], gid, tid if use_topic else '-',
            )
        except Exception as e:
            err = f"forward fail (chat={peer_id} group={gid} topic={tid}): {e}"
            _last_error[uid] = err
            logger.error("[%s] %s", uid, err)
            return None
    else:
        logger.info("[%s] forward disabled: chat=%s topic=%s", uid, peer_id, tid)

    if send_chart and wants_chart_message and raw_text:
        try:
            await _process_chart(
                uid, client, route, target, raw_text,
                pre_parsed=pre_parsed,
                previous_value=prev_value_for_caption,
                record_value=False,
            )
        except Exception as e:
            logger.error("[%s] chart pipeline failed: %s", uid, e)
    return parsed_value


async def _run_immediate(uid: int, client, items: list[dict]):
    """حالت لحظه‌ای: هر پیام منطبق، کامل پردازش می‌شود."""
    targets = (_routes.get(uid) or {}).get('targets') or {}
    for item in items:
        route = item['route']
        try:
            await _process_item(uid, client, route, targets.get(route['gid']), item)
        except Exception as e:
            logger.error("[%s] process failed topic=%s: %s", uid, route.get('topic_id'), e, exc_info=True)


async def _run_topic_batch(uid: int, client, items: list[dict], reason: str):
    """
    پردازش دسته‌ای آخرین پیام سورس‌های یک تاپیک (interval / refresh).
    هر پیام ثبت/فوروارد می‌شود ولی چارت فقط یک بار، برای جدیدترین مقدار، ارسال می‌شود.
    """
    if not items:
        return
    items = sorted(items, key=lambda it: it['date'].timestamp() if it.get('date') else 0)
    route0 = items[0]['route']
    gid, tid = route0['gid'], route0['topic_id']
    if route0.get('interval_minutes'):
        _last_run[(uid, gid, tid)] = time.monotonic()
    target = ((_routes.get(uid) or {}).get('targets') or {}).get(gid)
    prev = latest_rate(uid, gid, tid) if route0.get('chart_enabled') else None
    prev_v = float(prev['value']) if prev else None

    last: tuple[dict, float] | None = None
    for it in items:
        try:
            v = await _process_item(uid, client, it['route'], target, it, send_chart=False)
        except Exception as e:
            logger.error("[%s] %s: process failed topic=%s: %s", uid, reason, tid, e, exc_info=True)
            continue
        if v is not None:
            last = (it, v)

    logger.info("[%s] %s: topic=%s processed %d latest message(s)", uid, reason, tid, len(items))
    if last and route0.get('chart_enabled') and route0.get('chart_message_enabled', True):
        it, v = last
        try:
            await _process_chart(
                uid, client, it['route'], target, it['raw_text'],
                pre_parsed=(v, None), previous_value=prev_v, record_value=False,
            )
        except Exception as e:
            logger.error("[%s] chart pipeline failed: %s", uid, e)


async def _interval_loop(uid: int, client):
    """هر چند ثانیه بافر تاپیک‌های زمان‌بندی‌شده را چک و موارد سررسیده را پردازش می‌کند."""
    while True:
        await asyncio.sleep(INTERVAL_TICK_SECONDS)
        buf = _buffer.get(uid)
        if not buf:
            continue
        now = time.monotonic()
        for key in list(buf.keys()):
            items = buf.get(key)
            if not items:
                continue
            interval = int(next(iter(items.values()))['route'].get('interval_minutes') or 0)
            last_run = _last_run.get((uid,) + key)
            if interval and last_run is not None and now - last_run < interval * 60:
                continue
            buf.pop(key, None)
            _last_run[(uid,) + key] = now
            _spawn(uid, _run_topic_batch(uid, client, list(items.values()), 'interval'))


async def catch_up_latest(uid: int, client, scan_limit: int = 30) -> dict:
    """
    برای هر سورس، آخرین پیامِ منطبق با فیلترها را واکشی و پردازش می‌کند.
    پیام‌هایی که قبلاً پردازش شده‌اند (بر اساس message_id) رد می‌شوند.
    """
    data = _routes.get(uid)
    if not data:
        return {'topics': 0, 'messages': 0}
    batches: dict[tuple, list] = {}
    seen: set[int] = set()
    for entries in list(data['source_map'].values()):
        for route in entries:
            if id(route) in seen:
                continue
            seen.add(id(route))
            ent = route.get('source_entity')
            if ent is None:
                continue
            try:
                msgs = await client.get_messages(ent, limit=scan_limit)
            except Exception as e:
                logger.warning("[%s] refresh: fetch %r failed: %s", uid, route.get('source_chat'), e)
                continue
            filters = route.get('filters') or []
            pick = None
            for m in msgs or []:  # جدیدترین اول
                if getattr(m, 'action', None) is not None:
                    continue
                if _matches((m.text or m.message or '').lower(), filters):
                    pick = m
                    break
            if pick is None:
                continue
            peer = route.get('source_peer') or 0
            if int(pick.id) <= _cursor.get((uid, route['gid'], route['topic_id'], peer), 0):
                continue
            batches.setdefault((route['gid'], route['topic_id']), []).append({
                'route': route, 'chat': ent, 'msg_id': int(pick.id), 'peer_id': peer,
                'raw_text': pick.text or pick.message or '', 'date': pick.date,
            })

    n = 0
    for items in batches.values():
        await _run_topic_batch(uid, client, items, 'refresh')
        n += len(items)
    logger.info("[%s] refresh: caught up %d latest message(s) across %d topic(s)", uid, n, len(batches))
    return {'topics': len(batches), 'messages': n}


def install_handler(uid: int, client):
    """نصب یا جایگزینی NewMessage handler و حلقه‌ی interval برای کاربر."""
    old = _handlers.get(uid)
    if old is not None:
        try:
            client.remove_event_handler(old)
        except Exception:
            pass
        _handlers.pop(uid, None)
    _cancel_task(_interval_tasks.pop(uid, None))

    async def _handler(event):
        data = _routes.get(uid)
        if not data:
            return
        source_map = data['source_map']
        if not source_map:
            return

        try:
            chat = await event.get_chat()
            peer_id = int(get_peer_id(chat))
        except Exception:
            peer_id = int(event.chat_id) if event.chat_id else 0
        lookup = {peer_id, int(event.chat_id) if event.chat_id else 0}

        entries = None
        for k in lookup:
            if k and k in source_map:
                entries = source_map[k]
                break
        if not entries:
            return

        raw_text = (event.message.text or event.message.message or '')
        text_lower = raw_text.lower()

        immediate: list[dict] = []
        for route in entries:
            if not _matches(text_lower, route.get('filters') or []):
                continue
            item = {
                'route': route, 'chat': event.chat_id, 'msg_id': int(event.message.id),
                'peer_id': peer_id, 'raw_text': raw_text, 'date': event.message.date,
            }
            if route.get('interval_minutes'):
                # حالت زمان‌بندی‌شده: فقط آخرین پیام هر سورس نگه داشته می‌شود
                key = (route['gid'], route['topic_id'])
                _buffer.setdefault(uid, {}).setdefault(key, {})[route.get('source_peer') or peer_id] = item
            else:
                immediate.append(item)
        if immediate:
            _spawn(uid, _run_immediate(uid, client, immediate))

    client.add_event_handler(_handler, events.NewMessage(incoming=True))
    _handlers[uid] = _handler
    _interval_tasks[uid] = asyncio.get_running_loop().create_task(_interval_loop(uid, client))
    logger.info("[%s] handler installed", uid)


def uninstall_handler(uid: int, client):
    old = _handlers.pop(uid, None)
    if old and client:
        try:
            client.remove_event_handler(old)
        except Exception:
            pass
    _cancel_task(_interval_tasks.pop(uid, None))
    cancel_pending(uid)


def status(uid: int) -> str:
    """وضعیت برای پنل."""
    data = _routes.get(uid)
    if not data:
        return 'idle'
    n = sum(len(v) for v in data['source_map'].values())
    return 'running' if n > 0 else 'no_sources'


def stats(uid: int) -> dict:
    data = _routes.get(uid) or {'source_map': {}, 'targets': {}, 'forum': {}}
    return {
        'routes': sum(len(v) for v in data['source_map'].values()),
        'sources_peers': len(data['source_map']),
        'targets': len(data['targets']),
        'pending_tasks': sum(1 for t in _tasks.get(uid, ()) if not t.done()),
        'buffered': sum(len(v) for v in (_buffer.get(uid) or {}).values()),
        'last_error': _last_error.get(uid, ''),
    }


async def rebuild_and_install(uid: int, client, cfg: dict):
    """ترکیبی: build routes + install handler — برای فراخوانی پس از connect یا save config."""
    await build_routes(uid, client, cfg)
    install_handler(uid, client)


async def refresh_forwarder(uid: int, client, cfg: dict) -> dict:
    """
    ریفرش: خالی کردن صف و بافر + rebuild فورواردر + پردازش آخرین پیام هر سورس
    (catch-up در پس‌زمینه اجرا می‌شود تا درخواست پنل منتظر نماند).
    """
    dropped = cancel_pending(uid)
    await asyncio.sleep(0)  # اجازه بده cancel ها اعمال شوند
    await rebuild_and_install(uid, client, cfg)
    _spawn(uid, catch_up_latest(uid, client))
    logger.info("[%s] forwarder refreshed: %d queued item(s) dropped", uid, dropped)
    return {'dropped': dropped}


# ══════════════════════════════════════════════════════════
#  Backfill: واکشی تاریخچه‌ی پیام‌های یک سورس و ثبت نرخ‌ها
# ══════════════════════════════════════════════════════════
async def backfill_source(
    uid: int,
    client,
    source_chat,
    gid: str,
    topic_id: int,
    *,
    value_regex: str = '',
    filters: list | None = None,
    clean_text: bool = False,
    since_days: int = 90,
    max_messages: int = 50000,
    sleep_every: int = 250,
    sleep_seconds: float = 0.8,
    progress_cb=None,
    is_cancelled=None,
) -> dict:
    """
    تاریخچه‌ی یک سورس را تا `since_days` روز عقب اسکن می‌کند و نرخ‌های قابل
    استخراج را با timestamp اصلی پیام (UTC) و message_id در DB ثبت می‌کند.
    Dedupe با INSERT OR IGNORE روی (uid, gid, tid, source_peer, message_id).

    skip_unchanged و max_change_percent در backfill اعمال نمی‌شوند تا داده‌ی
    تاریخی کامل بماند (مسئولیت آن‌ها فقط برای live است).

    Returns:
        dict با کلیدهای: seen, inserted, duplicate, filtered, parse_fail,
        elapsed, floodwait_total, oldest, newest, cancelled
    """
    from datetime import datetime, timezone, timedelta
    from telethon.errors import FloodWaitError

    started = time.monotonic()
    filters = filters or []
    try:
        entity = await _resolve(client, int(source_chat))
    except (TypeError, ValueError):
        entity = await _resolve(client, source_chat)
    peer = int(get_peer_id(entity))

    now_utc = datetime.now(timezone.utc)
    stop_at = now_utc - timedelta(days=int(since_days))

    seen = inserted = duplicate = filtered = parse_fail = 0
    floodwait_total = 0
    oldest_iso: str | None = None
    newest_iso: str | None = None
    cancelled = False
    last_msg_id_for_resume: int | None = None

    def _report():
        if progress_cb:
            try:
                progress_cb({
                    'seen': seen,
                    'inserted': inserted,
                    'duplicate': duplicate,
                    'filtered': filtered,
                    'parse_fail': parse_fail,
                    'floodwait_total': floodwait_total,
                    'oldest': oldest_iso,
                    'newest': newest_iso,
                    'elapsed': round(time.monotonic() - started, 1),
                })
            except Exception:
                pass

    async def _iterate(offset_id: int = 0):
        nonlocal seen, inserted, duplicate, filtered, parse_fail
        nonlocal oldest_iso, newest_iso, last_msg_id_for_resume, cancelled
        kwargs: dict[str, Any] = {'limit': None}
        if offset_id:
            kwargs['offset_id'] = offset_id
        async for msg in client.iter_messages(entity, **kwargs):
            if is_cancelled and is_cancelled():
                cancelled = True
                return
            if msg.date is None:
                continue
            if msg.date < stop_at:
                return
            seen += 1
            last_msg_id_for_resume = int(msg.id)
            iso = msg.date.isoformat()
            if newest_iso is None:
                newest_iso = iso
            oldest_iso = iso

            text = (msg.message or msg.text or getattr(msg, 'raw_text', '') or '').strip()
            if not text:
                if seen % 50 == 0:
                    _report()
                if seen >= max_messages:
                    return
                continue

            if filters and not _matches(text.lower(), filters):
                filtered += 1
            else:
                cleaned = _clean_text(text) if clean_text else text
                v, raw = parse_value(cleaned, value_regex or None)
                if v is None:
                    parse_fail += 1
                else:
                    ok = record_rate(
                        uid, gid, int(topic_id), v, raw or '', peer,
                        message_id=int(msg.id),
                        created_at=msg.date.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'),
                    )
                    if ok:
                        inserted += 1
                    else:
                        duplicate += 1

            if seen % 50 == 0:
                _report()
            if sleep_every and seen and seen % sleep_every == 0:
                await asyncio.sleep(sleep_seconds)
            if seen >= max_messages:
                return

    try:
        offset_id = 0
        while True:
            try:
                await _iterate(offset_id=offset_id)
                break
            except FloodWaitError as e:
                wait = int(getattr(e, 'seconds', 30) or 30) + 3
                floodwait_total += wait
                logger.warning(
                    "[%s] backfill FloodWait %ds — sleeping (seen=%d inserted=%d)",
                    uid, wait, seen, inserted,
                )
                _report()
                await asyncio.sleep(wait)
                offset_id = last_msg_id_for_resume or 0
    except asyncio.CancelledError:
        cancelled = True
        raise
    finally:
        _report()

    # Aggregation روزانه پس از اتمام (همه روزهای بازه را دوباره محاسبه کن)
    aggregated_days = 0
    if inserted > 0 or duplicate > 0:
        try:
            days_with_data = list_days_in_range(uid, gid, int(topic_id), since_days=int(since_days) + 1)
            aggregated_days = aggregate_rate_daily(uid, gid, int(topic_id), days=days_with_data)
            logger.info("[%s] backfill aggregated %d days", uid, aggregated_days)
        except Exception as e:
            logger.error("[%s] backfill aggregate failed: %s", uid, e, exc_info=True)

    elapsed = round(time.monotonic() - started, 1)
    result = {
        'seen': seen,
        'inserted': inserted,
        'duplicate': duplicate,
        'filtered': filtered,
        'parse_fail': parse_fail,
        'floodwait_total': floodwait_total,
        'oldest': oldest_iso,
        'newest': newest_iso,
        'elapsed': elapsed,
        'cancelled': cancelled,
        'aggregated_days': aggregated_days,
    }
    logger.info(
        "[%s] backfill done src=%s topic=%s: %s",
        uid, source_chat, topic_id, result,
    )
    return result
