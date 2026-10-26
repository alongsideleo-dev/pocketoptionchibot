/* ── POCKET OPTION Dashboard Frontend ───────────────────────── */

// ── State ────────────────────────────────────────────────
let _state       = null;
let _queue       = [];
let _countdownId = null;
let _pollId      = null;
let _theme       = 'dark';
let _socket      = null;

// ── DOM helpers ──────────────────────────────────────────
const $ = id => document.getElementById(id);
const escHtml = s => String(s)
  .replace(/&/g, '&amp;')
  .replace(/</g, '&lt;')
  .replace(/>/g, '&gt;')
  .replace(/"/g, '&quot;');

// ── Theme ─────────────────────────────────────────────────
function applyTheme(t) {
  _theme = t;
  document.documentElement.setAttribute('data-theme', t);
  try { localStorage.setItem('theme', t); } catch (_) {}
  const icon = $('theme-icon');
  if (icon) icon.textContent = (t === 'dark') ? '☀' : '🌙';
}

function initTheme() {
  let saved = 'dark';
  try { saved = localStorage.getItem('theme') || 'dark'; } catch (_) {}
  applyTheme(saved);
  $('btn-theme')?.addEventListener('click', () => {
    applyTheme(_theme === 'dark' ? 'light' : 'dark');
  });
}

// ── Tabs ─────────────────────────────────────────────────
function initTabs() {
  document.querySelectorAll('.tabs .tab').forEach(tab => {
    tab.addEventListener('click', () => {
      const target = tab.getAttribute('data-tab');
      document.querySelectorAll('.tabs .tab').forEach(t => t.classList.remove('active'));
      document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
      tab.classList.add('active');
      $(`page-${target}`)?.classList.add('active');
      if (target === 'settings') loadSettings();
    });
  });
}

// ── Bot control ──────────────────────────────────────────
let _botActionBusy = false;

function setBtnStarting() {
  const btn = $('btn-toggle');
  if (!btn) return;
  btn.disabled = true;
  btn.className = 'btn-toggle loading';
  btn.innerHTML = '<div class="spinner"></div><div class="btn-text">STARTING...</div>';
  const hint = $('bot-status-text');
  if (hint) hint.textContent = 'Connecting to Telegram & Pocket Option...';
  updateHeaderStatus('starting');
}

function setBtnStopping() {
  const btn = $('btn-toggle');
  if (!btn) return;
  btn.disabled = true;
  btn.className = 'btn-toggle loading';
  btn.innerHTML = '<div class="spinner"></div><div class="btn-text">STOPPING...</div>';
  const hint = $('bot-status-text');
  if (hint) hint.textContent = 'Stopping trading bot...';
  updateHeaderStatus('stopping');
}

function updateBotButton(statusOrRunning) {
  let status = statusOrRunning;
  if (typeof statusOrRunning === 'boolean') {
    status = statusOrRunning ? 'running' : 'stopped';
  }

  // If user just initiated an action, don't allow background polling to wipe out the spinner
  if (_botActionBusy) {
    if (status === 'running' || status === 'stopped') {
      _botActionBusy = false;
    } else {
      return;
    }
  }

  const btn = $('btn-toggle');
  const hint = $('bot-status-text');
  if (!btn) return;

  if (status === 'starting') {
    btn.disabled = true;
    btn.className = 'btn-toggle loading';
    btn.innerHTML = '<div class="spinner"></div><div class="btn-text">STARTING...</div>';
    if (hint) hint.textContent = 'Connecting to Telegram & Pocket Option...';
    updateHeaderStatus('starting');
    return;
  }

  if (status === 'stopping') {
    btn.disabled = true;
    btn.className = 'btn-toggle loading';
    btn.innerHTML = '<div class="spinner"></div><div class="btn-text">STOPPING...</div>';
    if (hint) hint.textContent = 'Stopping trading bot...';
    updateHeaderStatus('stopping');
    return;
  }

  const isRunning = (status === 'running');
  btn.disabled = false;
  btn.className = 'btn-toggle ' + (isRunning ? 'running' : 'stopped');
  btn.innerHTML = `
    <div class="btn-icon">${isRunning ? '■' : '▶'}</div>
    <div class="btn-text">${isRunning ? 'STOP' : 'START'}</div>
  `;
  if (hint) {
    hint.textContent = isRunning
      ? 'Bot is active — monitoring Telegram for trading signals'
      : 'Click START to launch the bot';
  }
  updateHeaderStatus(isRunning ? 'running' : 'stopped');
}

function initBotToggle() {
  const btnToggle = $('btn-toggle');
  if (!btnToggle) return;
  btnToggle.addEventListener('click', async () => {
    if (_botActionBusy || btnToggle.classList.contains('loading')) {
      return;
    }
    const isRunning = btnToggle.classList.contains('running');
    const endpoint  = isRunning ? '/api/bot/stop' : '/api/bot/start';

    _botActionBusy = true;
    if (isRunning) {
      setBtnStopping();
    } else {
      setBtnStarting();
    }

    try {
      const res = await api(endpoint, 'POST');
      if (!res.success && res.message) {
        showToast(res.message, 'error');
        _botActionBusy = false;
        fetchStatus();
      }
    } catch (e) {
      showToast('Error: ' + e.message, 'error');
      _botActionBusy = false;
      fetchStatus();
    }
  });
}

// ── Formatting helpers ────────────────────────────────────
function fmtMoney(n) {
  if (n === null || n === undefined || isNaN(n)) return '—';
  const v = Number(n);
  const s = Math.abs(v).toFixed(2);
  if (v < 0) return '-$' + s;
  return '$' + s;
}

function fmtCountdown(sec) {
  if (sec === null || sec === undefined) return '--:--';
  const total = Math.max(0, Math.floor(sec));
  const m = Math.floor(total / 60);
  const s = total % 60;
  return `${m}:${s < 10 ? '0' : ''}${s}`;
}

function setBar(id, current, max) {
  const bar = $(id);
  if (!bar) return;
  if (!max || max <= 0) {
    bar.style.width = '0%';
    return;
  }
  const pct = Math.min(100, Math.max(0, (current / max) * 100));
  bar.style.width = pct.toFixed(1) + '%';
}

// ── Metrics ───────────────────────────────────────────────
function updateMetrics(s) {
  if (!s) return;

  // Balance & Account badge
  const balEl = $('metric-balance');
  const balSub = $('metric-balance-sub');
  const badge = $('acct-badge');
  const acct = (s.account_type || 'demo').toUpperCase();

  if (badge) {
    badge.textContent = acct;
    badge.className = 'acct-badge ' + (acct === 'LIVE' ? 'live' : '');
  }

  if (balEl) {
    balEl.textContent = (s.balance !== null && s.balance !== undefined)
      ? fmtMoney(s.balance)
      : '—';
  }
  if (balSub) {
    balSub.textContent = s.connections?.pocket_option || s.pocket_option_connected
      ? 'Active balance on Pocket Option'
      : 'Connect Pocket Option to see your balance';
  }

  // P&L Today
  const pnlEl = $('metric-pnl');
  if (pnlEl) {
    pnlEl.textContent = fmtMoney(s.daily_pnl || 0.0);
    pnlEl.className = 'metric-value ' + (s.daily_pnl > 0 ? 'text-green' : s.daily_pnl < 0 ? 'text-red' : '');
  }

  // Win Rate
  const wrEl = $('metric-winrate');
  const wrSub = $('metric-winrate-sub');
  const totalSettled = (s.wins || 0) + (s.losses || 0);
  if (wrEl) {
    if (totalSettled > 0) {
      const pct = Math.round((s.wins / totalSettled) * 100);
      wrEl.textContent = `${pct}%`;
      wrEl.className = 'metric-value ' + (pct >= 50 ? 'text-green' : 'text-red');
    } else {
      wrEl.textContent = '—';
      wrEl.className = 'metric-value';
    }
  }
  if (wrSub) {
    wrSub.textContent = totalSettled > 0
      ? `${s.wins}W / ${s.losses}L (${totalSettled} settled)`
      : 'No settled trades yet';
  }

  // Daily Trades
  const dtEl = $('metric-trades');
  const dtSub = $('metric-trades-sub');
  if (dtEl) dtEl.textContent = s.daily_trades || 0;
  if (dtSub) {
    dtSub.textContent = s.max_daily_trades ? `Limit: ${s.max_daily_trades}` : 'today';
  }
  setBar('bar-trades', s.daily_trades || 0, s.max_daily_trades || 0);

  // Daily Loss
  const dlEl = $('metric-loss');
  const dlSub = $('metric-loss-sub');
  if (dlEl) {
    dlEl.textContent = fmtMoney(s.daily_loss || 0.0);
    dlEl.className = 'metric-value ' + ((s.daily_loss || 0) > 0 ? 'text-red' : '');
  }
  if (dlSub) {
    dlSub.textContent = (s.max_daily_loss_enabled && s.max_daily_loss)
      ? `Limit: ${fmtMoney(s.max_daily_loss)}`
      : 'Limit disabled';
  }
  setBar('bar-loss', s.daily_loss || 0, s.max_daily_loss || 0);

  // In Flight
  const sigEl = $('metric-signals');
  const sigSub = $('metric-signals-sub');
  if (sigEl) sigEl.textContent = s.active_signals || 0;
  if (sigSub) {
    const act = s.active_trades || 0;
    sigSub.textContent = act > 0 ? `${act} live trade(s)` : 'scheduled & live trades';
  }
}

// ── Upcoming entries (countdown) ─────────────────────────
function setQueue(items) {
  _queue = (items || []).map(i => ({ ...i }));
  renderQueue();
  if (!_countdownId && _queue.length) {
    _countdownId = setInterval(tickCountdown, 1000);
  }
}

function tickCountdown() {
  let active = false;
  _queue.forEach(item => {
    if (item.seconds_left !== null && item.seconds_left > 0) {
      item.seconds_left = Math.max(0, item.seconds_left - 1);
      active = true;
    }
  });
  renderQueue();
  if (!active && _countdownId) {
    clearInterval(_countdownId);
    _countdownId = null;
  }
}

function renderQueue() {
  const box = $('queue');
  if (!box) return;
  if (!_queue.length) {
    box.innerHTML = '<div class="queue-empty">No signals scheduled — waiting for the next channel message.</div>';
    return;
  }
  box.innerHTML = _queue.map(item => {
    const isCall = (item.direction || '').toLowerCase() === 'call';
    return `
      <div class="queue-item">
        <div class="queue-asset">${escHtml(item.asset || '—')}</div>
        <div class="queue-badge ${isCall ? 'badge-call' : 'badge-put'}">
          ${isCall ? '▲ CALL' : '▼ PUT'}
        </div>
        <div class="queue-time">${escHtml(item.expiry || '')} @ ${escHtml(item.entry_time || '')}</div>
        <div class="queue-countdown">${fmtCountdown(item.seconds_left)}</div>
      </div>
    `;
  }).join('');
}

// ── Last trade ────────────────────────────────────────────
function renderLastTrade(t) {
  const box = $('last-trade');
  if (!box) return;
  if (!t) {
    box.innerHTML = '<div class="queue-empty">No trades placed yet.</div>';
    return;
  }
  const isCall = (t.direction || '').toLowerCase() === 'call';
  const res = (t.result || '').toLowerCase();
  const resBadge = res === 'win'
    ? '<span class="badge badge-win">WIN</span>'
    : res === 'loss'
      ? '<span class="badge badge-loss">LOSS</span>'
      : '<span class="badge badge-pending">PENDING</span>';

  box.innerHTML = `
    <div class="trade-card">
      <div class="trade-header">
        <span class="trade-asset">${escHtml(t.asset || '—')}</span>
        <span class="queue-badge ${isCall ? 'badge-call' : 'badge-put'}">
          ${isCall ? '▲ CALL' : '▼ PUT'}
        </span>
        ${resBadge}
      </div>
      <div class="trade-details">
        <span>Amount: <b>${fmtMoney(t.amount)}</b></span>
        <span>Duration: <b>${t.duration || 60}s</b></span>
        <span>Fill Latency: <b>${t.latency || 0}s</b></span>
        <span>Time: <b>${t.at || '—'}</b></span>
        ${t.profit !== null && t.profit !== undefined ? `<span>Profit: <b class="${t.profit >= 0 ? 'text-green' : 'text-red'}">${fmtMoney(t.profit)}</b></span>` : ''}
      </div>
    </div>
  `;
}

// ── Header status ─────────────────────────────────────────
function updateHeaderStatus(statusOrRunning) {
  const el = $('header-status');
  if (!el) return;
  const status = typeof statusOrRunning === 'boolean'
    ? (statusOrRunning ? 'running' : 'stopped')
    : String(statusOrRunning).toLowerCase();

  if (status === 'starting' || status === 'stopping') {
    el.className = 'header-status stopped';
    el.innerHTML = `<div class="status-dot pulse"></div>${status.toUpperCase()}`;
  } else if (status === 'running') {
    el.className = 'header-status running';
    el.innerHTML = '<div class="status-dot"></div>RUNNING';
  } else {
    el.className = 'header-status stopped';
    el.innerHTML = '<div class="status-dot"></div>STOPPED';
  }
}

// ── Alert banner ──────────────────────────────────────────
function setAlert(msg) {
  const banner = $('alert-banner');
  const text   = $('alert-text');
  if (!banner || !text) return;
  if (msg) {
    text.textContent = msg;
    banner.style.display = 'flex';
  } else {
    banner.style.display = 'none';
  }
}

function initAlertClose() {
  document.querySelector('.alert-close')?.addEventListener('click', () => {
    $('alert-banner').style.display = 'none';
  });
}

// ── Activity log ──────────────────────────────────────────
function addLog(entry, scroll = true) {
  const panel = $('log-panel');
  if (!panel) return;
  const div = document.createElement('div');
  div.className = 'log-entry';
  const lvl = (entry.level || 'INFO').toUpperCase();
  div.innerHTML = `
    <span class="log-time">${escHtml(entry.time || '--:--:--')}</span>
    <span class="log-level ${lvl}">${escHtml(lvl)}</span>
    <span class="log-msg ${lvl}">${escHtml(entry.message || '')}</span>
  `;
  panel.appendChild(div);
  if (scroll) {
    panel.scrollTop = panel.scrollHeight;
  }
}

async function loadInitialLogs() {
  try {
    const res = await api('/api/logs', 'GET');
    if (res && Array.isArray(res.logs) && res.logs.length) {
      const panel = $('log-panel');
      if (panel) {
        panel.innerHTML = '';
        res.logs.forEach(entry => addLog(entry, false));
        panel.scrollTop = panel.scrollHeight;
      }
    } else {
      const panel = $('log-panel');
      if (panel) {
        panel.innerHTML = '<div class="log-entry"><span class="log-time">--:--:--</span><span class="log-msg text-dim">No logs recorded yet. Start bot or connect accounts to begin.</span></div>';
      }
    }
  } catch (e) {
    console.debug('Could not load historical logs:', e);
  }
}

function initLogControls() {
  $('btn-clear-log')?.addEventListener('click', () => {
    const p = $('log-panel');
    if (p) p.innerHTML = '<div class="log-entry"><span class="log-time">--:--:--</span><span class="log-msg text-dim">Console view cleared (full history preserved in log file)</span></div>';
  });
  $('btn-download-log')?.addEventListener('click', () => {
    window.location.href = '/api/logs/download';
  });
}

// ── SocketIO events ───────────────────────────────────────
function initSocket() {
  try {
    _socket = io({ transports: ['websocket', 'polling'] });
    _socket.on('connect', () => console.log('SocketIO connected'));
    _socket.on('state_update', data => applyState(data));
    _socket.on('bot_status', data => {
      const st = data.status || (data.running ? 'running' : 'stopped');
      updateBotButton(st);
    });
    _socket.on('connection_update', data => updateConnectionBadges(data));
    _socket.on('log', entry => addLog(entry));
  } catch (e) {
    console.warn('SocketIO init failed, falling back to polling:', e);
  }
}

// ── Connection badges ─────────────────────────────────────
function updateConnectionBadges(conn) {
  if (!conn) return;
  setConnBadge('tg-badge-hdr', 'tg-badge', conn.telegram, 'Telegram');
  setConnBadge('po-badge-hdr', 'po-badge', conn.pocket_option, 'Pocket Option');
}

function setConnBadge(hdrId, settId, ok, name) {
  const hdr  = $(hdrId);
  const sett = $(settId);
  const stateCls = ok ? 'connected' : 'disconnected';
  const label    = ok ? 'Online' : 'Offline';

  if (hdr) {
    hdr.className = 'conn-badge ' + stateCls;
    hdr.innerHTML = `<div class="status-dot"></div>${name}`;
  }
  if (sett) {
    sett.className = 'conn-badge ' + stateCls;
    sett.innerHTML = `<div class="status-dot"></div>${label}`;
  }

  if (name === 'Telegram') {
    const btn = $('btn-tg-connect');
    if (btn) {
      if (ok) {
        btn.textContent = 'Disconnect';
        btn.classList.add('text-red');
      } else {
        btn.textContent = 'Connect';
        btn.classList.remove('text-red');
      }
    }
  }

  if (name === 'Pocket Option') {
    const btn = $('btn-po-connect');
    if (btn) {
      btn.textContent = ok ? 'Reconnect' : 'Connect';
    }
  }
}

// ── Apply State ───────────────────────────────────────────
function applyState(s) {
  if (!s) return;
  _state = s;
  const status = s.status || (s.bot_running ? 'running' : 'stopped');
  updateBotButton(status);
  updateMetrics(s);
  updateConnectionBadges(s.connections || {
    telegram: false,
    pocket_option: s.pocket_option_connected,
  });
  if (s.pending) setQueue(s.pending);
  renderLastTrade(s.last_trade);
  setAlert(s.alert);
}

async function fetchStatus() {
  try {
    const s = await api('/api/status', 'GET');
    applyState(s);
  } catch (e) {
    console.error('Status fetch failed:', e);
  }
}

// ── Telegram auth modal ───────────────────────────────────
function showTgStep(step) {
  document.querySelectorAll('#modal-telegram .modal-step').forEach(el => el.classList.remove('active'));
  $(`step-${step}`)?.classList.add('active');
}

function initTelegramModal() {
  $('btn-tg-connect')?.addEventListener('click', async () => {
    const btn = $('btn-tg-connect');
    if (btn && btn.textContent.trim().toLowerCase() === 'disconnect') {
      if (!confirm('Are you sure you want to disconnect Telegram?')) return;
      try {
        const res = await api('/api/telegram/disconnect', 'POST');
        if (res.success) {
          showToast('Telegram disconnected', 'info');
          fetchStatus();
        }
      } catch (e) {
        showToast('Error: ' + e.message, 'error');
      }
      return;
    }
    showTgStep('phone');
    openModal('modal-telegram');
  });

  $('btn-tg-send-code')?.addEventListener('click', async () => {
    const phone = $('tg-phone')?.value.trim();
    if (!phone) { showToast('Phone number required', 'error'); return; }
    setBtnBusy('btn-tg-send-code', true);
    try {
      const res = await api('/api/telegram/connect', 'POST', { phone });
      if (res.success) {
        showToast('Code sent!', 'success');
        showTgStep('code');
      } else {
        showToast(res.message || 'Failed to send code', 'error');
      }
    } catch (e) {
      showToast('Error: ' + e.message, 'error');
    } finally {
      setBtnBusy('btn-tg-send-code', false);
    }
  });

  $('btn-tg-verify')?.addEventListener('click', async () => {
    const code = $('tg-code')?.value.trim();
    if (!code) { showToast('Code required', 'error'); return; }
    setBtnBusy('btn-tg-verify', true);
    try {
      const res = await api('/api/telegram/verify', 'POST', { code });
      if (res.success) {
        showTgStep('success');
        fetchStatus();
      } else if (res.two_factor_required) {
        showTgStep('password');
      } else {
        showToast(res.message || 'Invalid code', 'error');
      }
    } catch (e) {
      showToast('Error: ' + e.message, 'error');
    } finally {
      setBtnBusy('btn-tg-verify', false);
    }
  });

  $('btn-tg-password')?.addEventListener('click', async () => {
    const password = $('tg-password')?.value;
    if (!password) { showToast('Password required', 'error'); return; }
    setBtnBusy('btn-tg-password', true);
    try {
      const res = await api('/api/telegram/password', 'POST', { password });
      if (res.success) {
        showTgStep('success');
        fetchStatus();
      } else {
        showToast(res.message || 'Incorrect password', 'error');
      }
    } catch (e) {
      showToast('Error: ' + e.message, 'error');
    } finally {
      setBtnBusy('btn-tg-password', false);
    }
  });
}

// ── Pocket Option connect modal ───────────────────────────
function showPoStep(step) {
  document.querySelectorAll('#modal-pocket-option .modal-step').forEach(el => el.classList.remove('active'));
  $(`po-step-${step}`)?.classList.add('active');
}

function initPocketOptionModal() {
  $('btn-po-connect')?.addEventListener('click', () => {
    const curSsid = $('s-po-ssid')?.value.trim() || '';
    const modalInput = $('po-modal-ssid');
    if (modalInput) modalInput.value = curSsid;

    const curAcct = $('s-account-type')?.value === 'live' ? 'false' : 'true';
    const modeSelect = $('po-modal-is-demo');
    if (modeSelect) modeSelect.value = curAcct;

    showPoStep('form');
    openModal('modal-pocket-option');
  });

  $('btn-po-save')?.addEventListener('click', async () => {
    const ssid = $('po-modal-ssid')?.value.trim();
    const isDemo = $('po-modal-is-demo')?.value === 'true';

    if (!ssid) {
      showToast('SSID is required', 'error');
      return;
    }

    showPoStep('testing');

    try {
      const res = await api('/api/pocket_option/connect', 'POST', { ssid, is_demo: isDemo });
      if (res.success) {
        const sub = $('po-success-sub');
        if (sub) {
          sub.textContent = `Connected! ${res.balance ? 'Balance: ' + fmtMoney(res.balance) : ''}`;
        }
        showPoStep('success');
        if ($('s-po-ssid')) $('s-po-ssid').value = ssid;
        if ($('s-account-type')) $('s-account-type').value = isDemo ? 'demo' : 'live';
        fetchStatus();
      } else {
        const failMsg = $('po-failed-reason');
        if (failMsg) failMsg.textContent = res.message || 'Connection test failed. Check your SSID.';
        showPoStep('failed');
      }
    } catch (e) {
      const failMsg = $('po-failed-reason');
      if (failMsg) failMsg.textContent = 'Network error: ' + e.message;
      showPoStep('failed');
    }
  });
}

// ── Settings ──────────────────────────────────────────────
async function loadSettings() {
  try {
    const cfg = await api('/api/settings', 'GET');
    if (!cfg) return;

    // Telegram
    setValue('s-api-id',   cfg.telegram?.api_id || '');
    setValue('s-api-hash', cfg.telegram?.api_hash || '');
    renderChannels(cfg.telegram?.channels || []);

    // Trading
    setValue('s-account-type',  cfg.trading?.account_type || 'demo');
    setValue('s-risk-mode',     cfg.trading?.risk_mode || 'fixed');
    setValue('s-risk-amount',   cfg.trading?.risk_amount ?? 1.0);
    setValue('s-max-trades',    cfg.trading?.max_daily_trades ?? 10);
    setValue('s-max-loss',      cfg.trading?.max_daily_loss ?? 50);
    setValue('s-max-concurrent',cfg.trading?.max_concurrent_trades ?? 1);
    setChecked('s-max-loss-enabled', cfg.trading?.max_daily_loss_enabled !== false);

    // Martingale
    setChecked('s-martingale-enabled', !!cfg.trading?.martingale_enabled);
    setValue('s-martingale-mult',  cfg.trading?.martingale_multiplier ?? 2.0);
    setValue('s-martingale-steps', cfg.trading?.martingale_steps ?? 2);
    toggleMartingaleFields();

    // Pocket Option Account
    setValue('s-po-ssid',    cfg.pocket_option?.ssid || '');
    setValue('s-early-entry',cfg.pocket_option?.early_entry_seconds ?? 1.0);
    setValue('s-late-grace', cfg.pocket_option?.late_entry_grace_seconds ?? 5.0);
    setTimezoneValue(cfg.pocket_option?.timezone || 'UTC-4');

    // Logging
    setValue('s-log-level', cfg.logging?.log_level || 'INFO');
    setValue('s-log-file',  cfg.logging?.log_file || 'pocket_bot.log');

    applyRiskModeUnits();
  } catch (e) {
    showToast('Failed to load settings: ' + e.message, 'error');
  }
}

function renderChannels(channels) {
  const box = $('channel-list');
  if (!box) return;
  const list = Array.isArray(channels) ? channels : [];
  if (list.length === 0) {
    box.innerHTML = `
      <div class="channel-row" data-idx="0">
        <input type="checkbox" class="ch-enabled" checked>
        <input type="text" class="form-input ch-id" value="" placeholder="Channel username or -100...">
        <button class="btn btn-ghost btn-sm text-red" onclick="removeChannel(0)">✕</button>
      </div>
    `;
    return;
  }
  box.innerHTML = list.map((ch, idx) => {
    const isObj = (typeof ch === 'object' && ch !== null);
    const enabled = isObj ? (ch.enabled !== false) : true;
    const identifier = isObj ? (ch.identifier ?? '') : ch;
    return `
      <div class="channel-row" data-idx="${idx}">
        <input type="checkbox" class="ch-enabled" ${enabled ? 'checked' : ''}>
        <input type="text" class="form-input ch-id" value="${escHtml(identifier)}" placeholder="Channel username or -100...">
        <button class="btn btn-ghost btn-sm text-red" onclick="removeChannel(${idx})">✕</button>
      </div>
    `;
  }).join('');
}

function addChannel() {
  const box = $('channel-list');
  if (!box) return;
  const idx = box.children.length;
  const div = document.createElement('div');
  div.className = 'channel-row';
  div.setAttribute('data-idx', idx);
  div.innerHTML = `
    <input type="checkbox" class="ch-enabled" checked>
    <input type="text" class="form-input ch-id" placeholder="Channel username or -100...">
    <button class="btn btn-ghost btn-sm text-red" onclick="removeChannel(${idx})">✕</button>
  `;
  box.appendChild(div);
}

function removeChannel(idx) {
  const row = document.querySelector(`.channel-row[data-idx="${idx}"]`);
  if (row) row.remove();
}

function getChannelsFromDOM() {
  const rows = document.querySelectorAll('#channel-list .channel-row');
  const out = [];
  rows.forEach(r => {
    const enabled = r.querySelector('.ch-enabled')?.checked ?? true;
    const idVal   = r.querySelector('.ch-id')?.value.trim() || '';
    if (idVal) {
      out.push({ enabled, identifier: isNaN(idVal) ? idVal : Number(idVal) });
    }
  });
  return out;
}

function toggleMartingaleFields() {
  const chk = $('s-martingale-enabled');
  const box = $('martingale-fields');
  if (box && chk) {
    box.style.display = chk.checked ? 'block' : 'none';
  }
}

function applyRiskModeUnits() {
  const mode = $('s-risk-mode')?.value;
  const lbl = $('lbl-risk-amount');
  if (lbl) {
    lbl.textContent = mode === 'percent' ? 'Risk Amount (% of balance)' : 'Risk Amount ($)';
  }
}

async function saveSettings() {
  const btn = $('btn-save');
  if (btn) btn.disabled = true;

  const payload = {
    telegram: {
      api_id:   $('s-api-id')?.value ? Number($('s-api-id').value) : null,
      api_hash: $('s-api-hash')?.value.trim() || '',
      channels: getChannelsFromDOM(),
    },
    trading: {
      account_type:           $('s-account-type')?.value || 'demo',
      risk_mode:              $('s-risk-mode')?.value || 'fixed',
      risk_amount:            parseFloat($('s-risk-amount')?.value || '1.0'),
      max_daily_trades:       parseInt($('s-max-trades')?.value || '10', 10),
      max_daily_loss:         parseFloat($('s-max-loss')?.value || '50.0'),
      max_daily_loss_enabled: $('s-max-loss-enabled')?.checked ?? true,
      max_concurrent_trades:  parseInt($('s-max-concurrent')?.value || '1', 10),
      martingale_enabled:     $('s-martingale-enabled')?.checked ?? false,
      martingale_multiplier:  parseFloat($('s-martingale-mult')?.value || '2.0'),
      martingale_steps:       parseInt($('s-martingale-steps')?.value || '2', 10),
    },
    pocket_option: {
      ssid:                      $('s-po-ssid')?.value.trim() || '',
      is_demo:                   $('s-account-type')?.value !== 'live',
      early_entry_seconds:       parseFloat($('s-early-entry')?.value || '1.0'),
      late_entry_grace_seconds:  parseFloat($('s-late-grace')?.value || '5.0'),
      timezone:                  $('s-timezone')?.value || 'UTC-4',
    },
    logging: {
      log_level: $('s-log-level')?.value || 'INFO',
      log_file:  $('s-log-file')?.value.trim() || 'pocket_bot.log',
    }
  };

  try {
    const res = await api('/api/settings', 'POST', payload);
    if (res.success) {
      showToast('Settings saved successfully!', 'success');
      fetchStatus();
    } else {
      showToast(res.message || 'Failed to save settings', 'error');
    }
  } catch (e) {
    showToast('Error: ' + e.message, 'error');
  } finally {
    if (btn) btn.disabled = false;
  }
}

// ── Modal helpers ─────────────────────────────────────────
function openModal(id) {
  const m = $(id);
  if (m) {
    m.classList.add('active', 'open');
  }
}

function closeModal(id) {
  const m = $(id);
  if (m) {
    m.classList.remove('active', 'open');
  }
}

window.openModal = openModal;
window.closeModal = closeModal;
window.showTgStep = showTgStep;
window.showPoStep = showPoStep;

function initModalCloseHandlers() {
  document.querySelectorAll('.modal-overlay').forEach(overlay => {
    overlay.addEventListener('click', e => {
      if (e.target === overlay) closeModal(overlay.id);
    });
  });
  document.querySelectorAll('.modal-close').forEach(btn => {
    btn.addEventListener('click', () => {
      const parent = btn.closest('.modal-overlay');
      if (parent) closeModal(parent.id);
    });
  });
}

// ── Toast notifications ───────────────────────────────────
function showToast(msg, type = 'info') {
  const div = document.createElement('div');
  div.className = `toast ${type}`;
  div.textContent = msg;
  document.body.appendChild(div);
  setTimeout(() => div.classList.add('visible'), 10);
  setTimeout(() => {
    div.classList.remove('visible');
    setTimeout(() => div.remove(), 300);
  }, 3500);
}

// ── API helper ────────────────────────────────────────────
async function api(path, method = 'GET', body = null) {
  const opts = {
    method,
    headers: { 'Content-Type': 'application/json' },
  };
  if (body) opts.body = JSON.stringify(body);
  const res = await fetch(path, opts);
  if (!res.ok) {
    const txt = await res.text();
    let j = {};
    try { j = JSON.parse(txt); } catch (_) {}
    throw new Error(j.message || `HTTP ${res.status}: ${txt}`);
  }
  return await res.json();
}

// ── Misc form helpers ─────────────────────────────────────
function setValue(id, val) {
  const el = $(id);
  if (el) el.value = val ?? '';
}

function setTimezoneValue(val) {
  const el = $('s-timezone');
  if (!el) return;
  const target = (val || 'UTC-4').trim();
  let found = false;
  for (let i = 0; i < el.options.length; i++) {
    if (el.options[i].value.toUpperCase() === target.toUpperCase()) {
      el.selectedIndex = i;
      found = true;
      break;
    }
  }
  if (!found) {
    const opt = document.createElement('option');
    opt.value = target;
    opt.textContent = `${target} (Custom)`;
    el.appendChild(opt);
    el.value = target;
  }
}

function setChecked(id, bool) {
  const el = $(id);
  if (el) el.checked = !!bool;
}

function setBtnBusy(id, busy) {
  const btn = $(id);
  if (btn) btn.disabled = busy;
}

// ── Initialization ────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  initTheme();
  initTabs();
  initBotToggle();
  initAlertClose();
  initLogControls();
  initSocket();
  initTelegramModal();
  initPocketOptionModal();
  initModalCloseHandlers();

  $('s-martingale-enabled')?.addEventListener('change', toggleMartingaleFields);
  $('s-risk-mode')?.addEventListener('change', applyRiskModeUnits);

  loadInitialLogs();
  fetchStatus();
  _pollId = setInterval(fetchStatus, 3000);
});
