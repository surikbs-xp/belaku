/**
 * GIFT Nifty Execution Desk - frontend engine.
 *
 * Polls the FastAPI JSON endpoints on a fixed interval and patches the DOM in
 * place (no page reloads, no framework). Destructive actions (mode switch to
 * LIVE, panic) always go through an explicit confirm dialog first.
 *
 * The whole file is deliberately plain ES2020: no build step, no bundler.
 */

'use strict';

const POLL_MS = 1500;
const ENDPOINTS = {
  status:    '/api/status',
  portfolio: '/api/portfolio',
  positions: '/api/positions?status=all&limit=200',
  orders:    '/api/orders?limit=500',
  trades:    '/api/trades?limit=500',
  signals:   '/api/signals?limit=100',
  toggle:    '/api/toggle-mode',
  pause:     '/api/pause',
  panic:     '/api/panic',
};

const state = {
  tab: 'monitor',
  orders: [],
  signals: [],
  trades: [],
  failures: 0,
  lastSync: null,
  busy: false,
  modeConfirming: false,
};

/* ── Small helpers ─────────────────────────────────────────────────── */

const $ = (id) => document.getElementById(id);

const num = (value) => (typeof value === 'number' && Number.isFinite(value) ? value : null);

/** ₹ formatter; returns an em dash for missing values. */
function inr(value, decimals = 0) {
  const n = num(value);
  if (n === null) return '—';
  const sign = n < 0 ? '-' : '';
  return `${sign}₹${Math.abs(n).toLocaleString('en-IN', {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  })}`;
}

function signedInr(value, decimals = 2) {
  const n = num(value);
  if (n === null) return '—';
  return `${n >= 0 ? '+' : '-'}₹${Math.abs(n).toLocaleString('en-IN', {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  })}`;
}

function pnlClass(value) {
  const n = num(value);
  if (n === null || n === 0) return 'flat';
  return n > 0 ? 'pos' : 'neg';
}

function duration(seconds) {
  const s = num(seconds);
  if (s === null) return '—';
  if (s < 60) return `${Math.round(s)}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${Math.round(s % 60)}s`;
  const h = Math.floor(s / 3600);
  return `${h}h ${Math.floor((s % 3600) / 60)}m`;
}

function timeAgo(seconds) {
  const s = num(seconds);
  if (s === null) return 'never';
  if (s < 60) return `${Math.round(s)}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  return `${Math.floor(s / 3600)}h ago`;
}

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  })[c]);
}

/** Write text only when it changed, and flash it so updates are visible. */
function setText(id, text, cssClass) {
  const el = $(id);
  if (!el) return;
  const next = String(text);
  if (el.textContent !== next) {
    el.textContent = next;
    el.classList.remove('flash');
    void el.offsetWidth; // restart the CSS animation
    el.classList.add('flash');
  }
  if (cssClass !== undefined) {
    el.classList.remove('pos', 'neg', 'flat');
    if (cssClass) el.classList.add(cssClass);
  }
}

function setPill(id, text, variant) {
  const el = $(id);
  if (!el) return;
  el.textContent = text;
  el.className = `pill pill-${variant}`;
}

function toast(message, kind = 'info') {
  const el = $('toast');
  el.textContent = message;
  el.className = `pointer-events-none fixed bottom-6 right-6 z-50 max-w-sm rounded-xl border px-4 py-3 text-sm shadow-xl toast-${kind}`;
  el.classList.remove('hidden');
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => el.classList.add('hidden'), 5000);
}

async function api(url, options) {
  const response = await fetch(url, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  const text = await response.text();
  let body = null;
  try {
    body = text ? JSON.parse(text) : null;
  } catch {
    body = { detail: text };
  }
  if (!response.ok) {
    const detail = (body && (body.detail || body.message)) || `HTTP ${response.status}`;
    throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
  }
  return body;
}

/* ── Confirm dialog ────────────────────────────────────────────────── */

function confirmDialog({ title, body, okLabel = 'Confirm', danger = true }) {
  return new Promise((resolve) => {
    const modal = $('modal');
    $('modal-title').textContent = title;
    $('modal-body').innerHTML = body;
    const ok = $('modal-ok');
    ok.textContent = okLabel;
    ok.className = `btn ${danger ? 'btn-danger' : 'btn-neutral'}`;

    const close = (result) => {
      modal.classList.add('hidden');
      modal.classList.remove('flex');
      ok.removeEventListener('click', onOk);
      $('modal-cancel').removeEventListener('click', onCancel);
      document.removeEventListener('keydown', onKey);
      resolve(result);
    };
    const onOk = () => close(true);
    const onCancel = () => close(false);
    const onKey = (event) => { if (event.key === 'Escape') close(false); };

    ok.addEventListener('click', onOk);
    $('modal-cancel').addEventListener('click', onCancel);
    document.addEventListener('keydown', onKey);
    modal.classList.remove('hidden');
    modal.classList.add('flex');
  });
}

/* ── Header + monitor ──────────────────────────────────────────────── */

function renderStatus(status) {
  const mode = status.mode;
  const market = status.market || {};
  setPill('pill-market', market.is_open ? 'Market open' : 'Market closed', market.is_open ? 'live' : 'idle');
  $('pill-market').title = market.reason || 'Market status unavailable';

  // The mark loop ticks every ENGINE_INTERVAL_SECONDS; a stale tick means that
  // thread died even though the API is still answering.
  const strategy = status.strategy || {};
  const hb = strategy.heartbeat || {};
  const age = num(hb.age_seconds);
  const interval = num(hb.interval_seconds) || 5;
  const loopHealthy = age !== null && age < interval * 3;
  setPill(
    'pill-loop',
    loopHealthy ? `Loop · ${timeAgo(age)}` : `Loop · stalled (${timeAgo(age)})`,
    loopHealthy ? 'live' : 'bad',
  );
  $('pill-loop').title =
    `marks ${hb.marks ?? 0} · ticks ${hb.ticks ?? 0} · every ${interval}s`
    + (hb.last_error ? ` · last error: ${hb.last_error}` : '');

  $('hdr-symbol').textContent = (status.symbol && status.symbol.current) || '—';
  $('hdr-market').textContent = market.is_open
    ? `Market open · closes ${(market.session_close || '').slice(11, 16)} IST`
    : market.reason || '—';

  renderEngine(status.engine || {}, market);

  const banner = $('paused-banner');
  banner.classList.toggle('hidden', !strategy.paused);
  banner.classList.toggle('flex', !!strategy.paused);

  const pauseBtn = $('btn-pause');
  pauseBtn.textContent = strategy.paused ? 'Resume' : 'Pause';

  const modeToggle = $('btn-mode-toggle');
  const isLive = mode === 'LIVE';
  modeToggle.setAttribute('aria-checked', String(isLive));
  modeToggle.classList.toggle('mode-toggle-live', isLive);
  modeToggle.disabled = state.busy || state.modeConfirming || (!isLive && !status.live_configured);
  $('mode-toggle-label').textContent = isLive ? 'LIVE' : 'PAPER';
  $('mode-toggle-caption').textContent = isLive
    ? 'Switch to Paper'
    : status.live_configured ? 'Switch to Live' : 'Kite required';

  const eng = status.engine || {};
  const feed = eng.feed || {};
  $('footer-left').textContent =
    `Mode ${mode} · uptime ${duration(status.uptime_seconds)} · `
    + `GIFT ${feed.bars ?? 0} bars · ${eng.signals_fired ?? 0} signals · ${eng.orders_sent ?? 0} orders`;
  $('footer-right').textContent =
    `Sync ${state.lastSync ? state.lastSync.toLocaleTimeString() : '—'} · ${state.failures ? `${state.failures} failed poll(s)` : 'healthy'}`;
}

/**
 * GIFT Nifty signal state. This is the bot's brain: it shows what the st.pine
 * conditions currently say on GIFT Nifty, whether the series is warm enough to
 * judge, and whether an order would be allowed right now.
 */
function renderEngine(eng, market) {
  const feed = eng.feed || {};
  const values = eng.values || {};
  const conditions = eng.conditions || {};

  // Distinguish socket startup from an actual stream of market data.
  const tickAge = num(feed.tick_age_seconds);
  const feedLive = !!feed.connected && tickAge !== null && tickAge < 120;
  const feedLabel = feedLive
    ? `GIFT · ${timeAgo(tickAge)}`
    : feed.starting
      ? 'GIFT · connecting'
      : feed.connected
        ? 'GIFT · waiting for ticks'
        : 'GIFT · disconnected';
  setPill(
    'pill-feed',
    feedLabel,
    feedLive ? 'live' : 'bad',
  );
  $('pill-feed').title =
    `${feed.symbol || 'GIFT NIFTY'} · ${feed.bars ?? 0} bars @ ${feed.interval || '?'}`
    + (feed.error ? ` · ${feed.error}` : '');

  // Signal badge: BULL/BEAR/NEUTRAL plus whether we are still warming up.
  const ready = !!eng.ready;
  const signal = eng.last_signal || 'NEUTRAL';
  const warmupHave = Math.max(0, Number(eng.warmup_have) || 0);
  const warmupNeeded = Math.max(1, Number(eng.warmup_needed) || 20);
  const warmupPercent = ready ? 100 : Math.min(100, (warmupHave / warmupNeeded) * 100);
  const warmupProgress = $('warmup-progress');
  warmupProgress.style.width = `${warmupPercent}%`;
  warmupProgress.parentElement.setAttribute('aria-valuemax', String(warmupNeeded));
  warmupProgress.parentElement.setAttribute('aria-valuenow', String(Math.min(warmupHave, warmupNeeded)));

  let signalText, signalClass;
  if (!ready) {
    signalText = `Warming up ${warmupHave}/${warmupNeeded}`;
    signalClass = 'idle';
  } else if (signal === 'BULL') {
    signalText = 'BULLISH';
    signalClass = 'live';
  } else if (signal === 'BEAR') {
    signalText = 'BEARISH';
    signalClass = 'bad';
  } else {
    signalText = 'NEUTRAL';
    signalClass = 'idle';
  }
  setPill('pill-signal', `Signal · ${signalText}`, signalClass);

  setText('e-signal', ready ? signal : 'WARMING UP',
    ready ? pnlClass(signal === 'BULL' ? 1 : signal === 'BEAR' ? -1 : 0) : 'flat');
  setText(
    'e-signal-sub',
    ready
      ? (eng.last_detail || 'Conditions evaluated on the latest completed candle.')
      : `Collecting closed candles · ${Math.max(0, warmupNeeded - warmupHave)} more to calculate indicators`,
  );
  setText('e-gift-price', feed.last_price === null || feed.last_price === undefined ? '—' : inr(feed.last_price, 2));
  setText('e-gift-sub', `${feed.bars ?? 0} bars · ${feed.ticks ?? 0} ticks · ${feed.interval || '—'}`);
  setText('e-exec', eng.execution_symbol || '—');
  setText('e-exec-sub', market.is_open
    ? 'NSE F&O open — orders live'
    : `orders held · ${market.reason || 'closed'}`);

  const box = $('conditions');
  if (!ready) {
    box.innerHTML = `
      <div class="warmup-message">
        <strong>Rule checks will appear when indicators are ready.</strong>
        <span>${warmupHave} of ${warmupNeeded} closed candles collected. No signal is being evaluated yet.</span>
      </div>`;
    ['e-rsi', 'e-tsv', 'e-psar', 'e-st'].forEach((id) => setText(id, '—'));
  } else {
    const order = ['supertrend', 'rsi', 'tsv', 'psar'];
    const labels = {
      supertrend: 'Trend vote · 2 of 3',
      rsi: 'RSI threshold',
      tsv: 'TSV direction',
      psar: 'Price vs PSAR',
    };
    box.innerHTML = ['buy', 'sell'].map((side) => `
      <div class="condition-panel condition-panel-${side}">
        <div class="condition-panel-heading">
          <strong>${side.toUpperCase()}</strong>
          <span>All four checks must pass</span>
        </div>
        <div class="condition-check-grid">
        ${order.map((key) => {
          const ok = !!conditions[`${side}_${key}`];
          return `<div class="condition-check ${ok ? 'condition-check-pass' : 'condition-check-fail'}">
            <span class="condition-check-icon">${ok ? '✓' : '—'}</span>
            <span>${labels[key]}</span>
          </div>`;
        }).join('')}
        </div>
      </div>`).join('');

    const format = (value, digits = 2) => num(value) === null ? '—' : Number(value).toFixed(digits);
    setText('e-rsi', format(values.rsi));
    setText('e-rsi-sub', conditions.buy_rsi
      ? 'BUY condition passes'
      : conditions.sell_rsi ? 'SELL condition passes' : 'BUY <20 · SELL >80');
    setText('e-tsv', format(values.tsv));
    setText('e-tsv-sub', conditions.buy_tsv
      ? 'Positive · BUY condition passes'
      : conditions.sell_tsv ? 'Negative · SELL condition passes' : 'BUY >0 · SELL <0');
    setText('e-psar', inr(values.psar, 2));
    setText('e-psar-sub', conditions.buy_psar
      ? 'Price above · BUY condition passes'
      : conditions.sell_psar ? 'Price below · SELL condition passes' : 'BUY above · SELL below');
    setText('e-st', `${values.supertrend_bull}/3 bullish · ${values.supertrend_bear}/3 bearish`);
    setText(
      'e-st-sub',
      `Lines ${format(values.st_10_1)} · ${format(values.st_11_2)} · ${format(values.st_12_3)}`,
    );
  }

  setText('e-counters',
    `${eng.evaluations ?? 0} evals · ${eng.signals_fired ?? 0} signals · `
    + `${eng.orders_sent ?? 0} sent · ${eng.orders_blocked ?? 0} blocked · ${eng.deferred ?? 0} deferred`);

  if (eng.last_error) {
    setActionStatus(`Engine: ${eng.last_error}`, 'err');
  }
}

function renderPortfolio(portfolio) {
  setText('m-equity', inr(portfolio.equity), pnlClass(portfolio.equity - portfolio.initial_balance));
  setText('m-equity-sub',
    `${inr(portfolio.initial_balance)} start · ${portfolio.total_return_pct >= 0 ? '+' : ''}${portfolio.total_return_pct}% return`);

  setText('m-margin', inr(portfolio.available_margin));
  setText('m-margin-sub',
    `${inr(portfolio.margin_used)} used · ${portfolio.margin_utilisation_pct}% of book`);

  const bar = $('m-margin-bar');
  bar.style.width = `${Math.min(100, Math.max(0, portfolio.margin_utilisation_pct))}%`;
  bar.style.backgroundColor =
    portfolio.margin_utilisation_pct > 80 ? 'var(--bad)'
      : portfolio.margin_utilisation_pct > 50 ? 'var(--warn)'
        : 'var(--brand)';

  setText('m-mode', portfolio.mode);
  setPill('m-mode-pill', portfolio.mode === 'LIVE' ? 'Live · real money' : 'Paper · simulated',
    portfolio.mode === 'LIVE' ? 'live' : 'paper');

  setText('m-pnl', signedInr(portfolio.total_pnl), pnlClass(portfolio.total_pnl));
  setText('m-pnl-sub',
    `${signedInr(portfolio.unrealized_pnl)} open · ${signedInr(portfolio.realized_today)} today`);

  setText('m-today', signedInr(portfolio.realized_today), pnlClass(portfolio.realized_today));
  setText('m-unreal', signedInr(portfolio.unrealized_pnl), pnlClass(portfolio.unrealized_pnl));
  setText('m-realised', signedInr(portfolio.realized_total), pnlClass(portfolio.realized_total));
  setText('m-winrate', `${portfolio.win_rate}%`);
  setText('m-pf', portfolio.profit_factor === null ? '∞' : portfolio.profit_factor);

  setText('a-net', signedInr(portfolio.total_pnl), pnlClass(portfolio.total_pnl));
  setText('a-return', `${portfolio.total_return_pct >= 0 ? '+' : ''}${portfolio.total_return_pct}%`,
    pnlClass(portfolio.total_return_pct));
  setText('a-dd', `${portfolio.max_drawdown_pct}%`, pnlClass(portfolio.max_drawdown_pct));
  setText('a-trades', portfolio.total_trades);
}

function renderPositions(payload) {
  const positions = payload.positions || [];
  const open = positions.filter((p) => p.status === 'OPEN');
  setText('pos-count', `${open.length} open · ${positions.length} total`);

  const body = $('positions-body');
  if (!open.length) {
    body.innerHTML = '<tr class="empty-row"><td colspan="7">Flat — no open positions.</td></tr>';
    return;
  }
  body.innerHTML = open.map((p) => `
    <tr>
      <td class="font-mono">${escapeHtml(p.symbol)}</td>
      <td><span class="tag ${p.side === 'LONG' ? 'tag-buy' : 'tag-sell'}">${escapeHtml(p.side)}</span></td>
      <td class="num">${p.quantity}</td>
      <td class="num">${inr(p.entry_price, 2)}</td>
      <td class="num">${inr(p.mark_price, 2)}</td>
      <td class="num ${pnlClass(p.unrealized_pnl)}">${signedInr(p.unrealized_pnl)}</td>
      <td><span class="tag tag-muted">${escapeHtml(p.mode)}</span></td>
    </tr>`).join('');
}

/* ── Orders + signals ──────────────────────────────────────────────── */

function filteredOrders() {
  const needle = $('orders-search').value.trim().toLowerCase();
  const mode = $('orders-mode').value;
  return state.orders.filter((o) => {
    if (mode && (o.mode || '').toUpperCase() !== mode) return false;
    if (!needle) return true;
    return Object.values(o).join(' ').toLowerCase().includes(needle);
  });
}

function renderOrders() {
  const rows = filteredOrders();
  const total = state.orders.length;
  setText('orders-caption', `${rows.length} of ${total} executions shown`);

  const body = $('orders-body');
  if (!rows.length) {
    body.innerHTML = '<tr class="empty-row"><td colspan="10">No matching executions.</td></tr>';
    return;
  }
  body.innerHTML = rows.map((o) => {
    const action = String(o.transaction_type || '').toUpperCase();
    const tag = action === 'BUY' ? 'tag-buy' : 'tag-sell';
    const status = String(o.status || '').toUpperCase();
    const statusTag = status === 'COMPLETE' ? 'tag-buy'
      : status === 'REJECTED' || status === 'CANCELLED' ? 'tag-sell'
        : 'tag-warn';
    return `
      <tr>
        <td class="font-mono text-slate-400">${escapeHtml((o.ts || '').replace('T', ' '))}</td>
        <td class="font-mono">${escapeHtml(o.symbol)}</td>
        <td><span class="tag ${tag}">${escapeHtml(action)}</span></td>
        <td class="num">${o.quantity}</td>
        <td class="num">${o.price === null || o.price === undefined ? '—' : inr(o.price, 2)}</td>
        <td><span class="tag ${statusTag}">${escapeHtml(status)}</span></td>
        <td><span class="tag tag-muted">${escapeHtml(o.mode)}</span></td>
        <td class="text-slate-400">${escapeHtml(o.source)}</td>
        <td class="font-mono text-slate-500">${escapeHtml(o.exchange_order_id || '—')}</td>
        <td class="max-w-[22rem] truncate text-slate-400" title="${escapeHtml(o.message || '')}">${escapeHtml(o.message || '')}</td>
      </tr>`;
  }).join('');
}

function renderSignals() {
  const body = $('signals-body');
  if (!state.signals.length) {
    body.innerHTML = '<tr class="empty-row"><td colspan="5">No engine signals yet — the bot records a row each time st.pine produces an action.</td></tr>';
    return;
  }
  body.innerHTML = state.signals.map((s) => {
    const action = String(s.action || '').toUpperCase();
    const tag = action === 'BUY' ? 'tag-buy' : action === 'SELL' ? 'tag-sell' : 'tag-muted';
    return `
    <tr>
      <td class="font-mono text-slate-400">${escapeHtml((s.ts || '').replace('T', ' '))}</td>
      <td><span class="tag ${tag}">${escapeHtml(action)}</span></td>
      <td><span class="tag ${s.accepted ? 'tag-buy' : 'tag-warn'}">${s.accepted ? 'placed' : 'held'}</span></td>
      <td class="max-w-[26rem] truncate text-slate-400" title="${escapeHtml(s.reason || '')}">${escapeHtml(s.reason || '')}</td>
      <td class="max-w-[20rem] truncate font-mono text-xs text-slate-500" title="${escapeHtml(s.payload || '')}">${escapeHtml(s.payload || '')}</td>
    </tr>`;
  }).join('');
}

function renderTrades() {
  const body = $('trades-body');
  if (!state.trades.length) {
    body.innerHTML = '<tr class="empty-row"><td colspan="10">No settled trades yet.</td></tr>';
    return;
  }
  body.innerHTML = state.trades.map((t) => `
    <tr>
      <td class="font-mono text-slate-400">${escapeHtml((t.exit_time || '').replace('T', ' '))}</td>
      <td class="font-mono">${escapeHtml(t.symbol)}</td>
      <td><span class="tag ${t.direction === 'LONG' ? 'tag-buy' : 'tag-sell'}">${escapeHtml(t.direction)}</span></td>
      <td class="num">${t.quantity}</td>
      <td class="num">${inr(t.entry_price, 2)}</td>
      <td class="num">${inr(t.exit_price, 2)}</td>
      <td class="num ${pnlClass(t.gross_pnl)}">${signedInr(t.gross_pnl)}</td>
      <td class="num text-slate-400">${inr(t.fees, 2)}</td>
      <td class="num ${pnlClass(t.net_pnl)}">${signedInr(t.net_pnl)}</td>
      <td class="text-slate-400">${escapeHtml(t.exit_reason || '—')}</td>
    </tr>`).join('');
}

/* ── Polling ───────────────────────────────────────────────────────── */

async function poll() {
  if (state.busy) return;
  try {
    const [status, portfolio, positions, orders, trades, signals] = await Promise.all([
      api(ENDPOINTS.status),
      api(ENDPOINTS.portfolio),
      api(ENDPOINTS.positions),
      api(ENDPOINTS.orders),
      api(ENDPOINTS.trades),
      api(ENDPOINTS.signals),
    ]);

    state.orders = orders.orders || [];
    state.signals = signals.signals || [];
    state.trades = trades.trades || [];
    state.lastSync = new Date();
    state.failures = 0;

    renderStatus(status);
    renderPortfolio(portfolio);
    renderPositions(positions);
    renderOrders();
    renderSignals();
    renderTrades();
    // Token health changes rarely; refresh it on a slow cadence instead of
    // adding a request to every 1.5s poll.
    if (!state.kite || Date.now() - (state.kiteCheckedAt || 0) > 20000) {
      state.kiteCheckedAt = Date.now();
      refreshKiteStatus();
    }
  } catch (error) {
    state.failures += 1;
    setPill('pill-feed', `Feed · error (${state.failures})`, 'bad');
    $('footer-right').textContent = `Sync failed: ${error.message}`;
    if (state.failures === 1) toast(`Polling failed: ${error.message}`, 'err');
  }
}

/* ── Actions ───────────────────────────────────────────────────────── */

function setActionStatus(message, kind) {
  const el = $('action-status');
  el.textContent = message;
  el.className = `header-action-status ${kind === 'err' ? 'neg' : kind === 'ok' ? 'pos' : 'text-slate-400'}`;
}

async function withBusy(fn) {
  state.busy = true;
  try {
    return await fn();
  } finally {
    state.busy = false;
    poll();
  }
}

async function applyMode() {
  const modeToggle = $('btn-mode-toggle');
  if (state.busy || modeToggle.disabled) return;

  const current = modeToggle.getAttribute('aria-checked') === 'true' ? 'LIVE' : 'PAPER';
  const target = current === 'LIVE' ? 'PAPER' : 'LIVE';

  state.modeConfirming = true;
  modeToggle.disabled = true;
  try {
    let confirm = false;
    if (target === 'LIVE') {
      confirm = await confirmDialog({
        title: 'Switch to LIVE trading?',
        body: 'This arms <strong>real orders with real money</strong> through Zerodha Kite. '
          + 'Entries are still sized against your margin and only placed during NSE F&amp;O hours.',
        okLabel: 'Arm LIVE trading',
      });
      if (!confirm) return;
    }

    state.modeConfirming = false;
    await withBusy(async () => {
      try {
        const result = await api(ENDPOINTS.toggle, {
          method: 'POST',
          body: JSON.stringify({ mode: target, confirm }),
        });
        toast(`Mode switched ${result.previous} → ${result.mode}`, 'ok');
        setActionStatus(`Mode switched to ${result.mode}.`, 'ok');
      } catch (error) {
        toast(`Mode switch failed: ${error.message}`, 'err');
        setActionStatus(error.message, 'err');
      }
    });
  } finally {
    state.modeConfirming = false;
    modeToggle.disabled = false;
  }
}

/** Poll Kite token health and reflect it in the controls panel. */
async function refreshKiteStatus() {
  try {
    const status = await api('/api/kite/status');
    const note = $('kite-note');
    const btn = $('btn-kite-login');
    const reconnectBtn = $('btn-feed-reconnect');
    const feed = status.feed || {};

    if (status.valid) {
      const age = num(feed.tick_age_seconds);
      if (feed.connected && age !== null && age < 120) {
        note.textContent = `Kite authenticated · GIFT ticks live (${timeAgo(age)})`;
        note.className = 'header-kite-note text-xs text-good';
      } else if (feed.starting) {
        note.textContent = 'Kite authenticated · GIFT market data is connecting…';
        note.className = 'header-kite-note text-xs text-warn';
      } else if (feed.connected) {
        note.textContent = 'Kite authenticated · connected, waiting for GIFT ticks';
        note.className = 'header-kite-note text-xs text-warn';
      } else {
        note.textContent = `Kite authenticated · GIFT feed stopped${feed.error ? `: ${feed.error}` : ''}`;
        note.className = 'header-kite-note text-xs text-warn';
      }
      btn.textContent = 'Refresh';
      btn.title = 'Refresh Kite login';
      $('kite-manual').classList.add('hidden');
      reconnectBtn.classList.remove('hidden');
      reconnectBtn.disabled = !!feed.starting;
    } else {
      note.textContent = status.detail || 'Not connected';
      note.className = 'header-kite-note text-xs text-warn';
      btn.textContent = status.api_key_present ? 'Connect' : 'API key missing';
      btn.title = status.api_key_present ? 'Connect to Kite' : 'KITE_API_KEY is missing';
      reconnectBtn.classList.add('hidden');
      $('kite-manual').classList.remove('hidden');
    }
    btn.disabled = !status.api_key_present;
    state.kite = status;
    note.title = note.textContent;
  } catch (error) {
    $('kite-note').textContent = `Token status unavailable: ${error.message}`;
    $('kite-note').className = 'header-kite-note text-xs text-bad';
    $('kite-note').title = $('kite-note').textContent;
  }
}

async function startKiteLogin() {
  await withBusy(async () => {
    try {
      const info = await api('/api/kite/login-url', { method: 'POST' });
      setActionStatus('Redirecting to Zerodha. After sign-in, the bot will validate the token and start market data.', 'info');
      window.location.assign(info.login_url);
    } catch (error) {
      toast(`Login failed: ${error.message}`, 'err');
      setActionStatus(error.message, 'err');
    }
  });
}

async function submitKiteToken() {
  const field = $('kite-token');
  const token = field.value.trim();
  if (!token) {
    toast('Paste a request_token first', 'info');
    return;
  }
  await withBusy(async () => {
    try {
      const result = await api('/api/kite/session', {
        method: 'POST',
        body: JSON.stringify({ request_token: token }),
      });
      field.value = '';
      const feed = result.feed || {};
      const feedMessage = feed.connected
        ? 'GIFT websocket connected; waiting for its first tick.'
        : feed.starting
          ? 'GIFT market data is connecting. Check the feed indicator for updates.'
          : feed.error || 'GIFT market data did not start.';
      toast(`Kite login accepted; ${feedMessage}`, feed.connected || feed.starting ? 'ok' : 'err');
      setActionStatus(
        `Token ${result.persisted ? 'saved' : 'active for this process only'}. ${feedMessage}`,
        feed.connected || feed.starting ? 'ok' : 'err',
      );
      await refreshKiteStatus();
    } catch (error) {
      toast(`Token rejected: ${error.message}`, 'err');
      setActionStatus(error.message, 'err');
    }
  });
}

async function reconnectGiftFeed() {
  await withBusy(async () => {
    try {
      const result = await api('/api/kite/feed/reconnect', { method: 'POST' });
      await refreshKiteStatus();
      const feed = result.feed || {};
      const message = feed.connected
        ? 'GIFT websocket connected; waiting for the next tick.'
        : feed.error || 'GIFT market data is connecting.';
      setActionStatus(message, feed.starting || feed.connected ? 'ok' : 'err');
      toast(message, feed.starting || feed.connected ? 'info' : 'err');
    } catch (error) {
      setActionStatus(error.message, 'err');
      toast(`Feed reconnect failed: ${error.message}`, 'err');
      await refreshKiteStatus();
    }
  });
}

async function togglePause() {
  const paused = !$('paused-banner').classList.contains('hidden');
  await withBusy(async () => {
    try {
      const result = await api(ENDPOINTS.pause, {
        method: 'POST',
        body: JSON.stringify({ paused: !paused, reason: 'DASHBOARD' }),
      });
      toast(result.paused ? 'Strategy paused' : 'Strategy resumed', 'ok');
      setActionStatus(result.paused ? 'New entries blocked.' : 'New entries allowed.', 'ok');
    } catch (error) {
      toast(`Pause failed: ${error.message}`, 'err');
    }
  });
}

async function panic() {
  const ok = await confirmDialog({
    title: '🚨 Panic square-off',
    body: 'This sends <strong>market orders to close every open position</strong> right now, '
      + 'then pauses the strategy. It cannot be undone.',
    okLabel: 'Square off now',
  });
  if (!ok) return;

  const btn = $('btn-panic');
  btn.disabled = true;
  btn.textContent = 'Squaring off…';
  await withBusy(async () => {
    try {
      const result = await api(ENDPOINTS.panic, {
        method: 'POST',
        body: JSON.stringify({ confirm: true, reason: 'PANIC_UI' }),
      });
      toast(result.reason || 'Square-off complete', result.ok ? 'ok' : 'err');
      setActionStatus(result.reason || '', result.ok ? 'ok' : 'err');
    } catch (error) {
      toast(`Panic failed: ${error.message}`, 'err');
      setActionStatus(error.message, 'err');
    } finally {
      btn.disabled = false;
      btn.textContent = 'Square off';
    }
  });
}

function exportCsv() {
  const rows = filteredOrders();
  if (!rows.length) {
    toast('Nothing to export', 'info');
    return;
  }
  const headers = ['ts', 'symbol', 'transaction_type', 'quantity', 'price', 'status', 'mode', 'source', 'exchange_order_id', 'message'];
  const csv = [
    headers.join(','),
    ...rows.map((row) => headers
      .map((key) => `"${String(row[key] ?? '').replace(/"/g, '""')}"`)
      .join(',')),
  ].join('\n');

  const url = URL.createObjectURL(new Blob([csv], { type: 'text/csv;charset=utf-8' }));
  const link = document.createElement('a');
  link.href = url;
  link.download = `order_book_${new Date().toISOString().slice(0, 16).replace(/[:T]/g, '')}.csv`;
  link.click();
  URL.revokeObjectURL(url);
}

/* ── Tabs ──────────────────────────────────────────────────────────── */

function switchTab(tab) {
  state.tab = tab;
  document.querySelectorAll('.tab').forEach((btn) => {
    btn.classList.toggle('tab-active', btn.dataset.tab === tab);
  });
  ['monitor', 'orders', 'analytics'].forEach((name) => {
    $(`view-${name}`).classList.toggle('hidden', name !== tab);
  });
  if (tab === 'orders') renderOrders();
}

/* ── Wiring ────────────────────────────────────────────────────────── */

function init() {
  document.querySelectorAll('.tab').forEach((btn) => {
    btn.addEventListener('click', () => switchTab(btn.dataset.tab));
  });

  $('btn-mode-toggle').addEventListener('click', applyMode);
  $('btn-kite-login').addEventListener('click', startKiteLogin);
  $('btn-feed-reconnect').addEventListener('click', reconnectGiftFeed);
  $('btn-kite-submit').addEventListener('click', submitKiteToken);
  $('kite-token').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') submitKiteToken();
  });
  $('btn-pause').addEventListener('click', togglePause);
  $('btn-resume').addEventListener('click', togglePause);
  $('btn-panic').addEventListener('click', panic);
  $('btn-orders-csv').addEventListener('click', exportCsv);

  let searchTimer = null;
  $('orders-search').addEventListener('input', () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(renderOrders, 120);
  });
  $('orders-mode').addEventListener('change', renderOrders);

  // Pause polling while the tab is hidden; refresh immediately on return so
  // the dashboard is never showing stale numbers when the user looks at it.
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) {
      clearInterval(state.timer);
    } else {
      poll();
      state.timer = setInterval(poll, POLL_MS);
    }
  });

  switchTab('monitor');
  poll();
  state.timer = setInterval(poll, POLL_MS);
}

document.addEventListener('DOMContentLoaded', init);
