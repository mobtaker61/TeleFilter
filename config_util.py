"""Normalize user config, migrate legacy format, forward stats."""
import json
import os
import sqlite3
import uuid
from datetime import datetime, timedelta

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'users.db')


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
    return {
        'id': g.get('id') or new_group_id(),
        'title': g.get('title') or 'گروه',
        'telegram_id': str(g.get('telegram_id', '')),
        'origin': g.get('origin', 'linked'),
        'topics': g.get('topics') or [],
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
        c.commit()


_init_stats_db()


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
