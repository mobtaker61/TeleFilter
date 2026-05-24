"""
In-process forwarder.

به جای اجرای subprocess جدا (main.py) که نیاز به session دوم و کپی فایل SQLite
داشت و باعث database is locked / readonly می‌شد، همان Telethon client پنل
رویدادهای NewMessage را مستقیم می‌گیرد و فوروارد می‌کند.
"""
from __future__ import annotations

import asyncio
import io
import logging
import random
import re
import time
from typing import Any

from telethon import events
from telethon.tl.functions.messages import ForwardMessagesRequest
from telethon.utils import get_peer_id

from config_util import (
    normalize_config, record_forward,
    parse_value, record_rate, get_rates, get_rates_smart, latest_rate,
    get_last_chart_msg, save_last_chart_msg,
    clean_text as _clean_text,
    aggregate_rate_daily, list_days_in_range,
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

# per-user state
_routes: dict[int, dict] = {}      # uid -> {'source_map', 'targets', 'forum'}
_handlers: dict[int, Any] = {}     # uid -> registered event handler callable
_last_error: dict[int, str] = {}   # uid -> last error string


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
            chart_label = str(t.get('chart_label') or t.get('name') or '')
            skip_unchanged = bool(t.get('skip_unchanged', True))
            chart_days = int(t.get('chart_days') or 7)
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
                        'chart_label': chart_label,
                        'value_regex': value_regex,
                        'skip_unchanged': skip_unchanged,
                        'chart_days': chart_days,
                        'max_change_percent': max_change_pct,
                        'clean_text': bool(s.get('clean_text', False)),
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


async def _process_chart(
    uid: int, client, route: dict, target, raw_text: str,
    pre_parsed: tuple[float | None, str | None] | None = None,
):
    """
    اگر برای این سورس chart فعال است:
      1) عدد را با regex استخراج کن (اگر pre_parsed نداده شده)
      2) در DB ذخیره کن
      3) نمودار را رندر و ارسال کن، نمودار قبلی را حذف کن
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
    record_rate(uid, gid, tid, value, raw_match or '', None)
    logger.info("[%s] chart: value=%s recorded topic=%s", uid, value, tid)

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
            return
        try:
            rates = get_rates(uid, gid, tid, since_hours=24 * days)
            png = render_rate_chart(
                rates,
                title=title,
                y_label=route.get('chart_label') or '',
            )
        except Exception as e:
            logger.error("[%s] chart render failed: %s", uid, e, exc_info=True)
            return
    if not png:
        logger.warning("[%s] chart render returned empty", uid)
        return

    old_msg = get_last_chart_msg(uid, gid, tid)
    if old_msg:
        try:
            await client.delete_messages(target, [int(old_msg)])
            logger.info("[%s] chart: deleted previous msg=%s", uid, old_msg)
        except Exception as e:
            logger.warning("[%s] chart: delete old msg=%s failed: %s", uid, old_msg, e)

    last_str = f"{int(value):,}" if value == int(value) else f"{value:,.4f}".rstrip('0').rstrip('.')
    caption = f"📊 {route.get('chart_label') or ''}\nآخرین: {last_str}".strip()
    try:
        named = _named_png(route.get('chart_label') or f'topic_{tid}', png)
        kwargs = {'caption': caption, 'force_document': False}
        if route.get('is_forum') and tid and tid > 0:
            kwargs['reply_to'] = int(tid)
        sent = await client.send_file(target, file=named, **kwargs)
        if sent and hasattr(sent, 'id'):
            save_last_chart_msg(uid, gid, tid, int(sent.id))
            logger.info("[%s] chart sent topic=%s msg=%s value=%s", uid, tid, sent.id, value)
        else:
            logger.warning("[%s] chart sent but no id returned: %r", uid, sent)
    except Exception as e:
        logger.error("[%s] chart send failed topic=%s: %s", uid, tid, e, exc_info=True)


def install_handler(uid: int, client):
    """نصب یا جایگزینی NewMessage handler برای کاربر."""
    old = _handlers.get(uid)
    if old is not None:
        try:
            client.remove_event_handler(old)
        except Exception:
            pass
        _handlers.pop(uid, None)

    async def _handler(event):
        data = _routes.get(uid)
        if not data:
            return
        source_map = data['source_map']
        targets = data['targets']
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

        for route in entries:
            if not _matches(text_lower, route.get('filters') or []):
                continue
            gid = route['gid']
            tid = route['topic_id']
            use_topic = route.get('is_forum') and tid > 0
            target = targets.get(gid)
            if not target:
                logger.warning("[%s] no target for group=%s", uid, gid)
                continue

            # ── چک‌های اعتبارسنجی نرخ ──
            # اگر چارت فعال است، عدد را یک بار parse می‌کنیم (cache pre_parsed)
            # و سپس دو چک می‌کنیم:
            #   ۱) skip_unchanged: مقدار == آخرین → کامل skip (نه فوروارد، نه چارت)
            #   ۲) max_change_percent: تغییر بیش از حد → کامل skip (outlier ضد data corruption)
            pre_parsed = None
            if route.get('chart_enabled') and raw_text:
                value_regex = route.get('value_regex') or None
                text_for_parse = _clean_text(raw_text) if route.get('clean_text') else raw_text
                v, raw_match = parse_value(text_for_parse, value_regex)
                if v is not None:
                    pre_parsed = (v, raw_match)
                    last = latest_rate(uid, gid, tid)
                    last_v = float(last.get('value') or 0) if last else None

                    # چک ۱: مقدار تکراری
                    if route.get('skip_unchanged', True) and last_v is not None and _values_equal(last_v, v):
                        logger.info(
                            "[%s] skip unchanged: chat=%s topic=%s value=%s (= last)",
                            uid, peer_id, tid, v,
                        )
                        continue

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
                            continue

            try:
                kwargs = {
                    'from_peer': event.chat_id,
                    'id': [event.message.id],
                    'to_peer': target,
                    'random_id': [random.randint(1, 2**63 - 1)],
                }
                if use_topic:
                    kwargs['top_msg_id'] = tid
                await client(ForwardMessagesRequest(**kwargs))
                record_forward(uid, gid, tid if use_topic else 0, peer_id)
                logger.info(
                    "[%s] forwarded chat=%s msg=%s → group=%s topic=%s",
                    uid, peer_id, event.message.id, gid, tid if use_topic else '-',
                )
            except Exception as e:
                err = f"forward fail (chat={peer_id} group={gid} topic={tid}): {e}"
                _last_error[uid] = err
                logger.error("[%s] %s", uid, err)
                continue

            if route.get('chart_enabled') and raw_text:
                try:
                    await _process_chart(uid, client, route, target, raw_text, pre_parsed=pre_parsed)
                except Exception as e:
                    logger.error("[%s] chart pipeline failed: %s", uid, e)

    client.add_event_handler(_handler, events.NewMessage(incoming=True))
    _handlers[uid] = _handler
    logger.info("[%s] handler installed", uid)


def uninstall_handler(uid: int, client):
    old = _handlers.pop(uid, None)
    if old and client:
        try:
            client.remove_event_handler(old)
        except Exception:
            pass


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
        'last_error': _last_error.get(uid, ''),
    }


async def rebuild_and_install(uid: int, client, cfg: dict):
    """ترکیبی: build routes + install handler — برای فراخوانی پس از connect یا save config."""
    await build_routes(uid, client, cfg)
    install_handler(uid, client)


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
    entity = await client.get_entity(source_chat)
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
