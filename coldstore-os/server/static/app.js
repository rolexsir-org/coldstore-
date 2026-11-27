/* ColdStore OS — office web app + installable Android PWA (no build step).
   Vanilla JS SPA over /api/v1. Offline: intake/dispatch writes queue to an
   IndexedDB outbox with stable operation_ids; server processes idempotently. */
'use strict';

const API = '/api/v1';
const state = {
  token: localStorage.getItem('cs_token') || null,
  me: JSON.parse(localStorage.getItem('cs_me') || 'null'),
  masters: {},           // varieties, grades, seasons, packages, chambers, parties
  route: location.hash.slice(2) || 'dashboard',
};

/* ---------------- utilities ---------------- */
const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => [...el.querySelectorAll(sel)];
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => (
  { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const nf = new Intl.NumberFormat('en-IN', { maximumFractionDigits: 3 });
const money = (minor) => '₹' + new Intl.NumberFormat('en-IN',
  { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format((minor || 0) / 100);
const kg = (v) => nf.format(v || 0) + ' kg';
const uuid = () => (crypto.randomUUID ? crypto.randomUUID()
  : 'op-' + Date.now() + '-' + Math.random().toString(16).slice(2));

function toast(msg, cls = '') {
  const t = $('#toast');
  t.textContent = msg;
  t.className = cls;
  t.classList.remove('hidden');
  clearTimeout(t._t);
  t._t = setTimeout(() => t.classList.add('hidden'), 4200);
}

function can(scope) {
  const p = state.me?.permissions || [];
  return p.includes('admin') || p.includes(scope);
}

/* ---------------- API client ---------------- */
class NetError extends Error {}
class ApiError extends Error {
  constructor(status, data) { super(data?.detail ? JSON.stringify(data.detail) : ''); this.status = status; this.data = data; }
}

async function api(method, path, body, opts = {}) {
  let resp;
  try {
    resp = await fetch(API + path, {
      method,
      headers: {
        'Content-Type': 'application/json',
        ...(state.token ? { Authorization: 'Bearer ' + state.token } : {}),
      },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (e) {
    throw new NetError('server unreachable — working offline');
  }
  if (resp.status === 401 && !opts.noAuthRedirect) { doLogout(); }
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new ApiError(resp.status, data);
  resp.data = data;
  return resp;
}
const get = (p) => api('GET', p).then((r) => r.data);
const post = (p, b) => api('POST', p, b).then((r) => r);
const put = (p, b) => api('PUT', p, b).then((r) => r);

async function openPrint(path) {
  try {
    const r = await api('GET', path);
    const text = await fetch(API + path, { headers: { Authorization: 'Bearer ' + state.token } }).then((x) => x.text());
    const w = window.open('');
    w.document.write(text);
    w.document.close();
  } catch (e) { errToast(e); }
}

function errToast(e) {
  if (e instanceof NetError) toast('OFFLINE: ' + e.message, 'err');
  else if (e instanceof ApiError) {
    const d = typeof e.data?.detail === 'object' ? e.data.detail.detail : e.data?.detail;
    toast(`Error ${e.status}: ${d || e.message}`, 'err');
  } else toast('Error: ' + e.message, 'err');
}

/* ---------------- offline outbox (IndexedDB) ---------------- */
const ODB = 'coldstore-outbox';
function odb() {
  return new Promise((res, rej) => {
    const r = indexedDB.open(ODB, 1);
    r.onupgradeneeded = () => r.result.createObjectStore('ops', { keyPath: 'operation_id' });
    r.onsuccess = () => res(r.result);
    r.onerror = () => rej(r.error);
  });
}
async function outboxPut(rec) {
  const db = await odb();
  return new Promise((res, rej) => {
    const tx = db.transaction('ops', 'readwrite');
    tx.objectStore('ops').put(rec);
    tx.oncomplete = res; tx.onerror = () => rej(tx.error);
  });
}
async function outboxAll() {
  const db = await odb();
  return new Promise((res) => {
    const out = [];
    const c = db.transaction('ops').objectStore('ops').openCursor();
    c.onsuccess = () => { if (c.result) { out.push(c.result.value); c.result.continue(); } else res(out); };
    c.onerror = () => res(out);
  });
}
async function outboxDel(id) {
  const db = await odb();
  return new Promise((res) => { const tx = db.transaction('ops', 'readwrite'); tx.objectStore('ops').delete(id); tx.oncomplete = res; });
}

async function refreshSyncbar() {
  const ops = await outboxAll();
  const pending = ops.filter((o) => o.status === 'pending');
  const conflicts = ops.filter((o) => o.status === 'conflict');
  const bar = $('#syncbar');
  if (!pending.length && !conflicts.length && navigator.onLine) { bar.classList.add('hidden'); return; }
  bar.classList.remove('hidden');
  bar.classList.toggle('conflict', conflicts.length > 0);
  const off = navigator.onLine ? '' : 'OFFLINE — ';
  $('#syncmsg').innerHTML = conflicts.length
    ? `${off}<b>${conflicts.length} op(s) in CONFLICT</b> (${esc(conflicts[0].error || '')}) · resolve with a manager · ${pending.length} pending`
    : `${off}${pending.length} op(s) queued for sync`;
}

async function submitOp(endpoint, body, label) {
  /* Business write with stable operation_id → queue offline or post online. */
  body.operation_id = body.operation_id || uuid();
  const op = { operation_id: body.operation_id, endpoint, body, label, queued_at: new Date().toISOString(), status: 'pending' };
  try {
    const r = await post(endpoint, body);
    return { ok: true, data: r.data, replay: r.headers.get('Idempotent-Replay') === 'true' };
  } catch (e) {
    if (e instanceof NetError) { await outboxPut(op); await refreshSyncbar(); return { ok: false, queued: true }; }
    throw e;
  }
}

async function runOp(endpoint, body, okMsg, opts = {}) {
  /* submitOp + queued/replay toasts; renders on completion (opts.render=false to skip). Returns response body or null when queued. */
  const r = await submitOp(endpoint, body);
  if (r.queued) { toast('Saved offline — will sync automatically when back online', 'warn'); if (opts.render !== false) render(); return null; }
  const msg = typeof okMsg === 'function' ? okMsg(r.data) : okMsg;
  toast(r.replay ? `${msg} (idempotent replay — already applied)` : msg, 'ok');
  if (opts.render !== false) render();
  return r.data;
}

let syncing = false;
async function syncOutbox() {
  if (syncing || !state.token) return;
  syncing = true;
  try {
    const ops = await outboxAll();
    for (const op of ops.filter((o) => o.status === 'pending')) {
      try {
        await post(op.endpoint, op.body);
        await outboxDel(op.operation_id);
      } catch (e) {
        if (e instanceof NetError) break;                    // still offline
        op.status = 'conflict';
        op.error = `server rejected (${e.status}): ${typeof e.data?.detail === 'object' ? e.data.detail.detail : e.data?.detail || ''}`;
        await outboxPut(op);                                  // visible + auditable, never silent
      }
    }
  } finally { syncing = false; await refreshSyncbar(); }
}
window.addEventListener('online', syncOutbox);
window.addEventListener('offline', refreshSyncbar);
$('#syncnow').addEventListener('click', () => { syncOutbox().then(render); });

/* ---------------- session ---------------- */
async function doLogin(ev) {
  ev.preventDefault();
  try {
    const r = await api('POST', '/auth/login', {
      username: $('#lu').value.trim(), password: $('#lp').value,
      device_label: navigator.userAgent.slice(0, 60),
    }, { noAuthRedirect: true }).then((x) => x.data);
    if (r.mfa_required) { showMfaStep(r.mfa_token); return; }
    finishLogin(r);
  } catch (e) { toast('Login failed: ' + (e.data?.detail || e.message), 'err'); }
}
function showMfaStep(mfaToken) {
  const box = document.querySelector('.login .box');
  box.innerHTML = `<h1>Two-factor check</h1><div class="sub">Enter the 6-digit code from your authenticator app</div>
    <form id="mf"><input id="mcode" inputmode="numeric" pattern="[0-9]{6}" maxlength="6" autocomplete="one-time-code" required style="font-size:22px;letter-spacing:6px;text-align:center">
    <div class="actions"><button class="btn" style="width:100%">Verify & sign in</button></div></form>
    <div class="note"><a href="/" onclick="location.reload();return false">← back</a></div>`;
  $('#mcode').focus();
  $('#mf').onsubmit = async (ev) => {
    ev.preventDefault();
    try {
      const r = await api('POST', '/auth/mfa/verify', { mfa_token: mfaToken, code: $('#mcode').value, device_label: navigator.userAgent.slice(0, 60) }).then((x) => x.data);
      finishLogin(r);
    } catch (e) { toast('Code rejected: ' + (e.data?.detail || e.message), 'err'); }
  };
}
async function finishLogin(r) {
  state.token = r.token; state.me = r.user;
  localStorage.setItem('cs_token', r.token);
  localStorage.setItem('cs_me', JSON.stringify(r.user));
  if ('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js');
  await loadMasters();
  render();
  syncOutbox();
}
function doLogout() {
  if (state.token) api('POST', '/auth/logout').catch(() => {});
  state.token = null; state.me = null;
  localStorage.removeItem('cs_token'); localStorage.removeItem('cs_me');
  render();
}

async function loadMasters() {
  const [varieties, grades, seasons, packages, chambers, parties] = await Promise.all([
    get('/varieties'), get('/grades'), get('/seasons'), get('/packages'),
    get('/chambers'), get('/parties'),
  ]);
  Object.assign(state.masters, { varieties, grades, seasons, packages, chambers, parties });
}
const M = () => state.masters;
const opt = (rows, val, label) => rows.map((r) => `<option value="${r[val]}">${esc(r[label] ?? r[val])}</option>`).join('');
const sel = (name, rows, val, label, cur) =>
  `<select name="${name}">${opt(rows, val, label)}</select>`;

/* ---------------- chrome ---------------- */
const NAV = [
  ['dashboard', 'Dashboard', 'M3 13h8V3H3v10Zm0 8h8v-6H3v6Zm10 0h8V11h-8v10Zm0-18v6h8V3h-8Z'],
  ['contracts', 'Contracts', 'M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8l-6-6Zm-1 2 4.5 4.5H13V4ZM8 13h8v1.5H8V13Zm0 4h8V18H8v-1Z'],
  ['intake', 'Intake', 'M12 2 2 7l10 5 10-5-10-5ZM2 17l10 5 10-5'],
  ['quality', 'Quality', 'M12 2 2 7l10 5 10-5-10-5ZM2 17l10 5 10-5M2 12l10 5 10-5'],
  ['lots', 'Lots & Trace', 'M20 7H4a2 2 0 0 0-2 2v10h20V9a2 2 0 0 0-2-2ZM2 7l3-4h14l3 4'],
  ['chambers', 'Chambers', 'M4 21V5a2 2 0 0 1 2-2h12a2 2 0 0 1 2 2v16M2 21h20M9 7h1m4 0h1M9 11h1m4 0h1M9 15h1m4 0h1'],
  ['orders', 'Orders', 'M4 4h16l-1 9H7L4 4ZM4 4 3 1H1m7 20a1 1 0 1 0 0-2 1 1 0 0 0 0 2Zm10 0a1 1 0 1 0 0-2 1 1 0 0 0 0 2Z'],
  ['dispatch', 'Dispatch', 'M1 3h15v13H1zM16 8h4l3 3v5h-7M5.5 21a2 2 0 1 0 0-4 2 2 0 0 0 0 4Zm13 0a2 2 0 1 0 0-4 2 2 0 0 0 0 4Z'],
  ['billing', 'Billing', 'M12 2v20M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6'],
  ['telemetry', 'CA Telemetry', 'M14 14.76V3.5a2.5 2.5 0 0 0-5 0v11.26a4.5 4.5 0 1 0 5 0Z'],
  ['reports', 'Reports', 'M3 3v18h18M8 17V9m4 8V5m4 12v-6'],
  ['admin', 'Admin', 'M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6Zm7-3a7 7 0 0 1-.1 1.2l2 1.6-2 3.4-2.4-1a7 7 0 0 1-2 1.2l-.4 2.6h-4l-.4-2.6a7 7 0 0 1-2-1.2l-2.4 1-2-3.4 2-1.6A7 7 0 0 1 5 12a7 7 0 0 1 .1-1.2l-2-1.6 2-3.4 2.4 1a7 7 0 0 1 2-1.2L9.9 3h4l.4 2.6a7 7 0 0 1 2 1.2l2.4-1 2 3.4-2 1.6a7 7 0 0 1 .1 1.2Z'],
];

function shell(content) {
  const nav = NAV.filter(([k]) => (k !== 'admin' || can('users.read') || can('backups.manage') || can('audit.read'))
    && (k !== 'telemetry' || can('telemetry.read'))
    && (k !== 'contracts' || can('contracts.read') || can('inward.read'))
    && (k !== 'quality' || can('quality.read'))
    && (k !== 'orders' || can('orders.read') || can('deliveries.read')))
    .map(([k, label, d]) => `<a href="#/${k}" class="${state.route === k ? 'active' : ''}">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="${d}"/></svg><span>${label}</span></a>`).join('');
  return `
  <aside>
    <div class="logo"><svg viewBox="0 0 64 64"><rect width="64" height="64" rx="14" fill="#7dd3a8"/><path d="M32 14c-2-4-6-6-10-6 4 1 7 3 9 6-7 1-12 8-12 16 0 10 6 18 13 18s13-8 13-18c0-8-5-15-12-16" fill="#0f3d2e"/></svg><b>ColdStore OS</b></div>
    <nav>${nav}</nav>
    <div class="who"><b>${esc(state.me.display_name)}</b>@${esc(state.me.username)} · <span class="rolechip">${esc(state.me.role)}</span><br><a href="#" id="sec" style="color:#b8d2c6;font-size:11px">security (MFA)</a> · <a href="#" id="lo" style="color:#b8d2c6;font-size:11px">sign out</a></div>
  </aside>
  <main>
    <div class="topbar"><h1>${NAV.find(([k]) => k === state.route)?.[1] || ''}</h1>
      <span class="mini">${navigator.onLine ? 'online' : '<b style="color:var(--bad)">offline</b>'}</span></div>
    <div class="content">${content}</div>
  </main>`;
}

function loginView() {
  return `<div class="login"><form class="box" id="lf">
    <h1><svg viewBox="0 0 64 64" width="30"><rect width="64" height="64" rx="14" fill="#0f3d2e"/><path d="M32 14c-2-4-6-6-10-6 4 1 7 3 9 6-7 1-12 8-12 16 0 10 6 18 13 18s13-8 13-18c0-8-5-15-12-16" fill="#7dd3a8"/></svg> ColdStore OS</h1>
    <div class="sub">CA Apple Edition · facility management platform</div>
    <label class="f">Username</label><input id="lu" autocomplete="username" required>
    <label class="f">Password</label><input id="lp" type="password" autocomplete="current-password" required>
    <div class="actions"><button class="btn" style="width:100%">Sign in</button></div>
    <div class="note">Authorized users only. All actions are audited.</div>
  </form></div>`;
}

/* ---------------- modal helper ---------------- */
function modal(title, bodyHtml, wide = true) {
  const o = document.createElement('div');
  o.className = 'overlay';
  o.innerHTML = `<div class="modal"><div class="mhead"><b>${esc(title)}</b><button class="xbtn">✕</button></div><div class="mbody">${bodyHtml}</div></div>`;
  o.querySelector('.xbtn').onclick = () => o.remove();
  o.addEventListener('click', (e) => { if (e.target === o) o.remove(); });
  document.body.appendChild(o);
  return o;
}
const formVals = (el) => Object.fromEntries(new FormData(el).entries());
const num = (v) => (v === '' || v == null ? undefined : Number(v));
const intOr = (v) => (v === '' || v == null ? undefined : parseInt(v, 10));

/* ================= DASHBOARD ================= */
async function viewDashboard() {
  const d = await get('/reports/dashboard');
  const cap = d.capacity;
  const chamberCards = (await get('/chambers')).map((c) => `
    <div class="ch" data-ch="${c.id}"><div class="code">${esc(c.code)}</div>
      <div class="bar"><i class="${c.occupancy_pct > 90 ? 'full' : c.occupancy_pct > 70 ? 'high' : ''}" style="width:${Math.min(100, c.occupancy_pct)}%"></i></div>
      <div class="kv">${kg(c.stored_kg)} / ${kg(c.capacity_kg)} · ${c.occupancy_pct}%</div>
      <div class="kv">${c.status}${c.status !== 'active' ? ' ⚠' : ''}</div></div>`).join('');
  const bd = (title, rows, key, val) => `<div class="card"><div class="k">${title}</div>
    ${rows.length ? rows.slice(0, 8).map((r) => `<div class="kv-line"><span style="flex:1">${esc(r[key])}</span><b>${kg(r[val])}</b></div>`).join('') : '<div class="note">no stock</div>'}</div>`;
  return `
  <div class="cards">
    <div class="card ok"><div class="k">Total stock</div><div class="v">${kg(d.stock.total_kg)}</div><div class="s">${nf.format(d.stock.total_units)} units</div></div>
    <div class="card"><div class="k">Capacity used</div><div class="v">${cap.occupancy_pct}%</div><div class="s">${kg(cap.used_kg)} / ${kg(cap.capacity_kg)}</div></div>
    <div class="card"><div class="k">Chambers</div><div class="v">${cap.chambers_part_filled}/${cap.chambers_active}</div><div class="s">part-filled / active</div></div>
    <div class="card"><div class="k">Intake today</div><div class="v">${kg(d.activity_today.intake_kg)}</div><div class="s">dispatch ${kg(d.activity_today.dispatch_kg)}</div></div>
    ${can('reports.financial') ? `
    <div class="card"><div class="k">Receivables</div><div class="v">${money(d.financials.receivables_minor)}</div><div class="s">billed ${money(d.financials.billed_total_minor)}</div></div>
    <div class="card ${d.financials.overdue_minor ? 'bad' : 'ok'}"><div class="k">Overdue</div><div class="v">${money(d.financials.overdue_minor)}</div><div class="s">${d.financials.overdue_count} invoice(s)</div></div>` : ''}
    <div class="card ${d.exceptions.pending_adjustments ? 'warn' : ''}"><div class="k">Exceptions</div><div class="v">${d.exceptions.pending_adjustments}</div><div class="s">pending adjustments · ${kg(d.exceptions.unreconciled_kg)} unreconciled · ${d.exceptions.draft_receipts} draft receipts</div></div>
  </div>
  <div class="panel"><h2>Chamber occupancy (${cap.chambers_active} active)</h2><div class="body"><div class="chgrid">${chamberCards || '<div class="note">no chambers configured</div>'}</div></div></div>
  <div class="cards">${bd('Stock by owner', d.breakdown.by_owner, 'owner', 'kg')}${bd('Stock by variety', d.breakdown.by_variety, 'variety', 'kg')}${bd('Stock by grade', d.breakdown.by_grade, 'grade', 'kg')}</div>
  <div class="note">As of ${d.as_of}Z — every figure derived from persisted ledger data.</div>`;
}

/* ================= INTAKE ================= */
async function viewIntake() {
  const q = $('#iq')?.value || '';
  const receipts = await get('/intake/receipts' + (q ? `?q=${encodeURIComponent(q)}` : ''));
  const rows = receipts.map((r) => `<tr>
    <td><b>${esc(r.receipt_no)}</b>${r.supersedes_id ? ' <span class="mini">(correction)</span>' : ''}</td>
    <td>${r.received_at.slice(0, 16).replace('T', ' ')}</td>
    <td>${esc(partyName(r.owner_id))}</td>
    <td>${esc(M().varieties.find((v) => v.id === r.variety_id)?.name || '')} / ${esc(M().grades.find((g) => g.id === r.grade_id)?.name || '')}</td>
    <td class="num">${kg(r.net_kg)}</td><td class="num">${r.unit_count}</td>
    <td>${esc(r.vehicle_no)}</td>
    <td><span class="badge ${r.status}">${r.status}</span></td>
    <td>${r.status === 'draft' && can('intake.verify') ? `<button class="btn small" data-verify="${r.id}">Verify</button>` : ''}
        ${r.status === 'verified' && can('intake.correct') ? `<button class="btn small ghost" data-correct="${r.id}">Correct</button>` : ''}
        <button class="btn small ghost" data-print="/intake/receipts/${r.id}/print">Print</button></td></tr>`).join('');
  return `
  <div class="toolbar">
    <input type="search" id="iq" placeholder="search receipt no / vehicle" value="${esc(q)}">
    <button class="btn ghost small" id="isearch">Search</button><span style="flex:1"></span>
    ${can('intake.write') ? '<button class="btn" id="newrcpt">+ New receipt</button>' : ''}
  </div>
  <div class="panel"><h2>Intake receipts</h2><div class="body">
    <table class="tbl"><thead><tr><th>No</th><th>Received</th><th>Owner</th><th>Variety/Grade</th><th class="num">Net</th><th class="num">Units</th><th>Vehicle</th><th>Status</th><th></th></tr></thead>
    <tbody>${rows || '<tr><td colspan="9" class="note">none yet</td></tr>'}</tbody></table></div></div>`;
}

function partyName(id) { const p = M().parties.find((x) => x.id === id); return p ? `${p.code} · ${p.name}` : `#${id}`; }

function newReceiptModal() {
  const m = M();
  const owners = m.parties.filter((p) => p.roles.includes('owner'));
  const growers = m.parties.filter((p) => p.roles.includes('grower'));
  const o = modal('New intake receipt', `<form id="rform"><div class="frm-grid">
    <div><label class="f">Pre-inward advice</label><select name="advice_id" id="r-adv"><option value="">— walk-in (no advice) —</option></select></div>
    <div><label class="f">Season</label>${sel('season_id', m.seasons, 'id', 'name')}</div>
    <div><label class="f">Owner *</label><select name="owner_id" required>${opt(owners, 'id', 'name')}</select></div>
    <div><label class="f">Grower</label><select name="grower_id"><option value="">—</option>${opt(growers, 'id', 'name')}</select></div>
    <div><label class="f">Vehicle no</label><input name="vehicle_no"></div>
    <div><label class="f">Weighbridge ref</label><input name="weighbridge_ref"></div>
    <div><label class="f">Variety *</label>${sel('variety_id', m.varieties, 'id', 'name')}</div>
    <div><label class="f">Grade *</label>${sel('grade_id', m.grades, 'id', 'name')}</div>
    <div><label class="f">Harvest year *</label><input name="harvest_year" type="number" value="2026" required></div>
    <div><label class="f">Packaging *</label>${sel('packaging_id', m.packages, 'id', 'name')}</div>
    <div><label class="f">Units (crates/bins) *</label><input name="unit_count" type="number" min="0" required></div>
    <div><label class="f">Gross kg *</label><input name="gross_kg" type="number" step="0.001" required></div>
    <div><label class="f">Tare kg *</label><input name="tare_kg" type="number" step="0.001" required></div>
    <div><label class="f">Condition notes</label><input name="condition_notes"></div>
  </div><div class="note">Net = gross − tare. The receipt is a <b>draft</b> until a second operator verifies it (maker/checker). Saved offline-safe.</div>
  <div class="actions"><button class="btn">Save draft</button></div></form>`);
  if (can('inward.read')) {
    get('/inward/advices?status=open').then((ads) => {
      const s = o.querySelector('#r-adv');
      s.innerHTML += ads.map((a) => `<option value="${a.id}" data-owner="${a.owner_id}" data-variety="${a.variety_id}" data-grade="${a.grade_id ?? ''}" data-kg="${a.expected_kg}" data-units="${a.expected_units}" data-vehicle="${esc(a.vehicle_no)}">${esc(a.advice_no)} · ${esc(partyName(a.owner_id))} · exp ${kg(a.expected_kg)}</option>`).join('');
      s.onchange = () => {
        const optEl = s.selectedOptions[0];
        if (!optEl || !optEl.dataset.owner) return;
        o.querySelector('[name="owner_id"]').value = optEl.dataset.owner;
        o.querySelector('[name="variety_id"]').value = optEl.dataset.variety;
        if (optEl.dataset.grade) o.querySelector('[name="grade_id"]').value = optEl.dataset.grade;
        o.querySelector('[name="vehicle_no"]').value = optEl.dataset.vehicle || '';
      };
    }).catch(() => {});
  }
  o.querySelector('#rform').onsubmit = async (ev) => {
    ev.preventDefault();
    const v = formVals(ev.target);
    const body = {
      season_id: +v.season_id, owner_id: +v.owner_id, grower_id: intOr(v.grower_id) || null,
      vehicle_no: v.vehicle_no || '', weighbridge_ref: v.weighbridge_ref || '',
      variety_id: +v.variety_id, grade_id: +v.grade_id, harvest_year: +v.harvest_year,
      packaging_id: +v.packaging_id, unit_count: +v.unit_count,
      gross_kg: +v.gross_kg, tare_kg: +v.tare_kg, condition_notes: v.condition_notes || '',
      advice_id: intOr(v.advice_id) || null,
    };
    try {
      const r = await submitOp('/intake/receipts', body, `receipt ${v.vehicle_no || ''}`);
      if (r.queued) toast('Offline — receipt queued to outbox with op id', 'ok');
      else toast(`Receipt ${r.data.receipt_no} saved`, 'ok');
      o.remove(); render();
    } catch (e) {
      if (e instanceof ApiError && e.status === 409 && confirm(String(e.data?.detail?.detail || e.data?.detail) + '\n\nConfirm as separate load?')) {
        try { const r = await submitOp('/intake/receipts', { ...body, force: true }); o.remove(); toast('Saved (forced)', 'ok'); render(); } catch (e2) { errToast(e2); }
      } else errToast(e);
    }
  };
}

function verifyModal(rid) {
  const active = M().chambers.filter((c) => c.status === 'active');
  const o = modal('Verify receipt', `<form id="vf">
    <label class="f">Allocate immediately to chamber (optional)</label>
    <select name="chamber_id"><option value="">— verify only, bin later —</option>
      ${active.map((c) => `<option value="${c.id}">${esc(c.code)} (avail ${kg(c.available_kg)})</option>`).join('')}</select>
    <label class="f">Note</label><input name="reason">
    <div class="note">Verification creates the lot. You cannot verify a receipt you created yourself.</div>
    <div class="actions"><button class="btn">Verify & create lot</button></div></form>`);
  o.querySelector('#vf').onsubmit = async (ev) => {
    ev.preventDefault();
    const v = formVals(ev.target);
    try {
      const d = await runOp(`/intake/receipts/${rid}/verify`, { chamber_id: intOr(v.chamber_id) || null, reason: v.reason || '' },
        (d) => `Lot ${d.lot_no} created`);
      if (d) o.remove(); loadMasters();
    } catch (e) { errToast(e); }
  };
}

/* ================= LOTS ================= */
async function viewLots() {
  const [lots, pendingAdj] = await Promise.all([get('/lots?limit=500'), get('/adjustments?status=pending')]);
  state.lotsNext = lots.length === 500 ? lots[lots.length - 1].id : null;
  const rows = lots.map(lotRow).join('');
  const adj = pendingAdj.map((a) => `<tr>
    <td>${esc(a.adj_no)}</td><td>${a.direction}</td><td>${esc(a.lot_no)}</td><td>${esc(a.chamber)}</td>
    <td class="num">${kg(a.qty_kg)}</td><td>${esc(a.reason)}</td>
    <td>${can('adjustments.approve') ? `<button class="btn small" data-approve="${a.id}">Approve</button>
      <button class="btn small ghost danger" data-reject="${a.id}">Reject</button>` : '<span class="badge pending">awaiting approval</span>'}</td></tr>`).join('');
  return `
  ${can('adjustments.approve') || true ? `<div class="panel"><h2>Pending stock adjustments (${pendingAdj.length})</h2><div class="body">
    <table class="tbl"><thead><tr><th>No</th><th>Type</th><th>Lot</th><th>Chamber</th><th class="num">Qty</th><th>Reason</th><th></th></tr></thead>
    <tbody>${adj || '<tr><td colspan="7" class="note">physical stock matches recorded stock</td></tr>'}</tbody></table></div></div>` : ''}
  <div class="panel"><h2>Lots</h2><div class="body">
    <table class="tbl"><thead><tr><th>Lot</th><th>Owner</th><th>Variety · Grade</th><th>Harvest</th><th class="num">Stored</th><th class="num">Un-binned</th><th class="num">Units</th><th></th></tr></thead>
    <tbody id="lotrows">${rows || '<tr><td colspan="8" class="note">no lots yet</td></tr>'}</tbody></table>
    <div class="pager" style="margin-top:8px">
      <span id="lotcount" class="note">showing ${lots.length}${state.lotsNext ? ' of many' : ''} lots</span>
      ${state.lotsNext ? '<button class="btn small ghost" data-morelots>Load next 500 ▾</button>' : ''}
    </div></div></div>`;
}

function lotRow(l) {
  return `<tr>
    <td><b>${esc(l.lot_no)}</b></td><td>${esc(l.owner_name)}</td><td>${esc(l.variety)} · ${esc(l.grade)}</td>
    <td>${l.harvest_year}</td><td class="num">${kg(l.stored_kg)}</td>
    <td class="num">${l.unallocated_kg ? kg(l.unallocated_kg) : '—'}</td>
    <td class="num">${nf.format(l.stored_units)}</td>
    <td><button class="btn small" data-lot="${l.id}">Open</button></td></tr>`;
}

async function loadMoreLots(btn) {
  btn.disabled = true;
  btn.textContent = 'Loading…';
  try {
    const more = await get(`/lots?limit=500&before_id=${encodeURIComponent(state.lotsNext)}`);
    const tbody = document.getElementById('lotrows');
    const note = tbody.querySelector('.note')?.closest('tr');
    if (note) note.remove();
    tbody.insertAdjacentHTML('beforeend', more.map(lotRow).join(''));
    $$('#lotrows [data-lot]').forEach((b) => b.onclick = () => lotModal(+b.dataset.lot));
    state.lotsNext = more.length === 500 ? more[more.length - 1].id : null;
    const cnt = document.getElementById('lotcount');
    if (cnt) cnt.textContent = `showing ${tbody.rows.length}${state.lotsNext ? ' of many' : ''} lots`;
    if (!state.lotsNext) btn.remove(); else { btn.disabled = false; btn.textContent = 'Load next 500 ▾'; }
  } catch (e) {
    btn.disabled = false;
    btn.textContent = 'Retry — load failed';
  }
}

async function lotModal(lotId) {
  const l = await get(`/lots/${lotId}`);
  const m = M();
  const balRows = l.balances.map((b) => `<tr><td>${esc(b.chamber)}</td><td>${esc(b.package)}</td><td class="num">${kg(b.qty_kg)}</td><td class="num">${b.unit_count}</td></tr>`).join('');
  const mvRows = l.movements.slice(0, 25).map((mv) => `<tr><td>${mv.created_at.slice(0, 16).replace('T', ' ')}</td>
    <td>${mv.type} ${mv.ref ? `<span class="mini">${esc(mv.ref)}</span>` : ''}</td>
    <td>${mv.from_chamber ? esc(mv.from_chamber) + ' → ' : ''}${mv.to_chamber ? esc(mv.to_chamber) : ''}${mv.to_lot ? ' ⇒ ' + esc(mv.to_lot) : ''}</td>
    <td class="num">${kg(mv.qty_kg)} / ${mv.unit_count}u</td><td>${esc(mv.reason)}</td></tr>`).join('');
  const o = modal(`Lot ${l.lot_no}`, `
    <div class="kv-line"><b>Owner</b><span>${esc(l.owner_code)} · ${esc(l.owner_name)}</span></div>
    <div class="kv-line"><b>Variety / Grade / Year</b><span>${esc(l.variety)} · ${esc(l.grade)} · ${l.harvest_year}</span></div>
    <div class="kv-line"><b>Accountable qty</b><span>${kg(l.received_kg)} (stored ${kg(l.stored_kg)}, un-binned ${kg(l.unallocated_kg)})</span></div>
    <div class="kv-line"><b>Trace</b><span class="trace">
      ${l.parents.map((p) => `<span>← ${esc(p.lot_no)} (${kg(p.qty_kg)})</span>`).join('')}
      [ <b>${esc(l.lot_no)}</b> ]
      ${l.children.map((c) => `<span>→ ${esc(c.lot_no)} (${kg(c.qty_kg)})</span>`).join('')}</span></div>
    <h3 style="font-size:13px;margin:14px 0 6px">Stored in</h3>
    <table class="tbl"><tbody>${balRows || '<tr><td class="note">not binned</td></tr>'}</tbody></table>
    <h3 style="font-size:13px;margin:14px 0 6px">Movement history</h3>
    <table class="tbl"><tbody>${mvRows || '<tr><td class="note">—</td></tr>'}</tbody></table>
    ${can('movements.write') ? `<div class="actions">
      ${l.unallocated_kg > 0 ? '<button class="btn ghost" id="m-alloc">Allocate to chamber</button>' : ''}
      <button class="btn ghost" id="m-transfer">Transfer</button>
      <button class="btn ghost" id="m-split">Split</button><button class="btn ghost" id="m-merge">Merge</button>
      <button class="btn ghost danger" id="m-adj">Report discrepancy</button></div>` : ''}
    ${can('barcodes.issue') ? `<div class="actions">
      <button class="btn ghost" id="m-barcode">🏷 Issue/print labels</button></div>` : ''}
    ${can('barcodes.read') ? `<div class="actions"><button class="btn ghost" id="m-scan">📷 Scan a label</button></div>` : ''}`);

  const wire = (id, fn) => { const b = o.querySelector(id); if (b) b.onclick = fn; };
  wire('#m-alloc', () => movementModal(o, `Allocate lot ${l.lot_no}`, async (v, done) => {
    await runOp(`/lots/${l.id}/allocate`, { chamber_id: +v.chamber_id, package_id: +v.package_id, qty_kg: +v.qty_kg, unit_count: +v.unit_count || 0, reason: v.reason || '' }, 'Allocation posted'); done();
  }, { max: l.unallocated_kg, chambers: true, pkg: true }));
  wire('#m-transfer', () => movementModal(o, `Transfer lot ${l.lot_no}`, async (v, done) => {
    await runOp(`/lots/${l.id}/transfer`, { from_chamber_id: +v.from_chamber_id, to_chamber_id: +v.to_chamber_id, package_id: +v.package_id, qty_kg: +v.qty_kg, unit_count: +v.unit_count || 0, reason: v.reason || '' }, 'Transfer posted'); done();
  }, { transfer: l.balances }));
  wire('#m-split', () => movementModal(o, `Split lot ${l.lot_no} (child keeps parentage)`, async (v, done) => {
    await runOp('/lots/split', { source_lot_id: l.id, chamber_id: +v.chamber_id, package_id: +v.package_id, qty_kg: +v.qty_kg, unit_count: +v.unit_count || 0, reason: v.reason || 'split' },
      (d) => `Child lot ${d.child_lot_no} created`); done();
  }, { fromLot: l }));
  wire('#m-merge', () => mergeModal(o, l));
  wire('#m-adj', () => movementModal(o, `Report discrepancy on lot ${l.lot_no}`, async (v, done) => {
    await runOp('/adjustments', { direction: v.direction, lot_id: l.id, chamber_id: +v.chamber_id, package_id: +v.package_id, qty_kg: +v.qty_kg, reason: v.reason },
      'Adjustment recorded — pending manager approval (maker/checker)'); done();
  }, { adj: l }));
  wire('#m-barcode', () => barcodeIssueModal(l.id, l.lot_no, l.stored_units));
  wire('#m-scan', () => scanModal());
}

async function refreshAll() { await loadMasters(); render(); }

function movementModal(parent, title, onSave, opts = {}) {
  const m = M();
  const active = m.chambers.filter((c) => c.status === 'active');
  let extra = '';
  if (opts.transfer) {
    extra = `<label class="f">From chamber</label><select name="from_chamber_id">${opt(active, 'id', 'code')}</select>`;
  }
  if (opts.adj) {
    extra = `<label class="f">Direction</label><select name="direction"><option value="shortage">shortage (physical < recorded)</option><option value="overage">overage (physical > recorded)</option></select>`;
  }
  const o2 = modal(title, `<form id="mf"><div class="frm-grid">
    ${extra}
    <div><label class="f">Chamber</label><select name="${opts.transfer ? 'to_chamber_id' : 'chamber_id'}">${opt(active, 'id', 'code')}</select></div>
    <div><label class="f">Packaging</label>${sel('package_id', m.packages, 'id', 'name')}</div>
    <div><label class="f">Qty kg</label><input name="qty_kg" type="number" step="0.001" required></div>
    <div><label class="f">Units</label><input name="unit_count" type="number" min="0" value="0"></div>
    <div><label class="f">Reason</label><input name="reason" ${opts.adj ? 'required' : ''}></div>
  </div><div class="actions"><button class="btn">Save</button></div></form>`);
  o2.querySelector('#mf').onsubmit = async (ev) => {
    ev.preventDefault();
    try {
      await onSave(formVals(ev.target), () => { o2.remove(); parent.remove(); refreshAll(); });
    } catch (e) { errToast(e); }
  };
}

function mergeModal(parent, lot) {
  const o2 = modal(`Merge — child lot will carry BOTH parents' exact contributions`, `<form id="mf2">
    <div class="frm-grid">
      <div><label class="f">This lot qty (from ${esc(lot.lot_no)})</label><input name="q1" type="number" step="0.001" required></div>
      <div><label class="f">Other lot id (same owner/variety/grade/year)</label><input name="lot2" type="number" required></div>
      <div><label class="f">Other lot qty</label><input name="q2" type="number" step="0.001" required></div>
      <div><label class="f">Chamber</label><select name="chamber_id">${opt(lot.balances.map((b) => ({ id: b.chamber_id, code: b.chamber })), 'id', 'code')}</select></div>
      <div><label class="f">Packaging</label>${sel('package_id', M().packages, 'id', 'name')}</div>
      <div><label class="f">Reason</label><input name="reason" required></div>
    </div><div class="actions"><button class="btn">Merge</button></div></form>`);
  o2.querySelector('#mf2').onsubmit = async (ev) => {
    ev.preventDefault();
    const v = formVals(ev.target);
    try {
      const d = await runOp('/lots/merge', {
        sources: [{ lot_id: lot.id, qty_kg: +v.q1, unit_count: 0 }, { lot_id: +v.lot2, qty_kg: +v.q2, unit_count: 0 }],
        chamber_id: +v.chamber_id, package_id: +v.package_id, reason: v.reason,
      }, (d) => `Merged into ${d.merged_lot_no}`);
      if (d) { o2.remove(); parent.remove(); loadMasters(); }
    } catch (e) { errToast(e); }
  };
}

/* ================= CHAMBERS ================= */
async function viewChambers() {
  const chs = M().chambers;
  const grid = chs.map((c) => `<div class="ch" data-ch="${c.id}">
    <div class="code">${esc(c.code)} <span class="badge ${c.status}">${c.status}</span></div>
    <div class="bar"><i class="${c.occupancy_pct > 90 ? 'full' : c.occupancy_pct > 70 ? 'high' : ''}" style="width:${Math.min(100, c.occupancy_pct)}%"></i></div>
    <div class="kv">${kg(c.stored_kg)} stored · ${kg(c.available_kg)} free</div>
    <div class="kv">${nf.format(c.occupied_units)} ${esc(c.unit_label)}</div></div>`).join('');
  return `<div class="panel"><h2>Chambers — click for contents</h2><div class="body">
    <div class="chgrid">${grid}</div></div></div>`;
}

async function chamberModal(cid) {
  const o = await get(`/chambers/${cid}/occupancy`);
  const rows = o.balances.map((b) => `<tr><td>${esc(b.lot_no)}</td><td>${esc(b.variety)}</td><td>${esc(b.package)}</td><td class="num">${kg(b.qty_kg)}</td><td class="num">${b.unit_count}</td></tr>`).join('');
  modal(`Chamber ${o.chamber}`, `
    <div class="kv-line"><b>Status</b><span class="badge ${o.status}">${o.status}</span></div>
    <div class="kv-line"><b>Stored / capacity</b><span>${kg(o.stored_kg)} / ${kg(o.capacity_kg)}</span></div>
    <div class="kv-line"><b>Available</b><span>${kg(o.available_kg)}</span></div>
    <table class="tbl" style="margin-top:10px"><thead><tr><th>Lot</th><th>Variety</th><th>Packaging</th><th class="num">Qty</th><th class="num">Units</th></tr></thead>
    <tbody>${rows || '<tr><td colspan="5" class="note">empty</td></tr>'}</tbody></table>`);
}

/* ================= DISPATCH ================= */
async function viewDispatch() {
  const ds = await get('/dispatch');
  const rows = ds.map((d) => `<tr>
    <td><b>${esc(d.dispatch_no)}</b></td><td>${d.dispatched_at.slice(0, 16).replace('T', ' ')}</td>
    <td>${esc(partyName(d.owner_id))}</td><td class="num">${kg(d.total_kg)}</td>
    <td>${esc(d.vehicle_no)}</td><td>${esc(d.authorized_person_name)}</td>
    <td><span class="badge ${d.status}">${d.status}</span></td>
    <td>${d.status === 'draft' && can('dispatch.write') ? `<button class="btn small" data-fin="${d.id}">Finalize</button>` : ''}
      ${d.status === 'finalized' && can('dispatch.void') ? `<button class="btn small ghost danger" data-void="${d.id}">Void</button>` : ''}
      <button class="btn small ghost" data-print="/dispatch/${d.id}/print">Print</button></td></tr>`).join('');
  return `<div class="toolbar"><span style="flex:1"></span>
    ${can('dispatch.write') ? '<button class="btn" id="newdsp">+ New dispatch</button>' : ''}</div>
  <div class="panel"><h2>Dispatch notes</h2><div class="body">
    <table class="tbl"><thead><tr><th>No</th><th>Date</th><th>Owner</th><th class="num">Qty</th><th>Vehicle</th><th>Released by</th><th>Status</th><th></th></tr></thead>
    <tbody>${rows || '<tr><td colspan="8" class="note">none yet</td></tr>'}</tbody></table></div></div>`;
}

async function newDispatchModal() {
  const m = M();
  const owners = m.parties.filter((p) => p.roles.includes('owner'));
  const o = modal('New dispatch note', `<form id="df"><div class="frm-grid">
    <div><label class="f">Season</label>${sel('season_id', m.seasons, 'id', 'name')}</div>
    <div><label class="f">Owner *</label><select name="owner_id" id="d-owner">${opt(owners, 'id', 'name')}</select></div>
    <div><label class="f">Released by (owner or registered agent) *</label><select name="released_by_party_id">${opt(m.parties.filter((p) => p.roles.includes('owner') || p.roles.includes('agent')), 'id', 'name')}</select></div>
    <div><label class="f">Authorized person name *</label><input name="authorized_person_name" required></div>
    <div><label class="f">ID proof type *</label><select name="id_proof_type"><option>aadhaar</option><option>pan</option><option>driving licence</option><option>voter id</option><option>other</option></select></div>
    <div><label class="f">ID proof no *</label><input name="id_proof_no" required></div>
    <div><label class="f">Vehicle no</label><input name="vehicle_no"></div>
    <div><label class="f">Destination</label><input name="destination"></div>
  </div>
  <h3 style="font-size:13px;margin:14px 0 6px">Lines</h3>
  <div id="dlines"></div>
  <button type="button" class="btn ghost small" id="addline">+ add line</button>
  <div class="actions"><button class="btn">Save draft</button></div></form>`);
  const linesEl = o.querySelector('#dlines');
  let lots = [];
  const ownerSel = o.querySelector('#d-owner');
  const loadLots = async () => { lots = await get(`/lots?owner_id=${ownerSel.value}`); linesEl.innerHTML = ''; addLine(); };
  const addLine = () => {
    const div = document.createElement('div');
    div.className = 'frm-grid';
    div.innerHTML = `
      <div><label class="f">Lot</label><select class="ln-lot">${lots.filter((l) => l.stored_kg > 0).map((l) => `<option value="${l.id}">${esc(l.lot_no)} · ${kg(l.stored_kg)}</option>`).join('')}</select></div>
      <div><label class="f">Chamber</label><select class="ln-ch"></select></div>
      <div><label class="f">Qty kg</label><input class="ln-qty" type="number" step="0.001" required></div>
      <div><label class="f">Units</label><input class="ln-units" type="number" value="0" min="0"></div>`;
    const lotSel = div.querySelector('.ln-lot');
    const chSel = div.querySelector('.ln-ch');
    const fillCh = async () => {
      const ld = await get(`/lots/${lotSel.value}`);
      chSel.innerHTML = ld.balances.map((b) => `<option value="${b.chamber_id}|${b.package_id}">${esc(b.chamber)} · ${esc(b.package)} · ${kg(b.qty_kg)} avail</option>`).join('');
    };
    lotSel.onchange = fillCh; fillCh();
    linesEl.appendChild(div);
  };
  ownerSel.onchange = loadLots;
  o.querySelector('#addline').onclick = addLine;
  await loadLots();
  o.querySelector('#df').onsubmit = async (ev) => {
    ev.preventDefault();
    const v = formVals(ev.target);
    const lines = [...linesEl.querySelectorAll('.frm-grid')].map((g) => {
      const [ch, pkg] = g.querySelector('.ln-ch').value.split('|');
      return { lot_id: +g.querySelector('.ln-lot').value, chamber_id: +ch, package_id: +pkg, qty_kg: +g.querySelector('.ln-qty').value, unit_count: +g.querySelector('.ln-units').value || 0 };
    });
    const body = {
      season_id: +v.season_id, owner_id: +v.owner_id, released_by_party_id: +v.released_by_party_id,
      authorized_person_name: v.authorized_person_name, id_proof_type: v.id_proof_type,
      id_proof_no: v.id_proof_no, vehicle_no: v.vehicle_no || '', destination: v.destination || '',
      lines,
    };
    try {
      const r = await submitOp('/dispatch', body, 'dispatch');
      if (r.queued) toast('Offline — dispatch queued to outbox', 'ok');
      else toast(`Dispatch ${r.data.dispatch_no} saved as draft — finalize to release stock`, 'ok');
      o.remove(); render();
    } catch (e) { errToast(e); }
  };
}

/* ================= BILLING ================= */
async function viewBilling() {
  const [invoices, tariffs, taxes, credits] = await Promise.all(
    [get('/invoices'), get('/tariffs'), get('/taxes'),
     can('billing.read') ? get('/credit-notes') : Promise.resolve([])]);
  const invRows = invoices.map((i) => `<tr>
    <td><b>${esc(i.invoice_no)}</b></td><td>${esc(partyName(i.owner_id))}</td>
    <td class="mini">${i.period_from} → ${i.period_to}</td>
    <td class="num">${money(i.subtotal_minor)}</td><td class="num">${money(i.tax_minor)}</td>
    <td class="num"><b>${money(i.total_minor)}</b></td><td class="num">${money(i.outstanding_minor)}</td>
    <td><span class="badge ${i.status}">${i.status}</span><div class="mini">tariff v${i.tariff_version}</div></td>
    <td>${i.status === 'draft' && can('billing.write') ? `<button class="btn small" data-finv="${i.id}">Finalize</button>` : ''}
      ${i.status === 'finalized' && can('billing.write') ? `<button class="btn small ghost" data-credit="${i.id}">Credit</button>` : ''}
      ${i.status !== 'void' && can('billing.void') ? `<button class="btn small ghost danger" data-vinv="${i.id}">Void</button>` : ''}
      <button class="btn small ghost" data-print="/invoices/${i.id}/print">Print</button></td></tr>`).join('');
  const tarRows = tariffs.map((t) => `<tr><td>${esc(t.code)} v${t.version}</td><td>${esc(t.name)}</td>
    <td><span class="badge ${t.status}">${t.status}</span></td><td class="mini">from ${t.effective_from}${t.owner_id ? ' · contract ' + esc(partyName(t.owner_id)) : ''}</td>
    <td>${t.status === 'draft' && can('billing.write') ? `<button class="btn small" data-act="${t.id}">Activate</button>` : ''}</td></tr>`).join('');
  return `
  <div class="toolbar"><span style="flex:1"></span>
    ${can('billing.write') ? `<button class="btn" id="geninv">Generate invoice</button>
    <button class="btn ghost" id="newtar">New tariff</button>
    <button class="btn ghost" id="newtax">Add tax</button>` : ''}
    <button class="btn ghost" id="stmtbtn">Owner statement / payment</button></div>
  <div class="panel"><h2>Invoices</h2><div class="body">
    <table class="tbl"><thead><tr><th>No</th><th>Owner</th><th>Period</th><th class="num">Subtotal</th><th class="num">Tax</th><th class="num">Total</th><th class="num">Outstanding</th><th>Status</th><th></th></tr></thead>
    <tbody>${invRows || '<tr><td colspan="9" class="note">no invoices</td></tr>'}</tbody></table></div></div>
  <div class="panel"><h2>Tariffs (versioned)</h2><div class="body">
    <table class="tbl"><thead><tr><th>Code</th><th>Name</th><th>Status</th><th>Scope</th><th></th></tr></thead><tbody>${tarRows}</tbody></table></div></div>
  ${can('billing.read') ? `<div class="panel"><h2>Credit notes (maker/checker; approved credits reduce outstanding)</h2><div class="body">
    <table class="tbl"><thead><tr><th>No</th><th>Invoice</th><th class="num">Amount</th><th>Reason</th><th>Status</th><th></th></tr></thead>
    <tbody>${creditRows(credits) || '<tr><td colspan="6" class="note">no credit notes</td></tr>'}</tbody></table></div></div>` : ''}`;
}

function creditRows(credits) {
  return credits.slice(0, 20).map((c) => `<tr>
    <td><b>${esc(c.credit_no)}</b></td><td>${esc((c.invoice_id || '').toString())}</td>
    <td class="num">${money(c.amount_minor)}</td><td class="mini">${esc(c.reason)}</td>
    <td><span class="badge ${c.status}">${c.status}</span></td>
    <td>${c.status === 'draft' && can('billing.approve') ? `<button class="btn small" data-cnap="${c.id}">Approve</button>
      <button class="btn small ghost danger" data-cnrj="${c.id}">Reject</button>` : ''}
      ${(c.status === 'draft' || c.status === 'approved') && can('billing.approve') ? `<button class="btn small ghost danger" data-cnvd="${c.id}">Void</button>` : ''}</td></tr>`).join('');
}

function creditNoteModal(invoiceId) {
  const o = modal('New credit note (draft — manager approves)', `<form id="cnf" class="frm-grid">
    <div><label class="f">Amount ₹</label><input name="amount" type="number" step="0.01" min="0.01" required></div>
    <div><label class="f">Reason (min 5 chars)</label><input name="reason" required minlength="5"></div>
    <div class="actions"><button class="btn">Save draft</button></div></form>
    <div class="note">Only an APPROVED credit note reduces the invoice outstanding. The invoice itself is never edited.</div>`);
  o.querySelector('#cnf').onsubmit = async (ev) => {
    ev.preventDefault();
    const v = formVals(ev.target);
    try {
      const d = await runOp('/credit-notes', { invoice_id: invoiceId,
        amount_minor: Math.round(+v.amount * 100), reason: v.reason },
        (d) => `Credit note ${d.credit_no} saved — awaiting approval`);
      if (d) o.remove();
    } catch (e) { errToast(e); }
  };
}

function genInvoiceModal() {
  const owners = M().parties.filter((p) => p.roles.includes('owner'));
  const o = modal('Generate invoice (draft; deterministic from ledger + tariff version)', `<form id="gf"><div class="frm-grid">
    <div><label class="f">Owner</label><select name="owner_id">${opt(owners, 'id', 'name')}</select></div>
    <div><label class="f">From</label><input name="period_from" type="date" required></div>
    <div><label class="f">To</label><input name="period_to" type="date" required></div>
    <div><label class="f">Discount ₹</label><input name="discount" type="number" step="0.01" value="0" min="0"></div>
    <div><label class="f">Due date</label><input name="due_date" type="date"></div>
  </div><div class="note">Storage is computed per lot per day from the movement ledger; one-time charges from receipts/dispatches. Rates snapshot onto each line.</div>
  <div class="actions"><button class="btn">Generate</button></div></form>`);
  o.querySelector('#gf').onsubmit = async (ev) => {
    ev.preventDefault();
    const v = formVals(ev.target);
    try {
      const d = await runOp('/invoices/generate', {
        owner_id: +v.owner_id, period_from: v.period_from, period_to: v.period_to,
        discount_minor: Math.round((+v.discount || 0) * 100),
        due_date: v.due_date || null,
      }, (d) => `Draft invoice ${d.invoice_no}: ${money(d.total_minor)}`);
      if (d) o.remove();
    } catch (e) { errToast(e); }
  };
}

function newTariffModal() {
  const o = modal('New tariff (draft — activates explicitly, supersedes prior version)', `<form id="tf"><div class="frm-grid">
    <div><label class="f">Code</label><input name="code" required placeholder="STD"></div>
    <div><label class="f">Name</label><input name="name" required></div>
    <div><label class="f">Version</label><input name="version" type="number" value="1" min="1"></div>
    <div><label class="f">Effective from</label><input name="effective_from" type="date" required></div>
  </div><h3 style="font-size:13px;margin:12px 0 4px">Rate lines</h3><div id="tlines"></div>
  <button type="button" class="btn ghost small" id="addt">+ add line</button>
  <div class="actions"><button class="btn">Save tariff</button></div></form>`);
  const tl = o.querySelector('#tlines');
  const add = () => {
    const d = document.createElement('div');
    d.className = 'frm-grid';
    d.innerHTML = `
      <div><label class="f">Charge</label><select class="t-cc"><option>storage</option><option>intake</option><option>weighing</option><option>grading</option><option>handling</option><option>unloading</option><option>loading</option></select></div>
      <div><label class="f">Basis</label><select class="t-basis"><option>per_kg</option><option>per_unit</option><option>per_receipt</option><option>per_dispatch</option><option>flat</option></select></div>
      <div><label class="f">Period</label><select class="t-period"><option>one_time</option><option>daily</option><option>monthly</option><option>seasonal</option></select></div>
      <div><label class="f">Rate ₹</label><input class="t-rate" type="number" step="0.0001" min="0" required></div>`;
    tl.appendChild(d);
  };
  o.querySelector('#addt').onclick = add; add();
  o.querySelector('#tf').onsubmit = async (ev) => {
    ev.preventDefault();
    const v = formVals(ev.target);
    const lines = [...tl.querySelectorAll('.frm-grid')].map((g) => ({
      charge_code: g.querySelector('.t-cc').value, basis: g.querySelector('.t-basis').value,
      period: g.querySelector('.t-period').value, rate: +g.querySelector('.t-rate').value,
    }));
    try {
      await post('/tariffs', { code: v.code, name: v.name, version: +v.version, effective_from: v.effective_from, lines });
      toast('Tariff saved as draft — activate to take effect', 'ok');
      o.remove(); render();
    } catch (e) { errToast(e); }
  };
}

async function statementModal() {
  const owners = M().parties.filter((p) => p.roles.includes('owner'));
  const o = modal('Owner statement & payment', `<form id="sf" class="frm-grid" style="margin-bottom:10px">
    <div><label class="f">Owner</label><select name="owner_id" id="s-owner">${opt(owners, 'id', 'name')}</select></div>
    <div style="display:flex;align-items:end"><button class="btn ghost small" type="submit">Refresh</button></div></form>
    <div id="stmt"></div>`);
  const render = async () => {
    const oid = o.querySelector('#s-owner').value;
    const st = await get(`/owners/${oid}/statement`);
    const invRows = st.invoices.map((i) => `<tr><td>${esc(i.invoice_no)}</td><td>${i.period_from} → ${i.period_to}</td>
      <td class="num">${money(i.total_minor)}</td><td class="num">${money(i.outstanding_minor)}</td><td><span class="badge ${i.status}">${i.status}</span></td></tr>`).join('');
    const payRows = st.payments.map((p) => `<tr><td>${esc(p.payment_no)}</td><td>${p.received_at.slice(0, 10)}</td><td>${p.method}</td><td class="num">${money(p.amount_minor)}</td></tr>`).join('');
    o.querySelector('#stmt').innerHTML = `
      <div class="cards" style="grid-template-columns:repeat(3,1fr)">
        <div class="card ${st.total_outstanding_minor ? 'warn' : 'ok'}"><div class="k">Outstanding</div><div class="v">${money(st.total_outstanding_minor)}</div></div>
        <div class="card"><div class="k">Credit balance</div><div class="v">${money(st.credit_minor)}</div></div>
        <div class="card"><div class="k">Net due</div><div class="v">${money(st.net_due_minor)}</div></div></div>
      <table class="tbl"><thead><tr><th>Invoice</th><th>Period</th><th class="num">Total</th><th class="num">Outstanding</th><th></th></tr></thead><tbody>${invRows || '<tr><td colspan="5" class="note">none</td></tr>'}</tbody></table>
      <table class="tbl" style="margin-top:12px"><thead><tr><th>Payment</th><th>Date</th><th>Method</th><th class="num">Amount</th></tr></thead><tbody>${payRows || '<tr><td colspan="4" class="note">none</td></tr>'}</tbody></table>
      <div class="actions"><button class="btn ghost small" id="pst">Print statement</button>
      ${can('payments.write') ? '<button class="btn small" id="pay">Record payment</button>' : ''}</div>`;
    o.querySelector('#pst').onclick = () => openPrint(`/owners/${oid}/statement/print`);
    const pb = o.querySelector('#pay');
    if (pb) pb.onclick = () => paymentModal(+oid, st.invoices.filter((i) => i.outstanding_minor > 0));
  };
  o.querySelector('#sf').onsubmit = (e) => { e.preventDefault(); render(); };
  await render();
}

function paymentModal(ownerId, openInvoices) {
  const o = modal('Record payment', `<form id="pf"><div class="frm-grid">
    <div><label class="f">Amount ₹</label><input name="amount" type="number" step="0.01" min="0.01" required></div>
    <div><label class="f">Method</label><select name="method"><option>cash</option><option>upi</option><option>bank</option><option>cheque</option></select></div>
    <div><label class="f">Reference</label><input name="reference"></div>
    <div><label class="f">Allocate to invoice (optional — else auto oldest)</label>
      <select name="invoice_id"><option value="">auto</option>${openInvoices.map((i) => `<option value="${i.invoice_no}|${i.outstanding_minor}">${esc(i.invoice_no)} · ${money(i.outstanding_minor)} due</option>`).join('')}</select></div>
  </div><div class="note">Idempotent — a retry reuses the same operation and can never double-post.</div>
  <div class="actions"><button class="btn">Post payment</button></div></form>`);
  o.querySelector('#pf').onsubmit = async (ev) => {
    ev.preventDefault();
    const v = formVals(ev.target);
    const body = { owner_id: ownerId, amount_minor: Math.round(+v.amount * 100), method: v.method, reference: v.reference || '', operation_id: uuid() };
    if (v.invoice_id) {
      const invs = await get(`/invoices?owner_id=${ownerId}`);
      const inv = invs.find((i) => i.invoice_no === v.invoice_id.split('|')[0]);
      if (inv) body.allocations = [{ invoice_id: inv.id, amount_minor: Math.min(body.amount_minor, inv.outstanding_minor) }];
    }
    try {
      const r = await submitOp('/payments', body, 'payment');
      if (r.queued) toast('Offline — payment queued', 'ok');
      else toast(`Payment ${r.data.payment_no} posted`, 'ok');
      o.remove(); render();
    } catch (e) {
      if (e instanceof ApiError && e.status === 409 && confirm(String(e.data?.detail?.detail || '') + '\nConfirm anyway?')) {
        try { await submitOp('/payments', { ...body, force: true }); o.remove(); toast('Posted (forced)', 'ok'); render(); } catch (e2) { errToast(e2); }
      } else errToast(e);
    }
  };
}

function newTaxModal() {
  const o = modal('Add tax configuration (facility-confirmed rates only)', `<form id="xf"><div class="frm-grid">
    <div><label class="f">Code</label><input name="code" required placeholder="GST9"></div>
    <div><label class="f">Name</label><input name="name" required></div>
    <div><label class="f">Rate %</label><input name="rate" type="number" step="0.001" min="0" max="100" required></div>
    <div><label class="f">Applies to</label><select name="applies_to"><option>ALL</option><option>storage</option><option>handling</option><option>intake</option></select></div>
  </div><div class="note">The rate is snapshotted onto every invoice at generation time.</div>
  <div class="actions"><button class="btn">Save</button></div></form>`);
  o.querySelector('#xf').onsubmit = async (ev) => {
    ev.preventDefault();
    const v = formVals(ev.target);
    try {
      await post('/taxes', { code: v.code, name: v.name, rate_percent: +v.rate, applies_to: v.applies_to, effective_from: new Date().toISOString().slice(0, 10) });
      toast('Tax config saved', 'ok'); o.remove(); render();
    } catch (e) { errToast(e); }
  };
}

/* ================= TELEMETRY (monitoring-only, Phase 2) ================= */
const TMETS = { temp: 'Temperature °C', rh: 'RH %', o2: 'O₂ %', co2: 'CO₂ %' };

function qualityBadge(q) {
  const colors = { GOOD: 'verified', SIMULATED: 'draft', SUSPECT: 'warn', STALE: 'void', DOWN: 'void' };
  return `<span class="badge ${colors[q] || 'draft'}">${esc(q)}</span>`;
}
function ageStr(s) {
  if (s == null) return '—';
  if (s < 95) return `${s}s`;
  if (s < 7200) return `${Math.round(s / 60)}m`;
  return `${Math.round(s / 3600)}h`;
}

function telemetryChart(points, gaps, thresholds) {
  const W = 960, H = 220, P = 34;
  if (points.length < 1) return `<div class="note">no stored points in this window</div>`;
  const xs = points.map((p) => +new Date(p.captured_at));
  const ys = points.map((p) => p.value);
  const lo = Math.min(...ys, ...thresholds.map((t) => t.limit_value));
  const hi = Math.max(...ys, ...thresholds.map((t) => t.limit_value));
  const pad = (hi - lo || 1) * 0.15;
  const t0 = Math.min(...xs), t1 = Math.max(...xs, t0 + 1);
  const X = (t) => P + ((t - t0) / (t1 - t0)) * (W - 2 * P);
  const Y = (v) => H - P + ((lo - pad - v) / (hi - lo + 2 * pad)) * (H - 2 * P);
  // split into runs at reported gaps — breaks render as TRUE data holes, never bridges
  const gapCuts = new Set(gaps.map((g) => g.to));
  const runs = [];
  let cur = [];
  for (const p of points) {
    if (gapCuts.has(p.captured_at) && cur.length) { runs.push(cur); cur = []; }
    cur.push(p);
  }
  if (cur.length) runs.push(cur);
  const col = { GOOD: 'var(--ok)', SIMULATED: 'var(--brand2)', SUSPECT: 'var(--accent)' };
  const lines = runs.map((run) => {
    const segs = {};
    run.forEach((p) => (segs[p.quality] = segs[p.quality] || []).push(p));
    return Object.entries(segs).map(([q, ps]) =>
      `<polyline fill="none" stroke="${col[q] || '#666'}" stroke-width="2" ${q === 'SIMULATED' ? 'stroke-dasharray="5 3"' : ''}
        points="${ps.map((p) => `${X(+new Date(p.captured_at)).toFixed(1)},${Y(p.value).toFixed(1)}`).join(' ')}"/>` +
      ps.map((p) => `<circle cx="${X(+new Date(p.captured_at)).toFixed(1)}" cy="${Y(p.value).toFixed(1)}" r="2.4" fill="${col[q] || '#666'}"/>`).join('')
    ).join('');
  }).join('');
  const threshLines = thresholds.map((t) =>
    `<line x1="${P}" x2="${W - P}" y1="${Y(t.limit_value)}" y2="${Y(t.limit_value)}" stroke="var(--bad)" stroke-dasharray="7 4"/>
     <text x="${P + 4}" y="${Y(t.limit_value) - 4}" font-size="11" fill="var(--bad)">${esc(t.direction)} ${t.limit_value}</text>`).join('');
  const gapSpans = gaps.map((g) =>
    `<line x1="${X(+new Date(g.from))}" x2="${X(+new Date(g.to))}" y1="${H - P + 14}" y2="${H - P + 14}" stroke="var(--bad)" stroke-width="3"/>
     <text x="${X(+new Date(g.from))}" y="${H - P + 26}" font-size="10" fill="var(--bad)">gap ${Math.round(g.seconds / 60)}m</text>`).join('');
  return `<svg viewBox="0 0 ${W} ${H}" style="width:100%;background:#fff;border:1px solid var(--line);border-radius:8px">
    ${threshLines}${lines}${gapSpans}
    <text x="${P}" y="${H - 6}" font-size="10" fill="#666">${new Date(t0).toLocaleTimeString()}</text>
    <text x="${W - P - 60}" y="${H - 6}" font-size="10" fill="#666">${new Date(t1).toLocaleTimeString()}</text>
    <text x="${P}" y="14" font-size="11" fill="#666">green=GOOD · dashed=SIMULATED · orange=SUSPECT · red line=threshold</text></svg>`;
}

async function viewTelemetry() {
  const st = await get('/telemetry/status');
  const ths = await get('/telemetry/thresholds');
  const alerts = await get('/telemetry/alerts');
  const events = await get('/telemetry/events?kind=!quarantine&limit=100');
  state.tcham = state.tcham || 'CA-01';
  state.tmet = state.tmet || 'temp';
  const since = new Date(Date.now() - 6 * 3600e3).toISOString();
  const hist = await get(`/telemetry/readings?chamber_code=${encodeURIComponent(state.tcham)}&metric=${state.tmet}&since=${since}`);
  const simBanner = st.simulated_data_present
    ? `<div class="panel" style="border-left:5px solid var(--accent);background:#fff7ed"><div class="body">
       <b style="color:var(--accent)">SIMULATED DATA</b> — one or more feeds come from the bench simulator,
       not validated live equipment. Decisions based on these values are NOT commissioning evidence.</div></div>` : '';
  const chambers = [...new Set(st.sensors.map((s) => s.chamber_code))];
  const sensorRows = st.sensors.map((s) => {
    const last = s.last_reading;
    return `<tr class="${s.status === 'STALE' ? 'row-bad' : ''}"><td>${esc(s.chamber_code)}</td><td>${esc(TMETS[s.metric] || s.metric)}</td>
      <td class="mono">${esc(s.sensor_id)}</td>
      <td>${last ? `<b>${last.value}</b> ${esc(s.unit)}` : '—'}</td>
      <td>${last ? ageStr(last.age_seconds) + ' ago' : 'no data'}</td>
      <td>${qualityBadge(s.status)}</td>
      <td>${s.calibration.status === 'overdue' ? `<span class="badge void">CAL OVERDUE ${esc(s.calibration.calibrated_until)}</span>` : esc(s.calibration.calibrated_until || 'unknown')}</td></tr>`;
  }).join('');
  const gwRows = st.gateways.map((g) => `<tr><td class="mono">${esc(g.gateway_key)}</td><td>${esc(g.name)}</td>
    <td>${g.status === 'up' ? '<span class="badge active">up</span>' : `<span class="badge void">DOWN${g.down_since ? ' since ' + new Date(g.down_since).toLocaleTimeString() : ''}</span>`}</td>
    <td>${ageStr(g.age_seconds)} ago</td></tr>`).join('');
  const alertRows = alerts.map((a) => `<tr><td>${new Date(a.raised_at).toLocaleTimeString()}</td><td>${esc(a.chamber_code)}</td>
    <td>${esc(a.kind)} ${esc(a.metric)}</td><td>${a.kind === 'threshold' ? `${a.direction} ${a.limit_value}, saw <b>${a.observed_value}</b>` : `silent ${Math.round(a.observed_value)}s`}</td>
    <td><span class="badge ${a.state === 'raised' ? 'void' : 'draft'}">${esc(a.state)}</span></td>
    <td>${a.state === 'raised' && can('telemetry.ack') ? `<button class="btn small" data-ack="${a.id}">ack</button>` : esc(a.acked_by ? `by ${a.acked_by}` : '')}</td></tr>`).join('');
  const evRows = events.slice(0, 25).map((e) => `<tr><td class="mini">${new Date(e.at).toLocaleTimeString()}</td>
    <td><span class="badge draft">${esc(e.kind)}</span></td><td class="mono">${esc(e.entity)}</td>
    <td class="mini">${esc(e.detail)}${e.gap_seconds != null ? ` <b>(gap ${e.gap_seconds}s)</b>` : ''}</td></tr>`).join('');
  const cfgPanel = can('telemetry.config') ? `<div class="panel"><div class="body"><h3>Thresholds (audited)</h3>
    <table><thead><tr><th>chamber</th><th>metric</th><th>dir</th><th>limit</th><th>hyst</th><th>min dur</th></tr></thead><tbody>
    ${ths.map((t) => `<tr><td>${esc(t.chamber_code)}</td><td>${esc(t.metric)}</td><td>${esc(t.direction)}</td><td>${t.limit_value}</td><td>${t.hysteresis}</td><td>${t.min_duration_s}s</td></tr>`).join('') || '<tr><td colspan="6" class="note">none set</td></tr>'}</tbody></table>
    <form id="thf" class="grid-4" style="margin-top:10px">
      <label class="f">Chamber<input name="chamber_code" required placeholder="CA-01"></label>
      <label class="f">Metric<select name="metric">${Object.keys(TMETS).map((m) => `<option>${m}</option>`).join('')}</select></label>
      <label class="f">Direction<select name="direction"><option>high</option><option>low</option></select></label>
      <label class="f">Limit<input name="limit_value" type="number" step="0.1" required></label>
      <label class="f">Hysteresis<input name="hysteresis" type="number" step="0.1" value="0.3"></label>
      <label class="f">Min duration (s)<input name="min_duration_s" type="number" value="60"></label>
      <div class="actions"><button class="btn small">Save threshold</button></div></form>
    <form id="sef" class="grid-4" style="margin-top:10px"><h3 style="grid-column:1/-1">Sensor registration (audited)</h3>
      <label class="f">Sensor ID<input name="sensor_id" required placeholder="SN-12345"></label>
      <label class="f">Chamber<input name="chamber_code" required></label>
      <label class="f">Metric<select name="metric">${Object.keys(TMETS).map((m) => `<option>${m}</option>`).join('')}</select></label>
      <label class="f">Poll interval s<input name="expected_interval_s" type="number" value="60"></label>
      <label class="f">Calibrated until<input name="calibrated_until" type="date"></label>
      <div class="actions"><button class="btn small">Register sensor</button></div></form></div></div>` : '';
  return `${simBanner}
  <div class="grid-2">
    <div class="panel"><div class="body"><h3>Sensors <span class="mini">${st.now.slice(11, 19)} UTC now</span></h3>
      <table class="dense"><thead><tr><th>chamber</th><th>metric</th><th>sensor</th><th>reading</th><th>age</th><th>quality</th><th>calibration</th></tr></thead>
      <tbody>${sensorRows || '<tr><td colspan="7" class="note">no telemetry sensors registered yet</td></tr>'}</tbody></table></div></div>
    <div class="panel"><div class="body"><h3>Open alerts</h3>
      <table class="dense"><thead><tr><th>raised</th><th>chamber</th><th>condition</th><th>value</th><th>state</th><th></th></tr></thead>
      <tbody>${alertRows || '<tr><td colspan="6" class="note">no open or acknowledged alerts</td></tr>'}</tbody></table>
      <h3 style="margin-top:14px">Gateways</h3>
      <table class="dense"><tbody>${gwRows || '<tr><td class="note">none yet</td></tr>'}</tbody></table></div></div>
  </div>
  <div class="panel"><div class="body"><h3>History — raw stored points, no interpolation (gaps drawn as breaks)</h3>
    <div style="display:flex;gap:10px;margin-bottom:8px">
      <select id="tcham">${chambers.map((c) => `<option ${c === state.tcham ? 'selected' : ''}>${esc(c)}</option>`).join('') || `<option>${esc(state.tcham)}</option>`}</select>
      <select id="tmet">${Object.entries(TMETS).map(([k, v]) => `<option value="${k}" ${k === state.tmet ? 'selected' : ''}>${esc(v)}</option>`).join('')}</select>
    </div>
    ${telemetryChart(hist.points, hist.gaps, ths.filter((t) => t.chamber_code === state.tcham && t.metric === state.tmet))}</div></div>
  ${cfgPanel}
  <div class="panel"><div class="body"><h3>Event log</h3>
    <table class="dense"><tbody>${evRows || '<tr><td class="note">no events yet</td></tr>'}</tbody></table></div></div>`;
}

/* ================= REPORTS ================= */
async function viewReports() {
  const receivables = can('reports.financial') ? await get('/reports/receivables') : null;
  const recRows = (receivables || []).map((r) => `<tr><td>${esc(r.owner)}</td>
    <td class="num">${money(r.outstanding_minor)}</td><td class="num">${money(r.overdue_minor)}</td>
    <td class="num">${money(r.credit_minor)}</td></tr>`).join('');
  return `
  <div class="panel"><h2>Stock reconciliation (from persisted ledgers)</h2><div class="body">
    <form id="srf" class="frm-grid" style="max-width:560px">
      <div><label class="f">From</label><input type="date" name="from_date" required></div>
      <div><label class="f">To</label><input type="date" name="to_date" required></div>
      <div style="display:flex;align-items:end"><button class="btn small">Run</button></div></form>
    <div id="srout"></div></div></div>
  ${receivables ? `<div class="panel"><h2>Receivables by owner</h2><div class="body">
    <table class="tbl"><thead><tr><th>Owner</th><th class="num">Outstanding</th><th class="num">Overdue</th><th class="num">Credit</th></tr></thead>
    <tbody>${recRows || '<tr><td colspan="4" class="note">no open receivables</td></tr>'}</tbody></table></div></div>` : ''}
  <div class="panel"><h2>Operations registers & summaries (workflow K)</h2><div class="body">
    <form id="orf" class="frm-grid" style="max-width:640px">
      <div><label class="f">Report</label><select name="kind">
        <option value="inward">Inward register (verified receipts)</option>
        <option value="outward">Outward register (finalized dispatches)</option>
        <option value="daily">Daily operations summary</option>
        <option value="occupancy">Chamber occupancy history</option>
        <option value="stockby">Stock by owner / variety / grade / chamber</option>
      </select></div>
      <div><label class="f">From / Day</label><input type="date" name="from_date" required></div>
      <div id="orf-to"><label class="f">To</label><input type="date" name="to_date"></div>
      <div id="orf-ch" style="display:none"><label class="f">Chamber</label><select name="chamber_id">${opt(M().chambers, 'id', 'code')}</select></div>
      <div id="orf-by" style="display:none"><label class="f">Group by</label><select name="by">
        <option value="owner">Owner</option><option value="variety">Variety</option>
        <option value="grade">Grade</option><option value="chamber">Chamber</option></select></div>
      <div style="display:flex;align-items:end"><button class="btn small">Run</button></div></form>
    <div id="orout"></div></div></div>`;
}

function renderInwardReport(r) {
  const rows = r.receipts.map((x) => `<tr><td><b>${esc(x.receipt_no)}</b></td>
    <td class="mini">${esc(x.received_at.slice(0, 16).replace('T', ' '))}</td><td>${esc(x.owner)}</td>
    <td>${esc(x.variety)}</td><td class="num">${kg(x.net_kg)}</td><td class="num">${nf(x.units)}</td>
    <td>${esc(x.vehicle_no || '')}</td><td>${x.lot_no ? `<a href="#/lots" class="link">${esc(x.lot_no)}</a>` : '—'}</td>
    <td class="mini">${esc(x.advice_no || 'walk-in')}</td></tr>`).join('');
  return `<div class="cards" style="margin-top:12px">
    <div class="card ok"><div class="k">Receipts</div><div class="v">${nf(r.count)}</div></div>
    <div class="card"><div class="k">Total net</div><div class="v">${kg(r.total_net_kg)}</div></div></div>
  <table class="tbl" style="margin-top:10px"><thead><tr><th>Receipt</th><th>When</th><th>Owner</th><th>Variety</th>
    <th class="num">Net kg</th><th class="num">Units</th><th>Vehicle</th><th>Lot</th><th>Advice</th></tr></thead>
    <tbody>${rows || '<tr><td colspan="9" class="note">no verified receipts in window</td></tr>'}</tbody></table>`;
}

function renderOutwardReport(r) {
  const rows = r.dispatches.map((x) => `<tr><td><b>${esc(x.dispatch_no)}</b></td>
    <td class="mini">${esc(x.dispatched_at.slice(0, 16).replace('T', ' '))}</td><td>${esc(x.owner)}</td>
    <td class="num">${kg(x.kg)}</td><td class="num">${nf(x.units)}</td><td>${esc(x.vehicle_no || '')}</td>
    <td>${esc(x.destination || '')}</td><td><span class="badge ${x.status}">${x.status}</span></td>
    <td class="mini">${x.order_id ? 'from order #' + x.order_id : 'direct'}</td></tr>`).join('');
  return `<div class="cards" style="margin-top:12px">
    <div class="card"><div class="k">Dispatch notes</div><div class="v">${nf(r.count)}</div></div>
    <div class="card ok"><div class="k">Finalized</div><div class="v">${kg(r.total_finalized_kg)} · ${nf(r.total_finalized_units)} units</div></div></div>
  <table class="tbl" style="margin-top:10px"><thead><tr><th>Dispatch</th><th>When</th><th>Owner</th>
    <th class="num">kg</th><th class="num">Units</th><th>Vehicle</th><th>Destination</th><th>Status</th><th>Order</th></tr></thead>
    <tbody>${rows || '<tr><td colspan="9" class="note">no dispatches in window</td></tr>'}</tbody></table>`;
}

function renderDailyReport(r) {
  return `<div class="cards" style="margin-top:12px">
    <div class="card ok"><div class="k">Intake</div><div class="v">${r.intake.receipts} rcpt · ${kg(r.intake.net_kg)}</div></div>
    <div class="card"><div class="k">Outward finalized</div><div class="v">${kg(r.outward.finalized_kg)} / ${r.outward.dispatches} notes</div></div>
    <div class="card"><div class="k">Collections</div><div class="v">${money(r.collections_minor)}</div></div>
    <div class="card"><div class="k">Quality</div><div class="v">${r.quality.inspections} insp · rej ${kg(r.quality.rejected_kg)} · dngr ${kg(r.quality.downgraded_kg)}</div></div></div>
  ${r.deliveries.length ? `<h3 style="margin-top:10px">Deliveries</h3><table class="tbl"><tbody>
    ${r.deliveries.map((d) => `<tr><td><b>${esc(d.delivery_no)}</b></td><td><span class="badge ${d.status}">${d.status}</span></td><td class="num">planned ${kg(d.planned_kg)}</td></tr>`).join('')}
  </tbody></table>` : ''}
  ${r.adjustments.length ? `<h3 style="margin-top:10px">Adjustments</h3><table class="tbl"><tbody>
    ${r.adjustments.map((a) => `<tr><td><b>${esc(a.adj_no)}</b></td><td>${esc(a.direction)}</td><td class="num">${kg(a.qty_kg)}</td><td><span class="badge ${a.status}">${a.status}</span></td></tr>`).join('')}
  </tbody></table>` : ''}`;
}

function renderOccupancyReport(r) {
  const max = Math.max(...r.days.map((d) => d.qty_kg), 1);
  const rows = r.days.map((d) => `<tr><td>${esc(d.day)}</td><td class="num">${kg(d.qty_kg)}</td>
    <td class="num">${d.occupancy_pct}%</td>
    <td style="width:40%"><div style="height:10px;background:var(--ok,#3a9);width:${(100 * d.qty_kg / max).toFixed(1)}%;border-radius:5px"></div></td></tr>`).join('');
  return `<h3 style="margin-top:12px">Chamber ${esc(r.chamber)} — capacity ${kg(r.capacity_kg)}</h3>
  <table class="tbl"><thead><tr><th>Day</th><th class="num">Stored</th><th class="num">Occupancy</th><th></th></tr></thead>
  <tbody>${rows}</tbody></table>`;
}

function renderStockByReport(rows) {
  return `<table class="tbl" style="margin-top:12px"><thead><tr><th>Code</th><th>Name</th>
    <th class="num">Stored kg</th><th class="num">Units</th></tr></thead><tbody>
    ${rows.map((x) => `<tr><td><b>${esc(x.code)}</b></td><td>${esc(x.name)}</td>
      <td class="num">${kg(x.qty_kg)}</td><td class="num">${nf(x.units)}</td></tr>`).join('')
      || '<tr><td colspan="4" class="note">no stock</td></tr>'}</tbody></table>`;
}

async function runOpsReport(ev) {
  ev.preventDefault();
  const f = ev.target, v = formVals(f);
  const kind = v.kind;
  try {
    if (kind === 'inward' || kind === 'outward') {
      const r = await get(`/reports/${kind}?from_date=${v.from_date}&to_date=${v.to_date || v.from_date}`);
      $('#orout').innerHTML = kind === 'inward' ? renderInwardReport(r) : renderOutwardReport(r);
    } else if (kind === 'daily') {
      $('#orout').innerHTML = renderDailyReport(await get(`/reports/daily?day=${v.from_date}`));
    } else if (kind === 'occupancy') {
      const to = v.to_date || v.from_date;
      $('#orout').innerHTML = renderOccupancyReport(await get(`/reports/occupancy-history/${v.chamber_id}?from_date=${v.from_date}&to_date=${to}`));
    } else {
      $('#orout').innerHTML = renderStockByReport(await get(`/reports/stock-by?by=${v.by || 'owner'}`));
    }
  } catch (e) { errToast(e); }
}

async function runStockReport(ev) {
  ev.preventDefault();
  const v = formVals(ev.target);
  try {
    const r = await get(`/reports/stock?from_date=${v.from_date}&to_date=${v.to_date}`);
    $('#srout').innerHTML = `<div class="cards" style="margin-top:12px">
      <div class="card"><div class="k">Opening</div><div class="v">${kg(r.opening_kg)}</div></div>
      <div class="card ok"><div class="k">Receipts</div><div class="v">${kg(r.receipts_kg)}</div></div>
      <div class="card"><div class="k">Dispatches</div><div class="v">${kg(r.dispatches_kg)}</div></div>
      <div class="card"><div class="k">Adjustments</div><div class="v">−${kg(r.adjustments_shortage_kg)} / +${kg(r.adjustments_overage_kg)}</div></div>
      <div class="card ${r.closing_equals_live_ledger === false ? 'bad' : 'ok'}"><div class="k">Closing</div><div class="v">${kg(r.closing_kg)}</div>
        <div class="s">${r.closing_equals_live_ledger === null ? 'window ends before today' : (r.closing_equals_live_ledger ? '= live ledger ✓' : 'MISMATCH vs live ledger!')}</div></div></div>`;
  } catch (e) { errToast(e); }
}

/* ================= ADMIN ================= */
async function viewAdmin() {
  let usersHtml = '', auditHtml = '', backupsHtml = '';
  if (can('users.read') || can('admin')) {
    const users = await get('/users');
    usersHtml = `<div class="panel"><h2>Users ${can('admin') ? '<button class="btn small" id="newuser" style="margin-left:auto">+ user</button>' : ''}</h2><div class="body">
      <table class="tbl"><thead><tr><th>User</th><th>Role</th><th>Active</th><th>Last login</th></tr></thead>
      <tbody>${users.map((u) => `<tr><td>${esc(u.username)} · ${esc(u.display_name)}</td><td><span class="rolechip">${esc(u.role)}</span></td>
        <td>${u.active ? '✓' : '—'}</td><td class="mini">${u.last_login_at ? u.last_login_at.slice(0, 16).replace('T', ' ') : 'never'}</td></tr>`).join('')}</tbody></table></div></div>`;
  }
  if (can('audit.read')) {
    const rows = await get('/admin/audit?limit=120');
    auditHtml = `<div class="panel"><h2>Audit trail (latest ${rows.length})</h2><div class="body">
      <table class="tbl"><thead><tr><th>When (UTC)</th><th>Actor</th><th>Action</th><th>Entity</th><th>Reason</th></tr></thead>
      <tbody>${rows.map((a) => `<tr><td class="mini">${a.occurred_at.slice(0, 19).replace('T', ' ')}</td><td>${esc(a.actor)}</td>
        <td>${esc(a.action)}</td><td>${esc(a.entity_type)} ${esc(a.entity_id)}</td><td class="mini">${esc(a.reason)}</td></tr>`).join('')}</tbody></table></div></div>`;
  }
  if (can('backups.manage')) {
    const b = await get('/admin/backups');
    backupsHtml = `<div class="panel"><h2>Backups <button class="btn small" id="dobak" style="margin-left:auto">Backup now</button></h2><div class="body">
      <table class="tbl"><thead><tr><th>File</th><th class="num">Size</th><th>SHA-256</th><th>Created</th></tr></thead>
      <tbody>${b.map((x) => `<tr><td>${esc(x.filename)}</td><td class="num">${nf.format(x.size_bytes)}</td><td class="mini">${esc(x.sha256.slice(0, 16))}…</td><td class="mini">${x.created_at.slice(0, 19).replace('T', ' ')}</td></tr>`).join('') || '<tr><td colspan="4" class="note">none yet</td></tr>'}</tbody></table>
      <div class="note">Restore/rollback procedure: docs/OPERATIONS.md — keep off-host copies.</div></div></div>`;
  }
  return usersHtml + auditHtml + backupsHtml || '<div class="note">No admin scopes for this role.</div>';
}

function newUserModal() {
  const o = modal('Create user', `<form id="uf"><div class="frm-grid">
    <div><label class="f">Username</label><input name="username" required></div>
    <div><label class="f">Display name</label><input name="display_name" required></div>
    <div><label class="f">Role</label><select name="role">${['system_admin', 'facility_manager', 'intake_operator', 'inventory_operator', 'billing_operator', 'dispatch_operator', 'ca_monitor', 'auditor'].map((r) => `<option>${r}</option>`).join('')}</select></div>
    <div><label class="f">Temporary password (min 10)</label><input name="password" required minlength="10"></div>
  </div><div class="actions"><button class="btn">Create</button></div></form>`);
  o.querySelector('#uf').onsubmit = async (ev) => {
    ev.preventDefault();
    try { await post('/users', formVals(ev.target)); toast('User created — must change password on first login', 'ok'); o.remove(); render(); }
    catch (e) { errToast(e); }
  };
}

/* ---------------- router & event wiring ---------------- */
const VIEWS = { dashboard: viewDashboard, contracts: viewContracts, intake: viewIntake, quality: viewQuality, lots: viewLots, chambers: viewChambers, orders: viewOrders, dispatch: viewDispatch, billing: viewBilling, telemetry: viewTelemetry, reports: viewReports, admin: viewAdmin };

async function render() {
  state.route = location.hash.slice(2) || 'dashboard';
  const app = $('#app');
  if (!state.token || !state.me) {
    app.innerHTML = loginView();
    $('#lf').onsubmit = doLogin;
    return;
  }
  let html;
  try { html = await VIEWS[state.route](); }
  catch (e) { html = `<div class="panel"><div class="body">Failed to load: ${esc((e.data && JSON.stringify(e.data.detail)) || e.message)}</div></div>`; }
  app.innerHTML = shell(html);
  wire();
  refreshSyncbar();
}

function mfaModal() {
  const o = modal('Security — two-factor authentication (TOTP)', `<div id="mfabody"><div class="note">loading…</div></div>`);
  const body = o.querySelector('#mfabody');
  (async () => {
    const st = await get('/auth/mfa/status');
    if (!st.totp_enabled) {
      body.innerHTML = `<div class="note">MFA is <b>off</b>. Enroll an authenticator app (Aegis, Google Authenticator, 1Password…) — required for managers/admins before pilot.</div>
        <div class="actions"><button class="btn small" id="enroll">Start enrollment</button></div><div id="ez"></div>`;
      body.querySelector('#enroll').onclick = async () => {
        const r = await post('/auth/mfa/enroll-start');
        body.querySelector('#ez').innerHTML = `<div class="box" style="border:1px solid var(--line);padding:10px;border-radius:8px;margin-top:10px;word-break:break-all">
          <div class="kv-line"><b>Secret</b><span style="font-family:monospace">${esc(r.data.secret)}</span></div>
          <div class="kv-line"><b>URI</b><span style="font-family:monospace;font-size:11px">${esc(r.data.otpauth_uri)}</span></div>
          <div class="mini">scan/type the URI in your authenticator, then enter its 6-digit code:</div>
          <form id="evf" style="display:flex;gap:8px;margin-top:8px"><input id="evcode" maxlength="6" placeholder="123456" required style="max-width:120px"><button class="btn small">Enable MFA</button></form></div>`;
        body.querySelector('#evf').onsubmit = async (ev) => {
          ev.preventDefault();
          try { await post('/auth/mfa/enroll-verify', { code: body.querySelector('#evcode').value }); toast('MFA enabled', 'ok'); o.remove(); }
          catch (e) { errToast(e); }
        };
      };
    } else {
      body.innerHTML = `<div class="note">MFA is <b style="color:var(--ok)">on</b> for this account.</div>
        <form id="disf" style="margin-top:8px"><label class="f">Password</label><input type="password" name="password" required>
        <label class="f">Authenticator code</label><input name="code" maxlength="6" required>
        <div class="actions"><button class="btn small danger">Disable MFA</button></div></form>`;
      body.querySelector('#disf').onsubmit = async (ev) => {
        ev.preventDefault();
        try { await post('/auth/mfa/disable', formVals(ev.target)); toast('MFA disabled', 'ok'); o.remove(); }
        catch (e) { errToast(e); }
      };
    }
  })();
}

function wire() {
  $('#lo')?.addEventListener('click', (e) => { e.preventDefault(); doLogout(); });
  $('#sec')?.addEventListener('click', (e) => { e.preventDefault(); mfaModal(); });
  // telemetry page
  $('#tcham')?.addEventListener('change', (e) => { state.tcham = e.target.value; render(); });
  $('#tmet')?.addEventListener('change', (e) => { state.tmet = e.target.value; render(); });
  $$('[data-ack]').forEach((b) => b.addEventListener('click', async () => {
    const note = prompt('Acknowledgement note (what did you check/do)?');
    if (note === null) return;
    try { await post(`/telemetry/alerts/${b.dataset.ack}/ack`, { note }); toast('Alert acknowledged', 'ok'); render(); }
    catch (e) { errToast(e); }
  }));
  $('#thf')?.addEventListener('submit', async (e) => {
    e.preventDefault();
    const v = formVals(e.target);
    try {
      await put('/telemetry/thresholds', { ...v, limit_value: Number(v.limit_value),
        hysteresis: Number(v.hysteresis || 0), min_duration_s: intOr(v.min_duration_s) || 0 });
      toast('Threshold saved (audited)', 'ok'); render();
    } catch (e2) { errToast(e2); }
  });
  $('#sef')?.addEventListener('submit', async (e) => {
    e.preventDefault();
    const v = formVals(e.target);
    try {
      await put('/telemetry/sensors', { ...v, expected_interval_s: intOr(v.expected_interval_s) || 60,
        calibrated_until: v.calibrated_until || undefined });
      toast('Sensor registered (audited)', 'ok'); render();
    } catch (e2) { errToast(e2); }
  });
  document.querySelectorAll('[data-print]').forEach((b) => b.onclick = () => openPrint(b.dataset.print));
  document.querySelectorAll('[data-verify]').forEach((b) => b.onclick = () => verifyModal(+b.dataset.verify));
  document.querySelectorAll('[data-correct]').forEach((b) => b.onclick = async () => {
    const reason = prompt('Correction reason (original receipt is preserved as superseded):');
    if (!reason || reason.length < 3) return;
    try { await runOp(`/intake/receipts/${b.dataset.correct}/correct`, {  reason, }); toast('Correction recorded', 'ok'); } catch (e) { errToast(e); }
  });
  $('#newrcpt')?.addEventListener('click', newReceiptModal);
  $('#isearch')?.addEventListener('click', render);
  $('#iq')?.addEventListener('keydown', (e) => { if (e.key === 'Enter') render(); });
  document.querySelectorAll('[data-lot]').forEach((b) => b.onclick = () => lotModal(+b.dataset.lot));
  document.querySelectorAll('[data-morelots]').forEach((b) => b.onclick = () => loadMoreLots(b));
  document.querySelectorAll('[data-ch]').forEach((el) => el.onclick = () => chamberModal(+el.dataset.ch));
  document.querySelectorAll('[data-approve]').forEach((b) => b.onclick = async () => {
    const note = prompt('Approval note:') || '';
    try { await runOp(`/adjustments/${b.dataset.approve}/approve`, { note }, 'Approved & applied to stock'); loadMasters(); } catch (e) { errToast(e); }
  });
  document.querySelectorAll('[data-reject]').forEach((b) => b.onclick = async () => {
    const note = prompt('Rejection reason:') || '';
    try { await runOp(`/adjustments/${b.dataset.reject}/reject`, {  note, }); toast('Rejected', 'ok'); } catch (e) { errToast(e); }
  });
  $('#newdsp')?.addEventListener('click', newDispatchModal);
  document.querySelectorAll('[data-fin]').forEach((b) => b.onclick = async () => {
    if (!confirm('Finalize dispatch? Stock leaves verified inventory now.')) return;
    try { await runOp(`/dispatch/${b.dataset.fin}/finalize`, {}, 'Dispatch finalized — stock deducted'); loadMasters(); } catch (e) { errToast(e); }
  });
  document.querySelectorAll('[data-void]').forEach((b) => b.onclick = async () => {
    const reason = prompt('Void reason (required, audited):');
    if (!reason || reason.length < 3) return;
    try { await runOp(`/dispatch/${b.dataset.void}/void`, { reason }, 'Voided — stock restored with audit trail'); loadMasters(); } catch (e) { errToast(e); }
  });
  $('#geninv')?.addEventListener('click', genInvoiceModal);
  $('#newtar')?.addEventListener('click', newTariffModal);
  $('#newtax')?.addEventListener('click', newTaxModal);
  $('#stmtbtn')?.addEventListener('click', () => { if (!can('billing.read')) return toast('No billing.read permission', 'err'); statementModal(); });
  document.querySelectorAll('[data-act]').forEach((b) => b.onclick = async () => {
    try { await post(`/tariffs/${b.dataset.act}/activate`); toast('Tariff activated (prior version superseded)', 'ok'); render(); } catch (e) { errToast(e); }
  });
  document.querySelectorAll('[data-finv]').forEach((b) => b.onclick = async () => {
    if (!confirm('Finalize invoice? It becomes a legal receivable and cannot be edited.')) return;
    try { await runOp(`/invoices/${b.dataset.finv}/finalize`, {}, (d) => `Invoice ${d.invoice_no} finalized`); } catch (e) { errToast(e); }
  });
  document.querySelectorAll('[data-vinv]').forEach((b) => b.onclick = async () => {
    const reason = prompt('Void reason (audited):');
    if (!reason || reason.length < 3) return;
    try { await runOp(`/invoices/${b.dataset.vinv}/void`, {  reason, }); toast('Invoice voided', 'ok'); } catch (e) { errToast(e); }
  });
  document.querySelectorAll('[data-credit]').forEach((b) => b.onclick = () => creditNoteModal(+b.dataset.credit));
  document.querySelectorAll('[data-cnap]').forEach((b) => b.onclick = async () => {
    const note = prompt('Approval note:') || 'approved';
    try { await runOp(`/credit-notes/${b.dataset.cnap}/approve`, {  note, }); toast('Credit note approved — outstanding reduced', 'ok'); }
    catch (e) { errToast(e); }
  });
  document.querySelectorAll('[data-cnrj]').forEach((b) => b.onclick = async () => {
    const note = prompt('Rejection note:') || 'rejected';
    try { await runOp(`/credit-notes/${b.dataset.cnrj}/reject`, {  note, }); toast('Credit note rejected', 'ok'); }
    catch (e) { errToast(e); }
  });
  document.querySelectorAll('[data-cnvd]').forEach((b) => b.onclick = async () => {
    const reason = prompt('Void reason:') || 'voided';
    try { await runOp(`/credit-notes/${b.dataset.cnvd}/void`, {  reason, }); toast('Credit note voided', 'ok'); }
    catch (e) { errToast(e); }
  });
  $('#srf')?.addEventListener('submit', runStockReport);
  $('#orf')?.addEventListener('submit', runOpsReport);
  $('#orf select[name="kind"]')?.addEventListener('change', (ev) => {
    const k = ev.target.value;
    $('#orf-to').style.display = (k === 'daily' || k === 'stockby') ? 'none' : '';
    $('#orf-ch').style.display = k === 'occupancy' ? '' : 'none';
    $('#orf-by').style.display = k === 'stockby' ? '' : 'none';
    $('#orf input[name="from_date"]').required = k !== 'stockby';
  });
  /* contracts & quality & orders wiring */
  $('#newctr')?.addEventListener('click', newContractModal);
  $('#newadv')?.addEventListener('click', () => newAdviceModal(null));
  document.querySelectorAll('[data-cact]').forEach((b) => b.onclick = async () => {
    const reason = prompt('Activation note (terms verified against…):');
    if (!reason || reason.length < 3) return;
    try { await runOp(`/contracts/${b.dataset.cact}/activate`, {  reason, }); toast('Contract active', 'ok'); }
    catch (e) { errToast(e); }
  });
  document.querySelectorAll('[data-cclose]').forEach((b) => b.onclick = async () => {
    const reason = prompt('Close reason (required):');
    if (!reason || reason.length < 3) return;
    const status = prompt('Close as: completed / cancelled / expired', 'completed');
    if (!status || !['completed', 'cancelled', 'expired'].includes(status)) return;
    try { await runOp(`/contracts/${b.dataset.cclose}/close/${status}`, {  reason, }); toast(`Contract ${status}`, 'ok'); }
    catch (e) { errToast(e); }
  });
  document.querySelectorAll('[data-cview]').forEach((b) => b.onclick = () => contractModal(+b.dataset.cview));
  document.querySelectorAll('[data-aview]').forEach((b) => b.onclick = () => adviceModal(+b.dataset.aview));
  document.querySelectorAll('[data-axc]').forEach((b) => b.onclick = async () => {
    const reason = prompt('Cancel reason:');
    if (!reason || reason.length < 3) return;
    try { await runOp(`/inward/advices/${b.dataset.axc}/cancel`, {  reason, }); toast('Advice cancelled', 'ok'); }
    catch (e) { errToast(e); }
  });
  document.querySelectorAll('[data-aclc]').forEach((b) => b.onclick = async () => {
    const reason = prompt('Close reason:');
    if (!reason || reason.length < 3) return;
    try { await runOp(`/inward/advices/${b.dataset.aclc}/close`, {  reason, }); toast('Advice closed', 'ok'); }
    catch (e) { errToast(e); }
  });
  $('#newqinsp')?.addEventListener('click', () => newInspectionModal(null));
  $('#qimport')?.addEventListener('click', gradingImportModal);
  $('#newqchk')?.addEventListener('click', newCheckModal);
  document.querySelectorAll('[data-qapply]').forEach((b) => b.onclick = () => applyInspectionModal(+b.dataset.qapply));
  document.querySelectorAll('[data-qcancel]').forEach((b) => b.onclick = async () => {
    const reason = prompt('Cancel reason:');
    if (!reason || reason.length < 3) return;
    try { await runOp(`/quality/inspections/${b.dataset.qcancel}/cancel`, {  reason, }); toast('Inspection cancelled', 'ok'); }
    catch (e) { errToast(e); }
  });
  document.querySelectorAll('[data-qview]').forEach((b) => b.onclick = () => inspectionModal(+b.dataset.qview));
  $('#neword')?.addEventListener('click', newOrderModal);
  document.querySelectorAll('[data-oconf]').forEach((b) => b.onclick = async () => {
    try { await runOp(`/orders/${b.dataset.oconf}/confirm`, {  reason: 'verified', }); toast('Order confirmed — availability validated', 'ok'); }
    catch (e) { errToast(e); }
  });
  document.querySelectorAll('[data-ocxl]').forEach((b) => b.onclick = async () => {
    const reason = prompt('Cancel reason:');
    if (!reason || reason.length < 3) return;
    try { await runOp(`/orders/${b.dataset.ocxl}/cancel`, {  reason, }); toast('Order cancelled', 'ok'); }
    catch (e) { errToast(e); }
  });
  document.querySelectorAll('[data-oview]').forEach((b) => b.onclick = () => orderModal(+b.dataset.oview));
  document.querySelectorAll('[data-dconf]').forEach((b) => b.onclick = () => deliveryConfirmModal(+b.dataset.dconf));
  document.querySelectorAll('[data-dcxl]').forEach((b) => b.onclick = async () => {
    const reason = prompt('Cancel reason:');
    if (!reason || reason.length < 3) return;
    try { await runOp(`/deliveries/${b.dataset.dcxl}/cancel`, {  reason, }); toast('Delivery cancelled', 'ok'); }
    catch (e) { errToast(e); }
  });
  $('#newuser')?.addEventListener('click', newUserModal);
  $('#dobak')?.addEventListener('click', async () => {
    try { const r = await post('/admin/backups'); toast(`Backup ${r.data.filename} (sha256 verified)`, 'ok'); render(); } catch (e) { errToast(e); }
  });
}

window.addEventListener('hashchange', render);

/* ---------------- boot ---------------- */
(async function boot() {
  if (state.token) {
    try {
      const me = await get('/auth/me');
      state.me = me;
      localStorage.setItem('cs_me', JSON.stringify(me));
      if ('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js');
      await loadMasters();
      syncOutbox();
    } catch (e) {
      if (e instanceof NetError && state.me) { /* offline boot with cached identity */ }
      else { /* handled by 401 redirect in api() */ }
    }
  }
  render();
})();

/* ================= CONTRACTS & PRE-INWARD (A, B) ================= */
async function viewContracts() {
  const [contracts, advices] = await Promise.all([
    get('/contracts'), can('inward.read') ? get('/inward/advices') : Promise.resolve([])]);
  const rows = contracts.map((c) => {
    const lineSummary = c.lines.map((l) => `${nf.format(l.expected_kg)} kg`).join(' + ');
    return `<tr>
    <td><b>${esc(c.contract_no)}</b></td><td>${esc(partyName(c.owner_id))}</td>
    <td>${esc(c.title || '—')}</td>
    <td class="mini">${c.valid_from} → ${c.valid_to}</td>
    <td class="num">${lineSummary || '—'}</td>
    <td>${esc(c.billing_cycle)}</td>
    <td><span class="badge ${c.status}">${c.status}</span></td>
    <td>${c.status === 'draft' && can('contracts.approve') ? `<button class="btn small" data-cact="${c.id}">Activate</button>` : ''}
      ${(c.status === 'active' || c.status === 'draft') && can('contracts.approve') ? `<button class="btn small ghost danger" data-cclose="${c.id}">Close</button>` : ''}
      <button class="btn small ghost" data-cview="${c.id}">Open</button></td></tr>`;
  }).join('');
  const advRows = advices.map((a) => `<tr>
    <td><b>${esc(a.advice_no)}</b></td><td>${esc(partyName(a.owner_id))}</td>
    <td>${a.expected_arrival_date}</td><td>${esc(a.vehicle_no || '—')}</td>
    <td class="num">${kg(a.expected_kg)}</td><td class="num">${a.expected_units}</td>
    <td><span class="badge ${a.status}">${a.status}</span></td>
    <td>${can('inward.write') && a.status === 'open' ? `<button class="btn small ghost danger" data-axc="${a.id}">Cancel</button>` : ''}
      ${can('inward.write') && (a.status === 'open' || a.status === 'arrived') ? `<button class="btn small ghost" data-aclc="${a.id}">Close</button>` : ''}
      <button class="btn small ghost" data-aview="${a.id}">Reconcile</button></td></tr>`).join('');
  return `
  <div class="toolbar"><span style="flex:1"></span>
    ${can('contracts.write') ? '<button class="btn" id="newctr">+ New contract</button>' : ''}
    ${can('inward.write') ? '<button class="btn ghost" id="newadv">+ Pre-inward advice</button>' : ''}</div>
  <div class="panel"><h2>Contracts &amp; crate bookings</h2><div class="body">
    <table class="tbl"><thead><tr><th>No</th><th>Owner</th><th>Title</th><th>Validity</th>
      <th class="num">Expected</th><th>Billing</th><th>Status</th><th></th></tr></thead>
    <tbody>${rows || '<tr><td colspan="8" class="note">no contracts yet</td></tr>'}</tbody></table></div></div>
  <div class="panel"><h2>Pre-inward advices (expected arrivals)</h2><div class="body">
    <table class="tbl"><thead><tr><th>No</th><th>Owner</th><th>Expected</th><th>Vehicle</th>
      <th class="num">Expected kg</th><th class="num">Units</th><th>Status</th><th></th></tr></thead>
    <tbody>${advRows || '<tr><td colspan="8" class="note">no advices yet</td></tr>'}</tbody></table></div></div>`;
}

function newContractModal() {
  const m = M();
  const owners = m.parties.filter((p) => p.roles.includes('owner'));
  const o = modal('New storage contract (draft — second user activates)', `<form id="cf"><div class="frm-grid">
    <div><label class="f">Season</label>${sel('season_id', m.seasons, 'id', 'name')}</div>
    <div><label class="f">Owner *</label><select name="owner_id">${opt(owners, 'id', 'name')}</select></div>
    <div><label class="f">Title</label><input name="title" placeholder="CA storage — crop 2026"></div>
    <div><label class="f">Valid from *</label><input name="valid_from" type="date" required></div>
    <div><label class="f">Valid to *</label><input name="valid_to" type="date" required></div>
    <div><label class="f">Agreed tariff (rate card)</label><select name="tariff_id" id="c-tariff"></select></div>
    <div><label class="f">Billing cycle</label><select name="billing_cycle"><option>monthly</option><option>seasonal</option><option>on_dispatch</option></select></div>
    <div><label class="f">Payment terms (days)</label><input name="payment_terms_days" type="number" value="30" min="0"></div>
    <div><label class="f">Notes</label><input name="notes"></div>
  </div><h3 style="font-size:13px;margin:14px 0 6px">Expected stock (lines)</h3><div id="clines"></div>
  <button type="button" class="btn ghost small" id="addcl">+ add line</button>
  <div class="note">Expected quantities drive gate reconciliation. The tariff references the agreed rates — billing snapshots them onto invoices at generation time.</div>
  <div class="actions"><button class="btn">Save draft</button></div></form>`);
  get('/tariffs').then((ts) => {
    const s = o.querySelector('#c-tariff');
    s.innerHTML = '<option value="">— attach later —</option>' +
      ts.filter((t) => t.status === 'active').map((t) =>
        `<option value="${t.id}">${esc(t.code)} v${t.version}${t.owner_id ? ' · ' + esc(partyName(t.owner_id)) : ''}</option>`).join('');
  }).catch(() => {});
  const linesEl = o.querySelector('#clines');
  const addLine = () => {
    const d = document.createElement('div');
    d.className = 'frm-grid';
    d.innerHTML = `
      <div><label class="f">Variety</label>${sel('', m.varieties, 'id', 'name').replace('name=""', 'class="cl-v"')}</div>
      <div><label class="f">Grade (optional)</label><select class="cl-g"><option value="">any</option>${opt(m.grades, 'id', 'name')}</select></div>
      <div><label class="f">Packaging</label>${sel('', m.packages, 'id', 'name').replace('name=""', 'class="cl-p"')}</div>
      <div><label class="f">Expected kg</label><input class="cl-kg" type="number" step="0.001" min="0" value="0"></div>
      <div><label class="f">Expected units</label><input class="cl-u" type="number" min="0" value="0"></div>`;
    linesEl.appendChild(d);
  };
  o.querySelector('#addcl').onclick = addLine; addLine();
  o.querySelector('#cf').onsubmit = async (ev) => {
    ev.preventDefault();
    const v = formVals(ev.target);
    const lines = [...linesEl.querySelectorAll('.frm-grid')].map((g) => ({
      variety_id: +g.querySelector('.cl-v').value,
      grade_id: intOr(g.querySelector('.cl-g').value) || null,
      package_id: +g.querySelector('.cl-p').value,
      expected_kg: +g.querySelector('.cl-kg').value || 0,
      expected_units: +g.querySelector('.cl-u').value || 0,
    }));
    try {
      const r = await submitOp('/contracts', {
        season_id: +v.season_id, owner_id: +v.owner_id, title: v.title || '',
        valid_from: v.valid_from, valid_to: v.valid_to,
        tariff_id: intOr(v.tariff_id) || null, billing_cycle: v.billing_cycle,
        payment_terms_days: +v.payment_terms_days || 30, notes: v.notes || '', lines,
      }, 'contract');
      if (r.queued) toast('Offline — contract queued', 'ok');
      else toast(`Contract ${r.data.contract_no} saved as draft`, 'ok');
      o.remove(); render();
    } catch (e) { errToast(e); }
  };
}

async function contractModal(cid) {
  const c = await get(`/contracts/${cid}`);
  const rec = c.reconciliation;
  const advRows = rec.advices.map((a) => `<tr><td>${esc(a.advice_no)}</td><td>${a.status}</td>
    <td class="num">${kg(a.expected_kg)}</td><td class="num">${kg(a.received_kg)}</td>
    <td class="num">${a.variance_kg}</td></tr>`).join('');
  let bp = '';
  if (can('billing.read')) {
    try { bp = await get(`/contracts/${cid}/billing-period`);
      bp = `<div class="kv-line"><b>Suggested next billing period</b><span>${bp.from} → ${bp.to} (${bp.billing_cycle})</span></div>`;
    } catch (e) { bp = ''; }
  }
  const o = modal(`Contract ${c.contract_no}`, `
    <div class="kv-line"><b>Owner</b><span>${esc(partyName(c.owner_id))}</span></div>
    <div class="kv-line"><b>Status</b><span><span class="badge ${c.status}">${c.status}</span></span></div>
    <div class="kv-line"><b>Validity</b><span>${c.valid_from} → ${c.valid_to}</span></div>
    <div class="kv-line"><b>Billing / terms</b><span>${esc(c.billing_cycle)} · ${c.payment_terms_days} days</span></div>
    ${bp}
    <h3 style="font-size:13px;margin:14px 0 6px">Expected vs received</h3>
    <div class="cards" style="grid-template-columns:repeat(3,1fr)">
      <div class="card"><div class="k">Expected</div><div class="v">${kg(rec.expected_kg)}</div></div>
      <div class="card ok"><div class="k">Received</div><div class="v">${kg(rec.received_kg)}</div></div>
      <div class="card ${Math.abs(rec.received_kg - rec.expected_kg) > 0 ? 'warn' : 'ok'}">
        <div class="k">Fulfilment</div><div class="v">${rec.fulfilment_pct ?? '—'}%</div></div></div>
    <table class="tbl"><thead><tr><th>Advice</th><th>Status</th><th class="num">Expected</th>
      <th class="num">Received</th><th class="num">Variance</th></tr></thead>
    <tbody>${advRows || '<tr><td colspan="5" class="note">no advices yet</td></tr>'}</tbody></table>
    ${can('inward.write') ? `<div class="actions"><button class="btn ghost small" id="c-adv">+ advice for this contract</button></div>` : ''}`);
  const b = o.querySelector('#c-adv');
  if (b) b.onclick = () => newAdviceModal(cid);
}

function newAdviceModal(contractId) {
  const m = M();
  const owners = m.parties.filter((p) => p.roles.includes('owner'));
  const o = modal('Pre-inward advice (expected arrival)', `<form id="af"><div class="frm-grid">
    <div><label class="f">Contract</label><select name="contract_id" id="a-contract"><option value="">— ad-hoc (no contract) —</option></select></div>
    <div><label class="f">Season</label>${sel('season_id', m.seasons, 'id', 'name')}</div>
    <div><label class="f">Owner *</label><select name="owner_id" id="a-owner">${opt(owners, 'id', 'name')}</select></div>
    <div><label class="f">Expected arrival *</label><input name="expected_arrival_date" type="date" required></div>
    <div><label class="f">Vehicle no</label><input name="vehicle_no"></div>
    <div><label class="f">Expected kg *</label><input name="expected_kg" type="number" step="0.001" min="0" required></div>
    <div><label class="f">Expected units</label><input name="expected_units" type="number" min="0" value="0"></div>
    <div><label class="f">Packaging</label>${sel('package_id', m.packages, 'id', 'name')}</div>
    <div><label class="f">Variety</label>${sel('variety_id', m.varieties, 'id', 'name')}</div>
    <div><label class="f">Grade (optional)</label><select name="grade_id"><option value="">—</option>${opt(m.grades, 'id', 'name')}</select></div>
    <div><label class="f">Harvest year</label><input name="harvest_year" type="number" value="2026"></div>
  </div><div class="note">Advice is linked at the gate: receipts reference it and reconciliation compares expected vs verified received.</div>
  <div class="actions"><button class="btn">Save advice</button></div></form>`);
  get('/contracts?status=active').then((cs) => {
    const s = o.querySelector('#a-contract');
    s.innerHTML += cs.map((c) => `<option value="${c.id}" ${c.id === contractId ? 'selected' : ''}>${esc(c.contract_no)} · ${esc(partyName(c.owner_id))}</option>`).join('');
    const first = cs.find((c) => c.id === contractId);
    if (first) o.querySelector('#a-owner').value = first.owner_id;
    s.onchange = () => { const sel2 = cs.find((c) => c.id === +s.value); if (sel2) o.querySelector('#a-owner').value = sel2.owner_id; };
  }).catch(() => {});
  o.querySelector('#af').onsubmit = async (ev) => {
    ev.preventDefault();
    const v = formVals(ev.target);
    try {
      const r = await submitOp('/inward/advices', {
        contract_id: intOr(v.contract_id) || null, season_id: +v.season_id,
        owner_id: +v.owner_id, expected_arrival_date: v.expected_arrival_date,
        vehicle_no: v.vehicle_no || '', expected_kg: +v.expected_kg,
        expected_units: +v.expected_units || 0, package_id: +v.package_id,
        variety_id: +v.variety_id, grade_id: intOr(v.grade_id) || null,
        harvest_year: +v.harvest_year,
      }, 'advice');
      if (r.queued) toast('Offline — advice queued', 'ok');
      else toast(`Advice ${r.data.advice_no} saved`, 'ok');
      o.remove(); render();
    } catch (e) { errToast(e); }
  };
}

async function adviceModal(aid) {
  const a = await get(`/inward/advices/${aid}`);
  const r = a.reconciliation;
  const rows = r.receipts.map((x) => `<tr><td>${esc(x.receipt_no)}</td><td>${x.status}</td>
    <td class="num">${kg(x.net_kg)}</td><td class="num">${x.units}</td><td>${esc(x.vehicle_no || '—')}</td></tr>`).join('');
  modal(`Advice ${a.advice_no} — expected vs received`, `
    <div class="cards" style="grid-template-columns:repeat(3,1fr)">
      <div class="card"><div class="k">Expected</div><div class="v">${kg(r.expected_kg)}</div><div class="s">${r.expected_units} units</div></div>
      <div class="card ok"><div class="k">Received</div><div class="v">${kg(r.received_kg)}</div><div class="s">${r.received_units} units · ${r.receipt_count} receipt(s)</div></div>
      <div class="card ${r.variance_kg < 0 ? 'bad' : 'ok'}"><div class="k">Variance</div>
        <div class="v">${r.variance_kg} kg</div><div class="s">${r.variance_pct === null ? 'no expectation' : r.variance_pct + '%'}</div></div></div>
    <table class="tbl" style="margin-top:12px"><thead><tr><th>Receipt</th><th>Status</th>
      <th class="num">Net</th><th class="num">Units</th><th>Vehicle</th></tr></thead>
    <tbody>${rows || '<tr><td colspan="5" class="note">no verified receipts yet</td></tr>'}</tbody></table>`);
}

/* ================= QUALITY (E) ================= */
async function viewQuality() {
  const [checks, inspections] = await Promise.all(
    [get('/quality/checks'), get('/quality/inspections')]);
  const chkRows = checks.map((c) => `<tr><td><b>${esc(c.code)}</b></td><td>${esc(c.name)}</td>
    <td>${esc(c.kind)}</td><td class="mini">${esc(c.description || '')}</td>
    <td><span class="badge ${c.active ? 'active' : 'void'}">${c.active ? 'active' : 'inactive'}</span></td></tr>`).join('');
  const rows = inspections.map((i) => `<tr>
    <td><b>${esc(i.inspection_no)}</b></td><td>${esc(i.lot_no || i.lot_id)}</td>
    <td>${i.inspected_at.slice(0, 16).replace('T', ' ')}</td>
    <td class="num">${kg(i.rejected_kg)}</td><td class="num">${kg(i.downgraded_kg)}</td>
    <td>${esc(i.verdict)}</td>
    <td><span class="badge ${i.status}">${i.status}</span></td>
    <td>${i.status === 'draft' && can('quality.write') ? `<button class="btn small" data-qapply="${i.id}">Apply</button>
      <button class="btn small ghost danger" data-qcancel="${i.id}">Cancel</button>` : ''}
      <button class="btn small ghost" data-qview="${i.id}">Open</button>
      <button class="btn small ghost" data-print="/quality/inspections/${i.id}/print">Print</button></td></tr>`).join('');
  return `
  <div class="toolbar"><span style="flex:1"></span>
    ${can('quality.write') ? '<button class="btn" id="newqinsp">+ New inspection</button>' : ''}
    ${can('quality.write') ? '<button class="btn ghost" id="qimport">Import grading file</button>' : ''}
    ${can('quality.approve') ? '<button class="btn ghost" id="newqchk">+ Check / defect</button>' : ''}</div>
  <div class="panel"><h2>Inspections</h2><div class="body">
    <table class="tbl"><thead><tr><th>No</th><th>Lot</th><th>When</th><th class="num">Rejected</th>
      <th class="num">Downgraded</th><th>Verdict</th><th>Status</th><th></th></tr></thead>
    <tbody>${rows || '<tr><td colspan="8" class="note">no inspections yet</td></tr>'}</tbody></table></div></div>
  <div class="panel"><h2>Inspection criteria (configurable)</h2><div class="body">
    <table class="tbl"><thead><tr><th>Code</th><th>Name</th><th>Kind</th><th>Description</th><th></th></tr></thead>
    <tbody>${chkRows || '<tr><td colspan="5" class="note">no criteria defined</td></tr>'}</tbody></table></div></div>`;
}

function newCheckModal() {
  const o = modal('New inspection criterion', `<form id="ckf" class="frm-grid">
    <div><label class="f">Code</label><input name="code" required placeholder="COLR"></div>
    <div><label class="f">Name</label><input name="name" required></div>
    <div><label class="f">Kind</label><select name="kind"><option>check</option><option>defect</option></select></div>
    <div><label class="f">Description</label><input name="description"></div>
    <div class="actions"><button class="btn">Save</button></div></form>`);
  o.querySelector('#ckf').onsubmit = async (ev) => {
    ev.preventDefault();
    try { await runOp('/quality/checks', formVals(ev.target), 'Criterion saved'); o.remove(); }
    catch (e) { errToast(e); }
  };
}

async function newInspectionModal(lotId) {
  const m = M();
  const [checks, lots] = await Promise.all([get('/quality/checks'), get('/lots?limit=200')]);
  const activeChecks = checks.filter((c) => c.active);
  const lotSel = lotId
    ? `<input type="hidden" name="lot_id" value="${lotId}"><div class="kv-line"><b>Lot</b><span>${esc((lots.find((l) => l.id === lotId) || {}).lot_no || lotId)}</span></div>`
    : `<div><label class="f">Lot</label><select name="lot_id">${lots.map((l) => `<option value="${l.id}">${esc(l.lot_no)} · ${esc(l.variety)}/${esc(l.grade)}</option>`).join('')}</select></div>`;
  const o = modal('New quality inspection (draft — apply moves stock)', `<form id="qf"><div class="frm-grid">
    ${lotSel}
    <div><label class="f">Verdict</label><select name="verdict"><option>pass</option><option>conditional</option><option>fail</option></select></div>
    <div><label class="f">Sampled units</label><input name="sampled_units" type="number" min="0" value="0"></div>
    <div><label class="f">Accepted kg</label><input name="accepted_kg" type="number" step="0.001" min="0" value="0"></div>
    <div><label class="f">Rejected kg</label><input name="rejected_kg" type="number" step="0.001" min="0" value="0"></div>
    <div><label class="f">Downgraded kg</label><input name="downgraded_kg" type="number" step="0.001" min="0" value="0"></div>
    <div><label class="f">Target grade (downgrade)</label><select name="target_grade_id"><option value="">—</option>${opt(m.grades, 'id', 'name')}</select></div>
    <div><label class="f">Notes</label><input name="notes"></div>
  </div>
  <h3 style="font-size:13px;margin:14px 0 6px">Check results</h3><div id="qitems"></div>
  <button type="button" class="btn ghost small" id="addqi">+ add check</button>
  <div class="note">Applying a rejected quantity removes it from the lot (audited movement); downgraded quantity becomes a child lot of the target grade. Quantities reconcile against the lot anchor.</div>
  <div class="actions"><button class="btn">Save draft</button></div></form>`);
  const itemsEl = o.querySelector('#qitems');
  const addItem = () => {
    const d = document.createElement('div');
    d.className = 'frm-grid';
    d.innerHTML = `
      <div><label class="f">Check</label><select class="qi-check">${activeChecks.map((c) => `<option value="${c.id}">${esc(c.code)} · ${esc(c.name)} (${c.kind})</option>`).join('')}</select></div>
      <div><label class="f">Result</label><select class="qi-result"><option>pass</option><option>fail</option><option>na</option><option>defect</option></select></div>
      <div><label class="f">Value</label><input class="qi-value"></div>
      <div><label class="f">Notes</label><input class="qi-notes"></div>`;
    itemsEl.appendChild(d);
  };
  o.querySelector('#addqi').onclick = addItem; addItem();
  o.querySelector('#qf').onsubmit = async (ev) => {
    ev.preventDefault();
    const v = formVals(ev.target);
    const items = [...itemsEl.querySelectorAll('.frm-grid')].map((g) => ({
      check_id: +g.querySelector('.qi-check').value,
      result: g.querySelector('.qi-result').value,
      value: g.querySelector('.qi-value').value || '',
      notes: g.querySelector('.qi-notes').value || '',
    }));
    try {
      const d = await runOp('/quality/inspections', {
        lot_id: +v.lot_id, sampled_units: +v.sampled_units || 0,
        accepted_kg: +v.accepted_kg || 0, rejected_kg: +v.rejected_kg || 0,
        downgraded_kg: +v.downgraded_kg || 0,
        target_grade_id: intOr(v.target_grade_id) || null,
        verdict: v.verdict, notes: v.notes || '', items,
      }, (d) => `Inspection ${d.inspection_no} saved as draft`);
      if (d) o.remove();
    } catch (e) { errToast(e); }
  };
}

async function applyInspectionModal(iid) {
  const i = await get(`/quality/inspections/${iid}`);
  const m = M();
  const o = modal(`Apply inspection ${i.inspection_no}`, `<form id="qaf" class="frm-grid">
    <div><label class="f">Where does the quantity sit?</label>
      <select name="chamber_id" id="qa-ch"><option value="">un-allocated stock of the lot</option>
      ${m.chambers.filter((c) => c.status === 'active').map((c) => `<option value="${c.id}">${esc(c.code)}</option>`).join('')}</select></div>
    <div><label class="f">Packaging (required if chamber)</label><select name="package_id">${opt(m.packages, 'id', 'name')}</select></div>
    <div><label class="f">Reason</label><input name="reason" required placeholder="what was found"></div>
    <div class="actions"><button class="btn">Apply to stock</button></div></form>
    <div class="note">Rejected ${kg(i.rejected_kg)} · downgraded ${kg(i.downgraded_kg)}. This posts audited movements and cannot be undone (void requires a new correction).</div>`);
  o.querySelector('#qaf').onsubmit = async (ev) => {
    ev.preventDefault();
    const v = formVals(ev.target);
    try {
      const d = await runOp(`/quality/inspections/${iid}/apply`, {
        chamber_id: intOr(v.chamber_id) || null, package_id: +v.package_id,
        reason: v.reason,
      }, (d) => `Applied — downgraded to ${d.apply_result.downgrade_lot_no || 'n/a'}`);
      if (d) { o.remove(); loadMasters(); }
    } catch (e) { errToast(e); }
  };
}

async function inspectionModal(iid) {
  const i = await get(`/quality/inspections/${iid}`);
  const items = i.items.map((it) => `<tr><td>${esc(it.check_code || it.check_id)}</td>
    <td>${esc(it.result)}</td><td>${esc(it.value)}</td><td>${esc(it.notes)}</td></tr>`).join('');
  const photos = i.photos.map((p) => `<tr><td>${esc(p.filename)}</td><td>${p.size_bytes} B</td><td>${esc(p.note)}</td></tr>`).join('');
  const o = modal(`Inspection ${i.inspection_no}`, `
    <div class="kv-line"><b>Lot</b><span>${esc(i.lot_no)}</span></div>
    <div class="kv-line"><b>Status / verdict</b><span><span class="badge ${i.status}">${i.status}</span> · ${esc(i.verdict)}</span></div>
    <div class="kv-line"><b>Quantities</b><span>accepted ${kg(i.accepted_kg)} · rejected ${kg(i.rejected_kg)} · downgraded ${kg(i.downgraded_kg)}</span></div>
    <div class="kv-line"><b>Applied</b><span>${i.applied_at || '—'}</span></div>
    <h3 style="font-size:13px;margin:14px 0 6px">Check results</h3>
    <table class="tbl"><tbody>${items || '<tr><td class="note">—</td></tr>'}</tbody></table>
    <h3 style="font-size:13px;margin:14px 0 6px">Photos</h3>
    <table class="tbl"><tbody>${photos || '<tr><td class="note">none</td></tr>'}</tbody></table>
    ${i.status !== 'cancelled' && can('quality.write') ? `<div class="actions">
      <input type="file" id="qphoto" accept="image/png,image/jpeg" style="max-width:220px">
      <button class="btn ghost small" id="qup">Attach photo</button></div>` : ''}`);
  const f = o.querySelector('#qphoto');
  if (f) o.querySelector('#qup').onclick = async () => {
    if (!f.files.length) return toast('Choose an image first', 'err');
    const buf = await f.files[0].arrayBuffer();
    const b64 = btoa(String.fromCharCode(...new Uint8Array(buf)));
    try {
      await post(`/quality/inspections/${iid}/photos`, { data_b64: b64, note: f.files[0].name });
      toast('Photo attached (sha256 recorded)', 'ok'); o.remove(); inspectionModal(iid);
    } catch (e) { errToast(e); }
  };
}

function gradingImportModal() {
  const o = modal('Grading file import (validated, all-or-nothing)', `<div class="note">
    Paste CSV rows: <code>lot_no,rejected_kg,downgraded_kg,target_grade_code,chamber_id,package_id,verdict,notes</code>
    — a dry run validates every row first; nothing moves until you apply.</div>
    <textarea id="qcsv" rows="6" style="width:100%;font-family:monospace;margin-top:8px" placeholder="LOT-000001,100,0,,,,pass,culls"></textarea>
    <div class="actions"><button class="btn ghost small" id="qdry">Validate (dry run)</button>
    <button class="btn small" id="qapply">Apply import</button></div><div id="qout"></div>`);
  const parse = () => $('#qcsv').value.trim().split('\n').filter(Boolean).map((line) => {
    const [lot_no, rejected_kg, downgraded_kg, target_grade_code, chamber_id, package_id, verdict, notes] = line.split(',');
    return { lot_no: (lot_no || '').trim(), rejected_kg: +rejected_kg || 0,
      downgraded_kg: +downgraded_kg || 0, target_grade_code: (target_grade_code || '').trim() || null,
      chamber_id: intOr(chamber_id) || null, package_id: intOr(package_id) || null,
      verdict: (verdict || 'pass').trim(), notes: (notes || '').trim() };
  });
  const run = async (dry) => {
    try {
      if (dry) {
        const r = await post('/quality/grading-import', { rows: parse(), dry_run: true, operation_id: uuid() });
        $('#qout').innerHTML = `<pre style="font-size:11px;white-space:pre-wrap">${esc(JSON.stringify(r.data, null, 1))}</pre>`;
        return;
      }
      const d = await runOp('/quality/grading-import', { rows: parse(), dry_run: false },
        (d) => `Import applied: ${d.applied} row(s) — stock moved with audit trail`);
      if (d) { $('#qout').innerHTML = `<pre style="font-size:11px;white-space:pre-wrap">${esc(JSON.stringify(d, null, 1))}</pre>`; loadMasters(); }
    } catch (e) { errToast(e); }
  };
  o.querySelector('#qdry').onclick = () => run(true);
  o.querySelector('#qapply').onclick = () => {
    if (!confirm('Apply import? Quantities move NOW (audited, all-or-nothing).')) return;
    run(false);
  };
}

/* ================= ORDERS, PICKING, PACKING, DELIVERY (H, I) ================= */
async function viewOrders() {
  const [orders, deliveries] = await Promise.all([get('/orders'), get('/deliveries')]);
  const rows = orders.map((o) => `<tr>
    <td><b>${esc(o.order_no)}</b></td><td>${esc(partyName(o.owner_id))}</td>
    <td>${esc(o.destination || '—')}</td><td class="num">${kg((o.lines || []).reduce((s, l) => s + l.qty_kg, 0))}</td>
    <td><span class="badge ${o.status}">${o.status}</span></td>
    <td>${o.dispatch_id ? `DSP #${o.dispatch_id}` : '—'}</td>
    <td>${o.status === 'draft' && can('orders.write') ? `<button class="btn small" data-oconf="${o.id}">Confirm</button>
      <button class="btn small ghost danger" data-ocxl="${o.id}">Cancel</button>` : ''}
      ${(o.status === 'confirmed' || o.status === 'picking' || o.status === 'packed') && can('orders.write') ? `<button class="btn small" data-oview="${o.id}">Pick / pack</button>` : ''}
      <button class="btn small ghost" data-oview="${o.id}">Open</button>
      <button class="btn small ghost" data-print="/orders/${o.id}/print">Print</button></td></tr>`).join('');
  const dlRows = deliveries.map((d) => `<tr>
    <td><b>${esc(d.delivery_no)}</b></td><td>${esc(partyName(d.owner_id))}</td>
    <td class="num">${kg(d.planned_kg)}</td><td class="num">${kg(d.delivered_kg)}</td>
    <td class="num">${kg(d.shortage_kg)}</td><td class="num">${kg(d.damage_kg)}</td>
    <td><span class="badge ${d.status}">${d.status}</span></td>
    <td>${d.status === 'in_transit' && can('deliveries.write') ? `<button class="btn small" data-dconf="${d.id}">Confirm</button>
      <button class="btn small ghost danger" data-dcxl="${d.id}">Cancel</button>` : ''}
      <button class="btn small ghost" data-print="/deliveries/${d.id}/print">Print</button></td></tr>`).join('');
  return `
  <div class="toolbar"><span style="flex:1"></span>
    ${can('orders.write') ? '<button class="btn" id="neword">+ New outward order</button>' : ''}</div>
  <div class="panel"><h2>Outward orders (order → confirm → pick → pack → dispatch)</h2><div class="body">
    <table class="tbl"><thead><tr><th>No</th><th>Owner</th><th>Destination</th><th class="num">Ordered</th>
      <th>Status</th><th>Dispatch</th><th></th></tr></thead>
    <tbody>${rows || '<tr><td colspan="7" class="note">no orders yet</td></tr>'}</tbody></table></div></div>
  <div class="panel"><h2>Deliveries</h2><div class="body">
    <table class="tbl"><thead><tr><th>No</th><th>Owner</th><th class="num">Planned</th>
      <th class="num">Delivered</th><th class="num">Shortage</th><th class="num">Damage</th>
      <th>Status</th><th></th></tr></thead>
    <tbody>${dlRows || '<tr><td colspan="8" class="note">no deliveries yet</td></tr>'}</tbody></table></div></div>`;
}

function newOrderModal() {
  const m = M();
  const owners = m.parties.filter((p) => p.roles.includes('owner'));
  const o = modal('New outward order (draft — second user confirms)', `<form id="of"><div class="frm-grid">
    <div><label class="f">Season</label>${sel('season_id', m.seasons, 'id', 'name')}</div>
    <div><label class="f">Owner *</label><select name="owner_id" id="o-owner">${opt(owners, 'id', 'name')}</select></div>
    <div><label class="f">Destination</label><input name="destination"></div>
    <div><label class="f">Notes</label><input name="notes"></div>
  </div><h3 style="font-size:13px;margin:14px 0 6px">Requested quantities</h3><div id="olines"></div>
  <button type="button" class="btn ghost small" id="addol">+ add line</button>
  <div class="note">Confirming validates availability. Picking selects concrete lots/chambers; stock leaves only when the dispatch note is finalized.</div>
  <div class="actions"><button class="btn">Save draft</button></div></form>`);
  const linesEl = o.querySelector('#olines');
  const addLine = () => {
    const d = document.createElement('div');
    d.className = 'frm-grid';
    d.innerHTML = `
      <div><label class="f">Variety (any)</label><select class="ol-v"><option value="">any</option>${opt(m.varieties, 'id', 'name')}</select></div>
      <div><label class="f">Grade (any)</label><select class="ol-g"><option value="">any</option>${opt(m.grades, 'id', 'name')}</select></div>
      <div><label class="f">Qty kg</label><input class="ol-kg" type="number" step="0.001" min="0" value="0"></div>
      <div><label class="f">Units</label><input class="ol-u" type="number" min="0" value="0"></div>
      <div><label class="f">Note</label><input class="ol-n"></div>`;
    linesEl.appendChild(d);
  };
  o.querySelector('#addol').onclick = addLine; addLine();
  o.querySelector('#of').onsubmit = async (ev) => {
    ev.preventDefault();
    const v = formVals(ev.target);
    const lines = [...linesEl.querySelectorAll('.frm-grid')].map((g) => ({
      variety_id: intOr(g.querySelector('.ol-v').value) || null,
      grade_id: intOr(g.querySelector('.ol-g').value) || null,
      qty_kg: +g.querySelector('.ol-kg').value || 0,
      units: +g.querySelector('.ol-u').value || 0,
      note: g.querySelector('.ol-n').value || '',
    }));
    try {
      const r = await submitOp('/orders', {
        season_id: +v.season_id, owner_id: +v.owner_id, destination: v.destination || '',
        notes: v.notes || '', lines,
      }, 'order');
      if (r.queued) toast('Offline — order queued', 'ok');
      else toast(`Order ${r.data.order_no} saved as draft`, 'ok');
      o.remove(); render();
    } catch (e) { errToast(e); }
  };
}

async function orderModal(oid) {
  const o = await get(`/orders/${oid}`);
  const m = M();
  const pickRows = o.picks.map((p) => `<tr><td>${esc(p.lot_no)}</td><td>${esc(p.chamber)}</td>
    <td>${esc(p.package)}</td><td class="num">${kg(p.qty_kg)}</td><td class="num">${p.units}</td>
    <td>${(o.status === 'confirmed' || o.status === 'picking') && can('orders.write') ? `<button class="btn small ghost danger" data-rmpick="${p.id}">Remove</button>` : ''}</td></tr>`).join('');
  const packRows = o.packing.map((p) => `<tr><td>${esc(p.package)}</td><td class="num">${p.unit_count}</td>
    <td class="num">${kg(p.gross_kg)}</td><td class="num">${kg(p.tare_kg)}</td><td>${esc(p.notes)}</td></tr>`).join('');
  const lineOpts = o.lines.map((l) => `<option value="${l.id}">line ${l.line_no} · ${kg(l.qty_kg)} / ${l.units}u</option>`).join('');
  const canPick = (o.status === 'confirmed' || o.status === 'picking') && can('orders.write');
  const canPack = (o.status === 'picking' || o.status === 'packed') && can('orders.write');
  const canDispatch = o.status === 'packed' && can('dispatch.write');
  const lots = o.status ? await get(`/lots?owner_id=${o.owner_id}`) : [];
  const modal2 = modal(`Order ${o.order_no}`, `
    <div class="kv-line"><b>Owner</b><span>${esc(partyName(o.owner_id))}</span></div>
    <div class="kv-line"><b>Status</b><span><span class="badge ${o.status}">${o.status}</span></span></div>
    <div class="kv-line"><b>Requested</b><span>${kg(o.ordered_kg)} / ${o.lines.reduce((s, l) => s + l.units, 0)}u · picked ${kg(o.picked_kg)}</span></div>
    <div class="kv-line"><b>Destination</b><span>${esc(o.destination || '—')}</span></div>
    <h3 style="font-size:13px;margin:14px 0 6px">Order lines</h3>
    <table class="tbl"><thead><tr><th>#</th><th class="num">Qty</th><th class="num">Units</th><th>Note</th></tr></thead>
    <tbody>${o.lines.map((l) => `<tr><td>${l.line_no}</td><td class="num">${kg(l.qty_kg)}</td><td class="num">${l.units}</td><td>${esc(l.note)}</td></tr>`).join('')}</tbody></table>
    <h3 style="font-size:13px;margin:14px 0 6px">Picks</h3>
    <table class="tbl"><thead><tr><th>Lot</th><th>Chamber</th><th>Pkg</th><th class="num">Qty</th><th class="num">Units</th><th></th></tr></thead>
    <tbody>${pickRows || '<tr><td colspan="6" class="note">nothing picked yet</td></tr>'}</tbody></table>
    ${canPick ? `<h3 style="font-size:13px;margin:14px 0 6px">Add pick</h3><div class="frm-grid">
      <div><label class="f">Order line</label><select id="pk-line">${lineOpts}</select></div>
      <div><label class="f">Lot</label><select id="pk-lot">${lots.filter((l) => l.stored_kg > 0).map((l) => `<option value="${l.id}">${esc(l.lot_no)} · ${kg(l.stored_kg)}</option>`).join('')}</select></div>
      <div><label class="f">Chamber</label><select id="pk-ch"></select></div>
      <div><label class="f">Qty kg</label><input id="pk-kg" type="number" step="0.001"></div>
      <div><label class="f">Units</label><input id="pk-u" type="number" value="0" min="0"></div>
      <div style="display:flex;align-items:end"><button class="btn small" id="pk-add">Pick</button></div></div>` : ''}
    <h3 style="font-size:13px;margin:14px 0 6px">Packing</h3>
    <table class="tbl"><thead><tr><th>Package</th><th class="num">Units</th><th class="num">Gross</th><th class="num">Tare</th><th>Notes</th></tr></thead>
    <tbody>${packRows || '<tr><td colspan="5" class="note">not packed</td></tr>'}</tbody></table>
    ${canPack ? `<div class="frm-grid"><div><label class="f">Package</label>${sel('pk2-p', m.packages, 'id', 'name')}</div>
      <div><label class="f">Units packed</label><input id="pk2-u" type="number" min="0" value="0"></div>
      <div><label class="f">Gross kg</label><input id="pk2-g" type="number" step="0.001" value="0"></div>
      <div><label class="f">Tare kg</label><input id="pk2-t" type="number" step="0.001" value="0"></div>
      <div style="display:flex;align-items:end"><button class="btn small" id="pk2-add">Record packing</button></div></div>` : ''}
    ${canDispatch ? `<div class="actions"><button class="btn" id="od-dsp">Create dispatch note from picks</button></div>` : ''}`);
  const lotSel = modal2.querySelector('#pk-lot');
  const chSel = modal2.querySelector('#pk-ch');
  const fillCh = async () => {
    if (!lotSel) return;
    const ld = await get(`/lots/${lotSel.value}`);
    chSel.innerHTML = ld.balances.map((b) => `<option value="${b.chamber_id}|${b.package_id}">${esc(b.chamber)} · ${esc(b.package)} · ${kg(b.qty_kg)}</option>`).join('');
  };
  if (lotSel) { lotSel.onchange = fillCh; fillCh(); }
  modal2.querySelector('#pk-add')?.addEventListener('click', async () => {
    const [ch, pkg] = chSel.value.split('|');
    try {
      await runOp(`/orders/${oid}/picks`, { line_id: +modal2.querySelector('#pk-line').value,
        lot_id: +lotSel.value, chamber_id: +ch, package_id: +pkg,
        qty_kg: +modal2.querySelector('#pk-kg').value, units: +modal2.querySelector('#pk-u').value || 0,
      }, 'Pick recorded — availability re-validated', { render: false });
      modal2.remove(); orderModal(oid);
    } catch (e) { errToast(e); }
  });
  modal2.querySelector('#pk2-add')?.addEventListener('click', async () => {
    try {
      await runOp(`/orders/${oid}/packing`, { package_id: +modal2.querySelector('select[name="pk2-p"]').value,
        unit_count: +modal2.querySelector('#pk2-u').value || 0,
        gross_kg: +modal2.querySelector('#pk2-g').value || 0, tare_kg: +modal2.querySelector('#pk2-t').value || 0,
      }, 'Packing recorded', { render: false });
      modal2.remove(); orderModal(oid);
    } catch (e) { errToast(e); }
  });
  modal2.querySelector('#od-dsp')?.addEventListener('click', () => dispatchFromOrderModal(oid));
  modal2.querySelectorAll('[data-rmpick]').forEach((b) => b.onclick = async () => {
    try { await api('DELETE', `/orders/${oid}/picks/${b.dataset.rmpick}`); modal2.remove(); orderModal(oid); }
    catch (e) { errToast(e); }
  });
}

function dispatchFromOrderModal(oid) {
  const o = modal('Create dispatch note (authorization details)', `<form id="df" class="frm-grid">
    <div><label class="f">Authorized person *</label><input name="authorized_person_name" required></div>
    <div><label class="f">ID proof type</label><select name="id_proof_type"><option>aadhaar</option><option>pan</option><option>driving licence</option><option>voter id</option><option>other</option></select></div>
    <div><label class="f">ID proof no *</label><input name="id_proof_no" required></div>
    <div><label class="f">Vehicle no</label><input name="vehicle_no"></div>
    <div class="actions"><button class="btn">Create draft dispatch</button></div></form>
    <div class="note">Picks are re-validated against live balances. Stock leaves inventory only at finalize (maker/checker stays).</div>`);
  o.querySelector('#df').onsubmit = async (ev) => {
    ev.preventDefault();
    const v = formVals(ev.target);
    try {
      const d = await runOp(`/orders/${oid}/dispatch`, { ...v },
        (d) => `Dispatch ${d.dispatch.dispatch_no} created — finalize on the Dispatch page`);
      if (d) o.remove();
    } catch (e) { errToast(e); }
  };
}

function deliveryConfirmModal(did) {
  const o = modal('Confirm delivery (quantities must fully account)', `<form id="dcf" class="frm-grid">
    <div><label class="f">Outcome</label><select name="status"><option>delivered</option><option>shortage</option><option>damaged</option></select></div>
    <div><label class="f">Delivered kg</label><input name="delivered_kg" type="number" step="0.001" min="0" required></div>
    <div><label class="f">Delivered units</label><input name="delivered_units" type="number" min="0" value="0"></div>
    <div><label class="f">Shortage kg</label><input name="shortage_kg" type="number" step="0.001" min="0" value="0"></div>
    <div><label class="f">Damage kg</label><input name="damage_kg" type="number" step="0.001" min="0" value="0"></div>
    <div><label class="f">Received by</label><input name="received_by_name"></div>
    <div><label class="f">Received by phone</label><input name="received_by_phone"></div>
    <div><label class="f">Shortage/damage note</label><input name="shortage_note"></div>
    <div class="actions"><button class="btn">Confirm</button></div></form>
    <div class="note">delivered + shortage + damage must equal the planned dispatch quantity.</div>`);
  o.querySelector('#dcf').onsubmit = async (ev) => {
    ev.preventDefault();
    const v = formVals(ev.target);
    try {
      const d = await runOp(`/deliveries/${did}/confirm`, {
        status: v.status, delivered_kg: +v.delivered_kg, delivered_units: +v.delivered_units || 0,
        shortage_kg: +v.shortage_kg || 0, damage_kg: +v.damage_kg || 0,
        received_by_name: v.received_by_name || '', received_by_phone: v.received_by_phone || '',
        shortage_note: v.shortage_note || '',
      }, 'Delivery confirmed (audited)');
      if (d) o.remove();
    } catch (e) { errToast(e); }
  };
}

/* ================= BARCODE SCAN TOOL (D) ================= */
function scanModal() {
  const o = modal('Scan / resolve barcode', `
    <form id="scf" style="display:flex;gap:8px">
      <input id="sc-code" placeholder="CS1-… (scan or type)" style="flex:1;font-family:monospace" autofocus>
      <button class="btn small">Resolve</button></form>
    <div id="sc-out" style="margin-top:10px"><div class="note">Point the scanner at a label — device camera scanning is used when the browser supports it.</div></div>
    <button class="btn ghost small" id="sc-cam" style="margin-top:8px">📷 Use camera</button>
    <video id="sc-video" width="280" height="200" style="margin-top:8px;border-radius:8px;display:none"></video>`);
  const out = o.querySelector('#sc-out');
  const resolve = async (code) => {
    try {
      const r = await get(`/barcodes/resolve?code=${encodeURIComponent(code)}`);
      if (!r.ok) {
        out.innerHTML = `<div class="card bad" style="max-width:420px"><div class="k">Not resolvable</div><div class="v" style="font-size:14px">${esc(r.error || 'unknown')}</div></div>`;
        return;
      }
      const lot = r.lot || {};
      out.innerHTML = `<div class="card ok" style="max-width:480px"><div class="k">${esc(r.kind)}${r.unit_seq ? ' · unit ' + r.unit_seq : ''}</div>
        <div class="v" style="font-size:16px">${esc(lot.lot_no || (r.entity || {}).receipt_no || '')}</div>
        <div class="s">${esc(lot.owner || '')} · ${esc(lot.variety || '')} ${esc(lot.grade || '')} · stored ${kg(lot.received_kg || 0)}</div></div>
        ${(r.balances || []).map((b) => `<div class="kv-line"><b>${esc(b.chamber)} · ${esc(b.package)}</b><span>${kg(b.qty_kg)} · ${b.units}u</span></div>`).join('')}
        ${r.receipt ? `<div class="kv-line"><b>Receipt</b><span>${esc(r.receipt.receipt_no)}${r.receipt.contract_no ? ' · contract ' + esc(r.receipt.contract_no) : ''}</span></div>` : ''}`;
    } catch (e) { errToast(e); }
  };
  o.querySelector('#scf').onsubmit = (ev) => { ev.preventDefault(); resolve(o.querySelector('#sc-code').value.trim()); };
  const inp = o.querySelector('#sc-code');
  inp.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); resolve(inp.value.trim()); } });
  inp.focus();
  // camera scanning when BarcodeDetector is available (Android Chrome)
  o.querySelector('#sc-cam').onclick = async () => {
    if (!('BarcodeDetector' in window)) return toast('Camera scanning not supported on this device — use the scanner gun or type the code', 'err');
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: 'environment' } });
      const video = o.querySelector('#sc-video');
      video.style.display = 'block';
      video.srcObject = stream;
      await video.play();
      const det = new BarcodeDetector({ formats: ['qr_code'] });
      const tick = async () => {
        if (!o.isConnected) { stream.getTracks().forEach((t) => t.stop()); return; }
        try {
          const codes = await det.detect(video);
          if (codes.length) { inp.value = codes[0].rawValue; resolve(codes[0].rawValue); }
        } catch (e) { /* keep scanning */ }
        requestAnimationFrame(tick);
      };
      tick();
    } catch (e) { errToast(e); }
  };
}

/* lots modal additions: label issuance */
async function barcodeIssueModal(lotId, lotNo, storedUnits) {
  const m = M();
  const o = modal(`Labels for lot ${lotNo}`, `<form id="bif" class="frm-grid">
    <div><label class="f">Label kind</label><select name="kind" id="bi-kind">
      <option value="crate_range">Crate range (per-unit codes)</option>
      <option value="lot">Lot document label (1)</option></select></div>
    <div><label class="f">Unit from</label><input name="unit_from" type="number" min="1" value="1"></div>
    <div><label class="f">Unit to</label><input name="unit_to" type="number" min="1" value="${Math.min(storedUnits || 1, 500)}"></div>
    <div><label class="f">Notes</label><input name="notes"></div>
    <div class="actions"><button class="btn">Issue labels</button></div></form>
    <div class="note">Codes are CS1-C-${esc(lotNo)}-U&lt;seq&gt;. Overlapping ranges are refused — a physical crate can never have two active identities. Print sheets carry real QR codes.</div>
    <div id="bi-out"></div>`);
  const kindSel = o.querySelector('#bi-kind');
  kindSel.onchange = () => {
    const range = kindSel.value === 'crate_range';
    o.querySelector('[name="unit_from"]').disabled = !range;
    o.querySelector('[name="unit_to"]').disabled = !range;
  };
  o.querySelector('#bif').onsubmit = async (ev) => {
    ev.preventDefault();
    const v = formVals(ev.target);
    try {
      const d = await runOp('/barcodes/issues', {
        kind: v.kind, entity_id: lotId,
        unit_from: v.kind === 'crate_range' ? intOr(v.unit_from) : null,
        unit_to: v.kind === 'crate_range' ? intOr(v.unit_to) : null,
        notes: v.notes || '',
      }, (d) => `${d.code_count} label code(s) issued`, { render: false });
      if (d) { o.remove(); openPrint(`/barcodes/issues/${d.id}/print`); loadMasters(); }
    } catch (e) { errToast(e); }
  };
}
