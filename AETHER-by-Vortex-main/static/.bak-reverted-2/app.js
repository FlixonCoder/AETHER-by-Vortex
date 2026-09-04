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
let latestRollingStats = {};  // param -> rolling stat object
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
    // T2: stop SGP4 propagation and globe rendering when the map is hidden.
    if (window.orbitMapSetPageActive) window.orbitMapSetPageActive(btn.dataset.page === 'orbitmap');
    if (btn.dataset.page === 'telemetry') { drawAllCharts(); renderRollingAnalysis(); }
    if (btn.dataset.page === 'anomalies') renderAnomalies();
    if (btn.dataset.page === 'memory') loadMemory();
    if (btn.dataset.page === 'audit') loadAuditLogs();
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

function updateAIModeBadge(mode, display) {
  const chip = $('aiModeChip');
  const dot = $('aiModeDot');
  const text = $('aiModeText');
  if (!chip || !text) return;
  text.textContent = display || mode || 'AI: READY';
  if (mode === 'LOCAL') {
    dot.className = 'dot dot-cyan';
    chip.style.borderColor = 'rgba(37,214,245,.4)';
  } else if (mode === 'CLOUD') {
    dot.className = 'dot dot-amber';
    chip.style.borderColor = 'rgba(245,158,11,.4)';
  } else {
    dot.className = 'dot dot-purple';
    chip.style.borderColor = 'rgba(192,132,252,.4)';
  }
}

function handleMessage(msg) {
  const d = msg.data || {};
  switch (msg.type) {
    case 'llm_mode_update':
      updateAIModeBadge(d.mode, d.display);
      toast(`AI Tier Active: ${d.display}`, 'info');
      break;
    case 'connected': onConnected(d); break;
    case 'telemetry_update':
      onTelemetry(d, msg.timestamp);
      if (d.llm_display || d.llm_mode) updateAIModeBadge(d.llm_mode, d.llm_display);
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
    case 'fix_options_ready': onFixOptions(d, msg.timestamp); break;
    case 'simulation_complete': onSimulationComplete(d, msg.timestamp); break;
    case 'validation_decision': onValidationDecision(d, msg.timestamp); break;
    case 'command_executed': onCommandExecuted(d, msg.timestamp); break;
    case 'post_monitor_result': onPostMonitorResult(d, msg.timestamp); break;
    case 'incident_resolved': onIncidentResolved(d, msg.timestamp); break;
    case 'approval_required': onApprovalRequired(d, msg.timestamp); break;
    case 'approval_decision': onApprovalDecision(d, msg.timestamp); break;
    case 'runbook_ready': onRunbookReady(d, msg.timestamp); break;
  }
}

function onConnected(status) {
  if (status.llm_info) {
    updateAIModeBadge(status.llm_info.mode, status.llm_info.display);
  }
  $('setBackendInfo').textContent = `AETHER Multi-Agent Satellite Server · Mode: ${status.llm_mode || 'LOCAL'}`;
  (status.activity_log || []).forEach(a =>
    addLog(a.level === 'warning' ? 'WARN' : a.level === 'success' ? 'OK' : 'INFO', `${a.agent}: ${a.message}`, a.timestamp));
  (status.active_anomalies || []).forEach(a => { anomalies.set(a.id, { ...a, status: 'DETECTED' }); });
  runbooks = status.runbooks || [];
  runbooksIssued = runbooks.length;
  renderAnomalies(); renderRunbookList();
  loadScenarios();
  loadMemory();
  loadAuditLogs();
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

  // Update rolling analysis display
  if (d.rolling_stats && Object.keys(d.rolling_stats).length) {
    latestRollingStats = d.rolling_stats;
    if ($('page-telemetry').classList.contains('active')) renderRollingAnalysis();
  }

  renderOverview();
  renderHealth();
  if ($('page-telemetry').classList.contains('active') && !telemetryPaused) { drawAllCharts(); renderPackets(); }
}
function hashCode(s) { let h = 0; for (let i = 0; i < s.length; i++) { h = (h << 5) - h + s.charCodeAt(i); h |= 0; } return h; }

/* ---------------- Rolling Telemetry Analysis Card ---------------- */
const ROLLING_STATUS_LABELS = {
  NORMAL:             'NORMAL',
  TRANSIENT_SPIKE:    'SPIKE',
  PERSISTENT_ANOMALY: 'ANOMALY',
  EMERGENCY:          'EMERGENCY',
};

function zScoreClass(z) {
  const az = Math.abs(z);
  if (az >= 4.5) return 'crit';
  if (az >= 2.5) return 'warn';
  return 'ok';
}

function renderRollingAnalysis() {
  const grid = $('rollingGrid');
  if (!grid) return;
  const stats = latestRollingStats;
  if (!stats || !Object.keys(stats).length) {
    grid.innerHTML = '<div class="empty-msg">Awaiting telemetry samples\u2026</div>';
    return;
  }

  const paramOrder = Object.keys(PARAMS); // keep canonical order
  const ordered = paramOrder.filter(p => stats[p]).concat(
    Object.keys(stats).filter(p => !PARAMS[p])  // unknown params last
  );

  ordered.forEach(param => {
    const s = stats[param];
    if (!s) return;
    const meta = PARAMS[param] || { label: param, unit: '' };
    const label = meta.label || param;
    const unit = meta.unit || '';
    const statusKey = s.status || 'NORMAL';
    const statusLabel = ROLLING_STATUS_LABELS[statusKey] || statusKey;
    const zCls = zScoreClass(s.z_score);

    // Z-score bar: 0-100% maps to 0-CRITICAL_Z (cap at 6 for display)
    const zBarPct = Math.min(100, Math.abs(s.z_score) / 6 * 100).toFixed(1);

    // Rate of change display
    const rocSign = s.rate_of_change >= 0 ? '+' : '';
    const rocStr = `${rocSign}${s.rate_of_change.toFixed(3)} ${unit}/s`;

    // Persistence display
    const ptick = s.persistence_ticks || 0;

    let tile = document.getElementById('rp-' + param);
    if (!tile) {
      tile = document.createElement('div');
      tile.className = 'rolling-param';
      tile.id = 'rp-' + param;
      grid.appendChild(tile);
    }

    // Only re-render innerHTML if status changed (avoids constant DOM churn)
    const prevStatus = tile.dataset.status;
    if (prevStatus !== statusKey || true /* always refresh values */) {
      tile.dataset.status = statusKey;
      tile.innerHTML = `
        <span class="rp-status ${statusKey}">${statusLabel}</span>
        <div class="rp-name">${esc(label)}</div>
        <div class="rp-rows">
          <div class="rp-row"><span class="k">Current</span><span class="v ${zCls}">${s.current.toFixed(3)} ${unit}</span></div>
          <div class="rp-row"><span class="k">Mean</span><span class="v">${s.mean.toFixed(3)} ${unit}</span></div>
          <div class="rp-row"><span class="k">Std Dev</span><span class="v">${s.std_dev.toFixed(3)}</span></div>
          <div class="rp-row"><span class="k">Z-Score</span><span class="v ${zCls}">${s.z_score.toFixed(2)} σ</span></div>
          <div class="rp-row"><span class="k">Range</span><span class="v">${s.min.toFixed(2)} – ${s.max.toFixed(2)}</span></div>
          <div class="rp-row"><span class="k">Rate</span><span class="v">${rocStr}</span></div>
          <div class="rp-row"><span class="k">Samples</span><span class="v">${s.sample_count} (${s.window_seconds.toFixed(0)}s)</span></div>
        </div>
        <div class="rp-zbar"><div class="rp-zfill ${zCls}" style="width:${zBarPct}%"></div></div>
        <div class="rp-persist">Persistence: <b>${ptick}</b> tick${ptick !== 1 ? 's' : ''}</div>`;
    }
  });

  // Remove tiles for params that no longer exist in stats
  grid.querySelectorAll('.rolling-param').forEach(el => {
    const p = el.id.replace('rp-', '');
    if (!stats[p]) el.remove();
  });
}



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

  // T3: if a reference satellite has been picked on the Orbit Map, these two
  // readouts show its real SGP4 state. Otherwise they fall back to LYRA-1's
  // simulated orbit, which is a closed-form approximation, not telemetry.
  const ref = window.orbitMapReferenceState ? window.orbitMapReferenceState() : null;
  if (ref) {
    setReadout('ovAlt', ref.altKm.toFixed(1) + ' km');
    setReadout('ovVel', (ref.speedKms * 3600).toFixed(0) + ' km/h');
  } else {
    const phase = (latestOrbital.orbital_phase_pct || 0) / 100 * 2 * Math.PI;
    setReadout('ovAlt', (512.6 + 2.2 * Math.sin(phase)).toFixed(1) + ' km');
    setReadout('ovVel', (7.56 + 0.008 * Math.cos(phase)).toFixed(2) + ' km/s');
  }

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
  if (!$('page-telemetry').classList.contains('active')) return;
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
  { name: 'Telemetry Ingest', ico: '📡' },
  { name: 'Watcher Detection', ico: '👁️' },
  { name: 'Criticality Scoring', ico: '⚖️' },
  { name: 'Identifier Agent', ico: '🔍' },
  { name: 'Fix Finder Agent', ico: '🧩' },
  { name: 'Digital Twin Sim', ico: '🧪' },
  { name: 'Safety Gate', ico: '🛡️' },
  { name: 'Execute & Verify', ico: '🚀' },
];
const wfState = { stage: 0, states: WF_STAGES.map(() => 'pending'), times: WF_STAGES.map(() => null), msg: 'Awaiting telemetry…', etaT: null };

function wfSet(idx, state, msg, ts) {
  if (state === 'done' && wfState.states[idx] === 'done') { if (msg) wfUpdateMsg(msg); return; }
  wfState.states[idx] = state;
  if (state === 'done') {
    wfState.times[idx] = ts || new Date();
    for (let i = 0; i < idx; i++) {
      if (wfState.states[i] !== 'done') { wfState.states[i] = 'done'; wfState.times[i] = wfState.times[i] || new Date(); }
    }
  }
  if (state === 'active') { wfState.stage = idx; wfState.etaT = Date.now() + 6000; }
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
  const scoreStr = a.criticality_score != null ? ` (Score: ${a.criticality_score}/100)` : '';
  addLog('ERROR', `ANOMALY ${a.id} — ${a.summary || a.anomaly_type} [${a.severity}]${scoreStr}`, ts);
  notify(`Anomaly detected: ${a.anomaly_type || 'threshold violation'} [${a.severity}]${scoreStr}`, 'error');
  wfSet(1, 'done', null, ts);
  wfSet(2, 'done', `Criticality score evaluated: ${a.criticality_score ?? '—'}/100 (${a.severity})`, ts);
  wfSet(3, 'active', 'Identifier Agent isolating root cause & hypotheses…');
  renderAnomalies();
}

function onAgentActivity(d, ts) {
  addLog('INFO', `${d.agent}: ${d.message}`, ts);
  const agentLog = $('agentLog');
  const stick = agentLog.scrollTop + agentLog.clientHeight >= agentLog.scrollHeight - 30;
  agentLog.innerHTML += `<div class="log-line"><span class="log-time">${fmtTime(ts)}</span>
    <span class="log-level INFO">[${esc(d.agent)}]</span><span class="log-msg">${esc(d.message)}</span></div>`;
  if (stick) agentLog.scrollTop = agentLog.scrollHeight;

  if (d.agent === 'IDENTIFIER') wfSet(3, 'active', 'Identifier formulating diagnostic hypotheses…');
  if (d.agent === 'FIX_FINDER') { wfSet(3, 'done', null, ts); wfSet(4, 'active', 'Fix Finder retrieving candidate procedures…'); }
  if (d.agent === 'SIMULATOR') { wfSet(4, 'done', null, ts); wfSet(5, 'active', 'Running digital twin forward simulations…'); }
  if (d.agent === 'SAFETY_GATE') { wfSet(5, 'done', null, ts); wfSet(6, 'active', 'Safety Gate verifying deterministic policy…'); }
  if (d.agent === 'EXECUTOR') { wfSet(6, 'done', null, ts); wfSet(7, 'active', 'Executing authorized commands on spacecraft…'); }
  if (d.agent === 'POST_MONITOR') wfSet(7, 'active', 'Verifying post-execution telemetry recovery…');
}

function onDiagnosis(d, ts) {
  addLog('OK', `Diagnosis ${d.incident_id || d.anomaly_id}: ${d.root_cause}`, ts);
  const a = anomalies.get(d.incident_id || d.anomaly_id);
  if (a) { a.diagnosis = d; a.status = 'DIAGNOSED'; renderAnomalies(); }
}

function onFixOptions(d, ts) {
  const incId = d.incident_id || d.anomaly_id;
  const a = anomalies.get(incId);
  if (a) { a.fix_options = d; a.status = 'FIXES GENERATED'; renderAnomalies(); }
  addLog('OK', `Fix Finder generated ${d.candidates?.length || 0} candidate action(s)`, ts);
}

function onSimulationComplete(d, ts) {
  addLog('OK', `Digital Twin evaluated ${d.simulations?.length || 0} simulation trajectory(ies)`, ts);
  wfSet(5, 'done', 'Digital twin simulations verified safe', ts);
  wfSet(6, 'active', 'Deterministic Safety Gate checking policy…');
}

function onValidationDecision(d, ts) {
  const val = d.validation || {};
  addLog('OK', `Safety Gate decision: ${val.decision} (Approved: ${val.approved_for_execution})`, ts);
  wfSet(6, 'done', `Safety gate cleared: ${val.decision}`, ts);
}

function onCommandExecuted(d, ts) {
  const ex = d.execution || {};
  addLog('OK', `Command dispatch complete: ${ex.status}`, ts);
  wfSet(7, 'active', 'Commands dispatched — awaiting telemetry stabilization…');
}

function onPostMonitorResult(d, ts) {
  const res = d.result || {};
  if (res.recovered) {
    addLog('OK', `Verification PASS: ${res.status_message}`, ts);
    wfSet(7, 'done', 'Telemetry restored to nominal limits', ts);
  } else {
    addLog('WARN', `Verification FAIL: ${res.status_message} — cycling re-diagnosis`, ts);
    notify(`Recovery criteria not met (Attempt #${res.attempt_number}) — re-diagnosing`, 'warn');
    wfSet(3, 'active', `Re-diagnosis loop initiated (Attempt #${res.attempt_number + 1})…`);
  }
}

function onIncidentResolved(d, ts) {
  const incId = d.incident_id || d.anomaly_id;
  const a = anomalies.get(incId);
  if (a) a.status = d.outcome === 'RECOVERED' ? 'RESOLVED' : 'ESCALATED';
  addLog('OK', `Incident ${incId} concluded [${d.outcome}] & indexed into RAG memory`, ts);
  notify(`Incident ${incId} resolved and stored in RAG memory`, 'info');
  wfSet(7, 'done', `Incident resolved: ${d.outcome}`, ts);
  renderAnomalies();
  loadMemory();
  loadAuditLogs();
  setTimeout(() => wfReset(), 7000);
}

function onApprovalRequired(d, ts) {
  pendingApprovals.set(d.incident_id || d.anomaly_id, d);
  const a = anomalies.get(d.incident_id || d.anomaly_id);
  if (a) a.status = 'AWAITING APPROVAL';
  addLog('WARN', `Operator approval required for ${d.incident_id || d.anomaly_id} [${d.severity}] (Score: ${d.criticality_score}/100)`, ts);
  notify(`Approval required: ${d.incident_id || d.anomaly_id} — ${d.severity} (Score ${d.criticality_score}/100)`, 'warn');
  wfSet(6, 'active', `⚠ ${d.severity} anomaly — awaiting human approval on Anomalies page`);
  renderAnomalies(); renderApproval();
}

function onApprovalDecision(d, ts) {
  const incId = d.incident_id || d.anomaly_id;
  const a = anomalies.get(incId);
  if (a) a.status = d.approved ? 'APPROVED' : 'DENIED';
  addLog(d.approved ? 'OK' : 'WARN', d.approved ? `Action approved for ${incId}` : `Action DENIED for ${incId}: ${d.reason || ''}`, ts);
  pendingApprovals.delete(incId);
  renderAnomalies(); renderApproval();
}

function onRunbookReady(d, ts) {
  runbooksIssued++;
  runbooks.push({ filename: d.filename, anomaly_id: d.anomaly_id, generated_at: d.generated_at, content: d.content });
  renderRunbookList();
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
  $('anoBody').innerHTML = shown.map(a => {
    const scoreTag = a.criticality_score != null ? `<span class="crit-score">${a.criticality_score}/100</span>` : '';
    const ragTag = (a.rag_matches && a.rag_matches.length > 0) ? `<span class="tag tag-green" title="Matched ${a.rag_matches.length} RAG incident(s)">🧠 RAG (${a.rag_matches.length})</span>` : '';
    return `<tr>
      <td>${fmtTime(a.detected_at)}</td>
      <td style="font-family:var(--mono);font-size:11px">${esc(a.id || a.incident_id)}</td>
      <td><span class="sev ${esc(a.severity)}">${esc(a.severity)}</span> ${scoreTag}</td>
      <td>${esc(a.primary_subsystem || a.affected_subsystem || '—')}</td>
      <td style="white-space:normal;max-width:360px">
        <div>${esc(a.summary || a.anomaly_type || '—')}</div>
        <div style="margin-top:3px">${ragTag}</div>
      </td>
      <td><span class="tag ${a.status === 'RESOLVED' ? 'tag-green' : a.status === 'AWAITING APPROVAL' ? 'tag-amber' : ''}">${esc(a.status)}</span></td>
    </tr>`;
  }).join('');
}
$('anoFilter').addEventListener('change', renderAnomalies);

function renderApproval() {
  const card = $('approvalCard');
  const entry = pendingApprovals.values().next().value;
  if (!entry) { card.style.display = 'none'; return; }
  card.style.display = 'block';

  const isCritical = entry.severity === 'CRITICAL' || entry.criticality_score >= 90;
  const anoId = entry.incident_id || entry.anomaly_id;
  $('apprAnoId').textContent = `${anoId} [${entry.severity} — Score ${entry.criticality_score}/100]`;

  let bannerHTML = '';
  if (isCritical) {
    bannerHTML = `<div class="appr-critical-banner">
      <span class="icon">🛑</span>
      <div class="txt">
        <b>CRITICAL ANOMALY — COMMANDER AUTHORIZATION REQUIRED</b><br>
        Autonomous execution inhibited by Deterministic Safety Gate. Spacecraft stability requires explicit human authorization.
      </div>
    </div>`;
  } else {
    bannerHTML = `<div class="appr-critical-banner" style="background:rgba(245,158,11,.15);border-color:rgba(245,158,11,.4)">
      <span class="icon">⚠️</span>
      <div class="txt">
        <b>HIGH SEVERITY ANOMALY — OPERATOR OVERSIGHT CONFIRMATION</b><br>
        Digital twin simulation verified safe. Operator approval requested prior to dispatching command sequence.
      </div>
    </div>`;
  }

  $('apprDiag').innerHTML = `${bannerHTML}
    <p><b>Diagnostic Root Cause:</b> ${esc(entry.diagnosis?.root_cause || 'Root cause verified.')}</p>
    <p><b>Subsystem:</b> ${esc(entry.diagnosis?.subsystem || '—')} &nbsp;·&nbsp; <b>Confidence:</b> ${Math.round((entry.diagnosis?.confidence || 0.9) * 100)}%</p>`;

  const opts = entry.candidates || [entry.candidate];
  $('apprOptions').innerHTML = opts.map(o => {
    const cmdList = (o.commands || []).map(c => `<code>${esc(c.command)}</code>`).join(', ');
    return `
      <div class="appr-opt rec" style="${isCritical ? 'border-color:rgba(239,68,68,.6)' : ''}">
        <div class="appr-opt-head">
          <span class="nm">${esc(o.name || 'Recovery Procedure')}</span>
          <span class="sev ${esc(o.risk || o.risk_level || 'LOW')}">${esc(o.risk || o.risk_level || 'LOW')} RISK</span>
        </div>
        <div class="desc" style="font-size:12px;color:var(--muted);margin:6px 0">${esc(o.description || '')}</div>
        <div class="appr-meta">
          <span>🎯 Commands: ${cmdList}</span>
          <span>✅ ${Math.round((o.estimated_recovery_probability || 0.9) * 100)}% recovery</span>
          <span>↩ ${o.reversible ? 'Reversible' : 'Irreversible'}</span>
        </div>
        <div class="appr-actions-row">
          <button class="btn sm ${isCritical ? 'danger' : ''}" onclick="approveProc('${esc(anoId)}','${esc(o.action_id)}')">
            ${isCritical ? '🛡️ AUTHORIZE EXECUTION' : '✓ Approve Procedure'}
          </button>
          <button class="btn sm" style="background:rgba(255,255,255,.08)" onclick="rejectProc('${esc(anoId)}')">
            ✕ Reject / Safe Hold
          </button>
        </div>
      </div>`;
  }).join('');
}

window.approveProc = async (id, actionId) => {
  try {
    const url = actionId ? `/api/approve/${id}?action_id=${actionId}` : `/api/approve/${id}`;
    await fetch(url, { method: 'POST' });
    addLog('OK', `Authorization transmitted for ${id}`);
    pendingApprovals.delete(id);
    renderApproval();
  } catch (e) { addLog('ERROR', 'Approval failed: ' + e.message); }
};

window.rejectProc = async (id) => {
  try {
    await fetch(`/api/reject/${id}`, { method: 'POST' });
    addLog('WARN', `Manual rejection sent for ${id} — holding in safe configuration`);
    pendingApprovals.delete(id);
    renderApproval();
  } catch (e) { addLog('ERROR', 'Rejection failed: ' + e.message); }
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
    runbookSource = d.content || '';
    paintRunbook();
    renderRunbookList();
  } catch (e) { addLog('ERROR', 'Failed to load runbook: ' + e.message); }
};
$('runbookClose').addEventListener('click', () => { $('runbookView').style.display = 'none'; currentRunbook = null; renderRunbookList(); });
$('runbookDl').addEventListener('click', () => { if (currentRunbook) download(currentRunbook, runbookSource, 'text/markdown'); });

/* ---------------- T5: runbook markdown rendering ---------------- */
let runbookSource = '';
let runbookView = 'rendered';

function paintRunbook() {
  const body = $('runbookBody');
  if (runbookView === 'rendered') {
    body.classList.add('md-rendered');
    body.innerHTML = AetherMD.render(runbookSource);
  } else {
    body.classList.remove('md-rendered');
    body.textContent = runbookSource || '(empty)';
  }
  $('mdViewRendered').classList.toggle('active', runbookView === 'rendered');
  $('mdViewSource').classList.toggle('active', runbookView === 'source');
}

$('mdViewRendered').addEventListener('click', () => { runbookView = 'rendered'; paintRunbook(); });
$('mdViewSource').addEventListener('click', () => { runbookView = 'source'; paintRunbook(); });

/* Cross-fade a readout instead of snapping it. Used by the Overview panel. */
function setReadout(id, text) {
  const el = $(id);
  if (!el || el.textContent === text) return;
  el.textContent = text;
  el.classList.remove('val-swap');
  void el.offsetWidth;          // force reflow so the animation re-runs
  el.classList.add('val-swap');
}
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
if ($('aiModeChip')) {
  $('aiModeChip').addEventListener('click', async () => {
    try {
      const r = await fetch('/api/llm-mode');
      const d = await r.json();
      notify(`Active AI: ${d.display} · Details: ${d.details}`, 'info');
    } catch (e) {}
  });
}

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

/* ---------------- RAG Memory UI ---------------- */
let memActiveTab = 'incidents'; // 'incidents' | 'procedures'
let memIncidents = [];
let memProcedures = [];

async function loadMemory() {
  try {
    const statsRes = await fetch('/api/memory/stats');
    const stats = await statsRes.json();
    if ($('memTotalInc')) $('memTotalInc').textContent = stats.total_incidents || 0;
    if ($('memRecovered')) $('memRecovered').textContent = stats.recovered_incidents || 0;
    if ($('memProcedures')) $('memProcedures').textContent = stats.total_procedures || 0;

    const incRes = await fetch('/api/memory/incidents?k=25');
    const incData = await incRes.json();
    memIncidents = incData.incidents || [];

    const procRes = await fetch('/api/memory/procedures');
    const procData = await procRes.json();
    memProcedures = procData.procedures || [];

    renderMemory();
  } catch (e) {
    console.error('Failed to load memory:', e);
  }
}

function renderMemory() {
  const container = $('memContent');
  if (!container) return;
  const q = ($('memSearch')?.value || '').toLowerCase().trim();

  if (memActiveTab === 'incidents') {
    const list = q ? memIncidents.filter(i => (i.anomaly + ' ' + i.root_cause + ' ' + i.subsystem + ' ' + (i.solution||'')).toLowerCase().includes(q)) : memIncidents;
    if (!list.length) {
      container.innerHTML = '<div class="empty-msg show">No episodic incidents matching search</div>';
      return;
    }
    container.innerHTML = list.slice().reverse().map(inc => {
      const simScore = inc.similarity_score != null ? `<span class="tag tag-green">Match ${(inc.similarity_score * 100).toFixed(0)}%</span>` : '';
      const lessons = (inc.lessons_learned || []).map(l => `<li>${esc(l)}</li>`).join('');
      return `<div class="mem-card">
        <div class="mem-head">
          <span class="mem-id">${esc(inc.incident_id)}</span>
          <div>${simScore} <span class="sev ${esc(inc.criticality || 'LOW')}">${esc(inc.criticality || 'LOW')}</span></div>
        </div>
        <div class="mem-title">${esc(inc.anomaly || 'Satellite Anomaly')}</div>
        <div class="mem-desc"><b>Root Cause:</b> ${esc(inc.root_cause || '—')}</div>
        <div class="mem-desc"><b>Solution:</b> ${esc(inc.solution || '—')} (Outcome: <b style="color:${inc.outcome === 'RECOVERED' ? 'var(--green)' : 'var(--amber)'}">${esc(inc.outcome || 'RECOVERED')}</b>)</div>
        ${lessons ? `<ul style="font-size:11px;color:var(--muted);padding-left:16px;margin-bottom:8px">${lessons}</ul>` : ''}
        <div class="mem-tags">
          <span class="mem-tag">Subsystem: ${esc(inc.subsystem || '—')}</span>
          <span class="mem-tag">${fmtTime(inc.timestamp)}</span>
        </div>
      </div>`;
    }).join('');
  } else {
    const list = q ? memProcedures.filter(p => (p.name + ' ' + p.subsystem + ' ' + p.anomaly_pattern + ' ' + p.description).toLowerCase().includes(q)) : memProcedures;
    if (!list.length) {
      container.innerHTML = '<div class="empty-msg show">No procedural rules matching search</div>';
      return;
    }
    container.innerHTML = list.map(proc => {
      const cmds = (proc.commands || []).map(c => `<code>${esc(c.command)}</code>`).join(' &rarr; ');
      return `<div class="mem-card">
        <div class="mem-head">
          <span class="mem-id">${esc(proc.procedure_id)}</span>
          <span class="sev ${esc(proc.risk || 'LOW')}">${esc(proc.risk || 'LOW')} RISK</span>
        </div>
        <div class="mem-title">${esc(proc.name || 'Operational Procedure')}</div>
        <div class="mem-desc">${esc(proc.description || '')}</div>
        <div class="mem-desc"><b>Trigger Pattern:</b> <i>${esc(proc.anomaly_pattern || '—')}</i></div>
        <div class="mem-desc"><b>Commands:</b> ${cmds || 'None'}</div>
        <div class="mem-tags">
          <span class="mem-tag">Subsystem: ${esc(proc.subsystem || '—')}</span>
          <span class="mem-tag">Success: ${Math.round((proc.success_rate || 0.9) * 100)}%</span>
          <span class="mem-tag">${proc.reversible ? 'Reversible' : 'Irreversible'}</span>
        </div>
      </div>`;
    }).join('');
  }
}

if ($('memTabInc')) {
  $('memTabInc').addEventListener('click', () => {
    memActiveTab = 'incidents';
    $('memTabInc').style.borderColor = 'var(--accent)';
    if ($('memTabProc')) $('memTabProc').style.borderColor = 'var(--line)';
    renderMemory();
  });
}
if ($('memTabProc')) {
  $('memTabProc').addEventListener('click', () => {
    memActiveTab = 'procedures';
    $('memTabProc').style.borderColor = 'var(--accent)';
    if ($('memTabInc')) $('memTabInc').style.borderColor = 'var(--line)';
    renderMemory();
  });
}
if ($('memSearch')) $('memSearch').addEventListener('input', renderMemory);
if ($('memRefresh')) $('memRefresh').addEventListener('click', () => { loadMemory(); toast('RAG Memory refreshed', 'ok'); });

/* ---------------- Audit Trail UI ---------------- */
let auditEntries = [];
async function loadAuditLogs() {
  try {
    const res = await fetch('/api/audit?limit=60');
    const data = await res.json();
    auditEntries = data.audit_logs || [];
    renderAuditTable();
  } catch (e) {
    console.error('Failed to load audit logs:', e);
  }
}

function renderAuditTable() {
  const tbody = $('auditBody');
  if (!tbody) return;
  const q = ($('auditSearch')?.value || '').toLowerCase().trim();
  const list = q ? auditEntries.filter(a => (a.incident_id + ' ' + a.agent + ' ' + a.action + ' ' + (a.llm_mode || '')).toLowerCase().includes(q)) : auditEntries;

  $('auditEmpty')?.classList.toggle('show', list.length === 0);
  tbody.innerHTML = list.slice().reverse().map(e => {
    const crit = e.criticality ? `<span class="crit-score">${e.criticality.score ?? '—'}/100</span>` : '—';
    const decision = e.validator_result?.decision || e.output?.outcome || e.final_outcome || e.action;
    return `<tr>
      <td>${fmtTime(e.timestamp)}</td>
      <td style="font-family:var(--mono);font-size:11px">${esc(e.incident_id)}</td>
      <td><span class="badge-agent">${esc(e.agent)}</span></td>
      <td><b>${esc(e.action)}</b></td>
      <td>${crit}</td>
      <td><span class="tag" style="font-size:10px">${esc(e.llm_mode || 'RULE_BASED')}</span></td>
      <td style="font-size:11.5px;max-width:280px;white-space:normal">${esc(decision)}</td>
    </tr>`;
  }).join('');
}

if ($('auditSearch')) $('auditSearch').addEventListener('input', renderAuditTable);
if ($('auditRefresh')) $('auditRefresh').addEventListener('click', () => { loadAuditLogs(); toast('Audit log refreshed', 'ok'); });
if ($('auditDl')) $('auditDl').addEventListener('click', () => {
  download(`aether_audit_${Date.now()}.jsonl`, auditEntries.map(e => JSON.stringify(e)).join('\n'), 'application/json');
});

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
