"""سورس‌های API: دریافت عدد از سرویس‌های HTTP/JSON (مثل GoldAPI) به‌جای پیام تلگرام."""
import asyncio
import json
import logging
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

logger = logging.getLogger('telefilter.api_sources')

# پیش‌تنظیم سرویس‌ها — فیلدهای خالی اتصال/سورس از اینجا پر می‌شوند
PRESETS: dict[str, dict] = {
    'goldapi': {
        'name': 'GoldAPI',
        'base_url': 'https://goldapi.dev/api/v1',
        'auth_header': 'X-API-Key',
        'hourly_limit': 100,
        'endpoint': '/price/XAU/USD',
        'value_path': 'price',
        'time_path': 'updatedAt',
    },
    'custom': {
        'name': 'Custom JSON API',
        'base_url': '',
        'auth_header': '',
        'hourly_limit': 0,
        'endpoint': '',
        'value_path': '',
        'time_path': '',
    },
}

DEFAULT_POLL_MINUTES = 5
REQUEST_TIMEOUT = 15

# وضعیت سهمیه per connection (در حافظه): {conn_id: {hour, count, blocked_until, remaining}}
_quota: dict[str, dict] = {}


class ApiSourceError(Exception):
    """خطای قابل نمایش به کاربر (بدون کلید API)."""


def preset(provider: str) -> dict:
    return PRESETS.get(provider or 'custom') or PRESETS['custom']


def norm_connection(c: dict) -> dict:
    provider = c.get('provider') if c.get('provider') in PRESETS else 'custom'
    p = preset(provider)
    try:
        limit = int(c.get('hourly_limit') if c.get('hourly_limit') not in (None, '') else p['hourly_limit'])
    except (TypeError, ValueError):
        limit = p['hourly_limit']
    return {
        'id': str(c.get('id') or ''),
        'name': str(c.get('name') or p['name']),
        'provider': provider,
        'base_url': str(c.get('base_url') or p['base_url']).rstrip('/'),
        'auth_header': str(c.get('auth_header') if c.get('auth_header') is not None else p['auth_header']),
        'api_key': str(c.get('api_key') or ''),
        'hourly_limit': max(0, limit),
    }


def mask_key(key: str) -> str:
    key = key or ''
    if not key:
        return ''
    return '••••' + key[-4:] if len(key) > 4 else '••••'


def _dig(data, path: str):
    """مسیر نقطه‌ای مثل data.price یا rates.0.value."""
    cur = data
    for part in [p for p in (path or '').split('.') if p]:
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(cur, dict):
            if part not in cur:
                return None
            cur = cur[part]
        else:
            return None
    return cur


def _parse_time(v) -> datetime:
    if isinstance(v, (int, float)) and v > 0:
        # ثانیه یا میلی‌ثانیه Unix
        return datetime.fromtimestamp(v / 1000 if v > 1e11 else v, tz=timezone.utc)
    if isinstance(v, str) and v.strip():
        try:
            dt = datetime.fromisoformat(v.strip().replace('Z', '+00:00'))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return datetime.now(timezone.utc)


def build_url(conn: dict, endpoint: str) -> str:
    endpoint = (endpoint or '').strip()
    if endpoint.startswith(('http://', 'https://')):
        return endpoint
    base = (conn.get('base_url') or '').rstrip('/')
    if not base:
        raise ApiSourceError('آدرس پایه‌ی اتصال API خالی است')
    return f"{base}/{endpoint.lstrip('/')}" if endpoint else base


def _check_quota(conn: dict):
    cid = conn.get('id') or conn.get('base_url') or ''
    q = _quota.setdefault(cid, {'hour': 0, 'count': 0, 'blocked_until': 0.0, 'remaining': None})
    now = time.time()
    if q['blocked_until'] > now:
        raise ApiSourceError(
            f"سهمیه‌ی API «{conn.get('name')}» پر شده — تا {int(q['blocked_until'] - now)} ثانیه دیگر صبر می‌شود")
    hour = int(now // 3600)
    if q['hour'] != hour:
        q.update(hour=hour, count=0, remaining=None)
    limit = int(conn.get('hourly_limit') or 0)
    if limit and q['count'] >= limit:
        raise ApiSourceError(f"سقف {limit} درخواست در ساعت برای «{conn.get('name')}» پر شده")
    if q['remaining'] is not None and q['remaining'] <= 0:
        raise ApiSourceError(f"سرویس «{conn.get('name')}» اعلام کرده سهمیه‌ی این ساعت تمام شده")
    q['count'] += 1
    return q


def _request(conn: dict, url: str) -> tuple[int, dict, dict]:
    headers = {'Accept': 'application/json', 'User-Agent': 'TeleFilter/1.0'}
    if conn.get('api_key') and conn.get('auth_header'):
        headers[conn['auth_header']] = conn['api_key']
    req = urllib.request.Request(url, headers=headers, method='GET')
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            body = resp.read().decode('utf-8', errors='replace')
            status, hdrs = resp.status, dict(resp.headers)
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', errors='replace') if e.fp else ''
        status, hdrs = e.code, dict(e.headers or {})
    except urllib.error.URLError as e:
        raise ApiSourceError(f"اتصال به API ناموفق: {e.reason}") from None
    except TimeoutError:
        raise ApiSourceError('API در زمان مقرر پاسخ نداد') from None
    try:
        data = json.loads(body) if body else {}
    except json.JSONDecodeError:
        data = {'_raw': body[:300]}
    return status, hdrs, data


def _header(hdrs: dict, name: str):
    for k, v in hdrs.items():
        if k.lower() == name.lower():
            return v
    return None


async def fetch_value(conn: dict, src: dict) -> tuple[float, datetime, dict]:
    """یک بار API را صدا می‌زند: (مقدار، زمان، JSON خام). در خطا ApiSourceError."""
    conn = norm_connection(conn)
    p = preset(conn['provider'])
    endpoint = src.get('endpoint') or p['endpoint']
    value_path = src.get('value_path') or p['value_path']
    time_path = src.get('time_path') if src.get('time_path') is not None else p['time_path']
    if not value_path:
        raise ApiSourceError('مسیر فیلد عدد (value_path) مشخص نشده')
    url = build_url(conn, endpoint)
    q = _check_quota(conn)

    status, hdrs, data = await asyncio.to_thread(_request, conn, url)

    rem = _header(hdrs, 'X-RateLimit-Remaining')
    try:
        q['remaining'] = int(rem) if rem not in (None, '', 'unlimited') else None
    except ValueError:
        q['remaining'] = None
    if status == 429:
        try:
            wait = int(_header(hdrs, 'Retry-After') or 0)
        except ValueError:
            wait = 0
        q['blocked_until'] = time.time() + (wait or 3600 - time.time() % 3600)
        raise ApiSourceError(f"سهمیه‌ی API «{conn['name']}» پر شده (429)")
    if status >= 400:
        err = data.get('error') if isinstance(data, dict) else None
        raise ApiSourceError(f"API خطای {status} داد" + (f": {err}" if err else ''))

    raw_v = _dig(data, value_path)
    try:
        value = float(str(raw_v).replace(',', '')) if raw_v is not None else None
    except ValueError:
        value = None
    if value is None:
        raise ApiSourceError(f"فیلد «{value_path}» در پاسخ API پیدا نشد یا عدد نیست")
    when = _parse_time(_dig(data, time_path)) if time_path else datetime.now(timezone.utc)
    return value, when, data


def estimate_hourly(intervals: list[int]) -> int:
    """تخمین تعداد درخواست در ساعت برای فهرست فاصله‌های (دقیقه) سورس‌ها."""
    return sum(-(-60 // (m or DEFAULT_POLL_MINUTES)) for m in intervals)
