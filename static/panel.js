/* TeleFilter panel UI */
let groups = [], cfgMap = {};
let selGroupId = null, selTopicId = null;
let tgTopicsByGroup = {};
let expandedGroups = new Set();
let tgOk = false, forumOk = false, botStatus = 'stopped', hasSession = false;
let needsApi = false, needsTelethon = false;
let isAdmin = window.TF_IS_ADMIN === true || window.TF_IS_ADMIN === 'true';
let loginMdl, settingsMdl, createMdl, setupMdl, adminMdl, linkGroupMdl, createGroupMdl;

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
      }));
      m[cfgKey(g.id, t.topic_id)] = { sources };
    }
  }
  return m;
}

function cfgKey(gid, tid) { return `${gid}:${tid}`; }

function ensureCfg() {
  const k = cfgKey(selGroupId, selTopicId);
  if (!cfgMap[k]) cfgMap[k] = { sources: [] };
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
    updateConnBadge();
  } catch { /* silent */ }
}

function updateConnBadge() {
  const b = document.getElementById('connBadge');
  if (hasSession && botStatus === 'running') {
    b.className = 'tbadge ok';
    b.innerHTML = '<i class="bi bi-circle-fill dot"></i> فوروارد فعال';
    b.onclick = null;
    b.style.cursor = 'default';
  } else if (hasSession) {
    b.className = 'tbadge warn';
    b.innerHTML = '<i class="bi bi-circle-fill dot"></i> در حال راه‌اندازی…';
    b.onclick = null;
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

function openTelethonSetup(auto) {
  const sub = document.getElementById('loginSubtitle');
  if (sub) {
    sub.textContent = auto
      ? 'برای فوروارد پیام‌ها، یک‌بار کد تأیید تلگرام را وارد کنید (همان اکانتی که با آن وارد پنل شدید).'
      : 'کد تأیید تلگرام را وارد کنید.';
  }
  openLogin();
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
    const s = await (await fetch(`/api/dashboard/stats?group_id=${encodeURIComponent(gid)}`)).json();
    const topics = tgTopicsByGroup[gid] || [];
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
          <h5 class="mb-1" style="font-weight:700">${esc(g.title)}</h5>
          <div class="text-muted" style="font-size:.8rem">ID: <span dir="ltr">${esc(g.telegram_id)}</span>
            · ${g.origin === 'created' ? 'ساخته‌شده در TeleFilter' : 'متصل از تلگرام'}</div>
        </div>
        <button class="btn btn-sm btn-outline-secondary" onclick="goHome()"><i class="bi bi-grid me-1"></i>داشبورد کل</button>
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
    const topics = tgTopicsByGroup[g.id] || [];
    const isOpen = expandedGroups.has(g.id);
    const gActive = selGroupId === g.id && !selTopicId;
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
          <div class="t-meta">${topics.length} topic · ${g.origin === 'created' ? 'ساخته‌شده' : 'متصل'}</div>
        </button>
        <div class="group-actions">
          <button type="button" class="btn-group-mini" title="Topic جدید" onclick="openCreateTopic('${g.id}', event)"><i class="bi bi-hash"></i></button>
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

async function syncTopicsForGroup(gid) {
  if (!tgOk || !forumOk) return;
  const icon = document.getElementById('syncIcon');
  if (icon) icon.className = 'bi bi-arrow-clockwise spin';
  try {
    const d = await (await fetch(`/api/telegram/topics?group_id=${encodeURIComponent(gid)}`)).json();
    if (d.topics) tgTopicsByGroup[gid] = d.topics;
  } catch { /* silent */ }
  if (icon) icon.className = 'bi bi-arrow-clockwise';
  renderTree();
}

async function syncAllTopics() {
  for (const g of groups) await syncTopicsForGroup(g.id);
}

// ── Groups ──────────────────────────────────────────────
function openLinkGroup() {
  if (needsTelethon) { openTelethonSetup(false); return; }
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
    box.innerHTML = d.dialogs.map(di => `
      <div class="link-row ${di.already_linked ? 'disabled' : ''}" data-id="${di.id}" data-title="${esc(di.title)}">
        <div><strong>${esc(di.title)}</strong><br><small class="text-muted">${di.id} ${di.is_forum ? '· Forum' : ''}</small></div>
        ${di.already_linked ? '<span class="badge bg-secondary">اضافه شده</span>' : '<i class="bi bi-plus-lg"></i>'}
      </div>`).join('');
    box.querySelectorAll('.link-row:not(.disabled)').forEach(el => {
      el.onclick = () => doLinkGroup(parseInt(el.dataset.id, 10), el.dataset.title);
    });
  } catch {
    box.innerHTML = '<p class="text-danger">خطا در دریافت لیست</p>';
  }
}

async function doLinkGroup(tid, title) {
  const d = await (await fetch('/api/groups/link', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ telegram_id: tid, title }),
  })).json();
  if (d.ok) {
    await loadConfigFromServer();
    linkGroupMdl.hide();
    showToast('گروه اضافه شد ✓', 'success');
    await syncTopicsForGroup(d.group.id);
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
    const d = await (await fetch('/api/groups/create', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title }),
    })).json();
    if (d.ok) {
      await loadConfigFromServer();
      createGroupMdl.hide();
      showToast('گروه ساخته شد ✓', 'success');
      await syncTopicsForGroup(d.group.id);
      selectGroup(d.group.id);
    } else showToast(d.msg || 'خطا', 'danger');
  } catch { showToast('خطای شبکه', 'danger'); }
  btn.disabled = false;
}

// ── Login ───────────────────────────────────────────────
function openLogin() {
  showLoginStep(1);
  ['loginPhone', 'loginCode', 'login2fa'].forEach(id => document.getElementById(id).value = '');
  hideLoginErrors();
  setLoginBtn('ارسال کد', doSendCode);
  loginMdl.show();
  setTimeout(() => document.getElementById('loginPhone').focus(), 300);
}

function showLoginStep(n) {
  [1, 2, 3].forEach(i => {
    document.getElementById(`lstep${i}`).classList.toggle('active', i === n);
    const d = document.getElementById(`sd${i}`);
    d.classList.toggle('active', i === n);
    d.classList.toggle('done', i < n);
  });
  document.getElementById('sl1').classList.toggle('done', n > 1);
  document.getElementById('sl2').classList.toggle('done', n > 2);
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
      showLoginStep(2);
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
      showLoginStep(3);
      setLoginBtn('تأیید رمز', doVerify2FA);
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
  loginMdl.hide();
  showToast('اتصال برقرار شد — فوروارد خودکار فعال می‌شود ✓', 'success');
  needsTelethon = false;
  hasSession = true;
  await refreshStatus();
  ensure_client_refresh();
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
    if (d.ok && d.topic_id) {
      createMdl.hide();
      await syncTopicsForGroup(selGroupId);
      selectTopic(selGroupId, d.topic_id);
      showToast(`Topic «${d.title}» ساخته شد ✓`, 'success');
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
  ensureCfg().sources.push({ chat: '', filters: [] });
  renderTopicDetail();
  markDirty();
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
  const sources = ensureCfg().sources;

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
        </div>${renderFilterRules(si, src.filters || [])}</div>`).join('')
      : '<div class="no-sources"><i class="bi bi-inbox"></i>سورسی نیست</div>'}
    <button class="btn-add-src mt-2" onclick="addSource()"><i class="bi bi-plus-circle"></i> سورس جدید</button>`;
}

function goHome() {
  selGroupId = null;
  selTopicId = null;
  loadDashboard();
}

async function saveAll() {
  const outGroups = groups.map(g => {
    const topics = [];
    const tlist = tgTopicsByGroup[g.id] || [];
    for (const t of tlist) {
      const data = cfgMap[cfgKey(g.id, t.id)];
      if (data?.sources?.length) {
        topics.push({ topic_id: t.id, name: t.title, sources: data.sources });
      }
    }
    for (const [k, data] of Object.entries(cfgMap)) {
      if (!k.startsWith(g.id + ':') || !data.sources?.length) continue;
      const tid = parseInt(k.split(':')[1], 10);
      if (topics.some(x => x.topic_id === tid)) continue;
      topics.push({ topic_id: tid, name: '', sources: data.sources });
    }
    return { id: g.id, title: g.title, telegram_id: g.telegram_id, origin: g.origin, topics };
  });
  const res = await fetch('/api/config', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ groups: outGroups }),
  });
  const d = await res.json();
  await loadConfigFromServer();
  markClean();
  showToast(d.restarted ? 'ذخیره شد ✓' : 'ذخیره شد ✓', 'success');
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
  await loadDashboard();
  markClean();
  if (!needsTelethon && groups.length) {
    if (groups.length === 1) expandedGroups.add(groups[0].id);
    await syncAllTopics();
  }
  setInterval(async () => {
    await refreshStatus();
    if (selTopicId) return;
    if (selGroupId) loadGroupDashboard(selGroupId);
    else loadDashboard();
  }, 12000);
}
