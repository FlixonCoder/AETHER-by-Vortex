/* ============================================================
   VORTEX — Satellite Ops Dashboard (frontend)
   Talks to the SatOps AI backend: WebSocket /ws + REST /api/*
   ============================================================ */

const $ = id => document.getElementById(id);

/* ---------------- Settings (persisted) ---------------- */
const DEFAULTS = { accent: '#25d6f5', window: 60, sound: false, anim: true };
let settings = { ...DEFAULTS };
try { Object.assign(settings, JSON.parse(localStorage.getItem('vortex_settings') || '{}')); } catch (e) {}
function saveSettings() { localStorage.setItem('vortex_settings', JSON.stringify(settings)); }
function applyAccent() {
  document.documentElement.style.setProperty('--accent', settings.accent);
  document.querySelectorAll('.acc').forEach(b => b.classList.toggle('sel', b.dataset.c === settings.accent));
}

/* ---------------- Telemetry parameter metadata (mirrors backend config) ---------------- */
const PARAMS = {
  battery_voltage_v:  { label: 'Battery Voltage', unit: 'V',   sub: 'EPS',     lo: 24.5, hi: 32.5, min: 20, max: 34 },
  battery_soc_pct:    { label: 'Battery SoC', unit: '%',       sub: 'EPS',     lo: 30,   hi: 98,   min: 0,  max: 100 },
  solar_current_a:    { label: 'Solar Current', unit: 'A',     sub: 'EPS',     lo: null, hi: 8.2,  min: 0,  max: 9 },
  bus_power_w:        { label: 'Bus Power', unit: 'W',         sub: 'EPS',     lo: 15,   hi: 100,  min: 0,  max: 120 },
  temp_obc_c:         { label: 'OBC Temp', unit: '°C',         sub: 'OBC',     lo: -5,   hi: 50,   min: -15, max: 65 },
  temp_battery_c:     { label: 'Battery Temp', unit: '°C',     sub: 'EPS',     lo: 0,    hi: 40,   min: -10, max: 50 },
  temp_payload_c:     { label: 'Payload Temp', unit: '°C',     sub: 'THERMAL', lo: -10,  hi: 55,   min: -25, max: 75 },
  attitude_error_deg: { label: 'Attitude Error', unit: '°',    sub: 'ADCS',    lo: null, hi: 2,    min: 0,  max: 8 },
  reaction_wheel_rpm: { label: 'Reaction Wheel', unit: 'RPM',  sub: 'ADCS',    lo: -5500, hi: 5500, min: -6000, max: 6000 },
  downlink_snr_db:    { label: 'Downlink SNR', unit: 'dB',     sub: 'COMMS',   lo: 12,   hi: null, min: 0,  max: 45 },
  memory_usage_pct:   { label: 'Memory Usage', unit: '%',      sub: 'OBC',     lo: null, hi: 85,   min: 0,  max: 100 },
  cpu_usage_pct:      { label: 'CPU Usage', unit: '%',         sub: 'OBC',     lo: null, hi: 80,   min: 0,  max: 100 },
};
const SUBSYSTEMS = {
  EPS: { name: 'Power System', ico: '🔋' },
  THERMAL: { name: 'Thermal System', ico: '🌡️' },
  COMMS: { name: 'Comm System', ico: '📡' },
  ADCS: { name: 'Attitude Control', ico: '🧭' },
  OBC: { name: 'On-Board Computer', ico: '💻' },
  PAYLOAD: { name: 'Payload System', ico: '📷' },
  PROPULSION: { name: 'Propulsion', ico: '🚀' },
};
const CHART_PARAMS = ['battery_soc_pct','battery_voltage_v','temp_obc_c','downlink_snr_db','attitude_error_deg','memory_usage_pct'];

/* ---------------- State ---------------- */
let ws = null, wsRetry = 0, connected = false;
let latestTelemetry = {}, latestOrbital = {}, latestViolations = [];
let telemetryPaused = false;
const history = {};            // param -> [{t, v}]
Object.keys(PARAMS).forEach(p => history[p] = []);
const logs = [];               // {t, level, msg}
const packets = [];            // rows for raw packet table
const anomalies = new Map();   // id -> anomaly record
let pendingApprovals = new Map(); // id -> {plan, diagnosis, severity}
let runbooks = [];             // {filename, anomaly_id, generated_at}
let scenarios = {};            // key -> {subsystem, severity, description}
let runbooksIssued = 0;
let simHistory = [];
try { simHistory = JSON.parse(localStorage.getItem('vortex_simhist') || '[]'); } catch (e) {}
let notifications = [];
let unread = 0;
let pktSeq = 1000;

/* ---------------- Utils ---------------- */
const pad = n => String(n).padStart(2, '0');
function fmtTime(d) { d = d ? new Date(d) : new Date(); return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`; }
function fmtDate(d) {
  d = d || new Date();
  return `${pad(d.getDate())} ${['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'][d.getMonth()]} ${d.getFullYear()}`;
}
function esc(s) { return String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function download(name, text, mime) {
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([text], { type: mime || 'text/plain' }));
  a.download = name; a.click(); URL.revokeObjectURL(a.href);
}

let audioCtx = null;
function beep(freq, dur) {
  if (!settings.sound) return;
  try {
    audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
    const o = audioCtx.createOscillator(), g = audioCtx.createGain();
    o.frequency.value = freq || 660; o.type = 'sine';
    g.gain.setValueAtTime(0.08, audioCtx.currentTime);
    g.gain.exponentialRampToValueAtTime(0.0001, audioCtx.currentTime + (dur || 0.18));
    o.connect(g); g.connect(audioCtx.destination);
    o.start(); o.stop(audioCtx.currentTime + (dur || 0.18));
  } catch (e) {}
}

function toast(msg, kind) {
  const el = document.createElement('div');
  el.className = 'toast ' + (kind || '');
  el.textContent = msg;
  $('toasts').appendChild(el);
  setTimeout(() => { el.style.opacity = '0'; el.style.transition = 'opacity .4s'; setTimeout(() => el.remove(), 400); }, 4200);
}

function notify(msg, kind) {
  notifications.unshift({ t: new Date(), msg, kind: kind || 'info' });
  notifications = notifications.slice(0, 60);
  unread++;
  renderNotifs();
  toast(msg, kind === 'error' ? 'error' : kind === 'warn' ? 'warn' : 'ok');
  if (kind === 'error') beep(340, 0.3); else if (kind === 'warn') beep(520, 0.2);
}
function renderNotifs() {
  const badge = $('notifBadge');
  badge.style.display = unread > 0 ? 'flex' : 'none';
  badge.textContent = unread > 99 ? '99+' : unread;
  const list = $('notifList');
  list.innerHTML = notifications.length
    ? notifications.map(n => `<div class="notif-item ${n.kind === 'error' ? 'error' : n.kind === 'warn' ? 'warn' : ''}">
        <div>${esc(n.msg)}</div><div class="n-time">${fmtTime(n.t)}</div></div>`).join('')
    : '<div class="empty-msg show">No notifications</div>';
}

/* ---------------- Logging ---------------- */
function addLog(level, msg, ts) {
  logs.push({ t: ts ? new Date(ts) : new Date(), level, msg });
  if (logs.length > 800) logs.splice(0, logs.length - 800);
  renderLogs();
  if (level === 'ERROR') beep(300, 0.25);
}
function logLineHTML(l) {
  return `<div class="log-line ${l.level === 'ERROR' ? 'err-line' : ''}">
    <span class="log-time">${fmtTime(l.t)}</span>
    <span class="log-level ${l.level}">[${l.level}]</span>
    <span class="log-msg">${esc(l.msg)}</span></div>`;
}
function filteredLogs(q, lvl) {
  q = (q || '').toLowerCase();
  return logs.filter(l => (!lvl || l.level === lvl) && (!q || l.msg.toLowerCase().includes(q)));
}
function renderLogs() {
  const ovBox = $('logStreamOv');
  const fl = filteredLogs($('logSearchOv').value, $('logFilterOv').value);
  const stick = ovBox.scrollTop + ovBox.clientHeight >= ovBox.scrollHeight - 30;
  ovBox.innerHTML = fl.slice(-120).map(logLineHTML).join('');
  if (stick) ovBox.scrollTop = ovBox.scrollHeight;
  if ($('logsModal').classList.contains('open')) renderModalLogs();
}
function renderModalLogs() {
  const fl = filteredLogs($('logSearchM').value, $('logFilterM').value);
  $('logsTotal').textContent = fl.length;
  const box = $('logStreamM');
  const stick = box.scrollTop + box.clientHeight >= box.scrollHeight - 30;
  box.innerHTML = fl.map(logLineHTML).join('');
  if (stick) box.scrollTop = box.scrollHeight;
}

/* ---------------- Clock ---------------- */
function tickClock() {
  const now = new Date();
  $('topClock').textContent = `${fmtTime(now)} ${Intl.DateTimeFormat().resolvedOptions().timeZone.split('/').pop().replace('_',' ')}`;
  $('topDate').textContent = fmtDate(now);
}
setInterval(tickClock, 1000); tickClock();

/* ---------------- Navigation ---------------- */
document.querySelectorAll('.nav-item').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.nav-item').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
    btn.classList.add('active');
    $('page-' + btn.dataset.page).classList.add('active');
    if (btn.dataset.page === 'telemetry') drawAllCharts();
    if (btn.dataset.page === 'anomalies') { renderAnomalies(); }
  });
});

/* ---------------- WebSocket ---------------- */
function connectWS() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onopen = () => {
    connected = true; wsRetry = 0;
    setSysStatus('online');
    addLog('OK', 'Uplink established — WebSocket connected to mission server');
  };
  ws.onclose = () => {
    if (connected) addLog('ERROR', 'Uplink lost — WebSocket disconnected. Reconnecting…');
    connected = false;
    setSysStatus('offline');
    setTimeout(connectWS, Math.min(1000 * 2 ** wsRetry++, 15000));
  };
  ws.onerror = () => ws.close();
  ws.onmessage = e => {
    let msg; try { msg = JSON.parse(e.data); } catch (err) { return; }
    handleMessage(msg);
  };
}
function setSysStatus(state) {
  const dot = $('sysDot'), txt = $('sysText'), tag = $('setConnTag');
  if (state === 'online') {
    dot.className = 'dot dot-green pulse'; txt.textContent = 'SYSTEM ONLINE';
    tag.textContent = 'CONNECTED'; tag.className = 'tag tag-green';
  } else {
    dot.className = 'dot dot-red'; txt.textContent = 'LINK DOWN';
    tag.textContent = 'DISCONNECTED'; tag.className = 'tag tag-red';
  }
}

function handleMessage(msg) {
  const d = msg.data || {};
  switch (msg.type) {
    case 'connected': onConnected(d); break;
    case 'telemetry_update':
      onTelemetry(d, msg.timestamp);
      if (window.orbitMapTelemetry) window.orbitMapTelemetry(d);
      if (window.heroTelemetry) window.heroTelemetry(d);
      break;
    case 'anomaly_detected':
      onAnomaly(d, msg.timestamp);
      if (window.orbitMapSeverity) window.orbitMapSeverity(d.severity);
      if (window.heroSeverity) window.heroSeverity(d.severity);
      break;
    case 'agent_activity': onAgentActivity(d, msg.timestamp); break;
    case 'diagnosis_complete': onDiagnosis(d, msg.timestamp); break;
    case 'validation_failed':
      addLog('WARN', `Digital twin rejected ${d.rejected_count} procedure(s): ${d.reason}`, msg.timestamp);
      wfSet(4, 'active', 'Digital twin revising rejected procedures…'); break;
    case 'validation_recovered':
      addLog('OK', 'Fallback procedure revalidated by digital twin', msg.timestamp); break;
    case 'recovery_options': onRecoveryOptions(d, msg.timestamp); break;
    case 'approval_required': onApprovalRequired(d, msg.timestamp); break;
    case 'approval_decision': onApprovalDecision(d, msg.timestamp); break;
    case 'runbook_ready': onRunbookReady(d, msg.timestamp); break;
  }
}

function onConnected(status) {
  if (status.offline_mode) $('demoChip').style.display = 'inline-flex';
  $('setBackendInfo').textContent = `SatOps AI mission server · ${status.offline_mode ? 'offline mock-LLM demo mode' : 'live Claude agent mode'}`;
  (status.activity_log || []).forEach(a =>
    addLog(a.level === 'warning' ? 'WARN' : a.level === 'success' ? 'OK' : 'INFO', `${a.agent}: ${a.message}`, a.timestamp));
  (status.active_anomalies || []).forEach(a => { anomalies.set(a.id, { ...a, status: 'DETECTED' }); });
  runbooks = status.runbooks || [];
  runbooksIssued = runbooks.length;
  renderAnomalies(); renderRunbookList();
  loadScenarios();
}

/* ---------------- Telemetry handling ---------------- */
function onTelemetry(d, ts) {
  latestTelemetry = d.values || {};
  latestOrbital = d.orbital || {};
  latestViolations = d.violations || [];
  const t = ts ? new Date(ts) : new Date();

  if (!telemetryPaused) {
    for (const p in latestTelemetry) {
      if (!history[p]) history[p] = [];
      history[p].push({ t, v: latestTelemetry[p] });
      if (history[p].length > 400) history[p].splice(0, history[p].length - 400);
    }
    packets.unshift({ t, id: 'PKT-' + (++pktSeq), vals: { ...latestTelemetry }, crc: (Math.abs(hashCode(JSON.stringify(latestTelemetry))) % 0xFFFF).toString(16).toUpperCase().padStart(4, '0') });
    if (packets.length > 40) packets.length = 40;
  }

  wfSet(0, 'done', null, t);
  if (wfState.stage <= 1) wfSet(1, 'active', 'Monitoring live telemetry for anomalies…');

  renderOverview();
  renderHealth();
  if ($('page-telemetry').classList.contains('active') && !telemetryPaused) { drawAllCharts(); renderPackets(); }
}
function hashCode(s) { let h = 0; for (let i = 0; i < s.length; i++) { h = (h << 5) - h + s.charCodeAt(i); h |= 0; } return h; }

function subsystemHealth() {
  // score each subsystem from live violations
  const out = {};
  for (const k in SUBSYSTEMS) out[k] = { score: 100, viol: [] };
  latestViolations.forEach(v => {
    const s = v.subsystem;
    if (out[s]) { out[s].score -= 35; out[s].viol.push(v); }
  });
  for (const k in out) out[k].score = Math.max(5, out[k].score);
  return out;
}
function overallHealth(sub) {
  const keys = Object.keys(sub);
  return Math.round(keys.reduce((a, k) => a + sub[k].score, 0) / keys.length);
}

function renderOverview() {
  const tv = latestTelemetry;
  if (tv.battery_soc_pct == null) return;

  const phase = (latestOrbital.orbital_phase_pct || 0) / 100 * 2 * Math.PI;
  const alt = 512.6 + 2.2 * Math.sin(phase);
  const vel = 7.56 + 0.008 * Math.cos(phase);
  $('ovAlt').textContent = alt.toFixed(1) + ' km';
  $('ovVel').textContent = vel.toFixed(2) + ' km/s';

  const batt = Math.max(0, Math.min(100, tv.battery_soc_pct));
  const solar = Math.max(0, Math.min(100, tv.solar_current_a / 8.5 * 100));
  $('ovBatt').textContent = batt.toFixed(0) + '%';
  $('ovBattBar').style.width = batt + '%';
  $('ovBattBar').style.background = batt < 30 ? 'var(--red)' : batt < 55 ? 'var(--amber)' : 'var(--green)';
  $('ovSolar').textContent = solar.toFixed(0) + '%';
  $('ovSolarBar').style.width = solar + '%';
  $('ovSolarBar').style.background = solar < 20 ? 'var(--amber)' : 'var(--green)';
  $('ovTemp').textContent = tv.temp_obc_c.toFixed(1) + ' °C';
  $('ovEclipse').textContent = latestOrbital.in_eclipse ? 'IN ECLIPSE 🌑' : 'SUNLIT ☀️';
  $('ovPhase').textContent = (latestOrbital.orbital_phase_pct || 0).toFixed(1) + '%';
  $('visInfo').textContent = `TICK ${latestOrbital.tick ?? '—'} · PHASE ${(latestOrbital.orbital_phase_pct || 0).toFixed(1)}% · ${latestOrbital.in_eclipse ? 'ECLIPSE' : 'SUNLIT'}`;

  const sub = subsystemHealth();
  const overall = overallHealth(sub);
  $('ringPct').textContent = overall + '%';
  const ring = $('ringVal');
  const C = 2 * Math.PI * 56;
  ring.style.strokeDasharray = C;
  ring.style.strokeDashoffset = C * (1 - overall / 100);
  ring.style.stroke = overall > 80 ? 'var(--green)' : overall > 55 ? 'var(--amber)' : 'var(--red)';

  const anyBad = latestViolations.length > 0;
  const satTag = $('satTag');
  if (!anyBad) { satTag.textContent = 'NOMINAL'; satTag.className = 'tag tag-green'; }
  else if (overall > 60) { satTag.textContent = 'DEGRADED'; satTag.className = 'tag tag-amber'; }
  else { satTag.textContent = 'CRITICAL'; satTag.className = 'tag tag-red'; }

  $('quickSubsys').innerHTML = ['EPS','THERMAL','COMMS','ADCS','OBC','PAYLOAD'].map(k => {
    const s = sub[k], st = s.score > 80 ? ['NOMINAL','ok'] : s.score > 45 ? ['WARNING','warn'] : ['FAULT','bad'];
    return `<li><span class="dot ${st[1] === 'ok' ? 'dot-green' : st[1] === 'warn' ? 'dot-amber' : 'dot-red'}"></span>
      ${SUBSYSTEMS[k].name}<span class="st ${st[1]}">${st[0]}</span></li>`;
  }).join('');
}

/* ---------------- Charts (hand-rolled canvas, no deps) ---------------- */
function buildChartGrid() {
  $('chartGrid').innerHTML = CHART_PARAMS.map(p => `
    <div class="card chart-card">
      <div class="chart-title">${PARAMS[p].label} (${PARAMS[p].unit})<span class="chart-now" id="now-${p}">—</span></div>
      <canvas class="tchart" id="chart-${p}"></canvas>
    </div>`).join('');
}
function drawChart(p) {
  const cv = $('chart-' + p); if (!cv) return;
  const dpr = window.devicePixelRatio || 1;
  const w = cv.clientWidth, h = cv.clientHeight;
  if (!w) return;
  cv.width = w * dpr; cv.height = h * dpr;
  const ctx = cv.getContext('2d'); ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, w, h);

  const data = history[p].slice(-settings.window);
  const meta = PARAMS[p];
  const now = $('now-' + p);
  if (data.length) {
    const v = data[data.length - 1].v;
    now.textContent = v.toFixed(meta.unit === 'RPM' ? 0 : 1) + ' ' + meta.unit;
    const bad = (meta.lo != null && v < meta.lo) || (meta.hi != null && v > meta.hi);
    now.className = 'chart-now' + (bad ? ' bad' : '');
  }
  if (data.length < 2) return;

  let lo = Math.min(...data.map(d => d.v)), hi = Math.max(...data.map(d => d.v));
  if (meta.lo != null) lo = Math.min(lo, meta.lo); if (meta.hi != null) hi = Math.max(hi, meta.hi);
  const padY = (hi - lo) * 0.15 || 1; lo -= padY; hi += padY;
  const X = i => i / (data.length - 1) * (w - 4) + 2;
  const Y = v => h - 3 - (v - lo) / (hi - lo) * (h - 6);

  // threshold lines
  ctx.setLineDash([4, 4]); ctx.lineWidth = 1;
  if (meta.hi != null) { ctx.strokeStyle = 'rgba(251,191,36,.45)'; ctx.beginPath(); ctx.moveTo(0, Y(meta.hi)); ctx.lineTo(w, Y(meta.hi)); ctx.stroke(); }
  if (meta.lo != null) { ctx.strokeStyle = 'rgba(251,191,36,.45)'; ctx.beginPath(); ctx.moveTo(0, Y(meta.lo)); ctx.lineTo(w, Y(meta.lo)); ctx.stroke(); }
  ctx.setLineDash([]);

  const accent = getComputedStyle(document.documentElement).getPropertyValue('--accent').trim() || '#25d6f5';
  // area fill
  const grad = ctx.createLinearGradient(0, 0, 0, h);
  grad.addColorStop(0, accent + '44'); grad.addColorStop(1, accent + '00');
  ctx.beginPath(); ctx.moveTo(X(0), Y(data[0].v));
  data.forEach((d, i) => ctx.lineTo(X(i), Y(d.v)));
  ctx.lineTo(X(data.length - 1), h); ctx.lineTo(X(0), h); ctx.closePath();
  ctx.fillStyle = grad; ctx.fill();
  // line
  ctx.beginPath(); ctx.moveTo(X(0), Y(data[0].v));
  data.forEach((d, i) => ctx.lineTo(X(i), Y(d.v)));
  ctx.strokeStyle = accent; ctx.lineWidth = 1.8; ctx.stroke();
  // last point
  const last = data[data.length - 1];
  ctx.beginPath(); ctx.arc(X(data.length - 1), Y(last.v), 3, 0, 7);
  ctx.fillStyle = accent; ctx.fill();
}
function drawAllCharts() { CHART_PARAMS.forEach(drawChart); }
window.addEventListener('resize', () => { if ($('page-telemetry').classList.contains('active')) drawAllCharts(); });

function renderPackets() {
  const cols = ['battery_voltage_v','battery_soc_pct','temp_obc_c','attitude_error_deg','downlink_snr_db','memory_usage_pct'];
  $('pktHead').innerHTML = '<th>Time</th><th>Packet</th>' + cols.map(c => `<th>${PARAMS[c].label}</th>`).join('') + '<th>CRC</th>';
  $('pktBody').innerHTML = packets.slice(0, 14).map(pk => {
    const tds = cols.map(c => {
      const v = pk.vals[c], m = PARAMS[c];
      const bad = (m.lo != null && v < m.lo) || (m.hi != null && v > m.hi);
      return `<td class="${bad ? 'bad' : ''}">${v.toFixed(1)}</td>`;
    }).join('');
    return `<tr><td>${fmtTime(pk.t)}</td><td>${pk.id}</td>${tds}<td>0x${pk.crc}</td></tr>`;
  }).join('');
}

/* ---------------- Health page ---------------- */
function renderHealth() {
  if (!$('page-health').classList.contains('active')) return;
  const sub = subsystemHealth();
  $('healthGrid').innerHTML = Object.keys(SUBSYSTEMS).map(k => {
    const s = sub[k];
    const st = s.score > 80 ? ['NOMINAL','ok'] : s.score > 45 ? ['WARNING','warn'] : ['FAULT','bad'];
    const params = Object.keys(PARAMS).filter(p => PARAMS[p].sub === k);
    const rows = params.map(p => {
      const v = latestTelemetry[p];
      return v == null ? '' : `${PARAMS[p].label}: <b>${v.toFixed(1)} ${PARAMS[p].unit}</b>`;
    }).filter(Boolean).join('<br>');
    return `<div class="card hcard">
      <div class="hcard-top"><span>${SUBSYSTEMS[k].ico}</span><span class="nm">${SUBSYSTEMS[k].name}</span>
        <span class="st ${st[1]}">${st[0]}</span></div>
      <div class="hbar"><div class="hbar-fill ${st[1] === 'ok' ? '' : st[1]}" style="width:${s.score}%"></div></div>
      <div class="hparams">${rows || '<i>No direct sensors</i>'}</div>
    </div>`;
  }).join('');
}

$('runDiag').addEventListener('click', async () => {
  const card = $('diagCard'), out = $('diagOut'), tag = $('diagTag');
  card.style.display = 'block'; out.textContent = ''; tag.textContent = 'RUNNING'; tag.className = 'tag tag-amber';
  $('runDiag').disabled = true;
  const lines = [];
  lines.push('> Initiating full diagnostics sweep on SAT-3A…');
  for (const p of Object.keys(PARAMS)) {
    const v = latestTelemetry[p], m = PARAMS[p];
    if (v == null) continue;
    const bad = (m.lo != null && v < m.lo) || (m.hi != null && v > m.hi);
    lines.push(`  [${bad ? 'FAIL' : ' OK '}] ${m.label.padEnd(16)} ${v.toFixed(2).padStart(9)} ${m.unit.padEnd(4)} (limits ${m.lo ?? '—'} … ${m.hi ?? '—'})`);
  }
  const fails = lines.filter(l => l.includes('[FAIL]')).length;
  lines.push('');
  lines.push(fails ? `> Sweep complete — ${fails} parameter(s) out of limits. See Anomalies page.` : '> Sweep complete — all parameters within operational limits.');
  for (const l of lines) {
    out.textContent += l + '\n';
    await new Promise(r => setTimeout(r, 90));
  }
  tag.textContent = fails ? `${fails} FAULTS` : 'ALL PASS';
  tag.className = 'tag ' + (fails ? 'tag-red' : 'tag-green');
  addLog(fails ? 'WARN' : 'OK', `Diagnostics sweep finished: ${fails ? fails + ' faults detected' : 'all systems pass'}`);
  $('runDiag').disabled = false;
});

/* ---------------- Workflow ---------------- */
const WF_STAGES = [
  { name: 'Data Ingestion', ico: '📡' },
  { name: 'Anomaly Detection', ico: '〽️' },
  { name: 'Diagnosis', ico: '📋' },
  { name: 'Recovery Plan', ico: '🧩' },
  { name: 'Simulation', ico: '🧪' },
  { name: 'Execution', ico: '🛡️' },
];
const wfState = { stage: 0, states: WF_STAGES.map(() => 'pending'), times: WF_STAGES.map(() => null), msg: 'Awaiting telemetry…', etaT: null };

function wfSet(idx, state, msg, ts) {
  if (state === 'done' && wfState.states[idx] === 'done') { if (msg) wfUpdateMsg(msg); return; }
  wfState.states[idx] = state;
  if (state === 'done') { wfState.times[idx] = ts || new Date(); for (let i = 0; i < idx; i++) if (wfState.states[i] !== 'done') { wfState.states[i] = 'done'; wfState.times[i] = wfState.times[i] || new Date(); } }
  if (state === 'active') { wfState.stage = idx; wfState.etaT = Date.now() + 8000; }
  if (msg) wfState.msg = msg;
  renderWorkflow();
}
function wfUpdateMsg(m) { wfState.msg = m; renderWorkflow(); }
function wfReset(msg) {
  wfState.states = WF_STAGES.map(() => 'pending');
  wfState.times = WF_STAGES.map(() => null);
  wfState.stage = 1;
  wfState.states[0] = 'done'; wfState.times[0] = new Date();
  wfState.states[1] = 'active';
  wfState.msg = msg || 'Monitoring live telemetry for anomalies…';
  renderWorkflow();
}
function wfTrackHTML() {
  return WF_STAGES.map((s, i) => {
    const st = wfState.states[i];
    const stateTxt = st === 'done' ? 'Completed' : st === 'active' ? 'In Progress' : 'Pending';
    const t = wfState.times[i] ? fmtTime(wfState.times[i]) : '--:--:--';
    return `<div class="wf-step ${st}">
      <div class="wf-ico">${s.ico}</div>
      <div class="wf-name">${i + 1}. ${s.name}</div>
      <div class="wf-state">${stateTxt}<br>${st === 'done' ? t : st === 'active' ? fmtTime() : '--:--:--'}</div>
    </div>`;
  }).join('');
}
function renderWorkflow() {
  $('wfTrackOv').innerHTML = wfTrackHTML();
  $('wfTrackFull').innerHTML = wfTrackHTML();
  $('wfMsgOv').textContent = wfState.msg; $('wfMsgFull').textContent = wfState.msg;
  const active = wfState.states.some(s => s === 'active') && wfState.stage > 1;
  const tag = $('wfModeTag');
  tag.textContent = active ? 'RESPONDING' : 'MONITORING';
  tag.className = 'tag ' + (active ? 'tag-amber' : '');
}
setInterval(() => {
  let eta = '--:--:--';
  if (wfState.etaT && wfState.states.some(s => s === 'active') && wfState.stage > 1) {
    const rem = Math.max(0, wfState.etaT - Date.now());
    const s = Math.ceil(rem / 1000);
    eta = `00:${pad(Math.floor(s / 60))}:${pad(s % 60)}`;
  }
  $('wfEtaOv').textContent = eta; $('wfEtaFull').textContent = eta;
}, 500);

/* ---------------- Pipeline events ---------------- */
function onAnomaly(a, ts) {
  anomalies.set(a.id, { ...a, status: 'DETECTED' });
  addLog('ERROR', `ANOMALY ${a.id} — ${a.summary || a.anomaly_type} [${a.severity}]`, ts);
  notify(`Anomaly detected: ${a.anomaly_type || 'threshold violation'} (${a.severity})`, 'error');
  wfSet(1, 'done', null, ts);
  wfSet(2, 'active', 'Analyzing subsystem telemetry and isolating root cause…');
  renderAnomalies();
}
function onAgentActivity(d, ts) {
  const lvl = d.status === 'running' ? 'INFO' : 'INFO';
  addLog(lvl, `${d.agent}: ${d.message}`, ts);
  const agentLog = $('agentLog');
  const stick = agentLog.scrollTop + agentLog.clientHeight >= agentLog.scrollHeight - 30;
  agentLog.innerHTML += `<div class="log-line"><span class="log-time">${fmtTime(ts)}</span>
    <span class="log-level INFO">[${esc(d.agent)}]</span><span class="log-msg">${esc(d.message)}</span></div>`;
  if (stick) agentLog.scrollTop = agentLog.scrollHeight;

  if (d.agent === 'DIAGNOSTICS') wfSet(2, 'active', 'Performing root-cause analysis…');
  if (d.agent === 'RECOVERY_PLANNER') { wfSet(2, 'done', null, ts); wfSet(3, 'active', 'Generating ranked recovery procedures…'); }
  if (d.agent === 'DIGITAL_TWIN') { wfSet(3, 'done', null, ts); wfSet(4, 'active', 'Validating procedures on digital twin…'); }
  if (d.agent === 'RUNBOOK_GENERATOR') { wfSet(4, 'done', null, ts); wfSet(5, 'active', 'Executing approved procedure & generating runbook…'); }
}
function onDiagnosis(d, ts) {
  addLog('OK', `Diagnosis ${d.anomaly_id}: ${d.root_cause}`, ts);
  const a = anomalies.get(d.anomaly_id);
  if (a) { a.diagnosis = d; a.status = 'DIAGNOSED'; renderAnomalies(); }
}
function onRecoveryOptions(d, ts) {
  const a = anomalies.get(d.anomaly_id);
  if (a) { a.plan = d.plan; a.status = 'PLAN READY'; renderAnomalies(); }
  addLog('OK', `Recovery plan validated for ${d.anomaly_id} (${(d.plan.validated_options || []).length} options)`, ts);
}
function onApprovalRequired(d, ts) {
  pendingApprovals.set(d.anomaly_id, d);
  const a = anomalies.get(d.anomaly_id);
  if (a) { a.status = 'AWAITING APPROVAL'; }
  addLog('WARN', `Operator approval required for ${d.anomaly_id} (severity ${d.severity})`, ts);
  notify(`Approval required: ${d.anomaly_id} — severity ${d.severity}`, 'warn');
  wfSet(5, 'active', `⚠ ${d.severity} anomaly — awaiting operator approval (Anomalies page)`);
  renderAnomalies(); renderApproval();
}
function onApprovalDecision(d, ts) {
  const a = anomalies.get(d.anomaly_id);
  if (a) a.status = d.auto_approved ? 'AUTO-APPROVED' : 'APPROVED';
  addLog('OK', d.auto_approved ? `Auto-approved (${d.severity} within policy)` : `Operator approved procedure #${d.approved_rank}`, ts);
  pendingApprovals.delete(d.anomaly_id);
  renderAnomalies(); renderApproval();
}
function onRunbookReady(d, ts) {
  runbooksIssued++;
  runbooks.push({ filename: d.filename, anomaly_id: d.anomaly_id, generated_at: d.generated_at, content: d.content });
  const a = anomalies.get(d.anomaly_id);
  if (a) a.status = 'RESOLVED';
  addLog('OK', `Runbook generated: ${d.filename}`, ts);
  notify(`Runbook ready for ${d.anomaly_id}`, 'info');
  wfSet(5, 'done', `Runbook ${d.filename} issued — response complete`, ts);
  renderAnomalies(); renderRunbookList();
  const key = simRunning; simRunning = null;
  if (key) {
    simHistory.unshift({ t: new Date().toISOString(), key, sub: scenarios[key]?.subsystem || '—', sev: scenarios[key]?.severity || '—', outcome: 'RESOLVED — runbook issued' });
    localStorage.setItem('vortex_simhist', JSON.stringify(simHistory.slice(0, 40)));
    renderSimHistory(); renderSimGrid();
  }
  setTimeout(() => wfReset(), 6000);
}

/* ---------------- Anomalies page ---------------- */
function renderAnomalies() {
  const rows = [...anomalies.values()].reverse();
  const filter = $('anoFilter').value;
  const shown = filter ? rows.filter(r => r.severity === filter) : rows;
  $('navAnoCount').style.display = rows.filter(r => r.status !== 'RESOLVED').length ? 'inline-flex' : 'none';
  $('navAnoCount').textContent = rows.filter(r => r.status !== 'RESOLVED').length;
  $('msActive').textContent = rows.length;
  $('msPending').textContent = pendingApprovals.size;
  $('msResolved').textContent = runbooksIssued;
  $('anoEmpty').classList.toggle('show', shown.length === 0);
  $('anoBody').innerHTML = shown.map(a => `
    <tr><td>${fmtTime(a.detected_at)}</td><td style="font-family:var(--mono);font-size:11px">${esc(a.id)}</td>
    <td><span class="sev ${esc(a.severity)}">${esc(a.severity)}</span></td>
    <td>${esc(a.primary_subsystem || '—')}</td>
    <td style="white-space:normal;max-width:380px">${esc(a.summary || a.anomaly_type || '—')}</td>
    <td><span class="tag ${a.status === 'RESOLVED' ? 'tag-green' : a.status === 'AWAITING APPROVAL' ? 'tag-amber' : ''}">${esc(a.status)}</span></td></tr>`).join('');
}
$('anoFilter').addEventListener('change', renderAnomalies);

function renderApproval() {
  const card = $('approvalCard');
  const entry = pendingApprovals.values().next().value;
  if (!entry) { card.style.display = 'none'; return; }
  card.style.display = 'block';
  $('apprAnoId').textContent = entry.anomaly_id;
  $('apprDiag').textContent = `Severity ${entry.severity}. Root cause: ${entry.diagnosis?.root_cause || '—'}`;
  const opts = entry.plan?.validated_options || [];
  const rec = entry.plan?.recommended_rank || 1;
  $('apprOptions').innerHTML = opts.map(o => `
    <div class="appr-opt ${o.rank === rec ? 'rec' : ''}">
      <div class="appr-opt-head"><span class="nm">#${o.rank} ${esc(o.name || 'Procedure')}</span>
        ${o.rank === rec ? '<span class="rec-tag">RECOMMENDED</span>' : ''}
        <span class="sev ${esc(o.risk_level || 'LOW')}">${esc(o.risk_level || '—')} RISK</span></div>
      <div class="appr-meta">
        <span>⏱ ${o.estimated_duration_min ?? '—'} min</span>
        <span>✅ ${Math.round((o.success_probability ?? 0) * 100)}% success</span>
        <span>↩ ${o.reversible ? 'Reversible' : 'Irreversible'}</span></div>
      <ol class="appr-steps">${(o.steps || []).map(s => `<li>${esc(s)}</li>`).join('')}</ol>
      <button class="btn sm" onclick="approveProc('${esc(entry.anomaly_id)}',${o.rank})">Approve &amp; Execute #${o.rank}</button>
    </div>`).join('');
}
window.approveProc = async (id, rank) => {
  try {
    await fetch(`/api/approve/${id}?rank=${rank}`, { method: 'POST' });
    addLog('OK', `Approval sent for ${id}, procedure #${rank}`);
  } catch (e) { addLog('ERROR', 'Approval request failed: ' + e.message); }
};

/* ---------------- Inject / simulations ---------------- */
let simRunning = null;
async function loadScenarios() {
  try {
    const r = await fetch('/api/scenarios'); scenarios = await r.json();
    renderSimGrid(); renderInjectList(); renderSimHistory();
  } catch (e) { addLog('ERROR', 'Failed to load scenarios: ' + e.message); }
}
function scenarioTitle(k) { return k.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase()); }
function renderSimGrid() {
  $('simGrid').innerHTML = Object.entries(scenarios).map(([k, s]) => `
    <div class="card sim-card">
      <div class="nm">${scenarioTitle(k)}</div>
      <div class="meta"><span class="sev ${esc(s.severity)}">${esc(s.severity)}</span><span class="tag">${esc(s.subsystem)}</span></div>
      <div class="desc">${esc(s.description)}</div>
      <button class="btn sm sim-run-btn" ${simRunning ? 'disabled' : ''} onclick="runSim('${k}')">${simRunning === k ? '⏳ Running…' : '▶ Run Scenario'}</button>
    </div>`).join('');
}
window.runSim = async (key) => {
  if (simRunning) return;
  simRunning = key;
  renderSimGrid();
  addLog('WARN', `Injecting fault scenario: ${key}`);
  notify(`Simulation started: ${scenarioTitle(key)}`, 'warn');
  try { await fetch(`/api/inject/${key}`, { method: 'POST' }); }
  catch (e) { addLog('ERROR', 'Injection failed: ' + e.message); simRunning = null; renderSimGrid(); return; }
  // watchdog: clear the "running" lock if pipeline never completes
  setTimeout(() => { if (simRunning === key) { simRunning = null; renderSimGrid(); } }, 120000);
};
function renderSimHistory() {
  $('simEmpty').classList.toggle('show', simHistory.length === 0);
  $('simHist').innerHTML = simHistory.map(h => `
    <tr><td>${fmtTime(h.t)}</td><td>${scenarioTitle(h.key)}</td><td>${esc(h.sub)}</td>
    <td><span class="sev ${esc(h.sev)}">${esc(h.sev)}</span></td><td>${esc(h.outcome)}</td></tr>`).join('');
}
function renderInjectList() {
  $('injectList').innerHTML = Object.entries(scenarios).map(([k, s]) => `
    <button class="inject-item" onclick="injectFromModal('${k}')">
      <div class="nm">${scenarioTitle(k)} <span class="sev ${esc(s.severity)}">${esc(s.severity)}</span></div>
      <div class="ds">${esc(s.description)}</div>
    </button>`).join('');
}
window.injectFromModal = (k) => { $('injectModal').classList.remove('open'); window.runSim(k); };
$('anoInjectBtn').addEventListener('click', () => $('injectModal').classList.add('open'));
$('injectClose').addEventListener('click', () => $('injectModal').classList.remove('open'));

/* ---------------- Reports / runbooks ---------------- */
let currentRunbook = null;
function renderRunbookList() {
  const list = $('runbookList');
  if (!runbooks.length) { list.innerHTML = '<p class="empty-msg show">No runbooks yet — they are generated automatically when the pipeline resolves an anomaly.</p>'; return; }
  list.innerHTML = [...runbooks].reverse().map(r => `
    <div class="rb-item ${currentRunbook === r.filename ? 'sel' : ''}" onclick="openRunbook('${esc(r.filename)}')">
      <div class="fn">${esc(r.filename)}</div>
      <div class="mt">${esc(r.anomaly_id)} · ${fmtTime(r.generated_at)}</div>
    </div>`).join('');
}
window.openRunbook = async (fn) => {
  try {
    const r = await fetch(`/api/runbooks/${encodeURIComponent(fn)}`);
    const d = await r.json();
    currentRunbook = fn;
    $('runbookView').style.display = 'block';
    $('runbookTitle').textContent = fn;
    $('runbookBody').textContent = d.content || '(empty)';
    renderRunbookList();
  } catch (e) { addLog('ERROR', 'Failed to load runbook: ' + e.message); }
};
$('runbookClose').addEventListener('click', () => { $('runbookView').style.display = 'none'; currentRunbook = null; renderRunbookList(); });
$('runbookDl').addEventListener('click', () => { if (currentRunbook) download(currentRunbook, $('runbookBody').textContent, 'text/markdown'); });
$('reportRefresh').addEventListener('click', async () => {
  try {
    const r = await fetch('/api/runbooks'); const d = await r.json();
    runbooks = d.runbooks || []; renderRunbookList();
    toast('Runbook list refreshed', 'ok');
  } catch (e) { addLog('ERROR', 'Refresh failed: ' + e.message); }
});

/* ---------------- Command console ---------------- */
const termHistory = []; let termIdx = -1;
const COMMANDS = {
  help: () => [
    'Available commands:',
    '  status            — live satellite status snapshot',
    '  health            — subsystem health summary',
    '  telemetry         — latest telemetry values',
    '  ping              — measure ground-station RTT',
    '  scenarios         — list injectable fault scenarios',
    '  inject <scenario> — inject a fault (e.g. inject attitude_drift)',
    '  approve <id> <n>  — approve recovery procedure rank n',
    '  runbooks          — list generated runbooks',
    '  history <param>   — recent history for a parameter',
    '  params            — list telemetry parameter names',
    '  clear             — clear terminal',
  ],
  params: () => Object.keys(PARAMS).map(p => `  ${p.padEnd(20)} ${PARAMS[p].label} (${PARAMS[p].unit})`),
  clear: () => { $('term').innerHTML = ''; return []; },
  telemetry: () => Object.keys(latestTelemetry).length
    ? Object.entries(latestTelemetry).map(([k, v]) => `  ${k.padEnd(20)} ${v.toFixed(2)} ${PARAMS[k]?.unit || ''}`)
    : ['No telemetry received yet.'],
  health: () => {
    const sub = subsystemHealth();
    return Object.keys(SUBSYSTEMS).map(k => {
      const s = sub[k];
      return `  ${SUBSYSTEMS[k].name.padEnd(20)} ${String(s.score).padStart(3)}%  ${s.score > 80 ? 'NOMINAL' : s.score > 45 ? 'WARNING' : 'FAULT'}`;
    }).concat(['', `  Overall: ${overallHealth(sub)}%`]);
  },
};
async function execTerm(raw) {
  const line = raw.trim(); if (!line) return;
  termPrint('SAT-3A> ' + line, 'cmd');
  termHistory.unshift(line); termIdx = -1;
  const [cmd, ...args] = line.split(/\s+/);

  if (COMMANDS[cmd]) { COMMANDS[cmd]().forEach(l => termPrint(l)); return; }
  try {
    switch (cmd) {
      case 'ping': {
        const t0 = performance.now();
        await fetch('/api/status');
        const rtt = Math.round(performance.now() - t0);
        $('cmdLatency').textContent = rtt;
        termPrint(`PONG — ground link RTT ${rtt} ms`, 'ok'); break;
      }
      case 'status': {
        const r = await fetch('/api/status'); const d = await r.json();
        termPrint(`Mode        : ${d.offline_mode ? 'OFFLINE DEMO (mock LLM)' : 'LIVE (Claude agents)'}`);
        termPrint(`Orbit phase : ${d.orbital?.orbital_phase_pct}%  ${d.orbital?.in_eclipse ? '(eclipse)' : '(sunlit)'}`);
        termPrint(`Tick        : ${d.orbital?.tick}`);
        termPrint(`Anomalies   : ${(d.active_anomalies || []).length} recorded, ${(d.pending_approvals || []).length} awaiting approval`);
        termPrint(`Runbooks    : ${(d.runbooks || []).length}`); break;
      }
      case 'scenarios':
        Object.entries(scenarios).forEach(([k, s]) => termPrint(`  ${k.padEnd(22)} [${s.severity}] ${s.subsystem}`));
        break;
      case 'inject': {
        if (!args[0] || !scenarios[args[0]]) { termPrint('Unknown scenario. Try: ' + Object.keys(scenarios).join(', '), 'err'); break; }
        await fetch(`/api/inject/${args[0]}`, { method: 'POST' });
        termPrint(`Fault "${args[0]}" injected — watch the Workflow page.`, 'ok'); break;
      }
      case 'approve': {
        if (args.length < 2) { termPrint('Usage: approve <anomaly_id> <rank>', 'err'); break; }
        const r = await fetch(`/api/approve/${args[0]}?rank=${args[1]}`, { method: 'POST' });
        termPrint(r.ok ? 'Approval submitted.' : 'Approval failed (unknown id?).', r.ok ? 'ok' : 'err'); break;
      }
      case 'runbooks': {
        const r = await fetch('/api/runbooks'); const d = await r.json();
        if (!(d.runbooks || []).length) termPrint('No runbooks generated yet.');
        (d.runbooks || []).forEach(rb => termPrint(`  ${rb.filename}`)); break;
      }
      case 'history': {
        if (!args[0] || !PARAMS[args[0]]) { termPrint('Usage: history <param> — see "params"', 'err'); break; }
        const r = await fetch(`/api/history/${args[0]}?n=10`); const d = await r.json();
        (d.history || []).forEach(h => termPrint(`  ${h.ts ? fmtTime(h.ts) : ''}  ${(h.value ?? h.v ?? 0).toFixed?.(2) ?? h.value}`));
        if (!(d.history || []).length) termPrint('No history yet.'); break;
      }
      default:
        termPrint(`Unknown command: ${cmd}. Type 'help'.`, 'err');
    }
  } catch (e) { termPrint('Command failed: ' + e.message, 'err'); }
}
function termPrint(text, cls) {
  const t = $('term');
  const div = document.createElement('div');
  div.className = 'term-line ' + (cls || '');
  div.textContent = text;
  t.appendChild(div); t.scrollTop = t.scrollHeight;
}
$('termInput').addEventListener('keydown', e => {
  const inp = e.target;
  if (e.key === 'Enter') { execTerm(inp.value); inp.value = ''; }
  else if (e.key === 'ArrowUp') { e.preventDefault(); if (termIdx < termHistory.length - 1) inp.value = termHistory[++termIdx]; }
  else if (e.key === 'ArrowDown') { e.preventDefault(); inp.value = termIdx > 0 ? termHistory[--termIdx] : (termIdx = -1, ''); }
  else if (e.key === 'Tab') {
    e.preventDefault();
    const all = [...Object.keys(COMMANDS), 'ping','status','scenarios','inject','approve','runbooks','history'];
    const m = all.filter(c => c.startsWith(inp.value));
    if (m.length === 1) inp.value = m[0] + ' ';
    else if (m.length > 1) termPrint(m.join('  '), 'dim');
  }
});
const QUICK = ['status','health','telemetry','ping','scenarios','inject attitude_drift','inject comms_loss','runbooks','clear'];
$('quickCmds').innerHTML = QUICK.map(c => `<button class="btn sm" data-c="${c}">${c}</button>`).join('');
$('quickCmds').addEventListener('click', e => { const c = e.target.dataset?.c; if (c) execTerm(c); });
termPrint('VORTEX Command Console — link to SAT-3A established.', 'dim');
termPrint("Type 'help' for available commands.", 'dim');
// periodic latency measurement
setInterval(async () => {
  try { const t0 = performance.now(); await fetch('/api/status'); $('cmdLatency').textContent = Math.round(performance.now() - t0); } catch (e) {}
}, 10000);

/* ---------------- Telemetry page controls ---------------- */
$('telPause').addEventListener('click', () => {
  telemetryPaused = !telemetryPaused;
  $('telPause').textContent = telemetryPaused ? '▶ Resume' : '⏸ Pause';
  addLog('INFO', telemetryPaused ? 'Telemetry display paused' : 'Telemetry display resumed');
});
$('telCsv').addEventListener('click', () => {
  const params = Object.keys(PARAMS);
  const n = Math.max(...params.map(p => history[p].length));
  if (!n) { toast('No telemetry to export yet', 'warn'); return; }
  let csv = 'time,' + params.join(',') + '\n';
  for (let i = 0; i < n; i++) {
    const row = params.map(p => history[p][i] ? history[p][i].v.toFixed(3) : '');
    const t = history[params[0]][i]?.t;
    csv += (t ? new Date(t).toISOString() : '') + ',' + row.join(',') + '\n';
  }
  download(`vortex_telemetry_${Date.now()}.csv`, csv, 'text/csv');
  toast('Telemetry CSV exported', 'ok');
});

/* ---------------- Logs UI ---------------- */
$('logSearchOv').addEventListener('input', renderLogs);
$('logFilterOv').addEventListener('change', renderLogs);
$('logsViewAll').addEventListener('click', () => { $('logsModal').classList.add('open'); renderModalLogs(); });
$('logsClose').addEventListener('click', () => $('logsModal').classList.remove('open'));
$('logSearchM').addEventListener('input', renderModalLogs);
$('logFilterM').addEventListener('change', renderModalLogs);
$('logsDl').addEventListener('click', () => {
  download(`vortex_logs_${Date.now()}.txt`,
    logs.map(l => `${l.t.toISOString()} [${l.level}] ${l.msg}`).join('\n'));
});
document.querySelectorAll('.modal-back').forEach(m => m.addEventListener('click', e => { if (e.target === m) m.classList.remove('open'); }));

/* ---------------- Notifications UI ---------------- */
$('bellBtn').addEventListener('click', () => {
  const p = $('notifPanel');
  p.classList.toggle('open');
  if (p.classList.contains('open')) { unread = 0; renderNotifs(); }
});
$('notifClear').addEventListener('click', () => { notifications = []; unread = 0; renderNotifs(); });
document.addEventListener('click', e => {
  if (!$('notifPanel').contains(e.target) && e.target !== $('bellBtn') && !$('bellBtn').contains(e.target))
    $('notifPanel').classList.remove('open');
});

/* ---------------- Settings UI ---------------- */
$('accents').addEventListener('click', e => {
  const c = e.target.dataset?.c;
  if (c) { settings.accent = c; saveSettings(); applyAccent(); drawAllCharts(); }
});
$('setWindow').value = settings.window;
$('setWindow').addEventListener('change', e => { settings.window = +e.target.value; saveSettings(); drawAllCharts(); });
$('setSound').checked = settings.sound;
$('setSound').addEventListener('change', e => { settings.sound = e.target.checked; saveSettings(); if (settings.sound) beep(660, 0.15); });
$('setAnim').checked = settings.anim;
$('setAnim').addEventListener('change', e => { settings.anim = e.target.checked; saveSettings(); });
$('setReset').addEventListener('click', () => {
  localStorage.removeItem('vortex_settings'); localStorage.removeItem('vortex_simhist');
  settings = { ...DEFAULTS }; simHistory = [];
  applyAccent(); renderSimHistory(); $('setWindow').value = settings.window;
  $('setSound').checked = false; $('setAnim').checked = true;
  toast('Local data reset', 'ok');
});

/* ---------------- Space canvas (Earth + satellite) ---------------- */
const stars = [];
const vis = { orbit: true, grid: false, anim: true, angle: 0 };
$('vcOrbit').addEventListener('click', e => { vis.orbit = !vis.orbit; e.currentTarget.classList.toggle('active', vis.orbit); });
$('vcGrid').addEventListener('click', e => { vis.grid = !vis.grid; e.currentTarget.classList.toggle('active', vis.grid); });
$('vc3d').addEventListener('click', e => { vis.anim = !vis.anim; e.currentTarget.classList.toggle('active', vis.anim); });

function drawSpace() {
  const cv = $('spaceCanvas');
  const w = cv.clientWidth, h = cv.clientHeight;
  if (!w) { requestAnimationFrame(drawSpace); return; }
  const dpr = window.devicePixelRatio || 1;
  if (cv.width !== w * dpr) { cv.width = w * dpr; cv.height = h * dpr; }
  const ctx = cv.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);

  // stars
  if (!stars.length) for (let i = 0; i < 130; i++) stars.push({ x: Math.random(), y: Math.random(), r: Math.random() * 1.3 + .3, tw: Math.random() * 6.28 });
  stars.forEach(s => {
    const a = 0.35 + 0.55 * Math.abs(Math.sin(s.tw + performance.now() / 1400));
    ctx.fillStyle = `rgba(220,235,255,${a})`;
    ctx.beginPath(); ctx.arc(s.x * w, s.y * h, s.r, 0, 7); ctx.fill();
  });

  // Earth (bottom arc)
  const er = w * 0.85, ex = w / 2, ey = h + er * 0.62;
  const g = ctx.createRadialGradient(ex, ey - er * .3, er * .4, ex, ey, er);
  g.addColorStop(0, '#0b3d91'); g.addColorStop(.55, '#0a2a66'); g.addColorStop(1, '#051733');
  ctx.fillStyle = g;
  ctx.beginPath(); ctx.arc(ex, ey, er, 0, 7); ctx.fill();
  // atmosphere glow
  ctx.strokeStyle = 'rgba(90,180,255,.35)'; ctx.lineWidth = 6;
  ctx.beginPath(); ctx.arc(ex, ey, er + 3, 0, 7); ctx.stroke();
  // city lights
  ctx.fillStyle = 'rgba(255,190,80,.5)';
  for (let i = 0; i < 40; i++) {
    const a = Math.PI * 1.15 + (i / 40) * Math.PI * 0.7;
    const rr = er * (0.86 + (i % 5) * 0.025);
    const x = ex + rr * Math.cos(a), y = ey + rr * Math.sin(a);
    if (y < h && y > 0) { ctx.beginPath(); ctx.arc(x, y, 1.2, 0, 7); ctx.fill(); }
  }

  if (vis.grid) {
    ctx.strokeStyle = 'rgba(37,214,245,.12)'; ctx.lineWidth = 1;
    for (let i = 1; i < 6; i++) { ctx.beginPath(); ctx.moveTo(w / 6 * i, 0); ctx.lineTo(w / 6 * i, h); ctx.stroke(); }
    for (let i = 1; i < 4; i++) { ctx.beginPath(); ctx.moveTo(0, h / 4 * i); ctx.lineTo(w, h / 4 * i); ctx.stroke(); }
  }

  // orbit ellipse
  const ox = w / 2, oy = h * 0.48, orx = w * 0.36, ory = h * 0.3;
  if (vis.orbit) {
    ctx.strokeStyle = 'rgba(37,214,245,.3)'; ctx.setLineDash([5, 6]); ctx.lineWidth = 1.2;
    ctx.beginPath(); ctx.ellipse(ox, oy, orx, ory, -0.25, 0, 7); ctx.stroke();
    ctx.setLineDash([]);
  }

  // satellite position — follow real orbital phase when animating
  if (vis.anim && settings.anim) vis.angle += 0.0035;
  const phasePct = latestOrbital.orbital_phase_pct;
  const baseAngle = phasePct != null ? (phasePct / 100) * Math.PI * 2 : 0;
  const a = baseAngle + vis.angle;
  const sx = ox + orx * Math.cos(a) * Math.cos(-0.25) - ory * Math.sin(a) * Math.sin(-0.25);
  const sy = oy + orx * Math.cos(a) * Math.sin(-0.25) + ory * Math.sin(a) * Math.cos(-0.25);

  // satellite body
  ctx.save();
  ctx.translate(sx, sy);
  ctx.rotate(a + Math.PI / 2);
  // panels
  ctx.fillStyle = '#1a3a7a';
  ctx.strokeStyle = 'rgba(120,180,255,.8)'; ctx.lineWidth = 1;
  ctx.fillRect(-34, -6, 24, 12); ctx.strokeRect(-34, -6, 24, 12);
  ctx.fillRect(10, -6, 24, 12); ctx.strokeRect(10, -6, 24, 12);
  // panel cell lines
  ctx.strokeStyle = 'rgba(140,200,255,.4)';
  for (let i = 1; i < 4; i++) {
    ctx.beginPath(); ctx.moveTo(-34 + i * 6, -6); ctx.lineTo(-34 + i * 6, 6); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(10 + i * 6, -6); ctx.lineTo(10 + i * 6, 6); ctx.stroke();
  }
  // body
  ctx.fillStyle = '#c8a94a';
  ctx.fillRect(-9, -9, 18, 18);
  ctx.strokeStyle = '#e8d190'; ctx.strokeRect(-9, -9, 18, 18);
  // dish
  ctx.beginPath(); ctx.arc(0, -13, 5, Math.PI, 0);
  ctx.fillStyle = '#d8dde8'; ctx.fill();
  ctx.restore();

  // signal pulse ring
  const pulse = (performance.now() / 900) % 1;
  ctx.strokeStyle = `rgba(37,214,245,${0.5 * (1 - pulse)})`;
  ctx.lineWidth = 1.5;
  ctx.beginPath(); ctx.arc(sx, sy, 14 + pulse * 26, 0, 7); ctx.stroke();

  requestAnimationFrame(drawSpace);
}

/* ---------------- Boot ---------------- */
applyAccent();
buildChartGrid();
renderWorkflow();
wfReset();
renderNotifs();
renderSimHistory();
addLog('INFO', 'VORTEX dashboard initialized — establishing uplink…');
connectWS();
requestAnimationFrame(drawSpace);
