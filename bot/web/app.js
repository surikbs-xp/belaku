/**
 * Belaku - frontend engine.
 *
 * Polls the FastAPI JSON endpoints on a fixed interval and patches the DOM in
 * place (no page reloads, no framework). Destructive actions (mode switch to
 * LIVE, panic) always go through an explicit confirm dialog first.
 *
 * The whole file is deliberately plain ES2020: no build step, no bundler.
 */

'use strict';

const POLL_MS = 1500;
/** Ledger rows (orders/trades/signals/positions) change only on a write. */
const LEDGER_MS = 5000;
/** Countdown refresh. Independent of POLL_MS so the seconds hand stays smooth. */
const TICK_MS = 250;
/** Log tail refresh. Independent of POLL_MS so the tab controls its own cost. */
const LOG_MS = 3000;
/** Tail length requested from /api/logs. */
const LOG_LINES = 600;
/** Samples kept for the client-side sparkline (last ~40 polls). */
const SPARK_POINTS = 40;
const ENDPOINTS = {
  status:    '/api/status',
  portfolio: '/api/portfolio',
  positions: '/api/positions?status=all&limit=200',
  orders:    '/api/orders?limit=500',
  trades:    '/api/trades?limit=500',
  signals:   '/api/signals?limit=100',
  analytics: '/api/analytics',
  holidays:  '/api/holidays',
  readiness: '/api/live/readiness',
  logs:      '/api/logs',
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
  // Candle countdown: the bar boundary comes from the server's bucket start, so
  // a skewed client clock cannot show the wrong time-to-close.
  candleBoundaryMs: null,
  serverOffsetMs: 0,
  // Client-side price samples; the API has no tick history endpoint.
  spark: [],
  // P&L calendar. `days` is the server's per-day rollup, `holidays` the NSE
  // closure list, and `month` the zero-indexed month currently displayed.
  days: {},
  holidays: {},
  month: null,
  selectedDay: null,
  calendarLoadedAt: 0,
  dayTrades: {},
  // LIVE pre-flight. Fetched on a slow cadence because the endpoint asks the
  // broker for margin and position state.
  readiness: null,
  readinessCheckedAt: 0,
  // Strategy configuration from /api/status, so the vote bar and labels follow
  // STRATEGY_SUPERTRENDS instead of hardcoded periods.
  strategyConfig: null,
  // Bot name from /api/status (BOT_NAME); drives the tab title and header.
  botName: null,
  // When the ledger rows were last fetched (see LEDGER_MS).
  ledgerLoadedAt: 0,
  // Log tail: the lines currently on screen, plus the interval that refreshes
  // them while the Logs tab is open.
  logsRaw: [],
  logTimer: null,
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

function formatIstTimestamp(value) {
  if (!value) return null;
  const timestamp = new Date(value);
  if (Number.isNaN(timestamp.getTime())) return null;
  return new Intl.DateTimeFormat('en-IN', {
    timeZone: 'Asia/Kolkata',
    day: '2-digit',
    month: 'short',
    year: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hourCycle: 'h23',
  }).format(timestamp);
}

function giftTimestamp(value, label) {
  const formatted = formatIstTimestamp(value);
  return formatted ? `${label} ${formatted} IST` : `${label} —`;
}

/** Clock time only (HH:MM IST) — denser than the full date for bar labels. */
function istClock(value) {
  if (!value) return null;
  const timestamp = new Date(value);
  if (Number.isNaN(timestamp.getTime())) return null;
  return new Intl.DateTimeFormat('en-GB', {
    timeZone: 'Asia/Kolkata',
    hour: '2-digit',
    minute: '2-digit',
    hourCycle: 'h23',
  }).format(timestamp);
}

/** Fixed-decimal number for indicator levels; em dash when missing. */
function level(value, decimals = 2) {
  const n = num(value);
  if (n === null) return '—';
  return n.toLocaleString('en-IN', {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  });
}

/** Signed index-point move, e.g. "+443.40". */
function signedPoints(value) {
  const n = num(value);
  if (n === null) return '—';
  return `${n >= 0 ? '+' : '-'}${level(Math.abs(n))}`;
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
  el.className = `toast toast-${kind}`;
  el.classList.remove('hidden');
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => el.classList.add('hidden'), 5000);
}

async function api(url, options) {
  const response = await fetch(url, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  // The session expired or was revoked. Reloading lands on the login page via
  // the server's redirect, which is better than a toast the operator may miss
  // while the dashboard silently stops updating.
  if (response.status === 401) {
    window.location.replace('/login');
    throw new Error('Session expired');
  }
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
  });
}

/* ── Header + monitor ──────────────────────────────────────────────── */

/**
 * Bot name from the server (BOT_NAME). Applied to the tab title and the header
 * so a rename needs no frontend edit; the brand mark takes its first letter.
 */
function applyBotName(name) {
  const label = String(name || '').trim();
  if (!label || label === state.botName) return;
  state.botName = label;
  document.title = label;
  setText('brand-title', label);
  setText('brand-mark', label.charAt(0).toUpperCase());
}

function renderStatus(status) {
  const mode = status.mode;
  applyBotName(status.bot_name);
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

  renderRiskBanner(status.engine || {});

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

  renderExecutionMeta(status);
}

/** Static execution facts a trader checks before trusting a fill. */
function renderExecutionMeta(status) {
  const config = status.config || {};
  const symbol = status.symbol || {};
  const feed = (status.engine || {}).feed || {};
  state.strategyConfig = status.strategy_config || null;
  setText('exec-lots', `${config.default_lots ?? '—'} lot`);
  setText('exec-product', `${config.order_type || '—'} · ${config.product || '—'}`);
  $('exec-product').title = symbol.next && symbol.next !== symbol.current
    ? `Rolls to ${symbol.next} on expiry`
    : '';
  setText('indicator-source', `GIFT ${feed.interval || '—'} · ${feed.bars ?? 0} bars · source ${feed.history_source || '—'}`);
  const summary = state.strategyConfig;
  if (summary?.supertrend_label) {
    setText('st-config-label', `SuperTrend ${summary.supertrend_label} with PSAR`);
  }
}

/**
 * GIFT Nifty signal state. This is the bot's brain: it shows what the st.pine
 * conditions currently say on GIFT Nifty, whether the series is warm enough to
 * judge, and whether an order would be allowed right now.
 */
function renderEngine(eng, market) {
  const feed = eng.feed || {};
  const closedValues = eng.previous_values
    || (Object.keys(eng.values || {}).length ? eng.values : null);
  const values = eng.current_values || closedValues || {};
  const conditions = eng.conditions || {};
  const indicatorConditions = eng.previous_conditions || conditions;
  const hasIndicatorValues = !!eng.current_values || !!eng.ready;

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
  const warmupNeeded = Math.max(1, Number(eng.warmup_needed) || 12);
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

  // Accent the whole hero panel off the signal so the route reads at a glance.
  const signalPanel = $('signal-panel');
  signalPanel.classList.remove('is-bull', 'is-bear', 'is-flat', 'is-warm');
  signalPanel.classList.add(
    !ready ? 'is-warm' : signal === 'BULL' ? 'is-bull' : signal === 'BEAR' ? 'is-bear' : 'is-flat',
  );
  $('signal-glyph').textContent =
    !ready ? '…' : signal === 'BULL' ? '▲' : signal === 'BEAR' ? '▼' : '•';

  setText('e-signal', ready ? signal : 'WARMING UP',
    ready ? pnlClass(signal === 'BULL' ? 1 : signal === 'BEAR' ? -1 : 0) : 'flat');
  setText(
    'e-signal-sub',
    ready
      ? (eng.last_detail || 'Conditions evaluated on the latest completed candle.')
      : `Collecting closed candles · ${Math.max(0, warmupNeeded - warmupHave)} more to calculate indicators`,
  );
  setText('e-bars', ready ? `${warmupHave} closed` : `${warmupHave} / ${warmupNeeded}`);

  const hasLivePrice = feed.last_price !== null && feed.last_price !== undefined;
  const signalSourcePrice = hasLivePrice ? feed.last_price : feed.last_candle_close;
  const signalSourceAt = hasLivePrice ? feed.last_tick_at : feed.last_candle_at;
  setText('e-gift-price', signalSourcePrice === null || signalSourcePrice === undefined
    ? '—'
    : inr(signalSourcePrice, 2));
  setText('e-gift-timestamp', giftTimestamp(
    signalSourceAt,
    hasLivePrice ? 'Last tick' : 'Last candle close',
  ));
  setText('e-gift-sub', `${feed.bars ?? 0} bars · ${feed.ticks ?? 0} ticks`);
  renderExecQuote(feed.exec, signalSourcePrice);
  setText('e-exec', eng.execution_symbol || '—');
  setText('e-exec-sub', market.is_open
    ? (eng.mode === 'PAPER' ? 'PAPER — simulated at market price' : 'NSE F&O open — orders live')
    : (eng.mode === 'PAPER'
      ? 'PAPER — completed GIFT close used after hours'
      : `orders held · ${market.reason || 'closed'}`));

  renderCandle(eng, feed, ready, warmupHave);
  renderSpark(signalSourcePrice);

  const box = $('conditions');
  const displayedBarAt = eng.current_values
    ? eng.current_bar_at
    : (eng.previous_bar_at || eng.last_bar_at);
  const indicatorTime = formatIstTimestamp(displayedBarAt);
  const indicatorLabel = eng.current_bar_in_progress ? 'Forming candle' : 'Latest closed candle';
  const indicatorTimestamp = indicatorTime
    ? `${indicatorLabel} · ${indicatorTime} IST`
    : 'Timestamp unavailable';
  ['e-st-time', 'e-psar-time'].forEach((id) => setText(id, indicatorTimestamp));
  if (!ready) {
    box.innerHTML = `
      <div class="warmup-message">
        <strong>Rule checks will appear when indicators are ready.</strong>
        <span>${warmupHave} of ${warmupNeeded} closed candles collected. No signal is being evaluated yet.</span>
      </div>`;
    ['e-psar', 'e-st'].forEach((id) => setText(id, '—'));
  } else {
    const order = ['supertrend', 'psar'];
    const labels = {
      supertrend: `Trend vote · ${state.strategyConfig?.min_agree ?? 2} of ${state.strategyConfig?.supertrends?.length ?? 3}`,
      psar: 'Price vs PSAR',
    };
    box.innerHTML = ['buy', 'sell'].map((side) => `
      <div class="condition-panel condition-panel-${side}">
        <div class="condition-panel-heading">
          <strong>${side.toUpperCase()}</strong>
          <span>Both checks must pass</span>
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

  }

  if (hasIndicatorValues) {
    const psarConditions = eng.current_values
      ? eng.current_conditions || conditions
      : indicatorConditions;
    const stConfig = Array.isArray(values.supertrend_config) && values.supertrend_config.length
      ? values.supertrend_config
      : (state.strategyConfig?.supertrends || []);
    const stTotal = Number(values.supertrend_total) || stConfig.length || 3;
    setText('e-psar', level(values.psar));
    setText('e-psar-sub', psarConditions.buy_psar
      ? 'Price above · BUY condition passes'
      : psarConditions.sell_psar ? 'Price below · SELL condition passes' : 'BUY above · SELL below');
    setText('e-st', `${values.supertrend_bull ?? 0}/${stTotal} bullish`);
    setText('e-st-sub', stConfig.length
      ? stConfig.map((item) => `${item.length}×${Number(item.multiplier)}`).join(' · ')
      : '—');
    // The bar, its captions and the count above all come from the same
    // index-aligned list, so they cannot disagree about which instance voted.
    renderStVotes(stConfig, values);
    // PSAR is a single binary check: close above SAR is bullish.
    const psarAbove = !!psarConditions.buy_psar;
    const psarBelow = !!psarConditions.sell_psar;
    renderVotes('psar-votes', psarAbove ? 1 : 0, psarBelow ? 1 : 0, 1);
  } else {
    ['e-psar', 'e-st'].forEach((id) => setText(id, '—'));
    renderStVotes(state.strategyConfig?.supertrends || [], {});
    renderVotes('psar-votes', 0, 0, 1);
  }

  setText('e-counters',
    `${eng.evaluations ?? 0} evals · ${eng.signals_fired ?? 0} signals · `
    + `${eng.orders_sent ?? 0} sent · ${(eng.orders_blocked ?? 0) + (eng.deferred ?? 0)} held`);

  if (eng.last_error) {
    setActionStatus(`Engine: ${eng.last_error}`, 'err');
  }
}

/**
 * Segmented green/red vote bar for a single count-based check (PSAR). `segments`
 * slots are drawn: green for each bullish vote, red for each bearish vote, then
 * grey placeholders — so both the count and the direction read from one visual
 * language. PSAR uses one slot because it is a single price-position check.
 */
function renderVotes(id, bull, bear, segments) {
  const host = $(id);
  if (!host) return;
  const up = Math.max(0, num(bull) || 0);
  const down = Math.max(0, num(bear) || 0);
  if (!up && !down) {
    host.innerHTML = '';
    return;
  }
  const filled = [
    ...Array(Math.min(up, segments)).fill('bull'),
    ...Array(Math.min(down, segments)).fill('bear'),
  ].slice(0, segments);
  while (filled.length < segments) filled.push('');
  host.innerHTML = filled
    .map((kind) => `<span class="vote-seg ${kind}"></span>`)
    .join('');
}

/**
 * SuperTrend vote bar and its per-instance captions, built in one pass so a
 * segment can never be coloured differently from its caption.
 *
 * `config` is index-aligned with `values.supertrend_config` — both come from
 * `strategy_params()` in the same order — so `item.direction` is the exact
 * direction that produced segment *i*, and `item.key` is the exact value
 * rendered beneath it. Deriving the count and the colours from one source is
 * what keeps the bar consistent with the caption and with the "N/3 bullish"
 * headline; filling the bar from aggregate bull/bear counts instead would place
 * the votes in a different order and mis-colour them.
 *
 * Empty directions (warm-up, or a missing config) draw unfilled slots, so the
 * bar still reserves the right number of cells.
 */
function renderStVotes(config, values) {
  const bar = $('st-votes');
  const legend = $('st-legend');
  if (!bar) return;
  const items = Array.isArray(config) ? config : [];
  const segments = Math.max(items.length, 1);

  if (bar.childElementCount !== segments) {
    bar.innerHTML = Array(segments)
      .fill('<span class="vote-seg"></span>')
      .join('');
  }
  Array.from(bar.children).forEach((seg, i) => {
    const item = items[i];
    const direction = item ? num(item.direction) : null;
    // Both producers agree: negative direction is bullish, positive bearish.
    const kind = direction === null || direction === 0
      ? ''
      : direction < 0 ? 'bull' : 'bear';
    seg.classList.toggle('bull', kind === 'bull');
    seg.classList.toggle('bear', kind === 'bear');
    seg.title = item
      ? `${item.length}×${Number(item.multiplier)} · ${kind === 'bull' ? 'bullish' : kind === 'bear' ? 'bearish' : 'flat'}`
      : '';
  });

  if (!legend) return;
  legend.innerHTML = items.map((item) => {
    const direction = num(item.direction);
    const kind = direction === null || direction === 0
      ? ''
      : direction < 0 ? 'bull' : 'bear';
    const label = `${item.length}×${Number(item.multiplier)}`;
    return `<span class="${kind}" title="${label} · ${kind === 'bull' ? 'bullish' : kind === 'bear' ? 'bearish' : 'flat'}">${level(values[item.key])}</span>`;
  }).join('');
}

/**
 * Candle countdown. The server's bucket start (`current_bar_at`) anchors the
 * boundary, so the client clock only has to measure elapsed time — a skewed
 * clock or a different timezone cannot shift the countdown.
 */
function renderCandle(eng, feed, ready, warmupHave) {
  const intervalSeconds = num(feed.interval_seconds) || 900;
  // Label the panel from the live interval so the heading cannot drift from the
  // timeframe the engine is actually evaluating.
  const intervalLabel = document.getElementById('candle-interval-label');
  if (intervalLabel) {
    intervalLabel.textContent = `${Math.round(intervalSeconds / 60)}-minute`;
  }
  const barAt = eng.current_bar_at || eng.previous_bar_at || eng.last_bar_at;
  const boundary = istClock(barAt);

  if (boundary) {
    const [hours, minutes] = boundary.split(':').map(Number);
    const nowIst = new Date(Date.now() + state.serverOffsetMs);
    const nowParts = new Intl.DateTimeFormat('en-GB', {
      timeZone: 'Asia/Kolkata', hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23',
    }).formatToParts(nowIst);
    const get = (type) => Number(nowParts.find((p) => p.type === type)?.value || 0);
    const elapsed = (get('hour') * 3600 + get('minute') * 60 + get('second'))
      - (hours * 3600 + minutes * 60);
    const remaining = Math.max(0, intervalSeconds - ((elapsed + intervalSeconds) % intervalSeconds));
    state.candleBoundaryMs = remaining;
  } else {
    state.candleBoundaryMs = null;
  }

  setText('candle-open', boundary ? `${boundary} IST` : '—');
  setText('candle-bar', eng.previous_bar_at ? `${istClock(eng.previous_bar_at)} IST` : '—');
  setPill(
    'candle-pill',
    !ready ? 'Warming up' : eng.current_bar_in_progress ? 'Forming' : 'Closed',
    !ready ? 'idle' : eng.current_bar_in_progress ? 'paper' : 'live',
  );
  tickCountdown();
}

/** Repaint only the countdown text; called on TICK_MS, not on every poll. */
function tickCountdown() {
  const el = $('candle-countdown');
  if (!el) return;
  const remaining = state.candleBoundaryMs;
  if (remaining === null) {
    el.textContent = '—:—';
    el.classList.remove('imminent');
    return;
  }
  el.textContent = `${Math.floor(remaining / 60)}:${String(remaining % 60).padStart(2, '0')}`;
  el.classList.toggle('imminent', remaining <= 30);
}

/** Client-side sparkline of recent polls; the API exposes no tick history. */
function renderSpark(price) {
  const value = num(price);
  if (value !== null) {
    const last = state.spark[state.spark.length - 1];
    if (last !== value) state.spark.push(value);
    if (state.spark.length > SPARK_POINTS) state.spark.shift();
  }

  const host = $('gift-spark');
  if (state.spark.length < 2) {
    host.innerHTML = '<span class="spark-empty">Building price trail…</span>';
    return;
  }
  const min = Math.min(...state.spark);
  const max = Math.max(...state.spark);
  const span = max - min || 1;
  host.innerHTML = state.spark.map((sample, index) => {
    const height = 12 + ((sample - min) / span) * 88;
    const previous = index === 0 ? sample : state.spark[index - 1];
    const trend = sample > previous ? 'up' : sample < previous ? 'down' : '';
    return `<span class="spark-bar ${trend}" style="height:${height.toFixed(1)}%"></span>`;
  }).join('');
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
    body.innerHTML = '<tr class="empty-row"><td colspan="8">Flat — no open positions.</td></tr>';
    return;
  }
  body.innerHTML = open.map((p) => {
    // Points is the raw price move; PnL is that move times quantity and lot
    // direction. Traders size off points, so show both.
    const entry = num(p.entry_price);
    const mark = num(p.mark_price);
    const move = entry === null || mark === null ? null : mark - entry;
    const points = move === null ? null : move * (p.side === 'LONG' ? 1 : -1);
    return `
    <tr>
      <td class="font-mono">${escapeHtml(p.symbol)}</td>
      <td><span class="tag ${p.side === 'LONG' ? 'tag-buy' : 'tag-sell'}">${escapeHtml(p.side)}</span></td>
      <td class="num">${p.quantity}</td>
      <td class="num">${inr(p.entry_price, 2)}</td>
      <td class="num">${inr(p.mark_price, 2)}</td>
      <td class="num ${pnlClass(p.unrealized_pnl)}">${signedInr(p.unrealized_pnl)}</td>
      <td class="num ${pnlClass(points)}">${points === null ? '—' : `${points >= 0 ? '+' : ''}${level(points)}`}</td>
      <td><span class="tag tag-muted">${escapeHtml(p.mode)}</span></td>
    </tr>`;
  }).join('');
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


/* ── P&L calendar ──────────────────────────────────────────────────── */

const MONTH_NAMES = ['January', 'February', 'March', 'April', 'May', 'June',
  'July', 'August', 'September', 'October', 'November', 'December'];

/** IST calendar date (YYYY-MM-DD) for a timestamp, matching the server rollup. */
function istDateKey(value) {
  const date = value ? new Date(value) : new Date();
  if (Number.isNaN(date.getTime())) return null;
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Asia/Kolkata', year: 'numeric', month: '2-digit', day: '2-digit',
  }).formatToParts(date);
  const get = (type) => parts.find((p) => p.type === type)?.value;
  return `${get('year')}-${get('month')}-${get('day')}`;
}

/** Signed P&L at a compact scale, e.g. "+23.8k" / "-4.7k" / "+386". */
function compactPnl(value) {
  const n = num(value);
  if (n === null || n === 0) return '—';
  const sign = n > 0 ? '+' : '-';
  const abs = Math.abs(n);
  if (abs >= 100000) return `${sign}${(abs / 100000).toFixed(1)}L`;
  if (abs >= 1000) return `${sign}${(abs / 1000).toFixed(1)}k`;
  return `${sign}${Math.round(abs)}`;
}

/** 1-3 intensity bucket for the heat shading, relative to the month's largest day. */
function intensityBucket(value, largest) {
  if (!largest) return 1;
  const ratio = Math.abs(value) / largest;
  if (ratio >= 0.66) return 3;
  if (ratio >= 0.33) return 2;
  return 1;
}

function renderCalendar() {
  const grid = $('cal-grid');
  if (!grid) return;

  const nowKey = istDateKey();
  if (!state.month) {
    const [year, month] = nowKey.split('-').map(Number);
    state.month = { year, month: month - 1 };
  }
  const { year, month } = state.month;

  const first = new Date(Date.UTC(year, month, 1));
  const daysInMonth = new Date(Date.UTC(year, month + 1, 0)).getUTCDate();
  // Monday-first grid, matching the NSE trading week and the header row.
  const leading = (first.getUTCDay() + 6) % 7;

  const monthDays = Object.entries(state.days)
    .filter(([key]) => key.startsWith(`${year}-${String(month + 1).padStart(2, '0')}`));
  const largest = Math.max(1, ...monthDays.map(([, day]) => Math.abs(num(day.net_pnl) || 0)));
  const monthNet = monthDays.reduce((sum, [, day]) => sum + (num(day.net_pnl) || 0), 0);
  const monthTrades = monthDays.reduce((sum, [, day]) => sum + (day.trades || 0), 0);
  const profitableDays = monthDays.filter(([, day]) => (num(day.net_pnl) || 0) > 0).length;

  setText('cal-caption', `${MONTH_NAMES[month]} ${year} · ${signedInr(monthNet, 0)} · `
    + `${monthTrades} trade${monthTrades === 1 ? '' : 's'} · ${profitableDays} green day${profitableDays === 1 ? '' : 's'}`);

  const cells = [];
  for (let i = 0; i < leading; i += 1) cells.push('<div class="cal-cell is-empty"></div>');

  for (let dayNumber = 1; dayNumber <= daysInMonth; dayNumber += 1) {
    const key = `${year}-${String(month + 1).padStart(2, '0')}-${String(dayNumber).padStart(2, '0')}`;
    const rollup = state.days[key];
    const holiday = state.holidays[key];
    const net = num(rollup?.net_pnl);
    const weekday = new Date(Date.UTC(year, month, dayNumber)).getUTCDay();
    const isWeekend = weekday === 0 || weekday === 6;

    const classes = ['cal-cell'];
    if (key === nowKey) classes.push('is-today');
    if (key === state.selectedDay) classes.push('is-selected');
    if (!rollup && (holiday || isWeekend)) classes.push('is-blank');
    if (holiday) classes.push('cal-holiday');
    if (net !== null && net !== 0) {
      classes.push(net > 0 ? 'cal-win' : 'cal-loss');
      classes.push(`${net > 0 ? 'cal-win' : 'cal-loss'}-${intensityBucket(net, largest)}`);
    }

    let meta;
    if (holiday) meta = escapeHtml(holiday);
    else if (net !== null && net !== 0) {
      meta = `${rollup.wins}W/${rollup.losses}L · ${rollup.trades} trade${rollup.trades === 1 ? '' : 's'}`;
    } else if (rollup) meta = `${rollup.trades} trade${rollup.trades === 1 ? '' : 's'} · flat`;
    else meta = isWeekend ? '' : 'no trades';

    const pnl = net !== null && net !== 0
      ? `<span class="cal-pnl">${compactPnl(net)}</span>`
      : '<span class="cal-pnl">—</span>';

    const title = holiday
      ? `${key} · ${holiday}`
      : rollup
        ? `${key} · ${signedInr(net, 0)} · ${rollup.trades} trades`
        : `${key} · no trades`;
    const tag = rollup ? 'button' : 'div';

    cells.push(`<${tag} class="${classes.join(' ')}" title="${escapeHtml(title)}"`
      + `${rollup ? ` type="button" data-cal-day="${key}"` : ''}>`
      + `<span class="cal-day">${dayNumber}</span>${pnl}`
      + `<span class="cal-meta">${meta}</span></${tag}>`);
  }

  // Pad the final week so the grid keeps its 7-column rhythm.
  while (cells.length % 7 !== 0) cells.push('<div class="cal-cell is-empty"></div>');
  grid.innerHTML = cells.join('');

  if (!monthDays.length) setText('cal-caption', `${MONTH_NAMES[month]} ${year} · no closed trades`);
}

/** Per-trade detail for the clicked calendar day. */
function renderCalendarDay(key) {
  const panel = $('cal-day-panel');
  if (!key) {
    panel.classList.add('hidden');
    return;
  }
  state.selectedDay = key;
  const rollup = state.days[key];
  const holiday = state.holidays[key];
  const trades = (state.dayTrades[key] || []).slice().reverse();

  $('cal-day-title').textContent = key;
  setText('cal-day-summary', rollup
    ? `${signedInr(rollup.net_pnl, 0)} · ${rollup.trades} trade${rollup.trades === 1 ? '' : 's'} · ${rollup.wins}W/${rollup.losses}L`
    : (holiday ? `Exchange holiday — ${holiday}` : 'No closed trades'));

  const body = $('cal-day-body');
  if (!trades.length) {
    body.innerHTML = `<tr class="empty-row"><td colspan="8">${holiday
      ? `No trading — ${escapeHtml(holiday)}`
      : 'No closed trades on this day.'}</td></tr>`;
  } else {
    body.innerHTML = trades.map((t) => {
      const move = (num(t.exit_price) - num(t.entry_price)) * (t.direction === 'LONG' ? 1 : -1);
      return `
      <tr>
        <td class="font-mono text-slate-400">${escapeHtml(String(t.exit_time || '').slice(11, 19))}</td>
        <td><span class="tag ${t.direction === 'LONG' ? 'tag-buy' : 'tag-sell'}">${escapeHtml(t.direction)}</span></td>
        <td class="num">${t.quantity}</td>
        <td class="num">${inr(t.entry_price, 2)}</td>
        <td class="num">${inr(t.exit_price, 2)}</td>
        <td class="num ${pnlClass(move)}">${signedPoints(move)}</td>
        <td class="num ${pnlClass(t.net_pnl)}">${signedInr(t.net_pnl)}</td>
        <td class="text-slate-400">${escapeHtml(t.exit_reason || '—')}</td>
      </tr>`;
    }).join('');
  }
  panel.classList.remove('hidden');
}

function shiftCalendarMonth(delta) {
  const { year, month } = state.month;
  const next = new Date(Date.UTC(year, month + delta, 1));
  state.month = { year: next.getUTCFullYear(), month: next.getUTCMonth() };
  state.selectedDay = null;
  renderCalendar();
  renderCalendarDay(null);
}

/** Load the daily rollup + holiday list that back the calendar. */
async function loadCalendarData() {
  const [analytics, holidays] = await Promise.all([
    api(ENDPOINTS.analytics),
    api(ENDPOINTS.holidays).catch(() => ({ holidays: {} })),
  ]);
  state.days = Object.fromEntries((analytics.days || []).map((day) => [day.date, day]));
  state.holidays = holidays.holidays || {};
  state.dayTrades = {};
  for (const trade of state.trades) {
    const key = String(trade.exit_time || '').slice(0, 10);
    if (!key) continue;
    (state.dayTrades[key] ||= []).push(trade);
  }
  state.calendarLoadedAt = Date.now();
}

function renderTrades() {
  const body = $('trades-body');
  if (!state.trades.length) {
    body.innerHTML = '<tr class="empty-row"><td colspan="10">No settled trades yet.</td></tr>';
    return;
  }
  body.innerHTML = state.trades.map((t) => {
    const move = num(t.exit_price) !== null && num(t.entry_price) !== null
      ? (num(t.exit_price) - num(t.entry_price)) * (t.direction === 'LONG' ? 1 : -1)
      : null;
    return `
    <tr>
      <td class="font-mono text-slate-400">${escapeHtml((t.exit_time || '').replace('T', ' '))}</td>
      <td class="font-mono">${escapeHtml(t.symbol)}</td>
      <td><span class="tag ${t.direction === 'LONG' ? 'tag-buy' : 'tag-sell'}">${escapeHtml(t.direction)}</span></td>
      <td class="num">${t.quantity}</td>
      <td class="num">${inr(t.entry_price, 2)}</td>
      <td class="num">${inr(t.exit_price, 2)}</td>
      <td class="num ${pnlClass(move)}">${move === null ? '—' : signedPoints(move)}</td>
      <td class="num ${pnlClass(t.gross_pnl)}">${signedInr(t.gross_pnl)}</td>
      <td class="num text-slate-400">${inr(t.fees, 2)}</td>
      <td class="num ${pnlClass(t.net_pnl)}">${signedInr(t.net_pnl)}</td>
      <td class="text-slate-400">${escapeHtml(t.exit_reason || '—')}</td>
    </tr>`;
  }).join('');
}


/* ── Polling ───────────────────────────────────────────────────────── */

/**
 * Ledger rows: positions, orders, trades and signals. These only change when the
 * bot writes a row, so they are fetched on LEDGER_MS rather than every poll.
 */
async function loadLedger() {
  const [positions, orders, trades, signals] = await Promise.all([
    api(ENDPOINTS.positions),
    api(ENDPOINTS.orders),
    api(ENDPOINTS.trades),
    api(ENDPOINTS.signals),
  ]);
  state.orders = orders.orders || [];
  state.signals = signals.signals || [];
  state.trades = trades.trades || [];
  renderPositions(positions);
  renderOrders();
  renderSignals();
  renderTrades();
}

async function poll() {
  if (state.busy) return;
  try {
    // Fast lane: only what changes tick-by-tick. The ledger endpoints are
    // fetched on a slower cadence below, so a poll is 2 requests instead of 6.
    const [status, portfolio] = await Promise.all([
      api(ENDPOINTS.status),
      api(ENDPOINTS.portfolio),
    ]);

    state.lastSync = new Date();
    state.failures = 0;

    // Track server clock drift so the countdown can trust it.
    const serverTime = status.server_time ? new Date(status.server_time) : null;
    if (serverTime && !Number.isNaN(serverTime.getTime())) {
      state.serverOffsetMs = serverTime.getTime() - Date.now();
    }

    renderStatus(status);
    renderPortfolio(portfolio);

    // Ledger rows only change when an order or trade is written, so they ride a
    // slower cadence. The first poll always loads them.
    if (Date.now() - (state.ledgerLoadedAt || 0) > LEDGER_MS) {
      state.ledgerLoadedAt = Date.now();
      loadLedger().catch(() => { /* keep the last good rows */ });
    }
    // The calendar rollup and holiday list change only when a trade settles, so
    // refetch them on the tab that shows them and slowly elsewhere.
    if (state.tab === 'analytics' || Date.now() - state.calendarLoadedAt > 60000) {
      loadCalendarData()
        .then(() => {
          renderCalendar();
          if (state.selectedDay) renderCalendarDay(state.selectedDay);
        })
        .catch(() => { /* the tab still shows the ledger */ });
    }
    // Token health changes rarely; refresh it on a slow cadence instead of
    // adding a request to every 1.5s poll.
    if (!state.kite || Date.now() - (state.kiteCheckedAt || 0) > 20000) {
      state.kiteCheckedAt = Date.now();
      refreshKiteStatus();
    }
    // Pre-flight is only meaningful once LIVE is armed, and it costs a broker
    // round-trip, so it rides the same slow cadence as the token check.
    if (status.mode === 'LIVE' && Date.now() - state.readinessCheckedAt > 20000) {
      state.readinessCheckedAt = Date.now();
      refreshReadiness();
    } else if (status.mode !== 'LIVE') {
      state.readiness = null;
      renderReadiness();
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

/** Fetch the LIVE pre-flight report. Failures are non-fatal: the banner just
 *  keeps its previous state rather than claiming LIVE is safe. */
async function refreshReadiness() {
  try {
    state.readiness = await api(ENDPOINTS.readiness);
  } catch (error) {
    state.readiness = { ready: false, checks: [], error: error.message };
  }
  renderReadiness();
}

/** Show the pre-flight banner only when LIVE is armed and something blocks it. */
function renderReadiness() {
  const banner = $('live-banner');
  const report = state.readiness;
  const show = !!report && !report.ready;
  banner.classList.toggle('hidden', !show);
  if (!show) return;

  const blocking = new Set(report.blocking || []);
  const failed = (report.checks || []).filter((c) => !c.ok);
  const list = failed.length ? failed : (report.checks || []);

  $('live-banner-title').textContent = report.error
    ? `LIVE pre-flight unavailable: ${report.error}`
    : `LIVE blocked by ${blocking.size} check${blocking.size === 1 ? '' : 's'}`;

  const ul = $('live-banner-checks');
  ul.replaceChildren(...list.map((check) => {
    const li = document.createElement('li');
    li.className = `live-check ${check.ok ? 'is-ok' : 'is-bad'}`;
    const mark = document.createElement('span');
    mark.className = 'live-check-mark';
    mark.textContent = check.ok ? '✓' : '✕';
    const text = document.createElement('span');
    text.textContent = check.detail;
    li.append(mark, text);
    return li;
  }));

  // Re-login only helps when the session itself is the problem.
  const needsLogin = blocking.has('kite_session') || blocking.has('credentials');
  $('btn-kite-relogin').classList.toggle('hidden', !needsLogin);
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

/* Risk breach banner. The engine latches a breach and pauses entries, so this
   is the operator's only signal that the bot stopped trading on its own. */
function renderRiskBanner(engine) {
  const banner = $('risk-banner');
  const breach = engine.risk_breach;
  if (!breach) {
    banner.classList.add('hidden');
    return;
  }
  const label = 'Stop-loss triggered';
  $('risk-banner-title').textContent = label;
  $('risk-banner-sub').textContent = breach.detail || '';
  banner.classList.remove('hidden');
}

async function clearRiskBreach() {
  const ok = await confirmDialog({
    title: 'Acknowledge risk breach',
    body: 'This clears the latched limit and allows new entries again. '
      + 'It does <strong>not</strong> reopen any position. Only continue if you '
      + 'have reviewed why the limit fired.',
    okLabel: 'Acknowledge',
    danger: false,
  });
  if (!ok) return;
  await withBusy(async () => {
    try {
      await api('/api/risk/clear', { method: 'POST' });
      toast('Risk breach acknowledged', 'ok');
      await poll();
    } catch (error) {
      toast(`Risk: ${error.message}`, 'err');
    }
  });
}

/* Nifty future ticks, shown beside GIFT Nifty. The basis is the interesting
   number: it is what the signal has to overcome before the trade is profitable,
   so it is shown next to the price rather than hidden. */
function renderExecQuote(exec, giftPrice) {
  if (!exec) {
    setText('e-exec-symbol', '—');
    setText('e-exec-price', '—');
    setText('e-exec-basis', '—');
    setText('e-exec-tick', 'Not configured');
    return;
  }

  setText('e-exec-symbol', exec.symbol || '—');

  const hasPrice = exec.price !== null && exec.price !== undefined;
  setText('e-exec-price', hasPrice
    ? inr(exec.price, 2)
    : '—');

  const hasGift = giftPrice !== null && giftPrice !== undefined;
  if (hasPrice && hasGift) {
    const basis = exec.price - giftPrice;
    setText('e-exec-basis', `${basis >= 0 ? '+' : ''}${basis.toFixed(2)} vs GIFT`);
  } else {
    setText('e-exec-basis', '—');
  }

  // Say where the number came from: a live tick is trustworthy, a polled quote
  // is up to one mark cycle old, and stale means the feed has stopped.
  if (!hasPrice) {
    setText('e-exec-tick', exec.subscribed
      ? 'Subscribed · awaiting first tick'
      : 'Waiting for a quote…');
    return;
  }
  const age = exec.age_seconds;
  const when = exec.last_tick_at ? formatIstTimestamp(exec.last_tick_at) : null;
  const how = exec.source === 'tick' ? 'tick' : 'quote';
  const stale = age !== null && age > 60;
  setText('e-exec-tick', when
    ? `Last ${how} ${when} IST${stale ? ' · stale' : ''}`
    : `Last ${how}${stale ? ' · stale' : ''}`);
}

async function signOut() {
  try {
    await api('/api/auth/logout', { method: 'POST' });
  } catch (error) {
    // Even if the call fails the cookie may be gone; the reload decides.
  }
  window.location.replace('/login');
}

/* Reveal the sign-out control only when a password is actually configured,
   so a local no-password dashboard is not cluttered with a dead button. */
async function initAuth() {
  try {
    const status = await api('/api/auth/status');
    if (status && status.auth_required) {
      $('btn-logout').classList.remove('hidden');
    }
  } catch (error) {
    // A failure here means the session is already gone; api() redirects.
  }
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

/* ── Logs ──────────────────────────────────────────────────────────── */

/**
 * Split a log line into its timestamp, level, logger and message.
 *
 * Mirrors the server's "%(asctime)s %(levelname)s %(name)s: %(message)s".
 * Anything that does not match (a traceback frame, a stray print) is returned
 * as a continuation so it stays attached to the entry that explains it.
 */
const LOG_LINE_RE = /^(\d{4}-\d{2}-\d{2}[ T][\d:,.]*)\s+(DEBUG|INFO|WARNING|ERROR|CRITICAL)\s+([\w.]*):?\s?([\s\S]*)$/;

function parseLogLine(line) {
  const match = LOG_LINE_RE.exec(line);
  if (!match) return { continuation: true, text: line };
  return {
    continuation: false,
    ts: match[1],
    level: match[2],
    logger: match[3],
    message: match[4],
  };
}

function renderLogLine(line) {
  const parsed = parseLogLine(line);
  if (parsed.continuation) {
    return `<div class="log-line is-continuation">${escapeHtml(parsed.text)}</div>`;
  }
  const severe = parsed.level === 'ERROR' || parsed.level === 'CRITICAL';
  return `<div class="log-line${severe ? ' has-error' : ''}">
    <span class="log-ts">${escapeHtml(parsed.ts)}</span>
    <span class="log-level log-level-${parsed.level}">${parsed.level}</span>
    <span class="log-logger">${escapeHtml(parsed.logger)}</span>
    <span class="log-message">${escapeHtml(parsed.message)}</span>
  </div>`;
}

/** True when the viewport is scrolled to (or near) the bottom. */
function logsAtBottom(el) {
  return el.scrollHeight - el.scrollTop - el.clientHeight < 24;
}

function renderLogs() {
  const viewport = $('logs-viewport');
  const lines = state.logsRaw;

  // Keep the rendered class in step with the checkbox — it starts checked, but
  // the class is applied here rather than in the HTML so the two cannot drift.
  viewport.classList.toggle('is-wrapped', $('logs-wrap').checked);

  if (!lines.length) {
    viewport.innerHTML = '<p class="log-empty">No log lines match the current filter.</p>';
    return;
  }

  // Only stick to the bottom when Follow is on *and* the user was already
  // there; otherwise keep their scroll position so a refresh does not yank the
  // view away from the line they are reading.
  const follow = $('logs-follow').checked && logsAtBottom(viewport);
  const previousTop = viewport.scrollTop;

  viewport.innerHTML = lines.map(renderLogLine).join('');
  viewport.scrollTop = follow ? viewport.scrollHeight : previousTop;
}

function renderLogMeta(payload) {
  const suffix = payload.search ? ` · matching “${payload.search}”` : '';
  const level = payload.level ? ` · ${payload.level}+` : '';
  setText('logs-caption', `${payload.count} of ${payload.scanned} tail lines${level}${suffix}`);
  setText('logs-path', payload.path || '—');
  if (payload.max_bytes) {
    setText('logs-window', `${Math.round(payload.max_bytes / 1024)} KB`);
  }
}

async function loadLogs() {
  const params = new URLSearchParams({ limit: String(LOG_LINES) });
  const level = $('logs-level').value;
  const search = $('logs-search').value.trim();
  if (level) params.set('level', level);
  if (search) params.set('search', search);

  const payload = await api(`${ENDPOINTS.logs}?${params}`);
  state.logsRaw = payload.lines || [];
  renderLogMeta(payload);
  renderLogs();
}

async function copyLogs() {
  if (!state.logsRaw.length) {
    toast('Nothing to copy', 'info');
    return;
  }
  const text = state.logsRaw.join('\n');
  try {
    await navigator.clipboard.writeText(text);
    toast(`Copied ${state.logsRaw.length} log lines`, 'ok');
  } catch {
    // Clipboard needs a secure context; http://127.0.0.1 counts as one, but a
    // LAN IP over plain http does not. Fall back to a selectable dump.
    const area = document.createElement('textarea');
    area.value = text;
    area.style.position = 'fixed';
    area.style.opacity = '0';
    document.body.appendChild(area);
    area.select();
    const ok = document.execCommand('copy');
    area.remove();
    toast(ok ? `Copied ${state.logsRaw.length} log lines` : 'Copy blocked by the browser', ok ? 'ok' : 'err');
  }
}

function downloadLogs() {
  if (!state.logsRaw.length) {
    toast('Nothing to download', 'info');
    return;
  }
  const url = URL.createObjectURL(
    new Blob([state.logsRaw.join('\n')], { type: 'text/plain;charset=utf-8' }),
  );
  const link = document.createElement('a');
  link.href = url;
  link.download = `belaku_${new Date().toISOString().slice(0, 16).replace(/[:T]/g, '')}.log`;
  link.click();
  URL.revokeObjectURL(url);
}

/* ── Tabs ──────────────────────────────────────────────────────────── */

function switchTab(tab) {
  state.tab = tab;
  document.querySelectorAll('.tab').forEach((btn) => {
    btn.classList.toggle('tab-active', btn.dataset.tab === tab);
  });
  ['monitor', 'orders', 'analytics', 'logs'].forEach((name) => {
    $(`view-${name}`).classList.toggle('hidden', name !== tab);
  });
  if (tab === 'orders') renderOrders();
  if (tab === 'analytics') renderCalendar();
  // The log tail is only interesting on its own tab, and each refresh costs a
  // file read, so the interval lives and dies with the tab.
  clearInterval(state.logTimer);
  state.logTimer = null;
  if (tab === 'logs') {
    // Refresh immediately so the tab is never shown empty for a whole interval.
    loadLogs().catch(() => { /* the previous tail stays on screen */ });
    state.logTimer = setInterval(() => {
      loadLogs().catch(() => { /* keep the last tail */ });
    }, LOG_MS);
  }
}

/* ── Wiring ────────────────────────────────────────────────────────── */

function init() {
  document.querySelectorAll('.tab').forEach((btn) => {
    btn.addEventListener('click', () => switchTab(btn.dataset.tab));
  });

  $('btn-mode-toggle').addEventListener('click', applyMode);
  $('btn-kite-login').addEventListener('click', startKiteLogin);
  $('btn-kite-relogin').addEventListener('click', startKiteLogin);
  $('btn-feed-reconnect').addEventListener('click', reconnectGiftFeed);
  $('btn-kite-submit').addEventListener('click', submitKiteToken);
  $('kite-token').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') submitKiteToken();
  });
  $('btn-pause').addEventListener('click', togglePause);
  $('btn-resume').addEventListener('click', togglePause);
  $('btn-panic').addEventListener('click', panic);
  $('btn-logout').addEventListener('click', signOut);
  $('btn-risk-clear').addEventListener('click', clearRiskBreach);
  $('btn-orders-csv').addEventListener('click', exportCsv);

  $('logs-level').addEventListener('change', () => {
    loadLogs().catch((error) => toast(`Logs: ${error.message}`, 'err'));
  });
  let logSearchTimer = null;
  $('logs-search').addEventListener('input', () => {
    clearTimeout(logSearchTimer);
    logSearchTimer = setTimeout(() => {
      loadLogs().catch((error) => toast(`Logs: ${error.message}`, 'err'));
    }, 200);
  });
  $('logs-wrap').addEventListener('change', (event) => {
    $('logs-viewport').classList.toggle('is-wrapped', event.target.checked);
  });
  $('btn-logs-copy').addEventListener('click', copyLogs);
  $('btn-logs-download').addEventListener('click', downloadLogs);
  $('logs-viewport').addEventListener('scroll', () => {
    // Unchecking Follow on manual scroll is the standard tail behaviour: it
    // makes "I am reading this line" explicit instead of guessing.
    if (!logsAtBottom($('logs-viewport'))) $('logs-follow').checked = false;
  });
  $('logs-follow').addEventListener('change', (event) => {
    if (event.target.checked) {
      const viewport = $('logs-viewport');
      viewport.scrollTop = viewport.scrollHeight;
    }
  });

  $('cal-prev').addEventListener('click', () => shiftCalendarMonth(-1));
  $('cal-next').addEventListener('click', () => shiftCalendarMonth(1));
  $('cal-today').addEventListener('click', () => {
    state.month = null;
    state.selectedDay = null;
    renderCalendar();
    renderCalendarDay(null);
  });
  $('cal-grid').addEventListener('click', (event) => {
    const cell = event.target.closest('[data-cal-day]');
    if (!cell) return;
    const key = cell.dataset.calDay;
    renderCalendarDay(state.selectedDay === key ? null : key);
    renderCalendar();
  });

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
      clearInterval(state.tickTimer);
      clearInterval(state.logTimer);
    } else {
      poll();
      state.timer = setInterval(poll, POLL_MS);
      state.tickTimer = setInterval(tickCountdown, TICK_MS);
      if (state.tab === 'logs') {
        loadLogs().catch(() => { /* keep the last tail */ });
        state.logTimer = setInterval(loadLogs, LOG_MS);
      }
    }
  });

  switchTab('monitor');
  initAuth();
  poll();
  state.timer = setInterval(poll, POLL_MS);
  state.tickTimer = setInterval(tickCountdown, TICK_MS);
}

document.addEventListener('DOMContentLoaded', init);
