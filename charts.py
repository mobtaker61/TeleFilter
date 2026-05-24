"""
Chart rendering for rate history — headless matplotlib → PNG bytes.

matplotlib به‌صورت lazy لود می‌شود تا اگر روی سرور نصب نباشد یا libGL/libGLib
نباشد، panel همچنان بالا بیاید و فقط فیچر چارت غیرفعال شود.
نصب پیش‌نیاز روی سرور Ubuntu/Debian:
    sudo apt-get install -y libgl1 libglib2.0-0
"""
from __future__ import annotations

import io
import logging
from datetime import datetime

logger = logging.getLogger('telefilter.charts')

_MPL_READY: bool | None = None
_LOAD_ERROR: str = ''


def _load_mpl(force: bool = False):
    """matplotlib را در اولین فراخوانی (یا force=True) لود می‌کند."""
    global _MPL_READY, _LOAD_ERROR
    if not force and _MPL_READY is True:
        return True
    if not force and _MPL_READY is False:
        return False
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot  # noqa: F401
        import matplotlib.dates   # noqa: F401
        import numpy              # noqa: F401
        _MPL_READY = True
        _LOAD_ERROR = ''
        logger.info("matplotlib loaded ok (version=%s)", matplotlib.__version__)
        return True
    except BaseException as e:  # نه فقط Exception — ImportError از numpy گاهی SystemExit می‌اندازد
        kind = type(e).__name__
        msg = str(e) or repr(e)
        _LOAD_ERROR = f'{kind}: {msg}'
        logger.error("matplotlib unavailable [%s]: %s", kind, msg)
        logger.error(
            "اگر روی سرور است، اجرا کنید: "
            "sudo apt-get install -y libgl1 libglib2.0-0 "
            "&& pip install --force-reinstall matplotlib numpy"
        )
        _MPL_READY = False
        return False


def is_available() -> bool:
    """آیا فیچر چارت قابل استفاده است؟"""
    if _MPL_READY is None:
        _load_mpl()
    return bool(_MPL_READY)


def reload() -> bool:
    """تلاش مجدد برای لود matplotlib — وقتی کاربر تازه نصبش کرده."""
    return _load_mpl(force=True)


def load_error() -> str:
    return _LOAD_ERROR


def _parse_dt(s: str) -> datetime:
    try:
        return datetime.strptime(s, '%Y-%m-%d %H:%M:%S')
    except ValueError:
        try:
            return datetime.fromisoformat(s)
        except Exception:
            return datetime.utcnow()


def render_rate_chart(
    rates: list[dict],
    title: str = '',
    y_label: str = '',
    accent: str = '#2563eb',
) -> bytes | None:
    """
    rates: [{'value': float, 'created_at': 'YYYY-MM-DD HH:MM:SS'}, ...]
    خروجی: PNG bytes یا None در صورت در دسترس نبودن matplotlib.
    """
    if not _load_mpl():
        return None

    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates

    if not rates:
        return _placeholder('داده‌ای ثبت نشده')

    xs = [_parse_dt(r['created_at']) for r in rates]
    ys = [float(r['value']) for r in rates]

    fig, ax = plt.subplots(figsize=(8.5, 4.2), dpi=130)
    ax.plot(xs, ys, color=accent, linewidth=2.4)

    ax.plot([xs[-1]], [ys[-1]], 'o', color=accent, markersize=8, zorder=5)
    last = f"{ys[-1]:,.4f}".rstrip('0').rstrip('.') if ys[-1] % 1 else f"{int(ys[-1]):,}"
    ax.annotate(
        f' {last}',
        xy=(xs[-1], ys[-1]),
        xytext=(8, 8), textcoords='offset points',
        fontsize=11, fontweight='bold', color=accent,
    )

    ax.set_title(title or 'Rate Chart', fontsize=12, pad=12)
    if y_label:
        ax.set_ylabel(y_label, fontsize=10)
    ax.grid(True, linestyle='--', alpha=0.35)
    ax.spines['top'].set_visible(False)
    ax.spines['right'].set_visible(False)

    locator = mdates.AutoDateLocator(maxticks=8)
    fmt = mdates.AutoDateFormatter(locator)
    ax.xaxis.set_major_locator(locator)
    ax.xaxis.set_major_formatter(fmt)
    fig.autofmt_xdate(rotation=0, ha='center')

    if len(ys) > 1:
        lo, hi = min(ys), max(ys)
        if hi == lo:
            pad = abs(hi) * 0.01 + 1
        else:
            pad = (hi - lo) * 0.1
        ax.set_ylim(lo - pad, hi + pad)

    n = len(rates)
    span = xs[-1] - xs[0]
    span_txt = _humanize_span(span)
    fig.text(0.5, 0.01, f'{n} points · {span_txt}', ha='center',
             fontsize=8, color='#64748b')

    buf = io.BytesIO()
    fig.tight_layout(rect=(0, 0.03, 1, 1))
    fig.savefig(buf, format='png', bbox_inches='tight', facecolor='white')
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()


def _humanize_span(delta) -> str:
    total = int(delta.total_seconds())
    if total < 3600:
        return f'{total // 60} min'
    if total < 86400:
        return f'{total // 3600} h'
    return f'{total // 86400} d'


def _placeholder(text: str) -> bytes | None:
    if not _load_mpl():
        return None
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(8, 4), dpi=120)
    ax.text(0.5, 0.5, text, ha='center', va='center', fontsize=14, color='#64748b')
    ax.set_xticks([])
    ax.set_yticks([])
    for s in ax.spines.values():
        s.set_visible(False)
    buf = io.BytesIO()
    fig.savefig(buf, format='png', bbox_inches='tight', facecolor='white')
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()
