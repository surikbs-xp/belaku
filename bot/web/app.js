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
/** Countdown refresh. Independent of POLL_MS so the seconds hand stays smooth. */
const TICK_MS = 250;
/** Samples kept for the client-side sparkline (last ~40 polls). */
const SPARK_POINTS = 40;
const ENDPOINTS = {
  status:    '/api/status',
  portfolio: '/api/portfolio',
  positions: '/api/positions?status=all&limit=200',
  orders:    '/api/orders?limit=500',
  trades:    '/api/trades?limit=500',
  signals:   '/api/signals?limit=100',
  equity:    '/api/equity?limit=2000',
  analytics: '/api/analytics',
  journal:   '/api/journal?limit=500',
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
  // Analytics + journal state. These load on a slower cadence than the 1.5s
  // monitor poll because none of it changes tick-to-tick.
  analytics: null,
  journal: [],
  journalLoadedAt: 0,
  selectedTradeId: null,
  journalRating: null,
  journalUnannotatedOnly: false,
  charts: {},
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

  renderExecutionMeta(status);
}

/** Static execution facts a trader checks before trusting a fill. */
function renderExecutionMeta(status) {
  const config = status.config || {};
  const symbol = status.symbol || {};
  const feed = (status.engine || {}).feed || {};
  setText('exec-lots', `${config.default_lots ?? '—'} lot`);
  setText('exec-product', `${config.order_type || '—'} · ${config.product || '—'}`);
  $('exec-product').title = symbol.next && symbol.next !== symbol.current
    ? `Rolls to ${symbol.next} on expiry`
    : '';
  setText('indicator-source', `GIFT ${feed.interval || '—'} · ${feed.bars ?? 0} bars · source ${feed.history_source || '—'}`);
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
      supertrend: 'Trend vote · 2 of 3',
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
    setText('e-psar', level(values.psar));
    setText('e-psar-sub', psarConditions.buy_psar
      ? 'Price above · BUY condition passes'
      : psarConditions.sell_psar ? 'Price below · SELL condition passes' : 'BUY above · SELL below');
    setText('e-st', `${values.supertrend_bull ?? 0}/3 bullish`);
    setText('e-st-sub', `Lines ${level(values.st_10_1)} · ${level(values.st_11_2)} · ${level(values.st_12_3)}`);
    renderVotes(values.supertrend_bull, values.supertrend_bear);
  } else {
    ['e-psar', 'e-st'].forEach((id) => setText(id, '—'));
    renderVotes(null, null);
  }

  setText('e-counters',
    `${eng.evaluations ?? 0} evals · ${eng.signals_fired ?? 0} signals · `
    + `${eng.orders_sent ?? 0} sent · ${(eng.orders_blocked ?? 0) + (eng.deferred ?? 0)} held`);

  if (eng.last_error) {
    setActionStatus(`Engine: ${eng.last_error}`, 'err');
  }
}

/** Three-segment bar showing the live 2-of-3 SuperTrend split. */
function renderVotes(bull, bear) {
  const host = $('st-votes');
  if (!host) return;
  const up = num(bull) || 0;
  const down = num(bear) || 0;
  if (!up && !down) {
    host.innerHTML = '';
    return;
  }
  const segments = [
    ...Array(up).fill('bull'),
    ...Array(down).fill('bear'),
  ];
  while (segments.length < 3) segments.push('');
  host.innerHTML = segments
    .slice(0, 3)
    .map((state) => `<span class="st-vote ${state}"></span>`)
    .join('');
}

/**
 * Candle countdown. The server's bucket start (`current_bar_at`) anchors the
 * boundary, so the client clock only has to measure elapsed time — a skewed
 * clock or a different timezone cannot shift the countdown.
 */
function renderCandle(eng, feed, ready, warmupHave) {
  const intervalSeconds = num(feed.interval_seconds) || 300;
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

/* ── Analytics ─────────────────────────────────────────────────────── */

/** Ratio/KPI formatters shared by the analytics panel. */
function ratio(value) {
  const n = num(value);
  if (n === null) return '—';
  if (!Number.isFinite(n)) return '∞';
  return n.toFixed(2);
}

function pct(value, decimals = 1) {
  const n = num(value);
  if (n === null) return '—';
  return `${n.toFixed(decimals)}%`;
}

function points(value) {
  const n = num(value);
  if (n === null) return '—';
  return `${n >= 0 ? '+' : ''}${n.toFixed(2)}`;
}

function hours(minutes) {
  const n = num(minutes);
  if (n === null || n === 0) return '—';
  if (n < 60) return `${Math.round(n)}m`;
  const h = Math.floor(n / 60);
  const m = Math.round(n % 60);
  return m ? `${h}h ${m}m` : `${h}h`;
}

function renderAnalytics(data) {
  state.analytics = data;
  const s = data.summary || {};
  const streaks = data.streaks || {};

  setText('a-net', signedInr(s.net_pnl), pnlClass(s.net_pnl));
  setText('a-net-sub', `${signedInr(s.gross_pnl)} gross · ${signedInr(-(s.fees || 0))} fees`);
  setText('a-winrate', s.trades ? pct(s.win_rate) : '—');
  setText('a-winrate-sub', `${s.wins ?? 0}W · ${s.losses ?? 0}L · ${s.breakeven ?? 0}BE`);
  setText('a-pf', s.trades ? ratio(s.profit_factor) : '—');
  setText('a-pf-sub', `${signedInr(s.gross_win)} won · ${signedInr(-(s.gross_loss || 0))} lost`);
  setText('a-expectancy', s.trades ? signedInr(s.expectancy) : '—');
  setText('a-expectancy-sub', `per trade · ${points(s.avg_points)} pts avg`);
  setText('a-dd', data.max_drawdown ? signedInr(data.max_drawdown) : '—', data.max_drawdown ? 'neg' : 'flat');
  setText('a-dd-sub', `${pct(data.max_drawdown_pct)} of peak`);
  setText('a-trades', s.trades ?? 0);
  setText('a-trades-sub', `avg hold ${hours(s.avg_hold_minutes)}`);

  setText('s-streak', streaks.current_kind === 'none'
    ? '—'
    : `${streaks.current_run} ${streaks.current_kind}${streaks.current_run === 1 ? '' : 'es'}`,
    streaks.current_kind === 'win' ? 'pos' : streaks.current_kind === 'loss' ? 'neg' : 'flat');
  setText('s-maxwin', streaks.max_win ?? '—');
  setText('s-maxloss', streaks.max_loss ?? '—');
  setText('s-bigwin', signedInr(s.largest_win), 'pos');
  setText('s-bigloss', signedInr(s.largest_loss), 'neg');
  setText('s-avgwin', signedInr(s.avg_win), 'pos');
  setText('s-avgloss', signedInr(s.avg_loss), 'neg');
  setText('s-hold', hours(s.avg_hold_minutes));
  setText('s-points', points(s.avg_points));
  setText('s-fees', inr(s.fees));

  renderBreakdown('breakdown-direction', (data.by_direction
    ? Object.entries(data.by_direction).map(([label, bucket]) => ({ label, bucket }))
    : []));
  renderBreakdown('breakdown-weekday',
    (data.by_weekday || []).map((row) => ({ label: row.weekday, bucket: row })));
  renderBreakdown('breakdown-hour',
    (data.by_hour || []).map((row) => ({ label: `${String(row.hour).padStart(2, '0')}:00`, bucket: row })));
  renderBreakdown('breakdown-exit',
    (data.by_exit_reason || []).map((row) => ({ label: row.reason, bucket: row })));

  renderDailyChart(data.days || []);
}

/** Horizontal bar per bucket, scaled to the largest absolute P&L. */
function renderBreakdown(id, rows) {
  const host = $(id);
  if (!host) return;
  if (!rows.length) {
    host.innerHTML = '<p class="breakdown-empty">No data yet.</p>';
    return;
  }
  const scale = Math.max(...rows.map((row) => Math.abs(num(row.bucket.net_pnl) || 0)), 1);
  host.innerHTML = rows.map(({ label, bucket }) => {
    const net = num(bucket.net_pnl) || 0;
    const width = Math.max(2, (Math.abs(net) / scale) * 100);
    const kind = net > 0 ? 'pos' : net < 0 ? 'neg' : 'flat';
    return `
      <div class="breakdown-row">
        <span class="breakdown-label">${escapeHtml(label)}</span>
        <span class="breakdown-track"><span class="breakdown-fill ${kind}" style="width:${width.toFixed(1)}%"></span></span>
        <span class="breakdown-meta ${pnlClass(net)}">${signedInr(net, 0)} · ${bucket.wins}W/${bucket.losses}L</span>
      </div>`;
  }).join('');
}

function renderDailyChart(days) {
  const host = $('daily-chart');
  const empty = $('daily-empty');
  if (!host) return;
  const usable = days.filter((day) => day.date && day.date !== 'unknown');
  setText('daily-caption', usable.length
    ? `${usable.length} day${usable.length === 1 ? '' : 's'}`
    : '—');

  if (!usable.length) {
    host.innerHTML = '';
    empty.classList.remove('hidden');
    return;
  }
  empty.classList.add('hidden');
  const max = Math.max(...usable.map((day) => Math.abs(day.net_pnl)), 1);
  host.innerHTML = usable.map((day) => {
    const height = Math.max(4, (Math.abs(day.net_pnl) / max) * 100);
    const kind = day.net_pnl >= 0 ? 'pos' : 'neg';
    return `
      <div class="daily-bar-wrap" title="${escapeHtml(day.date)} · ${signedInr(day.net_pnl, 0)} · ${day.trades} trades">
        <div class="daily-bar daily-bar-${kind}" style="height:${height.toFixed(1)}%"></div>
        <span class="daily-bar-label">${escapeHtml(day.date.slice(5))}</span>
      </div>`;
  }).join('');
}

/**
 * Equity curve via TradingView Lightweight Charts.
 *
 * Two failure modes are handled explicitly because both corrupted the chart
 * permanently in testing:
 *   * the library requires strictly ascending, unique timestamps, and the
 *     equity table can contain duplicate seconds;
 *   * a chart created while its tab is hidden has no measurable container and
 *     ends up throwing on every redraw, so it is only ever built on screen.
 *
 * The library is also optional: if the CDN is blocked, the KPIs and ledger
 * still work and the chart degrades to a notice.
 */
function renderEquityChart(points) {
  const host = $('equity-chart');
  const empty = $('equity-empty');
  if (!host) return;

  const byTime = new Map();
  for (const point of points || []) {
    const time = Math.floor(new Date(point.ts).getTime() / 1000);
    const value = num(point.equity);
    if (Number.isFinite(time) && value !== null) byTime.set(time, { time, value });
  }
  const series = [...byTime.values()].sort((a, b) => a.time - b.time);

  if (!series.length) {
    empty.classList.remove('hidden');
    return;
  }

  const lib = window.LightweightCharts;
  if (!lib) {
    host.innerHTML = '<p class="chart-empty">Chart library unavailable — see the ledger below.</p>';
    return;
  }
  // Creating a chart needs a laid-out container; updating an existing one does not.
  if (!state.charts.equity && (!host.clientWidth || !host.clientHeight)) return;

  empty.classList.add('hidden');

  try {
    if (!state.charts.equity) {
      state.charts.equity = lib.createChart(host, {
        autoSize: true,
        layout: { background: { color: 'transparent' }, textColor: '#8fa0b8', fontSize: 11 },
        grid: {
          vertLines: { color: 'rgba(27,39,64,0.5)' },
          horzLines: { color: 'rgba(27,39,64,0.5)' },
        },
        rightPriceScale: { borderColor: '#1b2740' },
        timeScale: { borderColor: '#1b2740', timeVisible: true, secondsVisible: false },
        crosshair: { mode: 0 },
      });
    }
    if (!state.charts.equitySeries) {
      state.charts.equitySeries = state.charts.equity.addSeries(lib.AreaSeries, {
        lineColor: '#38bdf8',
        topColor: 'rgba(56,189,248,0.35)',
        bottomColor: 'rgba(56,189,248,0.02)',
        lineWidth: 2,
        priceLineVisible: false,
      });
    }
    state.charts.equitySeries.setData(series);
    state.charts.equity.timeScale().fitContent();
    setText('equity-source', `${state.analytics?.equity_source || 'equity curve'} · ${series.length} points`);
  } catch (error) {
    // Tear the broken instance down and keep the rest of the tab usable.
    try { state.charts.equity?.remove(); } catch { /* already gone */ }
    state.charts.equity = null;
    state.charts.equitySeries = null;
    host.innerHTML = `<p class="chart-empty">Equity chart unavailable (${escapeHtml(error.message)}) — see the ledger below.</p>`;
  }
}

function renderTrades() {
  const body = $('trades-body');
  if (!state.trades.length) {
    body.innerHTML = '<tr class="empty-row"><td colspan="12">No settled trades yet.</td></tr>';
    return;
  }
  const notes = state.notesById || {};
  body.innerHTML = state.trades.map((t) => {
    const move = num(t.exit_price) !== null && num(t.entry_price) !== null
      ? (num(t.exit_price) - num(t.entry_price)) * (t.direction === 'LONG' ? 1 : -1)
      : null;
    const note = notes[String(t.id)];
    const hasNote = !!note;
    const label = hasNote
      ? [note.setup, note.tags].filter(Boolean).join(' · ') || 'note'
      : 'add note';
    return `
    <tr>
      <td class="font-mono text-slate-400">${escapeHtml((t.exit_time || '').replace('T', ' '))}</td>
      <td class="font-mono">${escapeHtml(t.symbol)}</td>
      <td><span class="tag ${t.direction === 'LONG' ? 'tag-buy' : 'tag-sell'}">${escapeHtml(t.direction)}</span></td>
      <td class="num">${t.quantity}</td>
      <td class="num">${inr(t.entry_price, 2)}</td>
      <td class="num">${inr(t.exit_price, 2)}</td>
      <td class="num ${pnlClass(move)}">${move === null ? '—' : points(move)}</td>
      <td class="num ${pnlClass(t.gross_pnl)}">${signedInr(t.gross_pnl)}</td>
      <td class="num text-slate-400">${inr(t.fees, 2)}</td>
      <td class="num ${pnlClass(t.net_pnl)}">${signedInr(t.net_pnl)}</td>
      <td class="text-slate-400">${escapeHtml(t.exit_reason || '—')}</td>
      <td><button type="button" class="journal-chip ${hasNote ? 'has-note' : ''}"
            data-journal-trade="${t.id}"
            title="${escapeHtml(hasNote ? (note.note || 'Journal entry saved') : 'Add a journal entry')}">
            ${hasNote ? '★' : '+'} ${escapeHtml(label)}
          </button></td>
    </tr>`;
  }).join('');
}

/* ── Journal ───────────────────────────────────────────────────────── */

/** Rebuild the setup datalist from notes already written, newest first. */
function refreshSetupOptions() {
  const setups = [...new Set(
    state.journal.map((entry) => entry.note && entry.note.setup).filter(Boolean),
  )].sort();
  $('setup-options').innerHTML = setups
    .map((setup) => `<option value="${escapeHtml(setup)}"></option>`)
    .join('');
}

function renderJournal() {
  const host = $('journal-list');
  const needle = $('journal-search').value.trim().toLowerCase();
  const annotated = state.journal.filter((entry) => entry.note).length;
  setText('journal-caption',
    `${state.journal.length} trades · ${annotated} annotated`);

  refreshSetupOptions();

  const rows = state.journal.filter((entry) => {
    const trade = entry.trade || {};
    const note = entry.note;
    if (state.journalUnannotatedOnly && note) return false;
    if (!needle) return true;
    const haystack = [
      trade.symbol, trade.direction, trade.exit_reason,
      note && note.setup, note && note.tags, note && note.note, note && note.mistakes,
    ].join(' ').toLowerCase();
    return haystack.includes(needle);
  });

  if (!rows.length) {
    host.innerHTML = `<p class="condition-empty">${state.journal.length
      ? 'No trades match this filter.'
      : 'No settled trades yet — the journal opens up after the first round-trip closes.'}</p>`;
    return;
  }

  host.innerHTML = rows.map((entry) => {
    const trade = entry.trade || {};
    const note = entry.note;
    const kind = num(trade.net_pnl) > 0 ? 'tag-buy' : num(trade.net_pnl) < 0 ? 'tag-sell' : 'tag-muted';
    const stars = note && note.rating ? '★'.repeat(note.rating) : '';
    const setup = (note && note.setup) || '';
    const tags = (note && note.tags) || '';
    const noteText = (note && note.note) || '';
    return `
      <button type="button" class="journal-item ${state.selectedTradeId === trade.id ? 'is-selected' : ''}"
              data-journal-select="${trade.id}">
        <span>
          <span class="tag ${kind}">${escapeHtml(trade.direction || '—')}</span>
        </span>
        <span class="journal-item-main">
          <span class="journal-item-head">
            <strong>${escapeHtml((trade.exit_time || '').slice(0, 16).replace('T', ' '))}</strong>
            <span class="tag ${num(trade.net_pnl) > 0 ? 'tag-buy' : 'tag-sell'}">${signedInr(trade.net_pnl)}</span>
            <span class="metric-sub">${points((num(trade.exit_price) - num(trade.entry_price)) * (trade.direction === 'LONG' ? 1 : -1))} pts</span>
          </span>
          <span class="journal-item-sub">${escapeHtml(
            [setup, tags, noteText].filter(Boolean).join(' · ') || 'No note yet',
          )}</span>
        </span>
        <span class="journal-item-flags">
          ${stars ? `<span class="journal-flag">${escapeHtml(stars)}</span>` : ''}
          <span class="journal-flag ${note ? 'is-annotated' : ''}">${note ? 'noted' : 'blank'}</span>
        </span>
      </button>`;
  }).join('');
}

function openJournalEditor(tradeId) {
  const entry = state.journal.find((row) => (row.trade || {}).id === tradeId);
  if (!entry) return;
  state.selectedTradeId = tradeId;
  const trade = entry.trade || {};
  const note = entry.note || {};

  $('journal-editor-empty').classList.add('hidden');
  $('journal-form').classList.remove('hidden');

  const move = (num(trade.exit_price) - num(trade.entry_price)) * (trade.direction === 'LONG' ? 1 : -1);
  $('journal-trade-summary').innerHTML = `
    <div><p class="metric-label">Direction</p><p class="metric-sub-inline">${escapeHtml(trade.direction || '—')}</p></div>
    <div><p class="metric-label">Net P&amp;L</p><p class="metric-sub-inline ${pnlClass(trade.net_pnl)}">${signedInr(trade.net_pnl)}</p></div>
    <div><p class="metric-label">Move</p><p class="metric-sub-inline ${pnlClass(move)}">${points(move)} pts</p></div>
    <div><p class="metric-label">Closed</p><p class="metric-sub-inline">${escapeHtml((trade.exit_time || '').slice(0, 16).replace('T', ' '))}</p></div>`;

  $('journal-setup').value = note.setup || '';
  $('journal-tags').value = note.tags || '';
  $('journal-note').value = note.note || '';
  $('journal-mistakes').value = note.mistakes || '';
  setJournalRating(note.rating || null);
  setJournalStatus(note.trade_id ? `Saved ${note.updated_at || ''}` : 'No note yet', note.trade_id ? 'ok' : '');
  renderJournal();
}

function setJournalRating(value) {
  state.journalRating = num(value);
  $('journal-rating').innerHTML = [1, 2, 3, 4, 5].map((star) => `
    <button type="button" class="rating-star ${state.journalRating >= star ? 'is-on' : ''}"
            data-rating="${star}" role="radio"
            aria-checked="${state.journalRating === star}"
            aria-label="${star} of 5">${star}</button>`).join('');
}

function setJournalStatus(message, kind) {
  const el = $('journal-status');
  el.textContent = message;
  el.className = `journal-status ${kind === 'ok' ? 'pos' : kind === 'err' ? 'neg' : ''}`;
}

async function saveJournal(event) {
  event.preventDefault();
  if (!state.selectedTradeId) return;
  setJournalStatus('Saving…');
  try {
    const result = await api(`/api/journal/${state.selectedTradeId}`, {
      method: 'PUT',
      body: JSON.stringify({
        note: $('journal-note').value,
        setup: $('journal-setup').value,
        tags: $('journal-tags').value,
        rating: state.journalRating || null,
        mistakes: $('journal-mistakes').value,
      }),
    });
    setJournalStatus(`Saved ${result.note?.updated_at || ''}`, 'ok');
    toast('Journal note saved', 'ok');
    await loadJournal();
    renderTrades();
    openJournalEditor(state.selectedTradeId);
  } catch (error) {
    setJournalStatus(error.message, 'err');
    toast(`Could not save note: ${error.message}`, 'err');
  }
}

async function clearJournalEntry() {
  if (!state.selectedTradeId) return;
  const confirmed = await confirmDialog({
    title: 'Delete this journal note?',
    body: 'The trade itself is untouched — only your written note is removed.',
    okLabel: 'Delete note',
  });
  if (!confirmed) return;
  try {
    await api(`/api/journal/${state.selectedTradeId}`, { method: 'DELETE' });
    toast('Journal note deleted', 'ok');
    await loadJournal();
    renderTrades();
    openJournalEditor(state.selectedTradeId);
  } catch (error) {
    setJournalStatus(error.message, 'err');
  }
}

async function loadJournal() {
  const data = await api(ENDPOINTS.journal);
  state.journal = data.entries || [];
  state.journalLoadedAt = Date.now();
  state.notesById = Object.fromEntries(
    state.journal.filter((entry) => entry.note)
      .map((entry) => [String((entry.trade || {}).id), entry.note]),
  );
  renderJournal();
}

/** Trade KPIs + equity curve. Cheap enough to refetch on a slow cadence. */
async function loadAnalytics() {
  const [analytics, equity] = await Promise.all([
    api(ENDPOINTS.analytics),
    api(ENDPOINTS.equity),
  ]);
  state.analyticsLoadedAt = Date.now();
  renderAnalytics(analytics);
  if (equity.source) analytics.equity_source = equity.source;
  renderEquityChart(equity.points || []);
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

    // Track server clock drift so the countdown can trust it.
    const serverTime = status.server_time ? new Date(status.server_time) : null;
    if (serverTime && !Number.isNaN(serverTime.getTime())) {
      state.serverOffsetMs = serverTime.getTime() - Date.now();
    }

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
    // Analytics and the journal only change when a trade settles, so load them
    // on the tab that shows them and refresh slowly in the background. Failures
    // here must not break the monitor poll, hence the catch.
    const slowLoad = [];
    if (state.tab === 'analytics') slowLoad.push(loadAnalytics());
    else if (Date.now() - (state.analyticsLoadedAt || 0) > 30000) slowLoad.push(loadAnalytics());
    if (state.tab === 'journal' && Date.now() - state.journalLoadedAt > 15000) slowLoad.push(loadJournal());
    if (slowLoad.length) {
      Promise.allSettled(slowLoad).catch(() => { /* reported per-loader */ });
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
  ['monitor', 'orders', 'analytics', 'journal'].forEach((name) => {
    $(`view-${name}`).classList.toggle('hidden', name !== tab);
  });
  if (tab === 'orders') renderOrders();
  if (tab === 'analytics') {
    loadAnalytics().catch((error) => toast(`Analytics unavailable: ${error.message}`, 'err'));
  }
  if (tab === 'journal') {
    loadJournal().catch((error) => toast(`Journal unavailable: ${error.message}`, 'err'));
  }
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

  // Journal: delegation keeps the list markup replaceable without rebinding.
  $('journal-list').addEventListener('click', (event) => {
    const item = event.target.closest('[data-journal-select]');
    if (item) openJournalEditor(Number(item.dataset.journalSelect));
  });
  $('trades-body').addEventListener('click', (event) => {
    const chip = event.target.closest('[data-journal-trade]');
    if (!chip) return;
    switchTab('journal');
    loadJournal()
      .then(() => openJournalEditor(Number(chip.dataset.journalTrade)))
      .catch((error) => toast(`Journal unavailable: ${error.message}`, 'err'));
  });
  $('journal-form').addEventListener('submit', saveJournal);
  $('journal-delete').addEventListener('click', clearJournalEntry);
  $('journal-clear').addEventListener('click', () => {
    $('journal-setup').value = '';
    $('journal-tags').value = '';
    $('journal-note').value = '';
    $('journal-mistakes').value = '';
    setJournalRating(null);
    setJournalStatus('Cleared — save to persist');
  });
  $('journal-rating').addEventListener('click', (event) => {
    const star = event.target.closest('[data-rating]');
    if (!star) return;
    const value = Number(star.dataset.rating);
    setJournalRating(state.journalRating === value ? null : value);
  });
  let journalTimer = null;
  $('journal-search').addEventListener('input', () => {
    clearTimeout(journalTimer);
    journalTimer = setTimeout(renderJournal, 120);
  });
  $('btn-journal-unannotated').addEventListener('click', () => {
    state.journalUnannotatedOnly = !state.journalUnannotatedOnly;
    $('btn-journal-unannotated').classList.toggle('btn-primary', state.journalUnannotatedOnly);
    $('btn-journal-unannotated').classList.toggle('btn-neutral', !state.journalUnannotatedOnly);
    renderJournal();
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
    } else {
      poll();
      state.timer = setInterval(poll, POLL_MS);
      state.tickTimer = setInterval(tickCountdown, TICK_MS);
    }
  });

  switchTab('monitor');
  poll();
  state.timer = setInterval(poll, POLL_MS);
  state.tickTimer = setInterval(tickCountdown, TICK_MS);
}

document.addEventListener('DOMContentLoaded', init);
