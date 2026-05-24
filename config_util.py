"""Normalize user config, migrate legacy format, forward stats, rate history."""
import json
import os
import re
import sqlite3
import uuid
from datetime import datetime, timedelta

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'users.db')

# Timezone محلی برای aggregation روزانه (Asia/Tehran = UTC+3:30).
# created_at در DB به‌صورت UTC ذخیره می‌شود؛ برای تقسیم به روز این modifierها را به DATE/datetime SQLite پاس می‌دهیم.
# توجه: SQLite چند modifier باید به‌صورت argument جدا باشند.
LOCAL_TZ_MODIFIERS = ("+3 hours", "+30 minutes")


def _day_expr(col: str = 'created_at') -> str:
    """SQL expression برای استخراج روز محلی از یک ستون timestamp UTC."""
    mods = ', '.join(f"'{m}'" for m in LOCAL_TZ_MODIFIERS)
    return f"DATE({col}, {mods})"


def new_group_id() -> str:
    return uuid.uuid4().hex[:12]


def empty_config() -> dict:
    return {'api_id': '', 'api_hash': '', 'groups': []}


def normalize_config(raw: dict) -> dict:
    if not raw:
        return empty_config()
    if 'groups' in raw:
        out = {
            'api_id': str(raw.get('api_id', '') or ''),
            'api_hash': str(raw.get('api_hash', '') or ''),
            'groups': [],
        }
        for g in raw.get('groups') or []:
            out['groups'].append(_norm_group(g))
        return out
    groups = []
    if raw.get('target_group_id'):
        groups.append({
            'id': 'default',
            'title': raw.get('target_group_title') or 'گروه اصلی',
            'telegram_id': str(raw['target_group_id']),
            'origin': 'linked',
            'topics': raw.get('topics') or [],
        })
    return {
        'api_id': str(raw.get('api_id', '') or ''),
        'api_hash': str(raw.get('api_hash', '') or ''),
        'groups': groups,
    }


def _norm_group(g: dict) -> dict:
    out = {
        'id': g.get('id') or new_group_id(),
        'title': g.get('title') or 'گروه',
        'telegram_id': str(g.get('telegram_id', '')),
        'origin': g.get('origin', 'linked'),
        'topics': [_norm_topic(t) for t in (g.get('topics') or [])],
    }
    if 'is_forum' in g:
        out['is_forum'] = bool(g['is_forum'])
    else:
        out['is_forum'] = any(int(t.get('topic_id') or 0) > 0 for t in (g.get('topics') or []))
    return out


_VALID_CHART_DAYS = (1, 3, 7, 15)


def _norm_chart_days(v) -> int:
    try:
        d = int(v)
    except (TypeError, ValueError):
        return 7
    return d if d in _VALID_CHART_DAYS else 7


def _norm_topic(t: dict) -> dict:
    try:
        max_change = float(t.get('max_change_percent', 10) or 0)
    except (TypeError, ValueError):
        max_change = 10.0
    if max_change < 0:
        max_change = 0.0
    try:
        order = int(t.get('chart_order', 0) or 0)
    except (TypeError, ValueError):
        order = 0
    return {
        'topic_id': t.get('topic_id'),
        'name': t.get('name', ''),
        'chart_enabled': bool(t.get('chart_enabled', False)),
        'chart_label': str(t.get('chart_label', '') or ''),
        'skip_unchanged': bool(t.get('skip_unchanged', True)),
        'chart_days': _norm_chart_days(t.get('chart_days', 7)),
        'chart_order': order,
        'max_change_percent': max_change,
        'sources': [_norm_source(s) for s in (t.get('sources') or [])],
    }


def _norm_source(s: dict) -> dict:
    return {
        'chat': s.get('chat', ''),
        'filters': s.get('filters', []),
        'value_regex': str(s.get('value_regex', '') or ''),
        'enabled': bool(s.get('enabled', True)),
        'clean_text': bool(s.get('clean_text', False)),
    }


def find_group(cfg: dict, group_id: str) -> dict | None:
    for g in cfg.get('groups') or []:
        if g['id'] == group_id:
            return g
    return None


def config_stats(cfg: dict) -> dict:
    cfg = normalize_config(cfg)
    groups = cfg.get('groups') or []
    sources = filters = topics = 0
    for g in groups:
        for t in g.get('topics') or []:
            topics += 1
            for s in t.get('sources') or []:
                sources += 1
                fl = s.get('filters') or []
                if not fl:
                    continue
                for rule in fl:
                    if isinstance(rule, str):
                        filters += 1
                    else:
                        filters += len(rule)
    return {
        'groups': len(groups),
        'topics': topics,
        'sources': sources,
        'filters': filters,
    }


def _init_stats_db():
    with sqlite3.connect(DB_PATH) as c:
        c.execute('''CREATE TABLE IF NOT EXISTS forward_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            group_id TEXT,
            topic_id INTEGER,
            source_peer INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )''')
        c.execute('CREATE INDEX IF NOT EXISTS idx_fwd_user_time ON forward_log(user_id, created_at)')

        c.execute('''CREATE TABLE IF NOT EXISTS rate_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            group_id TEXT NOT NULL,
            topic_id INTEGER NOT NULL,
            value REAL NOT NULL,
            raw_text TEXT,
            source_peer INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )''')
        c.execute('CREATE INDEX IF NOT EXISTS idx_rate_topic ON rate_history(user_id, group_id, topic_id, created_at)')
        # message_id برای dedupe در backfill (افزایش به جداول قدیمی)
        cols = {r[1] for r in c.execute("PRAGMA table_info(rate_history)").fetchall()}
        if 'message_id' not in cols:
            c.execute('ALTER TABLE rate_history ADD COLUMN message_id INTEGER')
        c.execute(
            'CREATE UNIQUE INDEX IF NOT EXISTS uq_rate_msg '
            'ON rate_history(user_id, group_id, topic_id, source_peer, message_id) '
            'WHERE message_id IS NOT NULL'
        )

        c.execute('''CREATE TABLE IF NOT EXISTS chart_message (
            user_id INTEGER NOT NULL,
            group_id TEXT NOT NULL,
            topic_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id, group_id, topic_id)
        )''')
        # ── Daily aggregation برای نمودارهای طولانی (>7 روز) ──
        # برای هر روز کاری یک ردیف: میانگین + min/max + open/close + count
        c.execute('''CREATE TABLE IF NOT EXISTS rate_daily (
            user_id INTEGER NOT NULL,
            group_id TEXT NOT NULL,
            topic_id INTEGER NOT NULL,
            day TEXT NOT NULL,             -- YYYY-MM-DD (در TZ محلی، Asia/Tehran)
            avg_value REAL NOT NULL,
            min_value REAL NOT NULL,
            max_value REAL NOT NULL,
            open_value REAL NOT NULL,
            close_value REAL NOT NULL,
            sample_count INTEGER NOT NULL,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id, group_id, topic_id, day)
        )''')
        c.execute('CREATE INDEX IF NOT EXISTS idx_rate_daily ON rate_daily(user_id, group_id, topic_id, day)')
        c.commit()


_init_stats_db()


# ══════════════════════════════════════════════════════════
#  Value parsing (price/rate extraction from message text)
# ══════════════════════════════════════════════════════════
PERSIAN_DIGITS = str.maketrans('۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩', '01234567890123456789')


def normalize_digits(s: str) -> str:
    """تبدیل ارقام فارسی/عربی به انگلیسی."""
    return (s or '').translate(PERSIAN_DIGITS)


# ── متن‌پاک‌کن: حذف ایموجی، علائم دکوراتیو، zero-width ───
# هر چه بیشتر سختگیرانه پاک‌سازی شود، regex راحت‌تر match می‌کند.
_EMOJI_CLEAN = re.compile(
    '['
    '\U0001F300-\U0001F6FF'    # Symbols & Pictographs
    '\U0001F7E0-\U0001F7FF'    # Geometric shapes (colored circles/squares)
    '\U0001F900-\U0001F9FF'    # Supplemental Symbols & Pictographs
    '\U0001FA00-\U0001FAFF'    # Extended Pictographs
    '\U00002600-\U000027BF'    # Misc Symbols + Dingbats (✓ ✅ ❌ ★ ☆ ...)
    '\U00002B00-\U00002BFF'    # Misc Symbols and Arrows (⭐ ⬆ ⬇ ...)
    '\U0001F1E0-\U0001F1FF'    # Regional Indicators (flags)
    '\u200B-\u200F'            # Zero-width & directional marks
    '\u202A-\u202E'            # Directional overrides
    '\u2060-\u206F'            # Word joiner / invisible
    '\uFE0F'                   # Variation Selector-16 (emoji style)
    '•◆◇★☆♥♦♣♠✦▶◀▲▼►◄'
    ']+'
)
# جداکننده‌های مرسوم هزارگان فارسی/عربی → کاما (تا regex استاندارد کار کند)
_THOUSANDS_SEP = re.compile(r'[٫٬’‘`]')
# علائم پانکچوئیشن غیرضروری اطراف اعداد (حفظ . , - + % و حروف فارسی/انگلیسی/عدد)
_PUNCT_NOISE = re.compile(r'[«»"\'\(\)\[\]\{\}؛;،,]{2,}')


_DOT_AS_THOUSANDS = re.compile(r'(\d)\.(?=\d{3}(?:\D|$))')


def clean_text(text: str) -> str:
    """
    پاک‌سازی متن قبل از regex:
      - حذف ایموجی، پرچم، علائم دکوراتیو
      - حذف zero-width characters
      - یکپارچه کردن جداکننده‌های هزارگان غیراستاندارد به ,
      - تبدیل هوشمند '.' در نقش جداکننده‌ی هزارگان به ',' (مثل '89.500' → '89,500')
        — اعشار واقعی مثل '1.5' دست‌نخورده می‌ماند چون فقط وقتی بعدش *دقیقاً 3 رقم*
        و سپس کاراکتر غیررقم (یا انتها) بیاید، تبدیل می‌شود.
      - collapse کردن whitespace ها به یک space
    رشته اصلی (پیام تلگرام) دست‌نخورده می‌ماند — این فقط برای parse است.
    """
    if not text:
        return ''
    s = _EMOJI_CLEAN.sub(' ', text)
    s = _THOUSANDS_SEP.sub(',', s)
    s = _PUNCT_NOISE.sub(' ', s)
    s = _DOT_AS_THOUSANDS.sub(r'\1,', s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s


def parse_value(text: str, regex: str | None = None) -> tuple[float | None, str | None]:
    """
    استخراج عدد از متن.
    اگر regex داده شود، گروه اول match یا خود match استفاده می‌شود.
    اگر regex خالی باشد، یک heuristic ساده: اولین عدد طولانی >= 2 رقم.
    خروجی: (value, raw_string) یا (None, None).
    """
    if not text:
        return None, None
    text = normalize_digits(text)
    match = None
    if regex:
        try:
            match = re.search(regex, text, re.MULTILINE)
        except re.error:
            return None, None
        if not match:
            return None, None
        raw = match.group(1) if match.groups() else match.group(0)
    else:
        m = re.search(r'\d[\d,،.\s]{1,15}\d', text)
        if not m:
            return None, None
        raw = m.group(0)
    cleaned = re.sub(r'[,،\s]', '', raw).strip().strip('.')
    if not cleaned:
        return None, None
    try:
        return float(cleaned), raw.strip()
    except ValueError:
        return None, None


# ══════════════════════════════════════════════════════════
#  Rate history
# ══════════════════════════════════════════════════════════
def record_rate(user_id: int, group_id: str, topic_id: int, value: float,
                raw_text: str = '', source_peer: int | None = None,
                message_id: int | None = None, created_at: str | None = None) -> bool:
    """
    ثبت یک نرخ. اگر message_id داده شود و در ترکیب (user/group/topic/source/msg)
    تکراری باشد، با INSERT OR IGNORE نادیده گرفته می‌شود.
    خروجی: True اگر insert واقعی انجام شد، False اگر تکراری بود یا نامعتبر.
    """
    if not user_id:
        return False
    with sqlite3.connect(DB_PATH) as c:
        cur = c.execute(
            'INSERT OR IGNORE INTO rate_history '
            ' (user_id, group_id, topic_id, value, raw_text, source_peer, message_id, created_at)'
            ' VALUES (?,?,?,?,?,?,?, COALESCE(?, CURRENT_TIMESTAMP))',
            (
                user_id, group_id or '', int(topic_id), float(value), raw_text or '',
                int(source_peer) if source_peer is not None else None,
                int(message_id) if message_id is not None else None,
                created_at,
            ),
        )
        c.commit()
        return cur.rowcount > 0


def get_rates(user_id: int, group_id: str, topic_id: int,
              since_hours: int = 168, limit: int | None = None) -> list[dict]:
    """تاریخچهٔ نرخ‌ها (ردیف‌های خام) در بازه زمانی (ساعت)؛ پیش‌فرض ۷ روز.
    اگر limit=None، همهٔ ردیف‌های بازه برمی‌گردند.
    """
    if not user_id:
        return []
    since = (datetime.utcnow() - timedelta(hours=int(since_hours))).strftime('%Y-%m-%d %H:%M:%S')
    with sqlite3.connect(DB_PATH) as c:
        c.row_factory = sqlite3.Row
        sql = (
            'SELECT id, value, raw_text, created_at FROM rate_history '
            ' WHERE user_id=? AND group_id=? AND topic_id=? AND created_at>=? '
            ' ORDER BY created_at ASC'
        )
        params: list = [user_id, group_id, int(topic_id), since]
        if limit is not None:
            sql += ' LIMIT ?'
            params.append(int(limit))
        rows = c.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def get_rates_daily(user_id: int, group_id: str, topic_id: int,
                    since_days: int = 90) -> list[dict]:
    """نرخ‌های aggregate شده‌ی روزانه — برای نمودارهای طولانی.
    خروجی: لیست {created_at (ISO 12:00 UTC), value (avg), min, max, sample_count}
    """
    if not user_id:
        return []
    since_day = (datetime.utcnow() - timedelta(days=int(since_days))).strftime('%Y-%m-%d')
    with sqlite3.connect(DB_PATH) as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            'SELECT day, avg_value, min_value, max_value, open_value, close_value, sample_count'
            ' FROM rate_daily'
            ' WHERE user_id=? AND group_id=? AND topic_id=? AND day>=?'
            ' ORDER BY day ASC',
            (user_id, group_id, int(topic_id), since_day),
        ).fetchall()
    # هر روز را به‌صورت نقطه‌ی وسط روز (12:00 UTC) برگردان تا با محور زمانی سازگار باشد
    out = []
    for r in rows:
        out.append({
            'created_at': f"{r['day']} 12:00:00",
            'value': r['avg_value'],
            'min': r['min_value'],
            'max': r['max_value'],
            'open': r['open_value'],
            'close': r['close_value'],
            'sample_count': r['sample_count'],
        })
    return out


def get_rates_smart(user_id: int, group_id: str, topic_id: int,
                    since_hours: int = 168, daily_threshold_hours: int = 168) -> dict:
    """
    خواندن هوشمند داده‌ها برای نمودار:
      - اگر بازه ≤ daily_threshold_hours (پیش‌فرض ۷ روز): تمام ردیف‌های خام
      - اگر بازه > آن: ترکیب rate_daily (روزهای کامل) + rate_history (روز جاری)

    خروجی: {'rates': [...], 'mode': 'raw'|'daily', 'count': int}
    """
    if since_hours <= daily_threshold_hours:
        rates = get_rates(user_id, group_id, topic_id, since_hours=since_hours, limit=None)
        return {'rates': rates, 'mode': 'raw', 'count': len(rates)}
    days = max(1, int(since_hours / 24))
    daily = get_rates_daily(user_id, group_id, topic_id, since_days=days)
    today_rates = get_rates(user_id, group_id, topic_id, since_hours=24, limit=None)
    rates = daily + today_rates
    return {'rates': rates, 'mode': 'daily', 'count': len(rates)}


def aggregate_rate_daily(user_id: int, group_id: str, topic_id: int,
                         days: list[str] | None = None) -> int:
    """
    محاسبه و درج/به‌روزرسانی aggregation روزانه از rate_history.
    اگر days داده نشود، همه‌ی روزهایی که داده دارند aggregate می‌شوند.
    خروجی: تعداد روزهای aggregate شده.
    """
    if not user_id:
        return 0
    with sqlite3.connect(DB_PATH) as c:
        c.row_factory = sqlite3.Row
        params: list = [user_id, group_id or '', int(topic_id)]
        day_expr = _day_expr('created_at')
        if days:
            placeholders = ','.join(['?'] * len(days))
            day_filter = f' AND {day_expr} IN ({placeholders})'
            params += list(days)
        else:
            day_filter = ''
        # با CTE و window functions، open/close هر روز را درست محاسبه می‌کنیم
        rows = c.execute(
            f'''
            WITH base AS (
              SELECT
                value,
                created_at,
                {day_expr} AS day,
                ROW_NUMBER() OVER (PARTITION BY {day_expr} ORDER BY created_at ASC)  AS rn_first,
                ROW_NUMBER() OVER (PARTITION BY {day_expr} ORDER BY created_at DESC) AS rn_last
              FROM rate_history
              WHERE user_id=? AND group_id=? AND topic_id=?{day_filter}
            )
            SELECT
              day,
              AVG(value)                                       AS avg_v,
              MIN(value)                                       AS min_v,
              MAX(value)                                       AS max_v,
              COUNT(*)                                         AS cnt,
              MAX(CASE WHEN rn_first=1 THEN value END)         AS open_v,
              MAX(CASE WHEN rn_last=1  THEN value END)         AS close_v
            FROM base
            GROUP BY day
            ''',
            params,
        ).fetchall()
        n = 0
        for r in rows:
            c.execute(
                'INSERT INTO rate_daily '
                ' (user_id, group_id, topic_id, day, avg_value, min_value, max_value,'
                '  open_value, close_value, sample_count, updated_at)'
                ' VALUES (?,?,?,?,?,?,?,?,?,?, CURRENT_TIMESTAMP)'
                ' ON CONFLICT(user_id, group_id, topic_id, day) DO UPDATE SET'
                ' avg_value=excluded.avg_value, min_value=excluded.min_value,'
                ' max_value=excluded.max_value, open_value=excluded.open_value,'
                ' close_value=excluded.close_value, sample_count=excluded.sample_count,'
                ' updated_at=CURRENT_TIMESTAMP',
                (user_id, group_id or '', int(topic_id), r['day'],
                 float(r['avg_v']), float(r['min_v']), float(r['max_v']),
                 float(r['open_v']) if r['open_v'] is not None else float(r['avg_v']),
                 float(r['close_v']) if r['close_v'] is not None else float(r['avg_v']),
                 int(r['cnt'])),
            )
            n += 1
        c.commit()
    return n


def list_days_in_range(user_id: int, group_id: str, topic_id: int,
                       since_days: int) -> list[str]:
    """لیست روزهای متمایزی که در این بازه داده دارند (برای aggregation)."""
    if not user_id:
        return []
    since = (datetime.utcnow() - timedelta(days=int(since_days))).strftime('%Y-%m-%d %H:%M:%S')
    day_expr = _day_expr('created_at')
    with sqlite3.connect(DB_PATH) as c:
        rows = c.execute(
            f'SELECT DISTINCT {day_expr} FROM rate_history'
            ' WHERE user_id=? AND group_id=? AND topic_id=? AND created_at>=?'
            ' ORDER BY 1',
            (user_id, group_id or '', int(topic_id), since),
        ).fetchall()
    return [r[0] for r in rows]


def delete_rate(user_id: int, group_id: str, topic_id: int, rate_id: int) -> bool:
    """حذف امن یک ردیف از تاریخچه — فقط متعلق به همان کاربر/گروه/تاپیک."""
    if not user_id:
        return False
    with sqlite3.connect(DB_PATH) as c:
        cur = c.execute(
            'DELETE FROM rate_history WHERE id=? AND user_id=? AND group_id=? AND topic_id=?',
            (int(rate_id), user_id, group_id, int(topic_id)),
        )
        c.commit()
        return cur.rowcount > 0


def delete_rates_range(user_id: int, group_id: str, topic_id: int,
                       min_value: float | None = None, max_value: float | None = None) -> int:
    """حذف ردیف‌هایی که خارج از بازهٔ مقبول هستند (برای پاک‌سازی outlier ها)."""
    if not user_id or (min_value is None and max_value is None):
        return 0
    where = 'user_id=? AND group_id=? AND topic_id=?'
    params: list = [user_id, group_id, int(topic_id)]
    conds = []
    if min_value is not None:
        conds.append('value < ?')
        params.append(float(min_value))
    if max_value is not None:
        conds.append('value > ?')
        params.append(float(max_value))
    where += ' AND (' + ' OR '.join(conds) + ')'
    with sqlite3.connect(DB_PATH) as c:
        cur = c.execute(f'DELETE FROM rate_history WHERE {where}', params)
        c.commit()
        return cur.rowcount


def latest_rate(user_id: int, group_id: str, topic_id: int) -> dict | None:
    if not user_id:
        return None
    with sqlite3.connect(DB_PATH) as c:
        c.row_factory = sqlite3.Row
        r = c.execute(
            'SELECT value, raw_text, created_at FROM rate_history '
            ' WHERE user_id=? AND group_id=? AND topic_id=? '
            ' ORDER BY created_at DESC LIMIT 1',
            (user_id, group_id, int(topic_id)),
        ).fetchone()
        return dict(r) if r else None


def get_last_chart_msg(user_id: int, group_id: str, topic_id: int) -> int | None:
    with sqlite3.connect(DB_PATH) as c:
        r = c.execute(
            'SELECT message_id FROM chart_message WHERE user_id=? AND group_id=? AND topic_id=?',
            (user_id, group_id, int(topic_id)),
        ).fetchone()
        return int(r[0]) if r else None


def save_last_chart_msg(user_id: int, group_id: str, topic_id: int, message_id: int):
    with sqlite3.connect(DB_PATH) as c:
        c.execute(
            'INSERT INTO chart_message (user_id, group_id, topic_id, message_id, updated_at)'
            ' VALUES (?,?,?,?,CURRENT_TIMESTAMP)'
            ' ON CONFLICT(user_id,group_id,topic_id) DO UPDATE SET'
            ' message_id=excluded.message_id, updated_at=CURRENT_TIMESTAMP',
            (user_id, group_id, int(topic_id), int(message_id)),
        )
        c.commit()


def clear_last_chart_msg(user_id: int, group_id: str, topic_id: int):
    with sqlite3.connect(DB_PATH) as c:
        c.execute(
            'DELETE FROM chart_message WHERE user_id=? AND group_id=? AND topic_id=?',
            (user_id, group_id, int(topic_id)),
        )
        c.commit()


def record_forward(user_id: int, group_id: str, topic_id: int, source_peer: int):
    if not user_id:
        return
    with sqlite3.connect(DB_PATH) as c:
        c.execute(
            'INSERT INTO forward_log (user_id, group_id, topic_id, source_peer) VALUES (?,?,?,?)',
            (user_id, group_id or '', topic_id, source_peer),
        )
        c.commit()


def group_config_stats(cfg: dict, group_id: str) -> dict:
    g = find_group(cfg, group_id)
    if not g:
        return {}
    topics = sources = filters = 0
    topic_list = []
    for t in g.get('topics') or []:
        topics += 1
        sc = len(t.get('sources') or [])
        sources += sc
        topic_list.append({'topic_id': t.get('topic_id'), 'name': t.get('name', ''), 'sources': sc})
        for s in t.get('sources') or []:
            fl = s.get('filters') or []
            for rule in fl:
                if isinstance(rule, str):
                    filters += 1
                else:
                    filters += len(rule)
    return {
        'id': g['id'],
        'title': g.get('title', ''),
        'telegram_id': g.get('telegram_id', ''),
        'origin': g.get('origin', 'linked'),
        'topics': topics,
        'sources': sources,
        'filters': filters,
        'topic_list': topic_list,
    }


def dashboard_stats(user_id: int, group_id: str | None = None) -> dict:
    base = {'forwards_today': 0, 'forwards_total': 0, 'chart': [], 'recent_topics': []}
    if not user_id:
        return base
    now = datetime.utcnow()
    today = now.strftime('%Y-%m-%d')
    week_ago = (now - timedelta(days=6)).strftime('%Y-%m-%d')
    gf = ' AND group_id=?' if group_id else ''
    params_base = [user_id]
    if group_id:
        params_base.append(group_id)
    with sqlite3.connect(DB_PATH) as c:
        c.row_factory = sqlite3.Row
        base['forwards_total'] = c.execute(
            f'SELECT COUNT(*) FROM forward_log WHERE user_id=?{gf}', params_base
        ).fetchone()[0]
        if group_id:
            base['forwards_today'] = c.execute(
                'SELECT COUNT(*) FROM forward_log WHERE user_id=? AND group_id=? AND date(created_at)=?',
                (user_id, group_id, today),
            ).fetchone()[0]
        else:
            base['forwards_today'] = c.execute(
                'SELECT COUNT(*) FROM forward_log WHERE user_id=? AND date(created_at)=?',
                (user_id, today),
            ).fetchone()[0]
        if group_id:
            p_week = [user_id, group_id, week_ago]
            p_chart_where = 'user_id=? AND group_id=? AND date(created_at)>=?'
        else:
            p_week = [user_id, week_ago]
            p_chart_where = 'user_id=? AND date(created_at)>=?'
        rows = c.execute(
            f'''SELECT date(created_at) AS d, COUNT(*) AS n
               FROM forward_log WHERE {p_chart_where}
               GROUP BY date(created_at) ORDER BY d''',
            p_week,
        ).fetchall()
        by_day = {r['d']: r['n'] for r in rows}
        chart = []
        for i in range(6, -1, -1):
            d = (now - timedelta(days=i)).strftime('%Y-%m-%d')
            chart.append({'date': d, 'count': by_day.get(d, 0)})
        base['chart'] = chart
        recent = c.execute(
            f'''SELECT group_id, topic_id, COUNT(*) AS n
               FROM forward_log WHERE user_id=?{gf}
               GROUP BY group_id, topic_id ORDER BY MAX(created_at) DESC LIMIT 8''',
            params_base,
        ).fetchall()
        base['recent_topics'] = [dict(r) for r in recent]
    return base
