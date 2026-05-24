"""رندر ApexCharts (headless chromium via Playwright) → PNG bytes.

این ماژول lazy لود می‌شود: تا اولین فراخوانی به Playwright دست نمی‌زند تا
اگر روی سرور نصب نباشد، فوروارد عادی همچنان کار کند و fallback به matplotlib شود.

نصب پیش‌نیاز روی سرور:
    pip install playwright
    playwright install --with-deps chromium

با reuse کردن browser instance، رندرهای بعدی بسیار سریع‌ترند (~300ms).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

logger = logging.getLogger('telefilter.apex_chart')

_PW_READY: bool | None = None
_LOAD_ERROR: str = ''
_browser = None
_playwright = None
_init_lock: asyncio.Lock | None = None


def _get_lock() -> asyncio.Lock:
    global _init_lock
    if _init_lock is None:
        _init_lock = asyncio.Lock()
    return _init_lock


async def _ensure_browser(force: bool = False) -> bool:
    global _PW_READY, _LOAD_ERROR, _browser, _playwright

    async with _get_lock():
        if not force and _PW_READY is True and _browser is not None:
            try:
                if _browser.is_connected():
                    return True
            except Exception:
                pass
        if not force and _PW_READY is False:
            return False

        try:
            from playwright.async_api import async_playwright
        except ImportError as e:
            _LOAD_ERROR = f'ImportError: {e} — pip install playwright'
            logger.error(_LOAD_ERROR)
            _PW_READY = False
            return False

        # cleanup قبلی (اگر force)
        if _browser is not None:
            try:
                await _browser.close()
            except Exception:
                pass
            _browser = None
        if _playwright is not None and force:
            try:
                await _playwright.stop()
            except Exception:
                pass
            _playwright = None

        try:
            if _playwright is None:
                _playwright = await async_playwright().start()
            _browser = await _playwright.chromium.launch(
                headless=True,
                args=['--no-sandbox', '--disable-dev-shm-usage'],
            )
            _PW_READY = True
            _LOAD_ERROR = ''
            logger.info("playwright chromium launched ok")
            return True
        except Exception as e:
            kind = type(e).__name__
            _LOAD_ERROR = f'{kind}: {e}'
            logger.error(
                "playwright launch failed [%s]: %s — اگر روی سرور است: "
                "playwright install --with-deps chromium", kind, e,
            )
            _PW_READY = False
            return False


def is_available() -> bool:
    """فقط می‌گوید آیا قبلاً init موفق بوده — برای بررسی، _ensure_browser را await کنید."""
    return _PW_READY is True


def load_error() -> str:
    return _LOAD_ERROR


async def reload() -> bool:
    return await _ensure_browser(force=True)


# ── HTML template ──────────────────────────────────────────
# تمام کد در یک رشته است تا تنها یک setContent لازم باشد.
_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="fa" dir="rtl">
<head>
<meta charset="UTF-8">
<style>
  @import url('https://fonts.googleapis.com/css2?family=Vazirmatn:wght@400;500;600;700&display=swap');
  *, *::before, *::after { box-sizing: border-box; font-family: 'Vazirmatn', sans-serif; }
  body { margin: 0; padding: 18px 22px; background: #ffffff; }
  .wrap { width: 920px; }
  .hdr { display: flex; justify-content: space-between; align-items: flex-end;
    margin-bottom: 12px; padding-bottom: 10px; border-bottom: 1.5px solid #e2e8f0; }
  .ttl { font-size: 22px; font-weight: 700; color: #1e293b; }
  .sub { font-size: 12px; color: #64748b; margin-top: 4px; }
  .last { text-align: center; }
  .last .v { font-size: 28px; font-weight: 700; color: #1e40af; line-height: 1; }
  .last .t { font-size: 10px; color: #94a3b8; margin-top: 4px; }
  .badge { display: inline-block; padding: 2px 9px; border-radius: 8px;
    font-size: 10px; font-weight: 600; margin-right: 6px; }
  .badge.raw   { background: #dcfce7; color: #166534; }
  .badge.daily { background: #fef3c7; color: #854d0e; }
  #chart { width: 920px; height: 420px; }
  .ftr { font-size: 10px; color: #94a3b8; text-align: center; margin-top: 8px; }
</style>
</head>
<body>
<div class="wrap">
  <div class="hdr">
    <div>
      <div class="ttl">__TITLE__</div>
      <div class="sub">__SUBTITLE__ <span class="badge __MODE__">__MODE_TXT__</span></div>
    </div>
    <div class="last">
      <div class="v">__LAST_VAL__</div>
      <div class="t">__LAST_TIME__</div>
    </div>
  </div>
  <div id="chart"></div>
  <div class="ftr">TeleFilter · __FOOTER__</div>
</div>
<script src="https://cdn.jsdelivr.net/npm/apexcharts"></script>
<script>
  const SERIES = __SERIES__;
  const MODE = "__MODE__";
  const LABEL = "__LABEL__";

  function fmtNum(n) {
    if (n == null) return '—';
    if (Number.isInteger(n)) return n.toLocaleString('en-US');
    return Number(n).toLocaleString('en-US', { maximumFractionDigits: 4 });
  }

  const options = {
    chart: {
      type: 'area', height: 420, fontFamily: 'Vazirmatn, sans-serif',
      animations: { enabled: false },
      toolbar: { show: false },
      zoom: { enabled: false },
    },
    series: [{ name: LABEL, data: SERIES }],
    dataLabels: { enabled: false },
    stroke: { curve: 'smooth', width: 2.8 },
    fill: {
      type: 'gradient',
      gradient: { shadeIntensity: 1, opacityFrom: .4, opacityTo: 0, stops: [0, 90, 100] },
    },
    colors: ['#1e40af'],
    markers: {
      size: MODE === 'daily' ? 5 : 3,
      colors: ['#fff'],
      strokeColors: '#1e40af',
      strokeWidth: 2,
    },
    grid: { borderColor: '#e2e8f0', strokeDashArray: 3 },
    xaxis: {
      type: 'datetime',
      labels: { datetimeUTC: false, style: { fontFamily: 'Vazirmatn', fontSize: '11px' } },
      axisBorder: { show: false },
    },
    yaxis: {
      labels: {
        style: { fontFamily: 'Vazirmatn', fontSize: '11px' },
        formatter: v => fmtNum(v),
      },
      opposite: true,
    },
    legend: { show: false },
    tooltip: { enabled: false },
  };

  const chart = new ApexCharts(document.querySelector("#chart"), options);
  chart.render().then(() => {
    window.__APEX_RENDERED = true;
  });
</script>
</body>
</html>
"""


def _build_html(rates: list[dict], title: str, mode: str, days: int) -> str:
    """HTML را با داده‌ها fill می‌کند."""
    def _ts_ms(s: str) -> int:
        # 'YYYY-MM-DD HH:MM:SS' (UTC) → epoch ms
        from datetime import datetime, timezone
        for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S'):
            try:
                dt = datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
                return int(dt.timestamp() * 1000)
            except ValueError:
                continue
        return 0

    series = [{'x': _ts_ms(r['created_at']), 'y': r['value']} for r in rates if r.get('created_at')]
    last_v = rates[-1]['value'] if rates else None
    last_t = rates[-1]['created_at'][:16] if rates else ''
    if last_v is not None:
        last_val = f"{int(last_v):,}" if last_v == int(last_v) else f"{last_v:,.4f}".rstrip('0').rstrip('.')
    else:
        last_val = '—'

    mode_txt = 'دستیار روزانه' if mode == 'daily' else 'لحظه‌ای'
    sub = f"{len(rates)} نقطه · بازه {days} روز اخیر"
    ftr = time.strftime('%Y-%m-%d %H:%M')

    return (_HTML_TEMPLATE
            .replace('__TITLE__', _esc(title or 'Rate Chart'))
            .replace('__SUBTITLE__', _esc(sub))
            .replace('__MODE__', mode if mode in ('raw', 'daily') else 'raw')
            .replace('__MODE_TXT__', _esc(mode_txt))
            .replace('__LAST_VAL__', _esc(last_val))
            .replace('__LAST_TIME__', _esc(last_t))
            .replace('__FOOTER__', _esc(ftr))
            .replace('__SERIES__', json.dumps(series))
            .replace('__LABEL__', _esc(title or 'rate')))


def _esc(s: str) -> str:
    return (str(s or '')
            .replace('&', '&amp;')
            .replace('<', '&lt;')
            .replace('>', '&gt;')
            .replace('"', '&quot;'))


async def render_chart_png(
    rates: list[dict],
    title: str = '',
    mode: str = 'raw',
    days: int = 7,
    timeout_ms: int = 8000,
) -> bytes | None:
    """
    رندر یک نمودار با ApexCharts و برگرداندن PNG bytes.
    اگر Playwright در دسترس نباشد، None برمی‌گرداند تا caller fallback کند.
    """
    if not await _ensure_browser():
        return None
    if not rates:
        return None

    html = _build_html(rates, title, mode, days)
    try:
        ctx = await _browser.new_context(viewport={'width': 980, 'height': 540})
        page = await ctx.new_page()
        try:
            await page.set_content(html, wait_until='load', timeout=timeout_ms)
            # منتظر بمان تا apex flag __APEX_RENDERED ست شود
            await page.wait_for_function('window.__APEX_RENDERED === true', timeout=timeout_ms)
            # یک مکث کوتاه برای font loading
            await page.wait_for_timeout(250)
            el = await page.query_selector('.wrap')
            if el is None:
                el = page
            png = await el.screenshot(type='png', omit_background=False)
            return png
        finally:
            await page.close()
            await ctx.close()
    except Exception as e:
        logger.error("apex render failed: %s", e, exc_info=True)
        return None


async def close_browser():
    """در shutdown صدا زده می‌شود."""
    global _browser, _playwright, _PW_READY
    if _browser is not None:
        try:
            await _browser.close()
        except Exception:
            pass
        _browser = None
    if _playwright is not None:
        try:
            await _playwright.stop()
        except Exception:
            pass
        _playwright = None
    _PW_READY = None
