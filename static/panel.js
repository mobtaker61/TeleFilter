/* TeleFilter panel UI */
let groups = [], cfgMap = {};
let selGroupId = null, selTopicId = null;
let tgTopicsByGroup = {};
let expandedGroups = new Set();
let tgOk = false, forumOk = false, botStatus = 'stopped', hasSession = false;
let needsApi = false, needsTelethon = false;
let isAdmin = window.TF_IS_ADMIN === true || window.TF_IS_ADMIN === 'true';
let loginMdl, settingsMdl, createMdl, setupMdl, adminMdl, linkGroupMdl, createGroupMdl;
let qrPollTimer = null;

document.addEventListener('DOMContentLoaded', async () => {
  loginMdl = new bootstrap.Modal(document.getElementById('loginModal'));
  settingsMdl = new bootstrap.Modal(document.getElementById('settingsModal'));
  createMdl = new bootstrap.Modal(document.getElementById('createModal'));
  setupMdl = new bootstrap.Modal(document.getElementById('setupModal'));
  adminMdl = new bootstrap.Modal(document.getElementById('adminModal'));
  linkGroupMdl = new bootstrap.Modal(document.getElementById('linkGroupModal'));
  createGroupMdl = new bootstrap.Modal(document.getElementById('createGroupModal'));

  document.addEventListener('click', e => {
    if (!e.target.closest('#userPill') && !e.target.closest('#userMenu'))
      document.getElementById('userMenu').style.display = 'none';
  });

  await loadConfigFromServer();
  await runOnboarding();
});

async function loadConfigFromServer() {
  const cfg = await (await fetch('/api/config')).json();
  groups = cfg.groups || [];
  cfgMap = buildCfgMapFromGroups(groups);
}

function buildCfgMapFromGroups(grps) {
  const m = {};
  for (const g of grps) {
    for (const t of g.topics || []) {
      const sources = (t.sources || []).map(s => ({
        chat: s.chat || '',
        filters: normalizeFilters(s.filters),
        value_regex: s.value_regex || '',
      }));
      m[cfgKey(g.id, t.topic_id)] = {
        sources,
        chart_enabled: !!t.chart_enabled,
        chart_label: t.chart_label || '',
      };
    }
  }
  return m;
}

function cfgKey(gid, tid) { return `${gid}:${tid}`; }

function groupIsForum(g) {
  if (!g) return false;
  if (g.is_forum === true) return true;
  if (g.is_forum === false) return false;
  const topics = tgTopicsByGroup[g.id];
  if (topics?.length) return topics.some(t => t.id > 0);
  return false;
}

function ensureCfg() {
  const k = cfgKey(selGroupId, selTopicId);
  if (!cfgMap[k]) cfgMap[k] = { sources: [], chart_enabled: false, chart_label: '' };
  if (cfgMap[k].chart_enabled === undefined) cfgMap[k].chart_enabled = false;
  if (cfgMap[k].chart_label === undefined) cfgMap[k].chart_label = '';
  return cfgMap[k];
}

async function runOnboarding() {
  await refreshStatus();
  if (needsApi) {
    renderTree();
    showToast('ادمین: API تلگرام را در setup سرور تنظیم کنید', 'warning');
    return;
  }
  if (needsTelethon) {
    openTelethonSetup(true);
    return;
  }
  await bootMain();
}

async function refreshStatus() {
  try {
    const s = await (await fetch('/api/status')).json();
    tgOk = s.connected;
    forumOk = s.has_forum_api;
    botStatus = s.bot;
    hasSession = !!s.has_session;
    needsApi = !!s.needs_api;
    needsTelethon = !!s.needs_telethon;
    if (s.is_admin !== undefined) isAdmin = !!s.is_admin;
    window._sourcesConfigured = s.sources_configured ?? 0;
    updateConnBadge();
  } catch { /* silent */ }
}

async function showForwarderLogs() {
  try {
    const d = await (await fetch('/api/forwarder/logs?n=40')).json();
    const lines = (d.logs || []).slice(-12).join('\n') || 'لاگی موجود نیست';
    showToast(lines.slice(0, 200), 'danger');
  } catch {
    showToast('خطا در خواندن لاگ فوروارد', 'danger');
  }
}

let statusPollTimer = null;

function startStatusPolling() {
  if (statusPollTimer) clearTimeout(statusPollTimer);
  const schedule = () => {
    const delay = (hasSession && botStatus !== 'running') ? 2500 : 30000;
    statusPollTimer = setTimeout(async () => {
      await refreshStatus();
      schedule();
    }, delay);
  };
  schedule();
  setTimeout(refreshStatus, 4000);
  setTimeout(refreshStatus, 9000);
}

function updateConnBadge() {
  const b = document.getElementById('connBadge');
  if (hasSession && botStatus === 'running') {
    b.className = 'tbadge ok';
    const srcHint = (window._sourcesConfigured === 0) ? ' — سورسی ذخیره نشده' : '';
    b.innerHTML = `<i class="bi bi-circle-fill dot"></i> فوروارد فعال${srcHint}`;
    b.onclick = null;
    b.style.cursor = 'default';
  } else if (hasSession && String(botStatus).startsWith('crashed')) {
    b.className = 'tbadge err';
    b.innerHTML = '<i class="bi bi-circle-fill dot"></i> فوروارد خطا — کلیک برای جزئیات';
    b.onclick = () => showForwarderLogs();
    b.style.cursor = 'pointer';
  } else if (hasSession && !tgOk) {
    b.className = 'tbadge warn';
    b.innerHTML = '<i class="bi bi-circle-fill dot"></i> تکمیل اتصال';
    b.onclick = () => openTelethonSetup(false);
    b.style.cursor = 'pointer';
  } else if (hasSession) {
    b.className = 'tbadge warn';
    b.innerHTML = '<i class="bi bi-circle-fill dot"></i> در حال راه‌اندازی…';
    b.onclick = null;
    b.style.cursor = 'default';
  } else if (needsTelethon) {
    b.className = 'tbadge warn';
    b.innerHTML = '<i class="bi bi-circle-fill dot"></i> تکمیل اتصال';
    b.onclick = () => openTelethonSetup(false);
    b.style.cursor = 'pointer';
  } else if (tgOk) {
    b.className = 'tbadge ok';
    b.innerHTML = '<i class="bi bi-circle-fill dot"></i> متصل';
    b.onclick = null;
  } else {
    b.className = 'tbadge err';
    b.innerHTML = '<i class="bi bi-circle-fill dot"></i> اتصال';
    b.onclick = () => openTelethonSetup(false);
    b.style.cursor = 'pointer';
  }
}

async function openTelethonSetup(auto) {
  stopQrPoll();
  loginMdl.show();
  showLoginView('loading');
  hideLoginErrors();
  try {
    const d = await (await fetch('/api/auth/prepare', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: '{}',
    })).json();
    if (d.already) {
      loginMdl.hide();
      await onLoginSuccess();
      return;
    }
    if (!d.ok) {
      showToast(d.error || 'خطا در اتصال', 'danger');
      showLoginView('phone');
      setLoginBtn('ارسال کد', doSendCode);
      return;
    }
    if (d.step === 'code') {
      const name = d.first_name || 'کاربر';
      const hint = d.masked_phone ? ` (${d.masked_phone})` : '';
      document.getElementById('loginSubtitle2').textContent =
        d.auto_code
          ? `${name}، کد به تلگرام شما ارسال شد${hint} — همان اکانتی که با آن وارد شدی.`
          : 'کد ۵ رقمی را وارد کن';
      showLoginView('code');
      setLoginBtn('تأیید کد', doVerifyCode);
      document.getElementById('loginBtn').disabled = false;
      setTimeout(() => document.getElementById('loginCode').focus(), 200);
      return;
    }
    if (d.step === 'qr') {
      const name = d.first_name || 'کاربر';
      document.getElementById('qrHint').textContent =
        `${name}، در اپ تلگرام همان اکانت را تأیید کن (بدون وارد کردن شماره)`;
      showLoginView('qr', d.qr_url);
      startQrPoll();
      return;
    }
    showLoginView('phone');
    setLoginBtn('ارسال کد', doSendCode);
  } catch {
    showToast('خطای شبکه', 'danger');
    showLoginView('phone');
    setLoginBtn('ارسال کد', doSendCode);
  }
}

function stopQrPoll() {
  if (qrPollTimer) {
    clearInterval(qrPollTimer);
    qrPollTimer = null;
  }
}

function startQrPoll() {
  stopQrPoll();
  qrPollTimer = setInterval(async () => {
    try {
      const d = await (await fetch('/api/auth/qr_status')).json();
      if (d.ok && d.done) {
        stopQrPoll();
        loginMdl.hide();
        await onLoginSuccess();
      } else if (d.error) {
        stopQrPoll();
        showToast(d.error, 'danger');
        showPhoneLoginFallback();
      }
    } catch { /* retry */ }
  }, 2000);
}

function showPhoneLoginFallback() {
  stopQrPoll();
  showLoginView('phone');
  setLoginBtn('ارسال کد', doSendCode);
  document.getElementById('loginBtn').disabled = false;
  setTimeout(() => document.getElementById('loginPhone').focus(), 200);
}

function showLoginView(mode, qrUrl) {
  ['loginLoading', 'lstepQr', 'lstep1', 'lstep2', 'lstep3'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.classList.remove('active');
  });
  const ind = document.getElementById('loginStepIndicator');
  const btn = document.getElementById('loginBtn');
  if (mode === 'loading') {
    document.getElementById('loginLoading').classList.add('active');
    if (ind) ind.style.display = 'none';
    if (btn) btn.style.display = 'none';
  } else if (mode === 'qr') {
    document.getElementById('lstepQr').classList.add('active');
    document.getElementById('qrImage').src =
      'https://api.qrserver.com/v1/create-qr-code/?size=220x220&data=' + encodeURIComponent(qrUrl || '');
    if (ind) ind.style.display = 'none';
    if (btn) btn.style.display = 'none';
  } else if (mode === 'code') {
    document.getElementById('lstep2').classList.add('active');
    if (ind) {
      ind.style.display = 'flex';
      ind.classList.add('compact');
    }
    showLoginStep(2);
    if (btn) btn.style.display = '';
  } else if (mode === 'phone') {
    document.getElementById('lstep1').classList.add('active');
    if (ind) {
      ind.style.display = 'flex';
      ind.classList.remove('compact');
    }
    showLoginStep(1);
    if (btn) btn.style.display = '';
  }
}

function onBadgeClick() {
  if (needsTelethon || !hasSession) openTelethonSetup(false);
}

function chartHtml(chart) {
  const maxC = Math.max(1, ...(chart || []).map(x => x.count));
  return (chart || []).map(x => {
    const h = Math.round((x.count / maxC) * 100);
    return `<div class="dash-bar-wrap" title="${x.date}: ${x.count}">
      <div class="dash-bar" style="height:${h}%"></div>
      <span class="dash-bar-lbl">${x.date.slice(5)}</span></div>`;
  }).join('') || '<span class="text-muted">هنوز داده‌ای نیست</span>';
}

function hideMainPanels() {
  document.getElementById('dashboardPanel').style.display = 'none';
  document.getElementById('groupDetailPanel').style.display = 'none';
  document.getElementById('topicDetail').style.display = 'none';
}

// ── Dashboard (کل حساب) ───────────────────────────────
async function loadDashboard() {
  hideMainPanels();
  const el = document.getElementById('dashboardPanel');
  el.style.display = 'block';
  selGroupId = null;
  selTopicId = null;
  renderTree();
  try {
    const s = await (await fetch('/api/dashboard/stats')).json();
    el.innerHTML = `
      <h5 class="mb-3" style="font-weight:700;color:#1e293b"><i class="bi bi-speedometer2 me-2"></i>داشبورد</h5>
      <div class="dash-grid">
        <div class="dash-card"><div class="dash-num">${s.groups || 0}</div><div class="dash-lbl">گروه</div></div>
        <div class="dash-card"><div class="dash-num">${s.topics || 0}</div><div class="dash-lbl">Topic</div></div>
        <div class="dash-card"><div class="dash-num">${s.sources || 0}</div><div class="dash-lbl">سورس</div></div>
        <div class="dash-card"><div class="dash-num">${s.filters || 0}</div><div class="dash-lbl">عبارت فیلتر</div></div>
        <div class="dash-card accent"><div class="dash-num">${s.forwards_today || 0}</div><div class="dash-lbl">فوروارد امروز</div></div>
        <div class="dash-card"><div class="dash-num">${s.forwards_total || 0}</div><div class="dash-lbl">کل فوروارد</div></div>
      </div>
      <div class="dash-chart-box">
        <div class="dash-chart-title"><i class="bi bi-bar-chart-fill"></i> فوروارد ۷ روز اخیر (همه گروه‌ها)</div>
        <div class="dash-chart">${chartHtml(s.chart)}</div>
      </div>
      <p class="dash-hint">روی نام گروه کلیک کنید — با + کنار گروه، Topics را باز کنید.</p>`;
  } catch {
    el.innerHTML = '<p class="text-muted">خطا در بارگذاری آمار</p>';
  }
}

// ── داشبورد گروه ───────────────────────────────────────
async function loadGroupDashboard(gid) {
  hideMainPanels();
  const el = document.getElementById('groupDetailPanel');
  el.style.display = 'block';
  const g = groups.find(x => x.id === gid);
  if (!g) return;
  el.innerHTML = '<div class="text-muted py-3"><i class="bi bi-hourglass-split spin"></i></div>';
  try {
    const [statsRes, infoRes] = await Promise.all([
      fetch(`/api/dashboard/stats?group_id=${encodeURIComponent(gid)}`),
      fetch(`/api/groups/${encodeURIComponent(gid)}/info`),
    ]);
    const s = await statsRes.json();
    const info = await infoRes.json();
    const topics = tgTopicsByGroup[gid] || [];
    const title = (info.ok && info.title) ? info.title : g.title;
    const about = (info.ok && info.about) ? info.about : '';
    const membersCount = info.members_count != null ? info.members_count : '—';
    const membersHtml = (info.participants || []).map(p => `
      <div class="member-row d-flex justify-content-between align-items-center gap-2">
        <span>${esc(p.name)}${p.username ? ` <span class="text-muted">@${esc(p.username)}</span>` : ''}</span>
        <span class="d-flex align-items-center gap-1">
          ${p.is_bot ? '<span class="badge bg-secondary">bot</span>' : ''}
          ${!p.is_bot ? `<button type="button" class="btn btn-sm btn-outline-danger py-0 px-1" title="حذف از گروه"
            onclick="removeGroupMember('${gid}',${p.id})"><i class="bi bi-person-x"></i></button>` : ''}
        </span>
      </div>`).join('') || '<p class="text-muted small mb-0">لیست اعضا در دسترس نیست — اکانت شما باید ادمین گروه باشد.</p>';
    const topicMini = (s.topic_list || []).map(t => {
      const tg = topics.find(x => x.id === t.topic_id);
      const name = tg?.title || t.name || `Topic ${t.topic_id}`;
      return `<div class="topic-mini-row" onclick="selectTopic('${gid}',${t.topic_id})">
        <span><i class="bi bi-hash text-primary me-1"></i>${esc(name)}</span>
        <span class="text-muted">${t.sources} سورس</span></div>`;
    }).join('') || '<p class="text-muted small mb-0">هنوز Topic با سورس ندارید.</p>';
    el.innerHTML = `
      <div class="group-profile-hdr">
        <div class="group-profile-icon"><i class="bi bi-people-fill"></i></div>
        <div style="flex:1">
          <h5 class="mb-1" style="font-weight:700">${esc(title)}</h5>
          <div class="text-muted" style="font-size:.8rem">ID: <span dir="ltr">${esc(g.telegram_id)}</span>
            · ${membersCount} عضو
            · ${g.origin === 'created' ? 'ساخته‌شده' : 'متصل'}</div>
          ${about ? `<p class="mt-2 mb-0" style="font-size:.85rem;color:#475569;line-height:1.7">${esc(about)}</p>` : ''}
        </div>
        <div class="d-flex flex-column gap-1">
          <button class="btn btn-sm btn-outline-secondary" onclick="goHome()"><i class="bi bi-grid me-1"></i>داشبورد کل</button>
          <button class="btn btn-sm btn-outline-danger" onclick="deleteGroupFromList('${gid}')"><i class="bi bi-trash me-1"></i>حذف از لیست</button>
        </div>
      </div>
      <div class="alert alert-light border mb-3 py-2" style="font-size:.78rem">
        <i class="bi bi-info-circle me-1"></i>
        فوروارد با <strong>اکانت شخصی</strong> شما انجام می‌شود (نه ربات). باید عضو گروه مقصد باشید و پس از تنظیم سورس‌ها <strong>ذخیره تغییرات</strong> بزنید.
      </div>
      <div class="dash-grid">
        <div class="dash-card"><div class="dash-num">${s.topics || 0}</div><div class="dash-lbl">Topic</div></div>
        <div class="dash-card"><div class="dash-num">${s.sources || 0}</div><div class="dash-lbl">سورس</div></div>
        <div class="dash-card"><div class="dash-num">${s.filters || 0}</div><div class="dash-lbl">فیلتر</div></div>
        <div class="dash-card accent"><div class="dash-num">${s.forwards_today || 0}</div><div class="dash-lbl">فوروارد امروز</div></div>
        <div class="dash-card"><div class="dash-num">${s.forwards_total || 0}</div><div class="dash-lbl">فوروارد این گروه</div></div>
        <div class="dash-card"><div class="dash-num">${topics.length}</div><div class="dash-lbl">Topic در تلگرام</div></div>
      </div>
      <div class="dash-chart-box mb-3">
        <div class="dash-chart-title"><i class="bi bi-bar-chart-fill"></i> فوروارد این گروه — ۷ روز</div>
        <div class="dash-chart">${chartHtml(s.chart)}</div>
      </div>
      <div class="members-box mb-3">
        <h6 style="font-size:.85rem;font-weight:600;color:#334155"><i class="bi bi-person-lines-fill me-1"></i>اعضا</h6>
        <div class="d-flex gap-2 mb-2">
          <input type="text" class="form-control form-control-sm" id="inviteMemberInput" dir="ltr"
            placeholder="@username یا شناسه عددی">
          <button type="button" class="btn btn-sm btn-primary" onclick="inviteGroupMember('${gid}')">افزودن</button>
        </div>
        <div class="members-list">${membersHtml}</div>
      </div>
      <div class="topic-mini-list">
        <h6 style="font-size:.85rem;font-weight:600;color:#334155"><i class="bi bi-hash me-1"></i>Topics و سورس‌ها</h6>
        ${topicMini}
      </div>`;
  } catch {
    el.innerHTML = '<p class="text-danger">خطا در بارگذاری</p>';
  }
}

// ── Tree sidebar (آکاردئون) ───────────────────────────
function toggleGroupExpand(gid, ev) {
  if (ev) ev.stopPropagation();
  if (expandedGroups.has(gid)) expandedGroups.delete(gid);
  else {
    expandedGroups.add(gid);
    syncTopicsForGroup(gid);
  }
  renderTree();
}

function renderTree() {
  const el = document.getElementById('treeList');
  if (needsApi) {
    el.innerHTML = '<div class="sidebar-msg"><i class="bi bi-key-fill"></i>API توسط ادمین سرور تنظیم نشده.</div>';
    return;
  }
  if (needsTelethon) {
    el.innerHTML = '<div class="sidebar-msg"><i class="bi bi-phone-fill"></i>در حال تکمیل اتصال تلگرام…</div>';
    return;
  }
  if (!groups.length) {
    el.innerHTML = `<div class="sidebar-msg"><i class="bi bi-folder-plus"></i>هنوز گروهی ندارید.<br>
      <button class="btn btn-sm btn-primary mt-2" onclick="openLinkGroup()">افزودن گروه</button></div>`;
    return;
  }
  el.innerHTML = groups.map(g => {
    const topics = tgTopicsByGroup[g.id] || (groupIsForum(g) ? [] : [{ id: 0, title: 'چت اصلی' }]);
    const isOpen = expandedGroups.has(g.id);
    const gActive = selGroupId === g.id && !selTopicId;
    const forum = groupIsForum(g);
    const topicRows = topics.map(t => {
      const cnt = (cfgMap[cfgKey(g.id, t.id)] || {}).sources?.length || 0;
      const active = selGroupId === g.id && selTopicId === t.id;
      return `<div class="topic-row child ${active ? 'active' : ''}" onclick="selectTopic('${g.id}',${t.id})">
        <div class="t-name">${esc(t.title)}</div>
        <span class="t-src-badge ${cnt ? '' : 'empty'}">${cnt ? cnt + ' سورس' : '—'}</span></div>`;
    }).join('');
    return `<div class="group-block">
      <div class="group-row ${gActive ? 'active' : ''}">
        <button type="button" class="group-expand ${isOpen ? 'open' : ''}" onclick="toggleGroupExpand('${g.id}', event)" title="نمایش Topics">
          <i class="bi bi-chevron-left"></i></button>
        <button type="button" class="group-title-btn" onclick="selectGroup('${g.id}')">
          <div class="t-name">${esc(g.title)}</div>
          <div class="t-meta">${forum ? topics.length + ' topic' : 'گروه/کانال عادی'} · ${g.origin === 'created' ? 'ساخته‌شده' : 'متصل'}</div>
        </button>
        <div class="group-actions">
          ${forum ? `<button type="button" class="btn-group-mini" title="Topic جدید" onclick="openCreateTopic('${g.id}', event)"><i class="bi bi-hash"></i></button>` : ''}
        </div>
      </div>
      <div class="topics-collapse ${isOpen ? 'open' : ''}">${topicRows || '<div class="sidebar-msg" style="padding:.45rem 1rem .45rem 2.2rem;font-size:.72rem">Topic ندارد — # را بزنید</div>'}</div>
    </div>`;
  }).join('');
}

function selectGroup(gid) {
  selGroupId = gid;
  selTopicId = null;
  renderTree();
  loadGroupDashboard(gid);
}

function selectTopic(gid, tid) {
  selGroupId = gid;
  selTopicId = tid;
  expandedGroups.add(gid);
  renderTree();
  renderTopicDetail();
}

function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

async function syncTopicsForGroup(gid, maxAttempts = 1) {
  if (!tgOk) return;
  const icon = document.getElementById('syncIcon');
  if (icon) icon.className = 'bi bi-arrow-clockwise spin';
  try {
    for (let i = 0; i < maxAttempts; i++) {
      const d = await (await fetch(`/api/telegram/topics?group_id=${encodeURIComponent(gid)}`)).json();
      if (d.topics) {
        tgTopicsByGroup[gid] = d.topics;
        const g = groups.find(x => x.id === gid);
        if (g && d.is_forum !== undefined) g.is_forum = d.is_forum;
        renderTree();
      }
      if (i < maxAttempts - 1) await sleep(450);
    }
  } catch { /* silent */ }
  if (icon) icon.className = 'bi bi-arrow-clockwise';
}

async function syncAllTopics() {
  for (const g of groups) await syncTopicsForGroup(g.id);
}

// ── Groups ──────────────────────────────────────────────
function openLinkGroup() {
  if (needsTelethon || (hasSession && !tgOk)) { openTelethonSetup(false); return; }
  document.getElementById('linkGroupList').innerHTML = '<div class="text-muted py-2">در حال بارگذاری…</div>';
  linkGroupMdl.show();
  loadLinkCandidates();
}

async function loadLinkCandidates() {
  const box = document.getElementById('linkGroupList');
  try {
    const d = await (await fetch('/api/telegram/dialogs')).json();
    if (!d.dialogs?.length) {
      box.innerHTML = '<p class="text-muted">گروهی یافت نشد.</p>';
      return;
    }
    box.innerHTML = d.dialogs.map(di => {
      const kindLbl = di.kind === 'channel' ? 'کانال' : (di.is_forum ? 'Forum' : 'گروه');
      return `
      <div class="link-row ${di.already_linked ? 'disabled' : ''}" data-id="${di.id}" data-title="${esc(di.title)}"
        data-forum="${di.is_forum ? '1' : '0'}">
        <div><strong>${esc(di.title)}</strong><br><small class="text-muted">${di.id} · ${kindLbl}</small></div>
        ${di.already_linked ? '<span class="badge bg-secondary">اضافه شده</span>' : '<i class="bi bi-plus-lg"></i>'}
      </div>`;
    }).join('');
    box.querySelectorAll('.link-row:not(.disabled)').forEach(el => {
      el.onclick = () => doLinkGroup(
        parseInt(el.dataset.id, 10),
        el.dataset.title,
        el.dataset.forum === '1',
      );
    });
  } catch {
    box.innerHTML = '<p class="text-danger">خطا در دریافت لیست</p>';
  }
}

async function doLinkGroup(tid, title, isForum = false) {
  const d = await (await fetch('/api/groups/link', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ telegram_id: tid, title, is_forum: !!isForum }),
  })).json();
  if (d.ok) {
    await loadConfigFromServer();
    linkGroupMdl.hide();
    showToast('گروه اضافه شد ✓', 'success');
    await syncTopicsForGroup(d.group.id, 5);
    selectGroup(d.group.id);
  }
}

function openCreateGroup() {
  document.getElementById('newGroupTitle').value = '';
  createGroupMdl.show();
}

async function doCreateGroup() {
  const title = document.getElementById('newGroupTitle').value.trim();
  if (!title) return;
  const btn = document.getElementById('createGroupBtn');
  btn.disabled = true;
  try {
    const forum = document.getElementById('newGroupForum')?.checked !== false;
    const d = await (await fetch('/api/groups/create', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title, forum }),
    })).json();
    if (d.ok) {
      await loadConfigFromServer();
      createGroupMdl.hide();
      showToast('گروه ساخته شد ✓', 'success');
      await syncTopicsForGroup(d.group.id, 5);
      selectGroup(d.group.id);
    } else showToast(d.msg || 'خطا', 'danger');
  } catch { showToast('خطای شبکه', 'danger'); }
  btn.disabled = false;
}

// ── Login ───────────────────────────────────────────────
function showLoginStep(n) {
  const compact = document.getElementById('loginStepIndicator')?.classList.contains('compact');
  const map = compact ? { 2: 1, 3: 2 } : { 1: 1, 2: 2, 3: 3 };
  const visual = map[n] || n;
  [1, 2, 3].forEach(i => {
    const d = document.getElementById(`sd${i}`);
    if (!d) return;
    if (compact && i === 3) {
      d.style.display = 'none';
      return;
    }
    d.style.display = '';
    d.classList.toggle('active', i === visual);
    d.classList.toggle('done', i < visual);
  });
  const sl2 = document.getElementById('sl2');
  if (sl2) sl2.classList.toggle('done', visual > 1);
  document.getElementById('sl1')?.classList.toggle('done', visual > 1);
}

function setLoginBtn(label, fn) {
  const b = document.getElementById('loginBtn');
  b.textContent = label;
  b.onclick = fn;
}

function hideLoginErrors() {
  ['err1', 'err2', 'err3'].forEach(id => document.getElementById(id).style.display = 'none');
}

function showLoginError(step, msg) {
  const el = document.getElementById(`err${step}`);
  el.textContent = msg;
  el.style.display = 'block';
}

function setBtnLoading(t) {
  const b = document.getElementById('loginBtn');
  b.disabled = true;
  b.innerHTML = `<i class="bi bi-hourglass-split spin me-1"></i>${t}`;
}

async function doSendCode() {
  const phone = document.getElementById('loginPhone').value.trim();
  if (!phone) { showLoginError(1, 'شماره را وارد کن'); return; }
  hideLoginErrors();
  setBtnLoading('در حال ارسال…');
  try {
    const d = await (await fetch('/api/auth/send_code', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ phone }),
    })).json();
    if (d.ok) {
      showLoginView('code');
      setLoginBtn('تأیید کد', doVerifyCode);
      document.getElementById('loginBtn').disabled = false;
      setTimeout(() => document.getElementById('loginCode').focus(), 100);
    } else {
      showLoginError(1, d.error || 'خطا');
      setLoginBtn('ارسال کد', doSendCode);
      document.getElementById('loginBtn').disabled = false;
    }
  } catch {
    showLoginError(1, 'خطای شبکه');
    setLoginBtn('ارسال کد', doSendCode);
    document.getElementById('loginBtn').disabled = false;
  }
}

async function doVerifyCode() {
  const code = document.getElementById('loginCode').value.trim();
  if (!code) { showLoginError(2, 'کد را وارد کن'); return; }
  hideLoginErrors();
  setBtnLoading('در حال تأیید…');
  try {
    const d = await (await fetch('/api/auth/verify_code', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ code }),
    })).json();
    if (d.ok) await onLoginSuccess();
    else if (d.need_2fa) {
      document.getElementById('lstep3').classList.add('active');
      document.getElementById('loginStepIndicator').style.display = 'flex';
      document.getElementById('loginStepIndicator').classList.remove('compact');
      showLoginStep(3);
      setLoginBtn('تأیید رمز', doVerify2FA);
      document.getElementById('loginBtn').style.display = '';
      document.getElementById('loginBtn').disabled = false;
    } else {
      showLoginError(2, d.error || 'خطا');
      setLoginBtn('تأیید کد', doVerifyCode);
      document.getElementById('loginBtn').disabled = false;
    }
  } catch {
    showLoginError(2, 'خطای شبکه');
    setLoginBtn('تأیید کد', doVerifyCode);
    document.getElementById('loginBtn').disabled = false;
  }
}

async function doVerify2FA() {
  const pw = document.getElementById('login2fa').value;
  if (!pw) { showLoginError(3, 'رمز را وارد کن'); return; }
  hideLoginErrors();
  setBtnLoading('در حال تأیید…');
  try {
    const d = await (await fetch('/api/auth/verify_2fa', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ password: pw }),
    })).json();
    if (d.ok) await onLoginSuccess();
    else {
      showLoginError(3, d.error || 'خطا');
      setLoginBtn('تأیید رمز', doVerify2FA);
      document.getElementById('loginBtn').disabled = false;
    }
  } catch {
    showLoginError(3, 'خطای شبکه');
    setLoginBtn('تأیید رمز', doVerify2FA);
    document.getElementById('loginBtn').disabled = false;
  }
}

async function onLoginSuccess() {
  stopQrPoll();
  loginMdl.hide();
  showToast('اتصال برقرار شد — فوروارد خودکار فعال می‌شود ✓', 'success');
  needsTelethon = false;
  hasSession = true;
  await loadConfigFromServer();
  await refreshStatus();
  await bootMain();
}

async function ensure_client_refresh() {
  await fetch('/api/status');
}

// ── Settings ────────────────────────────────────────────
function openSettings() {
  if (!isAdmin) {
    showToast('API توسط ادمین سرور تنظیم می‌شود', 'warning');
    return;
  }
  settingsMdl.show();
}

function saveSettings() {
  settingsMdl.hide();
}

// ── Topics ──────────────────────────────────────────────
function openCreateTopic(gid, ev) {
  if (ev) ev.stopPropagation();
  const g = gid || selGroupId;
  if (!g) { showToast('گروه مشخص نیست', 'warning'); return; }
  const grp = groups.find(x => x.id === g);
  if (grp && !groupIsForum(grp)) {
    showToast('این گروه Forum نیست — روی «چت اصلی» سورس اضافه کنید', 'warning');
    return;
  }
  selGroupId = g;
  if (!tgOk) { openTelethonSetup(false); return; }
  document.getElementById('newTopicTitle').value = '';
  createMdl.show();
}

async function doCreateTopic() {
  const title = document.getElementById('newTopicTitle').value.trim();
  if (!title || !selGroupId) return;
  const btn = document.getElementById('createBtn');
  btn.disabled = true;
  try {
    const d = await (await fetch('/api/telegram/topics', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title, group_id: selGroupId }),
    })).json();
    if (d.ok) {
      createMdl.hide();
      expandedGroups.add(selGroupId);
      await syncTopicsForGroup(selGroupId, 6);
      let tid = d.topic_id;
      if (!tid && tgTopicsByGroup[selGroupId]) {
        const found = [...tgTopicsByGroup[selGroupId]].reverse().find(t => t.title === title);
        if (found) tid = found.id;
      }
      if (tid) selectTopic(selGroupId, tid);
      else renderTree();
      showToast(`Topic «${title}» ساخته شد ✓`, 'success');
    } else showToast(d.msg || 'خطا', 'danger');
  } catch { showToast('خطای شبکه', 'danger'); }
  btn.disabled = false;
}

function syncTopics() {
  if (selGroupId) syncTopicsForGroup(selGroupId);
  else syncAllTopics();
}

// ── Sources & filters (topic detail) ────────────────────
function addSource() {
  ensureCfg().sources.push({ chat: '', filters: [], value_regex: '' });
  renderTopicDetail();
  markDirty();
}

function updateSourceRegex(si, val) {
  ensureCfg().sources[si].value_regex = val;
  markDirty();
}

function toggleChartEnabled(val) {
  ensureCfg().chart_enabled = !!val;
  renderTopicDetail();
  markDirty();
}

function updateChartLabel(val) {
  ensureCfg().chart_label = val;
  markDirty();
}

async function testSourceRegex(si) {
  const src = ensureCfg().sources[si];
  const sampleEl = document.getElementById(`regex_sample_${si}`);
  const outEl = document.getElementById(`regex_out_${si}`);
  const text = (sampleEl?.value || '').trim();
  if (!text) {
    outEl.innerHTML = '<span class="text-warning">متن نمونه را وارد کنید</span>';
    return;
  }
  try {
    const d = await (await fetch('/api/parse_value/test', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text, regex: src.value_regex || '' }),
    })).json();
    if (d.ok) {
      outEl.innerHTML = `<span class="text-success">✓ مقدار استخراج‌شده: <b>${d.value}</b>${d.raw ? ` <small class="text-muted">(${esc(d.raw)})</small>` : ''}</span>`;
    } else {
      outEl.innerHTML = `<span class="text-danger">✗ ${esc(d.msg || 'استخراج ناموفق')}</span>`;
    }
  } catch {
    outEl.innerHTML = '<span class="text-danger">خطای شبکه</span>';
  }
}

function openChartPage() {
  if (!selGroupId || selTopicId === null || selTopicId === undefined) return;
  window.open(`/chart/${encodeURIComponent(selGroupId)}/${selTopicId}`, '_blank');
}

async function testChartSend() {
  if (!selGroupId || selTopicId === null || selTopicId === undefined) return;
  const btn = document.getElementById('btnTestChart');
  const out = document.getElementById('chartDiagOut');
  if (btn) { btn.disabled = true; btn.innerHTML = '<i class="bi bi-hourglass-split"></i> در حال ارسال…'; }
  out.innerHTML = '';
  try {
    const r = await fetch(
      `/api/charts/${encodeURIComponent(selGroupId)}/${selTopicId}/test_send`,
      { method: 'POST' }
    );
    const d = await r.json();
    if (d.ok) {
      out.innerHTML = `<span class="text-success">✓ چارت ارسال شد (msg_id=${d.message_id}, ${d.rates_count} نقطه)</span>`;
      showToast('چارت تستی ارسال شد ✓', 'success');
    } else {
      const hint = d.hint ? `<br><code class="small">${esc(d.hint)}</code>` : '';
      out.innerHTML = `<span class="text-danger">✗ ${esc(d.msg || 'خطا')} (${esc(d.stage || '')})</span>${hint}`;
      showToast(d.msg || 'خطا در ارسال چارت', 'danger');
    }
  } catch (e) {
    out.innerHTML = '<span class="text-danger">خطای شبکه</span>';
  }
  if (btn) { btn.disabled = false; btn.innerHTML = '<i class="bi bi-send"></i> ارسال چارت آزمایشی'; }
}

async function checkChartStatus(retry = true) {
  const out = document.getElementById('chartDiagOut');
  out.innerHTML = '<span class="text-muted">در حال بررسی…</span>';
  try {
    const url = '/api/charts/status' + (retry ? '?retry=1' : '');
    const d = await (await fetch(url)).json();
    if (d.matplotlib_available) {
      out.innerHTML = '<span class="text-success">✓ matplotlib در دسترس است</span>';
    } else {
      const err = d.error || 'خطای نامشخص (احتمالاً سرویس restart نشده)';
      out.innerHTML = `<span class="text-danger">✗ matplotlib در دسترس نیست</span>
        <br><small class="text-muted">${esc(err)}</small>
        <br><code class="small">${esc(d.install_hint || '')}</code>`;
    }
  } catch {
    out.innerHTML = '<span class="text-danger">خطای شبکه</span>';
  }
}

function deleteSource(si) {
  ensureCfg().sources.splice(si, 1);
  renderTopicDetail();
  markDirty();
}

function updateChat(si, val) {
  ensureCfg().sources[si].chat = val;
  markDirty();
}

function addFilterRule(si) {
  ensureCfg().sources[si].filters.push([]);
  renderTopicDetail();
  markDirty();
}

function deleteFilterRule(si, ri) {
  ensureCfg().sources[si].filters.splice(ri, 1);
  renderTopicDetail();
  markDirty();
}

function onPhraseKey(e, si, ri) {
  if (e.key !== 'Enter') return;
  const phrase = e.target.value.trim();
  if (!phrase) return;
  ensureCfg().sources[si].filters[ri].push(phrase);
  e.target.value = '';
  renderTopicDetail();
  markDirty();
}

function deletePhrase(si, ri, pi) {
  const rule = ensureCfg().sources[si].filters[ri];
  rule.splice(pi, 1);
  if (rule.length === 0) ensureCfg().sources[si].filters.splice(ri, 1);
  renderTopicDetail();
  markDirty();
}

function renderFilterRules(si, filters) {
  if (!filters.length) {
    return `<div class="filter-rules-wrap"><span class="all-badge">همه پیام‌ها فوروارد می‌شوند</span>
      <button class="btn-add-rule ms-2" onclick="addFilterRule(${si})">+ فیلتر</button></div>`;
  }
  const rulesHtml = filters.map((rule, ri) => {
    const phrasesWithAnd = rule.map((phrase, pi) =>
      (pi > 0 ? '<span class="rule-and-badge">و</span>' : '') +
      `<span class="filter-tag">${esc(phrase)}<button onclick="deletePhrase(${si},${ri},${pi})"><i class="bi bi-x"></i></button></span>`
    ).join('');
    return `${ri > 0 ? '<div class="rule-row"><span class="rule-or-badge">── یا ──</span></div>' : ''}
      <div class="rule-row">${phrasesWithAnd}
        <input class="filter-input" placeholder="عبارت… Enter" onkeydown="onPhraseKey(event,${si},${ri})">
        <button class="btn-del-rule" onclick="deleteFilterRule(${si},${ri})"><i class="bi bi-trash3"></i></button></div>`;
  }).join('');
  return `<div class="filter-rules-wrap">${rulesHtml}
    <button class="btn-add-rule" onclick="addFilterRule(${si})">+ قانون (یا)</button></div>`;
}

function renderTopicDetail() {
  hideMainPanels();
  const detail = document.getElementById('topicDetail');
  if (!selGroupId || !selTopicId) {
    if (selGroupId) loadGroupDashboard(selGroupId);
    else loadDashboard();
    return;
  }
  detail.style.display = 'block';
  const g = groups.find(x => x.id === selGroupId);
  const topics = tgTopicsByGroup[selGroupId] || [];
  const tgt = topics.find(t => t.id === selTopicId);
  const title = tgt ? tgt.title : `Topic ${selTopicId}`;
  const cfg = ensureCfg();
  const sources = cfg.sources;
  const chartOn = !!cfg.chart_enabled;

  detail.innerHTML = `
    <div class="topic-hdr">
      <div class="topic-hdr-icon"><i class="bi bi-hash"></i></div>
      <div>
        <div class="topic-hdr-title">${esc(title)}</div>
        <div class="topic-hdr-id">${esc(g?.title || '')} · Topic ${selTopicId}</div>
      </div>
      <button class="btn btn-sm btn-outline-secondary me-0 ms-auto" onclick="selectGroup('${selGroupId}')">
        <i class="bi bi-people me-1"></i>پروفایل گروه</button>
    </div>

    <div class="chart-section">
      <div class="chart-toggle">
        <label class="form-check form-switch m-0">
          <input class="form-check-input" type="checkbox" id="chartEnabled" ${chartOn ? 'checked' : ''}
                 onchange="toggleChartEnabled(this.checked)">
          <span class="form-check-label fw-bold">
            <i class="bi bi-graph-up-arrow text-primary me-1"></i> نمودار نرخ این تاپیک
          </span>
        </label>
        ${chartOn ? `<button class="btn btn-sm btn-outline-primary" onclick="openChartPage()">
          <i class="bi bi-bar-chart-line me-1"></i>نمایش نمودار</button>` : ''}
      </div>
      ${chartOn ? `
        <div class="chart-fields">
          <label class="small text-muted mb-1">برچسب نمودار (نام واحد یا نرخ)</label>
          <input class="form-control form-control-sm" value="${esc(cfg.chart_label || '')}"
                 oninput="updateChartLabel(this.value)" placeholder="مثلاً: دلار به افغانی">
          <div class="form-text small">
            <i class="bi bi-info-circle"></i>
            عبارت regex برای استخراج مقدار را در هر سورس جدا تنظیم کنید (پیام هر سورس فرمت خود را دارد).
          </div>
          <div class="d-flex gap-2 mt-2 flex-wrap align-items-center">
            <button class="btn btn-sm btn-outline-secondary" onclick="checkChartStatus()">
              <i class="bi bi-shield-check"></i> بررسی وضعیت چارت
            </button>
            <button class="btn btn-sm btn-outline-primary" id="btnTestChart" onclick="testChartSend()">
              <i class="bi bi-send"></i> ارسال چارت آزمایشی
            </button>
            <span id="chartDiagOut" class="small flex-grow-1"></span>
          </div>
        </div>` : ''}
    </div>

    <div class="sec-head">
      <h6><i class="bi bi-broadcast-pin"></i> سورس‌ها</h6>
      <button class="btn btn-sm btn-outline-primary" onclick="addSource()"><i class="bi bi-plus-lg me-1"></i>سورس</button>
    </div>
    ${sources.length ? sources.map((src, si) => `
      <div class="source-card">
        <div class="src-top">
          <i class="bi bi-telegram"></i>
          <input type="text" dir="ltr" value="${esc(src.chat || '')}" onchange="updateChat(${si},this.value)" placeholder="@channel">
          <button class="src-del" onclick="deleteSource(${si})"><i class="bi bi-x-circle-fill"></i></button>
        </div>
        ${renderFilterRules(si, src.filters || [])}
        ${chartOn ? `
          <div class="src-regex">
            <label class="small text-muted mb-1">
              <i class="bi bi-regex me-1"></i> Regex استخراج عدد (این سورس):
            </label>
            <input class="form-control form-control-sm" dir="ltr"
                   value="${esc(src.value_regex || '')}"
                   oninput="updateSourceRegex(${si}, this.value)"
                   placeholder="مثلاً: ([\\d,\\.]+)">
            <div class="regex-test mt-2">
              <textarea id="regex_sample_${si}" class="form-control form-control-sm" rows="2"
                        placeholder="یک نمونه پیام از این سورس را اینجا بچسبانید برای تست…"></textarea>
              <div class="d-flex align-items-center gap-2 mt-1">
                <button class="btn btn-sm btn-outline-secondary" onclick="testSourceRegex(${si})">
                  <i class="bi bi-play-fill"></i> تست
                </button>
                <span id="regex_out_${si}" class="small"></span>
              </div>
            </div>
          </div>` : ''}
      </div>`).join('')
      : '<div class="no-sources"><i class="bi bi-inbox"></i>سورسی نیست</div>'}
    <button class="btn-add-src mt-2" onclick="addSource()"><i class="bi bi-plus-circle"></i> سورس جدید</button>
    <p class="text-warning mt-3 mb-0" style="font-size:.78rem"><i class="bi bi-exclamation-triangle me-1"></i>
      پس از تغییرات حتماً <strong>ذخیره تغییرات</strong> را بزنید تا فوروارد و نمودار به‌روز شوند.</p>`;
}

function goHome() {
  selGroupId = null;
  selTopicId = null;
  loadDashboard();
}

async function deleteGroupFromList(gid) {
  if (!confirm('این گروه فقط از لیست TeleFilter حذف می‌شود.\nدر تلگرام باقی می‌ماند. ادامه؟')) return;
  try {
    const d = await (await fetch(`/api/groups/${encodeURIComponent(gid)}`, { method: 'DELETE' })).json();
    if (d.ok) {
      await loadConfigFromServer();
      delete tgTopicsByGroup[gid];
      goHome();
      showToast('از لیست حذف شد ✓', 'success');
    } else showToast('خطا در حذف', 'danger');
  } catch { showToast('خطای شبکه', 'danger'); }
}

async function inviteGroupMember(gid) {
  const inp = document.getElementById('inviteMemberInput');
  const user = inp?.value?.trim();
  if (!user) { showToast('username یا ID وارد کنید', 'warning'); return; }
  try {
    const d = await (await fetch(`/api/groups/${encodeURIComponent(gid)}/members`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ user }),
    })).json();
    if (d.ok) {
      showToast(d.msg || 'دعوت ارسال شد ✓', 'success');
      if (inp) inp.value = '';
      loadGroupDashboard(gid);
    } else showToast(d.msg || 'خطا', 'danger');
  } catch { showToast('خطای شبکه', 'danger'); }
}

async function removeGroupMember(gid, memberId) {
  if (!confirm('این کاربر از گروه حذف شود؟')) return;
  try {
    const d = await (await fetch(`/api/groups/${encodeURIComponent(gid)}/members/${memberId}`, {
      method: 'DELETE',
    })).json();
    if (d.ok) {
      showToast('عضو حذف شد ✓', 'success');
      loadGroupDashboard(gid);
    } else showToast(d.msg || 'خطا', 'danger');
  } catch { showToast('خطای شبکه', 'danger'); }
}

async function saveAll() {
  const buildTopic = (topic_id, name, data) => ({
    topic_id,
    name,
    chart_enabled: !!data.chart_enabled,
    chart_label: data.chart_label || '',
    sources: (data.sources || []).map(s => ({
      chat: s.chat || '',
      filters: s.filters || [],
      value_regex: s.value_regex || '',
    })),
  });
  const outGroups = groups.map(g => {
    const topics = [];
    let tlist = tgTopicsByGroup[g.id] || [];
    if (!tlist.length && !groupIsForum(g)) {
      tlist = [{ id: 0, title: 'چت اصلی' }];
    }
    for (const t of tlist) {
      const data = cfgMap[cfgKey(g.id, t.id)];
      if (data?.sources?.length) {
        topics.push(buildTopic(t.id, t.title, data));
      }
    }
    for (const [k, data] of Object.entries(cfgMap)) {
      if (!k.startsWith(g.id + ':') || !data.sources?.length) continue;
      const tid = parseInt(k.split(':')[1], 10);
      if (topics.some(x => x.topic_id === tid)) continue;
      topics.push(buildTopic(tid, '', data));
    }
    const row = { id: g.id, title: g.title, telegram_id: g.telegram_id, origin: g.origin, topics };
    if (g.is_forum !== undefined) row.is_forum = g.is_forum;
    return row;
  });
  const res = await fetch('/api/config', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ groups: outGroups }),
  });
  await res.json();
  await loadConfigFromServer();
  markClean();
  showToast('ذخیره شد ✓ — فوروارد در حال به‌روزرسانی', 'success');
  await refreshStatus();
}

function normalizeFilters(filters) {
  if (!filters?.length) return [];
  if (typeof filters[0] === 'string') return filters.map(f => [f]);
  return filters;
}

function markDirty() {
  const b = document.getElementById('saveBtn');
  b.className = 'btn-save dirty';
  b.innerHTML = '<i class="bi bi-floppy-fill"></i> ذخیره *';
}

function markClean() {
  const b = document.getElementById('saveBtn');
  b.className = 'btn-save';
  b.innerHTML = '<i class="bi bi-floppy-fill"></i> ذخیره تغییرات';
}

function showToast(msg, type = 'success') {
  const b = document.getElementById('toastBox');
  b.style.background = { success: '#16a34a', danger: '#dc2626', warning: '#d97706' }[type] || '#1e40af';
  b.textContent = msg;
  b.style.display = 'block';
  setTimeout(() => b.style.display = 'none', 3000);
}

function esc(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function toggleUserMenu() {
  const m = document.getElementById('userMenu');
  m.style.display = m.style.display === 'none' ? 'block' : 'none';
}

async function openAdmin() {
  adminMdl.show();
  await loadAdminUsers();
}

async function loadAdminUsers() {
  const body = document.getElementById('adminBody');
  body.innerHTML = '<div class="text-center text-muted py-3"><i class="bi bi-hourglass-split spin"></i></div>';
  try {
    const d = await (await fetch('/api/admin/users')).json();
    if (!d.users?.length) { body.innerHTML = '<p class="text-muted text-center">کاربری نیست</p>'; return; }
    body.innerHTML = d.users.map(u => `
      <div class="admin-user-row">
        <div class="admin-user-info">
          <div class="admin-user-name">${esc(u.first_name)} ${u.is_admin ? '<span class="badge bg-primary">ادمین</span>' : ''}</div>
          <div class="admin-user-meta">@${esc(u.username || '—')} · Bot: ${u.bot_status}</div>
        </div>
        <div>${!u.is_approved ? `<button class="btn btn-sm btn-success" onclick="adminApprove(${u.tg_id},false)">تأیید</button>` :
          `<button class="btn btn-sm btn-outline-danger" onclick="adminRevoke(${u.tg_id})">لغو</button>`}</div>
      </div>`).join('');
  } catch { body.innerHTML = '<p class="text-danger">خطا</p>'; }
}

async function adminApprove(uid, asAdmin) {
  await fetch(`/api/admin/users/${uid}/approve`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ admin: asAdmin }),
  });
  showToast('تأیید شد', 'success');
  loadAdminUsers();
}

async function adminRevoke(uid) {
  if (!confirm('لغو دسترسی؟')) return;
  await fetch(`/api/admin/users/${uid}/revoke`, { method: 'POST' });
  loadAdminUsers();
}

async function bootMain() {
  await refreshStatus();
  renderTree();
  await loadDashboard();
  markClean();
  if (!needsTelethon && groups.length) {
    if (groups.length === 1) expandedGroups.add(groups[0].id);
    await syncAllTopics();
    renderTree();
  }
  startStatusPolling();
}
