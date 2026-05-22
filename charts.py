"""Chart rendering for rate history — headless matplotlib → PNG bytes."""
from __future__ import annotations

import io
from datetime import datetime

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.dates as mdates


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
) -> bytes:
    """
    rates: [{'value': float, 'created_at': 'YYYY-MM-DD HH:MM:SS'}, ...]
    خروجی: PNG bytes.
    """
    if not rates:
        return _placeholder('داده‌ای ثبت نشده')

    xs = [_parse_dt(r['created_at']) for r in rates]
    ys = [float(r['value']) for r in rates]

    fig, ax = plt.subplots(figsize=(8.5, 4.2), dpi=130)
    ax.plot(xs, ys, color=accent, linewidth=2.2, marker='o', markersize=4,
            markerfacecolor='white', markeredgewidth=1.6)

    # نقطه آخر برجسته
    ax.plot([xs[-1]], [ys[-1]], 'o', color=accent, markersize=9, zorder=5)
    last = f"{ys[-1]:,.4f}".rstrip('0').rstrip('.') if ys[-1] % 1 else f"{int(ys[-1]):,}"
    ax.annotate(
        f' {last}',
        xy=(xs[-1], ys[-1]),
        xytext=(8, 8), textcoords='offset points',
        fontsize=11, fontweight='bold', color=accent,
    )

    ax.set_title(title or 'تغییرات نرخ', fontsize=12, pad=12)
    if y_label:
        ax.set_ylabel(y_label, fontsize=10)
    ax.grid(True, linestyle='--', alpha=0.35)
    ax.spines['top'].set_visible(False)
    ax.spines['right'].set_visible(False)

    # فرمت تاریخ X
    locator = mdates.AutoDateLocator(maxticks=8)
    fmt = mdates.AutoDateFormatter(locator)
    ax.xaxis.set_major_locator(locator)
    ax.xaxis.set_major_formatter(fmt)
    fig.autofmt_xdate(rotation=0, ha='center')

    # رنج Y کمی پدینگ
    if len(ys) > 1:
        lo, hi = min(ys), max(ys)
        if hi == lo:
            pad = abs(hi) * 0.01 + 1
        else:
            pad = (hi - lo) * 0.1
        ax.set_ylim(lo - pad, hi + pad)

    # تعداد نقطه و بازه در subtitle
    n = len(rates)
    span = xs[-1] - xs[0]
    span_txt = _humanize_span(span)
    fig.text(0.5, 0.01, f'{n} نقطه · {span_txt}', ha='center',
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
        return f'{total // 60} دقیقه'
    if total < 86400:
        return f'{total // 3600} ساعت'
    return f'{total // 86400} روز'


def _placeholder(text: str) -> bytes:
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
