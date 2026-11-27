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
/** Log tail refresh. Independent of POLL_MS so the tab controls its own cost. */
const LOG_MS = 3000;
/** Tail length requested from /api/logs. */
const LOG_LINES = 600;
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
  strategies: '/api/strategies',
  logs:      '/api/logs',
  toggle:    '/api/toggle-mode',
  pause:     '/api/pause',
  panic:     '/api/panic',
  stopAll:   '/api/stop-all',
  stopAllClear: '/api/stop-all/clear',
  reconcile: '/api/orders/reconcile',
  pending:   '/api/orders/pending',
};

/** The one book the dashboard reads. Both strategies execute on the NIFTY
    future, so there is a single ledger and no venue switch. */
const VENUES = {
  futures: {
    label: 'Nifty future',
    orders: 'orders',
    trades: 'trades',
    signals: 'signals',
    analytics: 'analytics',
    portfolio: 'portfolio',
  },
};

const state = {
  tab: 'monitor',
  orders: [],
  signals: [],
  trades: [],
  positions: [],
  // Orders the broker has not finished with. They block new entries, so the
  // dashboard shows them separately from the execution log.
  pending: [],
  // Last futures portfolio payload. The Performance tab's futures venue needs
  // it, but the tab can be opened (or the venue switched) between two fast
  // polls, so the tiles read this cache instead of blanking to "—".
  portfolio: null,
  // Last /api/status payload. The book tiles need the execution future's own
  // price to show the basis against the signal series, and that price rides the
  // status feed rather than the strategies feed.
  status: null,
  failures: 0,
  lastSync: null,
  busy: false,
  modeConfirming: false,
  // Candle countdown: the bar boundary comes from the server's bucket start, so
  // a skewed client clock cannot show the wrong time-to-close.
  serverOffsetMs: 0,
  // Client-side price samples; the API has no tick history endpoint.
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
  // Strategy configuration from /api/status, so the indicator labels follow the
  // server's SuperTrend and PSAR settings instead of hardcoded periods.
  strategyConfig: null,
  strategies: null,
  // Bot name from /api/status (BOT_NAME); drives the tab title and header.
  botName: null,
  // When the ledger rows were last fetched (see LEDGER_MS).
  ledgerLoadedAt: 0,
  // Front-month rollover, from /api/status. Drives the header pill.
  rollover: null,
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
    // A first paint (placeholder -> value) is not an update, so it must not
    // flash: switching tabs would otherwise light up every metric at once and
    // read as a burst of activity that never happened.
    const firstPaint = el.textContent === '' || el.textContent === '—';
    el.textContent = next;
    if (!firstPaint) {
      el.classList.remove('flash');
      void el.offsetWidth; // restart the CSS animation
      el.classList.add('flash');
    }
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

/**
 * Ask the operator to confirm an action.
 *
 * ``body`` is HTML so a caller can emphasise the consequence. The optional
 * ``strategyId``/``action``/``mode`` triple renders a fact block, which is what
 * makes a real-money confirmation unambiguous: the operator sees which book,
 * which action and which mode before pressing the button. ``warn`` adds a red
 * line for the irreversible cases.
 */
function confirmDialog(opts) {
  const { title, body, okLabel = 'Confirm', danger = true } = opts;
  const { strategyId, action, mode, warn } = opts;
  return new Promise((resolve) => {
    const modal = $('modal');
    $('modal-title').textContent = title;
    $('modal-body').innerHTML = body;

    const facts = $('modal-facts');
    facts.classList.toggle('hidden', !strategyId);
    setText('modal-fact-strategy', strategyId || '—');
    setText('modal-fact-action', action || '—');
    setText('modal-fact-mode', mode || '—');

    const warnEl = $('modal-warn');
    warnEl.textContent = warn || '';
    warnEl.classList.toggle('hidden', !warn);

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
    ok.focus();
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

  // Signal-path latency. Detection is bar-close to verdict; the round-trip is
  // verdict to confirmed fill. Both are measured in the engine, not estimated
  // here, so the pill reports what actually happened.
  const lat = (status.engine || {}).latency || {};
  const detect = num(lat.detect_ms);
  const roundTrip = num(lat.fill_ms) ?? num(lat.submit_ms);
  const latencyPill = $('pill-signal-latency');
  if (latencyPill) {
    if (detect === null && roundTrip === null) {
      setPill('pill-signal-latency', 'Signal —', 'idle');
      latencyPill.title = 'No signal has been evaluated yet';
    } else {
      const parts = [];
      if (detect !== null) parts.push(`detect ${Math.round(detect)}ms`);
      if (roundTrip !== null) parts.push(`fill ${Math.round(roundTrip)}ms`);
      // A detection over a second means the engine missed the bar close and
      // fell back to the poll, which is the thing the event-driven wake exists
      // to prevent - so it is flagged rather than shown as normal.
      const slow = detect !== null && detect > 1000;
      setPill('pill-signal-latency', `Signal · ${parts.join(' / ')}`, slow ? 'bad' : 'live');
      latencyPill.title =
        `bar close → verdict ${detect === null ? '—' : Math.round(detect) + 'ms'}`
        + ` · verdict → fill ${roundTrip === null ? '—' : Math.round(roundTrip) + 'ms'}`
        + (slow ? ' · detection fell back to the poll interval' : '');
    }
  }

  $('hdr-symbol').textContent = (status.symbol && status.symbol.current) || '—';
  $('hdr-market').textContent = market.is_open
    ? `Market open · closes ${(market.session_close || '').slice(11, 16)} IST`
    : market.reason || '—';


  const banner = $('paused-banner');
  banner.classList.toggle('hidden', !strategy.paused);
  banner.classList.toggle('flex', !!strategy.paused);

  renderRiskBanner(status.engine || {});
  renderAlerts(status);

  const pauseBtn = $('btn-pause');
  pauseBtn.textContent = strategy.paused ? 'Resume' : 'Pause';

  // STOP ALL is a latch, not a toggle: the button reflects the latched state and
  // clearing it is a separate, deliberate action. Showing it as a toggle would
  // invite a second click that silently resumes trading.
  const stopAllBtn = $('btn-stop-all');
  if (stopAllBtn) {
    const stopped = !!strategy.stopped_all;
    stopAllBtn.textContent = stopped ? 'STOP ALL ✓' : 'STOP ALL';
    stopAllBtn.classList.toggle('btn-danger', !stopped);
    stopAllBtn.classList.toggle('btn-neutral', stopped);
    stopAllBtn.title = stopped
      ? `STOP ALL engaged (${strategy.stopped_all_reason || 'emergency'}) — click to clear`
      : 'Emergency halt: block new entries in every book without closing positions';
    stopAllBtn.dataset.stopped = stopped ? '1' : '0';
  }

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
    + `${feed.bars ?? 0} bars · ${eng.signals_fired ?? 0} signals · ${eng.orders_sent ?? 0} orders`;
  $('footer-right').textContent =
    `Sync ${state.lastSync ? state.lastSync.toLocaleTimeString() : '—'} · ${state.failures ? `${state.failures} failed poll(s)` : 'healthy'}`;

  renderBuild(status.build);
  renderLatency((status.kite || {}).latency);

  renderExecutionMeta(status);
  renderRolloverPill(status.engine || {});
}

/**
 * Front-month rollover, as a header pill.
 *
 * Deliberately quiet: the engine already blocks entries in a dead contract and
 * rolls five days early, so this is a nudge rather than an alarm. It stays
 * hidden until the contract is close enough to expiry to be worth a glance, and
 * the tooltip carries the detail.
 */
function renderRolloverPill(engine) {
  const pill = $('pill-rollover');
  if (!pill) return;

  const rollover = engine.rollover || state.rollover;
  state.rollover = rollover || null;
  if (!rollover || !rollover.expiry) {
    pill.classList.add('hidden');
    return;
  }

  const expired = !!rollover.rolled;
  const inWindow = !!rollover.in_rollover_window;
  const days = num(rollover.days_to_calendar_expiry);
  // A week is the window in which a rollover is still a decision rather than an
  // incident, so that is when the pill appears.
  const imminent = expired || inWindow || (days !== null && days <= 7);
  pill.classList.toggle('hidden', !imminent);
  if (!imminent) return;

  const current = rollover.current_symbol || '—';
  const calendar = rollover.calendar_symbol || current;
  const rolloverDays = num(rollover.rollover_days);

  if (expired) {
    setPill('pill-rollover', `Rollover · ${rollover.open_symbol || calendar} expired`, 'bad');
    pill.title =
      `${rollover.open_symbol || calendar} has stopped trading, so new entries are `
      + `blocked until it is squared off. ${current} is the contract the engine would `
      + 'trade next; the open position is an expiry to resolve by hand.';
  } else if (inWindow) {
    setPill('pill-rollover', `Rollover · ${current}`, 'live');
    pill.title =
      `Rolled over to ${current} ${rolloverDays} days before expiry. `
      + `${calendar} expires ${rollover.calendar_expiry_date || '—'} `
      + `(${days} day${days === 1 ? '' : 's'} away). A flip now closes any ${calendar} `
      + `position and opens ${current}; a signal that agrees leaves it alone, so a `
      + `quiet market can carry it to the close.`;
  } else {
    setPill('pill-rollover', `Rollover · ${days}d`, 'paper');
    pill.title =
      `${calendar} expires ${rollover.expiry_label || rollover.expiry_date || '—'} `
      + `(last Tuesday, 15:30 IST close). The engine rolls to ${rollover.next_symbol} `
      + `${rolloverDays} days before expiry.`;
  }
}

/** Static execution facts a trader checks before trusting a fill. */
/**
 * Which build is serving, as a footer pill.
 *
 * The point is to answer "did my deploy actually land?" without shelling into
 * the container. A short SHA is enough to compare against `git log`; the full
 * SHA and build time go in the tooltip. An image built without provenance says
 * so rather than showing a version that would be a guess.
 */
function renderBuild(build) {
  const pill = $('pill-build');
  if (!pill) return;
  const sha = (build && build.short_sha) || '';
  if (!sha) {
    pill.textContent = 'Build unknown';
    pill.className = 'pill pill-idle';
    pill.title = 'This image was built without BUILD_SHA; a local build or a bare python run.';
    return;
  }
  pill.textContent = `Build ${sha}`;
  pill.className = 'pill pill-live';
  const parts = [`commit ${build.sha}`];
  if (build.ref) parts.push(`ref ${build.ref}`);
  if (build.time) parts.push(`built ${build.time}`);
  pill.title = parts.join(' · ');
}

/**
 * Round-trip to Kite's API, as a header pill.
 *
 * This is the number the region choice actually buys: a host in Mumbai sits a
 * few milliseconds from Kite's ticker, where a Sydney or US host pays a
 * long-haul hop. Showing the median rather than the last sample keeps a single
 * blip from looking like a trend.
 */
function renderLatency(latency) {
  const pill = $('pill-latency');
  if (!pill) return;
  if (!latency || !latency.available) {
    pill.textContent = 'Latency —';
    pill.className = 'pill pill-idle';
    pill.title = (latency && latency.reason) || 'No successful round-trip to Kite yet.';
    return;
  }
  const median = latency.median_ms;
  // Thresholds are about what the number means, not about being fast: under
  // 50ms is a same-region path, under 150ms is a neighbouring region, and
  // beyond that the host is a long way from the broker.
  const variant = median < 50 ? 'live' : (median < 150 ? 'paper' : 'bad');
  pill.textContent = `Latency ${median}ms`;
  pill.className = `pill pill-${variant}`;
  pill.title = [
    `median ${median}ms over ${latency.samples} sample${latency.samples === 1 ? '' : 's'}`,
    `last ${latency.last_ms}ms`,
    `range ${latency.min_ms}–${latency.max_ms}ms`,
  ].join(' · ');
}

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

  // The contract's own expiry, derived from the symbol so it costs no broker
  // round-trip on a poll that runs every 1.5s.
  const expiry = symbol.expiry || (feed.exec && feed.exec.expiry) || null;
  setText('exec-expiry', expiry || '—');
  $('exec-expiry').title = expiry
    ? `${symbol.current || ''} stops trading at the 15:30 IST close on ${expiry}`
    : 'Expiry unavailable';
  setText('exec-next', symbol.next || '—');
  $('exec-next').title = symbol.next_expiry
    ? `${symbol.next} expires ${symbol.next_expiry}`
    : '';

  // Name the series the active strategy actually reads. This said "GIFT"
  // unconditionally, so a NIFTY-signalled book was labelled with the wrong
  // instrument - the one thing a signal panel must never get wrong.
  const active = (state.strategies?.runs || [])[0] || {};
  const activeSrc = active.source || {};
  const seriesName = activeSrc.symbol || feed.symbol || 'series';
  setText('e-signal-source-label', `${seriesName} · signal source`);

  // The market strip's prices. These lived in renderEngine, which went with the
  // hero; the strip survived, so its writers have to live somewhere.
  const hasLivePrice = feed.last_price !== null && feed.last_price !== undefined;
  const price = hasLivePrice ? feed.last_price : feed.last_candle_close;
  const priceAt = hasLivePrice ? feed.last_tick_at : feed.last_candle_at;
  setText('e-gift-price', price === null || price === undefined ? '—' : inr(price, 2));
  setText('e-gift-timestamp', giftTimestamp(
    priceAt, hasLivePrice ? 'Last tick' : 'Last candle close',
  ));
  renderExecQuote(feed.exec, price);

  const eng = status.engine || {};
  const market = status.market || {};

  // Feed health, not which series the strategy reads - those are different
  // things now, so naming the instrument here was wrong.
  const tickAge = num(feed.tick_age_seconds);
  const feedLive = !!feed.connected && tickAge !== null && tickAge < 120;
  setPill('pill-feed', feedLive
    ? `Feed · ${timeAgo(tickAge)}`
    : feed.starting ? 'Feed · connecting'
      : feed.connected ? 'Feed · waiting for ticks' : 'Feed · disconnected',
  feedLive ? 'live' : 'bad');
  $('pill-feed').title =
    `${feed.symbol || '—'} · ${feed.bars ?? 0} bars @ ${feed.interval || '?'}`
    + (feed.error ? ` · ${feed.error}` : '');

  setText('e-exec', eng.execution_symbol || '—');
  setText('e-exec-sub', market.is_open
    ? (eng.mode === 'PAPER' ? 'PAPER — simulated at market price' : 'NSE F&O open — orders live')
    : (eng.mode === 'PAPER'
      ? "PAPER — future's last price used after hours"
      : `orders held · ${market.reason || 'closed'}`));
}

function renderPortfolio(portfolio) {
  // The aggregate panel is gone: each band carries its own account, and the
  // Performance tab carries the venue-scoped totals. What is left here is the
  // cache the Performance tab reads between polls, plus its tiles.
  state.portfolio = portfolio;
  renderAnalyticsTiles(portfolio);
}


/**
 * Performance-tab headline tiles.
 *
 * These follow the venue selector, not the execution mode: the whole point of
 * the tab is to compare the two books, so showing the futures numbers under the
 * index heading would defeat it. The futures venue reuses the portfolio payload
 * the fast poll already fetched; the index venue reads its own endpoint.
 */
function renderAnalyticsTiles(futuresPortfolio) {
  // Fall back to the cached payload: the tab can be opened between two fast
  // polls, and blanking the tiles there reads as "no data" rather than "not
  // fetched yet".
  const source = futuresPortfolio || state.portfolio;
  const venue = VENUES.futures;

  if (!source) {
    ['a-net', 'a-return', 'a-dd', 'a-trades'].forEach((id) => setText(id, '—'));
    setText('analytics-venue-note', `${venue.label} · —`);
    return;
  }

  setText('a-net', signedInr(source.total_pnl), pnlClass(source.total_pnl));
  setText('a-return', `${source.total_return_pct >= 0 ? '+' : ''}${source.total_return_pct}%`,
    pnlClass(source.total_return_pct));
  setText('a-dd', `${source.max_drawdown_pct}%`, pnlClass(source.max_drawdown_pct));
  setText('a-trades', source.total_trades);

  setText('analytics-venue-note',
    `${venue.label} · ${source.mode || '—'} · ${source.open_position_count ?? 0} open`);
}


/**
 * Every book's closed-trade record, side by side.
 *
 * The venue tiles above blend the books into one number, which is the one thing
 * a trader cannot use when two books are being compared: a book that is up
 * Rs 40,000 and one that is down Rs 40,000 are not "flat", they are one working
 * strategy and one that should be switched off.
 */
function renderBookPerformance() {
  const host = $('books-performance');
  if (!host) return;
  const payload = state.strategies || {};
  const runs = payload.runs || [];
  const perf = payload.performance || {};
  const accounts = payload.accounts || {};

  if (!runs.length) {
    host.innerHTML = '<p class="condition-empty">No strategy is configured.</p>';
    return;
  }

  host.innerHTML = runs.map((run) => {
    const book = perf[run.id] || {};
    const acct = accounts[run.id] || {};
    const trades = book.trades ?? 0;
    const net = num(book.net_pnl);
    const equity = num(acct.equity);
    const mode = acct.mode || run.mode || 'PAPER';
    const pf = num(book.profit_factor);
    const cell = (label, value, cls, note) => `
      <div class="perf-book-cell">
        <span class="perf-book-label">${escapeHtml(label)}</span>
        <span class="perf-book-value ${cls || ''}">${escapeHtml(value)}</span>
        ${note ? `<span class="perf-book-note">${escapeHtml(note)}</span>` : ''}
      </div>`;
    return `
      <div class="perf-book">
        <div class="perf-book-head">
          <span class="perf-book-name">${escapeHtml(run.name || run.id)}</span>
          <span class="book-band-mode ${mode === 'LIVE' ? 'is-live' : 'is-paper'}">${escapeHtml(mode)}</span>
          ${run.seconds ? `<span class="perf-book-tf">${Math.round(run.seconds / 60)}m</span>` : ''}
        </div>
        <div class="perf-book-grid">
          ${cell('Net P&L', net === null ? '—' : signedInr(net), pnlClass(net),
    `${trades} trade${trades === 1 ? '' : 's'}`)}
          ${cell('Win rate', trades ? `${book.win_rate ?? 0}%` : '—', '',
    pf === null ? 'PF —' : `PF ${pf}`)}
          ${cell('Equity', equity === null ? '—' : inr(equity), '',
    acct.return_pct === undefined || acct.return_pct === null ? 'from ' + (acct.source === 'zerodha' ? 'Zerodha' : 'ledger')
      : `${acct.return_pct > 0 ? '+' : ''}${acct.return_pct}%`)}
        </div>
      </div>`;
  }).join('');
}

/** One book's heading, subtotal and rows. */
function bookSection(name, open, emptyText, note) {
  const subtotal = open.reduce((sum, p) => sum + (num(p.unrealized_pnl) || 0), 0);
  const head = `
    <div class="book-head">
      <span class="book-name">${escapeHtml(name)}</span>
      ${note ? `<span class="book-note">${escapeHtml(note)}</span>` : ''}
      <span class="book-subtotal ${open.length ? pnlClass(subtotal) : ''}">${
  open.length ? signedInr(subtotal) : '—'}</span>
    </div>`;

  if (!open.length) {
    return `<div class="book">${head}<p class="book-empty">${escapeHtml(emptyText)}</p></div>`;
  }

  const rows = open.map((p) => {
    const profit = num(p.profit_points);
    return `
      <tr>
        <td class="font-mono">${escapeHtml(p.symbol)}</td>
        <td><span class="tag ${p.direction === 'BUY' ? 'tag-buy' : 'tag-sell'}">${
  escapeHtml(p.direction || p.side)}</span></td>
        <td class="num">${p.quantity}</td>
        <td class="num">${inr(p.entry_price, 2)}</td>
        <td class="num">${inr(p.mark_price, 2)}</td>
        <td class="num pnl-strong ${pnlClass(profit)}">${
  profit === null ? '—' : `${profit >= 0 ? '+' : ''}${level(profit)}`}</td>
        <td class="num ${pnlClass(p.unrealized_pnl)}">${signedInr(p.unrealized_pnl)}</td>
        <td class="num">${escapeHtml(p.duration || '—')}</td>
        <td><span class="tag tag-muted">Holding</span></td>
      </tr>`;
  }).join('');

  return `
    <div class="book">
      ${head}
      <div class="table-wrap">
        <table class="data-table data-table-wide">
          <thead>
            <tr>
              <th>Symbol</th>
              <th>Dir</th>
              <th class="num">Qty</th>
              <th class="num">Entry</th>
              <th class="num">Market</th>
              <th class="num">P/L pts</th>
              <th class="num">P/L ₹</th>
              <th>Duration</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody>${rows}</tbody>
        </table>
      </div>
    </div>`;
}

/* ── Orders + signals ──────────────────────────────────────────────── */

/** Rows for the Orders tab. There is one ledger, so this is the whole book. */
function venueOrders() {
  return state.orders;
}

/** Empty-state text for a ledger table. */
function emptyLedgerText(noun, venue, counts, filtered = false) {
  if (filtered) return `No matching ${noun}.`;
  return `No ${noun} yet.`;
}

// Column counts, in one place so an "empty ledger" row can never span the wrong
// number of columns. They were literal 10/5/11 and drifted every time a column
// was added - a mis-spanned cell leaves the empty state short of the header,
// which reads as a broken table rather than as an empty one.
const ORDERS_COLS = 11;
const SIGNALS_COLS = 6;
const TRADES_COLS = 12;

/**
 * The display name of the book that owns a ledger row.
 *
 * Every row carries a ``strategy_id``, but an id like ``gift-15m`` is not what
 * the operator named the book. The tile shows "GIFT 15m"; the ledger has to use
 * the same word or the two views of one strategy read as two strategies.
 *
 * A row whose id matches no configured book is *not* renamed - it is a real
 * position in nobody's book, and hiding that behind a friendly label would be
 * the opposite of helpful.
 */
function bookLabel(strategyId) {
  const runs = (state.strategies && state.strategies.runs) || [];
  const id = String(strategyId || '');
  if (!id) return '—';
  const run = runs.find((r) => r.id === id);
  return run ? (run.name || run.id) : id;
}

/** The book filter's current value, or '' for every book. */
function selectedBook() {
  const el = $('orders-book');
  return el ? el.value : '';
}

function filteredOrders() {
  const needle = $('orders-search').value.trim().toLowerCase();
  const mode = $('orders-mode').value;
  const book = selectedBook();
  return venueOrders().filter((o) => {
    if (mode && (o.mode || '').toUpperCase() !== mode) return false;
    // Scoped to one book when the operator asked for it: with three books in one
    // ledger, "the order book" is a mixture and the row that matters can be
    // buried under two other strategies'.
    if (book && (o.strategy_id || '') !== book) return false;
    if (!needle) return true;
    return Object.values(o).join(' ').toLowerCase().includes(needle);
  });
}

function renderOrders() {
  const rows = filteredOrders();
  const total = venueOrders().length;
  const venue = VENUES.futures;
  setText('orders-caption', `${rows.length} of ${total} executions · ${venue.label}`);

  const body = $('orders-body');
  if (!rows.length) {
    body.innerHTML = `<tr class="empty-row"><td colspan="${ORDERS_COLS}">${
      emptyLedgerText('executions', 'futures', { futures: state.orders.length },
        rows.length !== total)}</td></tr>`;
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
        <td class="text-slate-300">${escapeHtml(bookLabel(o.strategy_id))}</td>
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
  const venue = VENUES.futures;
  // The book filter applies here too. A decision log across two books is two
  // interleaved strategies, and the one being debugged is the one the operator
  // selected - the same control the order book above already uses.
  const book = selectedBook();
  const rows = state.signals
    .filter((s) => !book || (s.strategy_id || '') === book);

  setText('signals-caption',
    `${rows.length} decision${rows.length === 1 ? '' : 's'} · ${venue.label}`);

  if (!rows.length) {
    body.innerHTML = `<tr class="empty-row"><td colspan="${SIGNALS_COLS}">${
      emptyLedgerText('signals', 'futures', { futures: state.signals.length },
        book ? rows.length !== state.signals.length : false)}</td></tr>`;
    return;
  }
  body.innerHTML = rows.map((s) => {
    const action = String(s.action || '').toUpperCase();
    const tag = action === 'BUY' ? 'tag-buy' : action === 'SELL' ? 'tag-sell' : 'tag-muted';
    return `
    <tr>
      <td class="font-mono text-slate-400">${escapeHtml((s.ts || '').replace('T', ' '))}</td>
      <td class="text-slate-300">${escapeHtml(bookLabel(s.strategy_id))}</td>
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

  if (!monthDays.length) {
    setText('cal-caption', `${MONTH_NAMES[month]} ${year} · no closed trades`);
  }
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
  const trades = state.trades;
  for (const trade of trades) {
    const key = String(trade.exit_time || '').slice(0, 10);
    if (!key) continue;
    (state.dayTrades[key] ||= []).push(trade);
  }
  state.calendarLoadedAt = Date.now();
}

function renderTrades() {
  const body = $('trades-body');
  const rows = state.trades;
  const venue = VENUES.futures;

  setText('trades-caption',
    `${rows.length} settled round-trip${rows.length === 1 ? '' : 's'} · ${venue.label}`);

  if (!rows.length) {
    body.innerHTML = `<tr class="empty-row"><td colspan="${TRADES_COLS}">${
      emptyLedgerText('settled trades', 'futures', { futures: state.trades.length })}</td></tr>`;
    return;
  }
  body.innerHTML = rows.map((t) => {
    const move = num(t.exit_price) !== null && num(t.entry_price) !== null
      ? (num(t.exit_price) - num(t.entry_price)) * (t.direction === 'LONG' ? 1 : -1)
      : null;
    return `
    <tr>
      <td class="font-mono text-slate-400">${escapeHtml((t.exit_time || '').replace('T', ' '))}</td>
      <td class="text-slate-300">${escapeHtml(bookLabel(t.strategy_id))}</td>
      <td class="font-mono">${escapeHtml(t.symbol)}</td>
      <td><span class="tag ${t.direction === 'LONG' ? 'tag-buy' : 'tag-sell'}">${escapeHtml(t.direction)}</span></td>
      <td class="num">${t.quantity}</td>
      <td class="num">${inr(t.entry_price, 2)}</td>
      <td class="num">${inr(t.exit_price, 2)}</td>
      <td class="num ${pnlClass(move)}">${move === null ? '—' : signedPoints(move)}</td>
      <td class="num ${pnlClass(t.gross_pnl)}">${signedInr(t.gross_pnl)}</td>
      <td class="num text-slate-400">${inr(t.fees, 2)}</td>
      <td class="num ${pnlClass(t.net_pnl)}">${signedInr(t.net_pnl)}</td>
      <td class="text-slate-400">${renderExitReason(t.exit_reason)}</td>
    </tr>`;
  }).join('');
}

/**
 * The exit reason, with a signal exit called out.
 *
 * The signal flipping against the position is the strategy's only automatic
 * exit, so it is tagged rather than left as one more grey word among SIGNAL and
 * FLAT. SIGNAL_FLAT is no longer produced - a disagreeing bar holds - but old
 * rows still carry it and must keep rendering as an exit.
 */
function renderExitReason(reason) {
  const text = String(reason || '').toUpperCase();
  if (!text) return '—';
  if (
    text.includes('SIGNAL_FLIP') ||
    text.includes('SIGNAL_FLAT') ||
    text.includes('SIGNAL_EXIT')
  ) {
    return `<span class="tag tag-sell">EXIT · ${escapeHtml(text)}</span>`;
  }
  return escapeHtml(reason);
}


/* ── Polling ───────────────────────────────────────────────────────── */

/**
 * Ledger rows: positions, orders, trades and signals. These only change when the
 * bot writes a row, so they are fetched on LEDGER_MS rather than every poll.
 */
async function loadLedger() {
  // The pending strip follows the mode filter, not the process mode: an operator
  // looking at LIVE orders wants to know which LIVE orders are stuck, and the
  // server's default would answer for PAPER while the table showed LIVE.
  const mode = $('orders-mode') ? $('orders-mode').value : '';
  const pendingUrl = mode ? `${ENDPOINTS.pending}?mode=${mode}` : ENDPOINTS.pending;
  const [orders, trades, signals, pending] = await Promise.all([
    api(ENDPOINTS.orders),
    api(ENDPOINTS.trades),
    api(ENDPOINTS.signals),
    api(pendingUrl).catch(() => ({ orders: [], count: 0 })),
  ]);
  state.orders = orders.orders || [];
  state.signals = signals.signals || [];
  state.trades = trades.trades || [];
  state.pending = pending.orders || [];
  renderOrders();
  renderPending();
  renderSignals();
  renderTrades();
}

/**
 * Unresolved orders, and the fact that they block new entries.
 *
 * Hidden entirely when there are none: a permanent empty table is noise, and the
 * absence of the strip is itself the signal that nothing is stuck.
 */
function renderPending() {
  const strip = $('pending-strip');
  const rows = state.pending || [];
  strip.classList.toggle('hidden', !rows.length);
  if (!rows.length) return;
  setText('pending-strip-note',
    `${rows.length} order${rows.length === 1 ? '' : 's'} - new entries are blocked until these resolve`);
  $('pending-body').innerHTML = rows.map((o) => {
    const action = String(o.transaction_type || '').toUpperCase();
    const tag = action === 'BUY' ? 'tag-buy' : 'tag-sell';
    return `
      <tr>
        <td class="font-mono text-slate-400">${escapeHtml((o.ts || '').replace('T', ' '))}</td>
        <td class="text-slate-300">${escapeHtml(bookLabel(o.strategy_id))}</td>
        <td class="font-mono">${escapeHtml(o.symbol)}</td>
        <td><span class="tag ${tag}">${escapeHtml(action)}</span></td>
        <td class="num">${o.quantity}</td>
        <td><span class="tag tag-warn">${escapeHtml(String(o.status || '').toUpperCase())}</span></td>
        <td class="font-mono text-slate-500">${escapeHtml(o.exchange_order_id || '—')}</td>
        <td class="max-w-[22rem] truncate text-slate-400" title="${escapeHtml(o.message || '')}">${escapeHtml(o.message || '')}</td>
      </tr>`;
  }).join('');
}

/**
 * Resolve unresolved orders against the broker now.
 *
 * The same pass the bot runs on its own clock, exposed so an operator can clear
 * a stuck block without restarting - and so the readiness report can be re-run
 * straight after a manual fix.
 */
async function reconcileOrders() {
  const button = $('btn-orders-reconcile');
  if (button.disabled) return;
  button.disabled = true;
  button.textContent = 'Reconciling…';
  try {
    // Reconcile the mode being viewed, not the process mode: an operator looking
    // at LIVE orders means the LIVE ones.
    const mode = $('orders-mode') ? $('orders-mode').value : '';
    const url = mode ? `${ENDPOINTS.reconcile}?mode=${mode}` : ENDPOINTS.reconcile;
    const result = await api(url, { method: 'POST' });
    const parts = [];
    if (result.filled) parts.push(`${result.filled} filled`);
    if (result.closed) parts.push(`${result.closed} closed`);
    if (result.still_open) parts.push(`${result.still_open} still open`);
    if (result.unknown) parts.push(`${result.unknown} unknown`);
    const unmatched = (result.trade_book || {}).unmatched || [];
    if (unmatched.length) parts.push(`${unmatched.length} unmatched fill(s)`);
    toast(
      parts.length ? `Reconciled: ${parts.join(', ')}` : 'Nothing to reconcile',
      (result.errors || []).length || unmatched.length ? 'warn' : 'ok',
    );
    await loadLedger();
  } catch (error) {
    toast(`Reconcile failed: ${error.message}`, 'err');
  } finally {
    button.disabled = false;
    button.textContent = 'Reconcile';
  }
}

/**
 * Open positions, on the fast lane.
 *
 * These are the numbers a trader watches, and the mark loop rewrites them every
 * few seconds. Leaving them on the ledger cadence stacked a 5 s fetch on top of
 * a 5 s mark, so the table could show a price up to 10 s old - which reads as
 * latency in the bot rather than in the refresh.
 */
async function loadPositions() {
  const positions = await api(ENDPOINTS.positions);
  state.positions = positions.positions || [];
  renderPositions();
  renderDashPositions();
  // The unrealised half of the summary comes from these rows, so it is repainted
  // here rather than only on the slower ledger cadence.
  if (state.strategies) renderSummary(state.strategies);
}

/**
 * Live open positions for the dashboard.
 *
 * A compact table rather than one card per book: on the dashboard the question
 * is "what am I holding and is it working", read in one pass, so the rows share
 * a column grid and the book is a column rather than a heading. The Orders tab
 * keeps the per-book cards for the case where the detail matters.
 */
function renderDashPositions() {
  const host = $('dash-positions');
  if (!host) return;

  const runs = (state.strategies && state.strategies.runs) || [];
  const accounts = (state.strategies && state.strategies.accounts) || {};
  const byId = new Map(runs.map((r) => [r.id, r]));
  // Every open position, including any whose strategy_id matches no configured
  // book. Those are real positions - an order booked under an id the dashboard
  // does not know about - and dropping them made the totals say "flat" while the
  // ledger held a live position. They are shown with their raw id and a warning.
  const open = (state.positions || []).filter((p) => p.status === 'OPEN');

  let pnl = 0;
  let exposure = 0;
  for (const row of open) {
    pnl += num(row.unrealized_pnl) || 0;
    exposure += Math.abs((num(row.mark_price) || 0) * (num(row.quantity) || 0));
  }

  setText('dash-positions-caption', open.length
    ? `${open.length} open leg${open.length === 1 ? '' : 's'} · marked live`
    : 'Flat — no book is holding a position');
  setText('dash-positions-pnl', open.length ? signedInr(pnl) : '—', pnlClass(open.length ? pnl : null));
  setText('dash-positions-exposure', open.length ? inr(exposure) : '—');

  if (!open.length) {
    host.innerHTML = '<p class="dash-positions-empty">No open positions. Each book enters on its own signal.</p>';
    return;
  }

  // Grouped by book, in the order the tiles appear (column, then timeframe), so
  // the panel reads the same way as the board above it. A trader scanning a leg
  // needs to know which book and clock it belongs to before anything else - the
  // same contract held by a 15m and a 60m book is two different trades.
  const rows = open.slice().sort((a, b) => String(a.entry_time || '').localeCompare(String(b.entry_time || '')));
  const order = new Map(runs.map((r, i) => [r.id, i]));
  const groups = new Map();
  for (const row of rows) {
    const key = row.strategy_id || '—';
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(row);
  }
  const keys = [...groups.keys()].sort((a, b) => {
    const ai = order.has(a) ? order.get(a) : 999;
    const bi = order.has(b) ? order.get(b) : 999;
    return ai - bi || String(a).localeCompare(String(b));
  });

  const cell = (label, value, cls) => `
    <td class="num ${cls || ''}" data-label="${escapeHtml(label)}">${escapeHtml(value)}</td>`;

  host.innerHTML = keys.map((key) => {
    const legs = groups.get(key);
    const run = byId.get(key);
    const acct = accounts[key] || {};
    const known = !!run;
    const mode = acct.mode || run?.mode || legs[0].mode || 'PAPER';
    const tf = run?.seconds ? `${Math.round(run.seconds / 60)}m` : (legs[0].timeframe || '');
    const name = known ? (run.name || key) : (legs[0].strategy || key);
    const instrument = String(
      legs[0].symbol || (known ? (run.symbol || run.source?.symbol || '') : ''),
    ).replace(/^(NSEIX:|NSE:)/i, '');
    const subtotal = legs.reduce((sum, r) => sum + (num(r.unrealized_pnl) || 0), 0);
    return `
      <div class="pos-group${known ? '' : ' is-orphan'}">
        <div class="pos-group-head">
          <span class="pos-group-name">${escapeHtml(name)}</span>
          ${tf ? `<span class="pos-group-tf">${escapeHtml(tf)}</span>` : ''}
          <span class="pos-group-instrument">${escapeHtml(instrument || '—')}</span>
          <span class="book-band-mode ${mode === 'LIVE' ? 'is-live' : 'is-paper'}">${escapeHtml(mode)}</span>
          ${known ? '' : `<span class="dash-orphan-tag" title="${escapeHtml(
            `No configured book has the id "${key}". The position exists in the ledger but belongs to no tile - square it off.`)}">unattributed</span>`}
          <span class="pos-group-subtotal ${pnlClass(subtotal)}">${signedInr(subtotal)}</span>
        </div>
        <table class="dash-positions-table">
          <thead>
            <tr>
              <th>Side</th><th class="num">Qty</th><th class="num">Entry</th>
              <th class="num">LTP</th><th class="num">Stop</th><th class="num">Points</th>
              <th class="num">P&amp;L</th><th>Opened</th>
            </tr>
          </thead>
          <tbody>
            ${legs.map((row) => {
              const k = num(row.position_key) || 1;
              const entry = num(row.entry_price);
              const mark = num(row.mark_price);
              const points = entry === null || mark === null ? null : (mark - entry) * k;
              const legPnl = num(row.unrealized_pnl);
              return `
                <tr>
                  <td><span class="tag ${k === 1 ? 'tag-buy' : 'tag-sell'}">${k === 1 ? 'LONG' : 'SHORT'}</span></td>
                  ${cell('Qty', row.quantity)}
                  ${cell('Entry', level(entry))}
                  ${cell('LTP', level(mark))}
                  ${cell('Points', signedPoints(points), points === null ? '' : (points >= 0 ? 'pos' : 'neg'))}
                  ${cell('P&L', signedInr(legPnl), pnlClass(legPnl))}
                  <td class="text-slate-400" data-label="Opened">${escapeHtml(istClock(row.entry_time) || '—')}</td>
                </tr>`;
            }).join('')}
          </tbody>
        </table>
      </div>`;
  }).join('');
}

/**
 * Open positions across every book, one card each.
 *
 * The books carry separate ledgers and can run in different modes, so they are
 * listed per book rather than summed: a live leg and a paper leg are not the
 * same exposure and must never be added together. A book with nothing open still
 * gets a card, because "flat" is information - an empty panel would leave the
 * reader unsure whether the book was flat or the panel was broken.
 */
function renderPositions() {
  const host = $('positions-grid');
  if (!host) return;

  const runs = state.strategies && state.strategies.runs ? state.strategies.runs : [];
  const accounts = (state.strategies && state.strategies.accounts) || {};

  const books = [];
  for (const run of runs) {
    books.push({
      id: run.id,
      name: run.name || run.id,
      seconds: run.seconds,
      mode: (accounts[run.id] && accounts[run.id].mode) || run.mode || 'PAPER',
      source: 'futures',
      rows: state.positions.filter(
        (p) => (p.strategy_id || '') === run.id && p.status === 'OPEN',
      ),
    });
  }

  let open = 0;
  let unrealised = 0;
  let exposure = 0;
  for (const book of books) {
    for (const row of book.rows) {
      open += 1;
      unrealised += num(row.unrealized_pnl) || 0;
      exposure += Math.abs((num(row.mark_price) || 0) * (num(row.quantity) || 0));
    }
  }
  const holding = books.filter((b) => b.rows.length).length;

  setText('positions-caption', open
    ? `${open} open leg${open === 1 ? '' : 's'} across ${holding} book(s)`
    : 'No book is holding a position');
  const summary = $('positions-summary');
  if (summary) {
    summary.textContent = open
      ? `${signedInr(unrealised)} · ${inr(exposure)} notional`
      : 'Flat';
    summary.className = `pill ${open ? (unrealised > 0 ? 'pill-live' : 'pill-bad') : 'pill-idle'}`;
  }

  if (!books.length) {
    host.innerHTML = '<p class="condition-empty">No strategy is configured.</p>';
    return;
  }

  // Legs whose book is unknown would otherwise vanish: they belong to no card,
  // so a real position would be invisible on both tabs at once.
  const known = new Set(books.map((b) => b.id));
  const orphans = state.positions.filter(
    (p) => p.status === 'OPEN' && !known.has(p.strategy_id || ''),
  );
  if (orphans.length) {
    const sample = orphans[0] || {};
    books.push({
      id: '__orphans', name: 'Unattributed', seconds: 0, mode: sample.mode || 'PAPER',
      source: 'futures', rows: orphans, orphan: true,
    });
  }

  const legTable = (rows, venue) => {
    if (!rows.length) return '';
    return `
      <table class="position-table">
        <thead>
          <tr>
            <th>${escapeHtml(venue)}</th><th class="num">Qty</th><th class="num">Entry</th>
            <th class="num">Mark</th><th class="num">Points</th>
            <th class="num">P&amp;L</th><th>Since</th>
          </tr>
        </thead>
        <tbody>
          ${rows.map((row) => {
            const key = num(row.position_key) || 1;
            const entry = num(row.entry_price);
            const mark = num(row.mark_price);
            const points = entry === null || mark === null ? null : (mark - entry) * key;
            const pnl = num(row.unrealized_pnl);
            return `
              <tr>
                <td><span class="tag ${key === 1 ? 'tag-buy' : 'tag-sell'}">${escapeHtml(row.side || '')}</span></td>
                <td class="num">${escapeHtml(row.quantity)}</td>
                <td class="num">${level(entry)}</td>
                <td class="num">${level(mark)}</td>
                <td class="num ${points === null ? '' : (points >= 0 ? 'pos' : 'neg')}">${signedPoints(points)}</td>
                <td class="num ${pnlClass(pnl)}">${signedInr(pnl)}</td>
                <td class="text-slate-400">${escapeHtml(istClock(row.entry_time) || '—')}</td>
              </tr>`;
          }).join('')}
        </tbody>
      </table>`;
  };

  host.innerHTML = books.map((book) => {
    const isLive = book.mode === 'LIVE';
    const body = book.rows.length
      ? legTable(book.rows, 'Side')
      : '<p class="position-flat">Flat — nothing open.</p>';
    return `
      <div class="position-card${book.orphan ? ' is-orphan' : ''}">
        <div class="position-card-head">
          <span class="position-card-name">${escapeHtml(book.name)}</span>
          <span class="book-band-mode ${isLive ? 'is-live' : 'is-paper'}">${escapeHtml(book.mode)}</span>
          ${book.seconds ? `<span class="position-card-tf">${Math.round(book.seconds / 60)}m</span>` : ''}
        </div>
        ${body}
      </div>`;
  }).join('');
}

async function poll() {
  if (state.busy) return;
  try {
    // Fast lane: only what changes tick-by-tick. The ledger endpoints are
    // fetched on a slower cadence below, so a poll is 3 requests instead of 6.
    const [status, portfolio, strategies] = await Promise.all([
      api(ENDPOINTS.status),
      api(ENDPOINTS.portfolio),
      // Per-strategy state changes on the same cadence as the engine, so it
      // belongs on the fast lane rather than the slower ledger one.
      api(ENDPOINTS.strategies).catch(() => null),
    ]);

    state.lastSync = new Date();
    state.failures = 0;
    state.status = status;

    // Track server clock drift so the countdown can trust it.
    const serverTime = status.server_time ? new Date(status.server_time) : null;
    if (serverTime && !Number.isNaN(serverTime.getTime())) {
      state.serverOffsetMs = serverTime.getTime() - Date.now();
    }

    renderStatus(status);
    renderPortfolio(portfolio);
    if (strategies) {
      state.strategies = strategies;
      renderSummary(strategies);
      renderStrategies(strategies);
      renderBookPerformance();
      // Positions read the book list to label each card, so they are repainted
      // when it arrives - otherwise a book that has never polled before would
      // show its legs under no card at all.
      renderPositions();
      renderDashPositions();
    }
    // Positions ride the fast lane: the mark loop rewrites them every few
    // seconds, so they are tick-by-tick data, not ledger data.
    loadPositions().catch(() => { /* keep the last good rows */ });

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
      // A short label plus a dot. The full sentence used to sit here and was
      // truncated to "Kite authenticated · GIF…" at any realistic width, which
      // told the operator nothing. The detail moves to the tooltip.
      if (feed.connected && age !== null && age < 120) {
        note.textContent = 'Kite';
        note.title = `Kite authenticated · GIFT ticks live (${timeAgo(age)})`;
        note.className = 'header-kite-note is-ok';
      } else if (feed.starting) {
        note.textContent = 'Kite';
        note.title = 'Kite authenticated · GIFT market data is connecting…';
        note.className = 'header-kite-note is-warn';
      } else if (feed.connected) {
        note.textContent = 'Kite';
        note.title = 'Kite authenticated · connected, waiting for GIFT ticks';
        note.className = 'header-kite-note is-warn';
      } else {
        note.textContent = 'Kite';
        note.title = `Kite authenticated · GIFT feed stopped${feed.error ? `: ${feed.error}` : ''}`;
        note.className = 'header-kite-note is-warn';
      }
      btn.textContent = 'Refresh';
      btn.title = 'Refresh Kite login';
      $('kite-manual').classList.add('hidden');
      reconnectBtn.classList.remove('hidden');
      reconnectBtn.disabled = !!feed.starting;
    } else {
      note.textContent = 'Kite';
      note.title = status.detail || 'Not connected';
      note.className = 'header-kite-note is-warn';
      btn.textContent = status.api_key_present ? 'Connect' : 'API key missing';
      btn.title = status.api_key_present ? 'Connect to Kite' : 'KITE_API_KEY is missing';
      reconnectBtn.classList.add('hidden');
      $('kite-manual').classList.remove('hidden');
    }
    btn.disabled = !status.api_key_present;
    state.kite = status;
    note.title = note.textContent;
  } catch (error) {
    $('kite-note').textContent = 'Kite';
    $('kite-note').className = 'header-kite-note is-bad';
    $('kite-note').title = `Token status unavailable: ${error.message}`;
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

/* Live trade monitor: one row per open position across both books. */
/* Stop-out notice. A stop closes one trade and the strategy keeps running, so
   this is informational: it says which trade was closed, not that the bot has
   stopped. Nothing needs acknowledging for trading to continue. */
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
    title: 'Dismiss stop-out notice',
    body: 'This only clears the notice. The strategy is still running and will '
      + 'take the next fresh signal on its own - nothing is paused, and no '
      + 'position is reopened.',
    okLabel: 'Dismiss',
    danger: false,
  });
  if (!ok) return;
  await withBusy(async () => {
    try {
      await api('/api/risk/clear', { method: 'POST' });
      toast('Stop-out notice dismissed', 'ok');
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

async function stopAll() {
  const btn = $('btn-stop-all');
  // Already latched: this click clears it, which is a different confirmation.
  if (btn && btn.dataset.stopped === '1') {
    const ok = await confirmDialog({
      title: 'Clear STOP ALL',
      body: 'This allows <strong>new entries</strong> in every strategy again. '
        + 'Only clear it once you are satisfied the problem is resolved.',
      okLabel: 'Resume trading',
    });
    if (!ok) return;
    btn.disabled = true;
    await withBusy(async () => {
      try {
        const result = await api(ENDPOINTS.stopAllClear, { method: 'POST' });
        toast(result.stopped_all ? 'STOP ALL still engaged' : 'STOP ALL cleared',
          result.stopped_all ? 'err' : 'ok');
        setActionStatus('STOP ALL cleared', 'ok');
      } catch (error) {
        toast(`Clear failed: ${error.message}`, 'err');
        setActionStatus(error.message, 'err');
      } finally {
        btn.disabled = false;
      }
    });
    return;
  }

  const ok = await confirmDialog({
    title: '🛑 STOP ALL',
    body: 'This blocks <strong>new entries in every strategy</strong> immediately and '
      + 'latches until you clear it, including across a restart. Open positions are '
      + '<strong>not</strong> closed and have no stop — use Square off '
      + 'if you also want them closed.',
    okLabel: 'Stop all trading',
  });
  if (!ok) return;

  btn.disabled = true;
  btn.textContent = 'Stopping…';
  await withBusy(async () => {
    try {
      const result = await api(ENDPOINTS.stopAll, {
        method: 'POST',
        body: JSON.stringify({ confirm: true, reason: 'STOP_ALL_UI' }),
      });
      toast(result.note || 'STOP ALL engaged', 'ok');
      setActionStatus(result.note || 'STOP ALL engaged', 'ok');
    } catch (error) {
      toast(`STOP ALL failed: ${error.message}`, 'err');
      setActionStatus(error.message, 'err');
    } finally {
      btn.disabled = false;
      btn.textContent = 'STOP ALL';
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

/**
 * The levels behind the signal chips, and how far price is from flipping them.
 *
 * A verdict alone says "short". It does not say whether the next bar is likely
 * to keep it, which is the question a trader actually asks of a live signal. The
 * distance to each rule's flip is that answer, so it is shown as a number rather
 * than left to be worked out from two levels and a price.
 */
/**
 * How far price must travel to flip each rule, and whether that is close.
 *
 * Shared by the active-book panel and the per-book rows so the two can never
 * disagree about what "near" means. The threshold is a quarter of the bar's own
 * range rather than a fixed point count: 16 points is close on a 66-point bar
 * and nothing on a 400-point one.
 */
function flipGaps(close, values) {
  const psar = num(values.psar);
  const st = num(values[values.supertrend_config?.key] ?? values.supertrend);
  const range = num(values.candle_range);
  const limit = range ? range * 0.25 : null;
  const gap = (level) => (close === null || level === null ? null : Math.abs(close - level));
  const near = (g) => (g === null || limit === null ? false : g <= limit);
  return {
    close, psar, st, range, limit,
    psarGap: gap(psar), stGap: gap(st),
    psarNear: near(gap(psar)), stNear: near(gap(st)),
  };
}

/**
 * Pause or resume one book.
 *
 * Delegated from the container rather than bound per tile: the tiles are
 * rebuilt from HTML on every poll, so a listener attached to a button would be
 * discarded within 1.5 s and the second click would do nothing.
 */
async function toggleBookPause(event) {
  const button = event.target.closest('[data-book-pause]');
  if (!button || button.disabled) return;
  const strategyId = button.dataset.bookPause;
  const paused = button.getAttribute('aria-pressed') === 'true';
  button.disabled = true;
  try {
    const result = await api('/api/books/pause', {
      method: 'POST',
      body: JSON.stringify({ strategy_id: strategyId, paused: !paused }),
    });
    toast(`${strategyId} ${result.paused ? 'paused' : 'resumed'}`,
      result.paused ? 'info' : 'ok');
    await poll();
  } catch (error) {
    toast(`Could not change ${strategyId}: ${error.message}`, 'err');
    button.disabled = false;
  }
}

/**
 * Switch one book between PAPER and LIVE.
 *
 * Arming LIVE is confirmed first, because it is the act that starts placing real
 * orders. The server enforces the same confirmation and the pre-flight checks;
 * this dialog is the operator's own guard, not the only one.
 */
async function toggleBookMode(event) {
  const button = event.target.closest('[data-book-mode]');
  if (!button || button.disabled) return;
  const strategyId = button.dataset.bookMode;
  const current = button.dataset.bookModeCurrent;
  const target = current === 'LIVE' ? 'PAPER' : 'LIVE';
  if (target === 'LIVE') {
    const ok = await confirmDialog({
      title: 'Arm LIVE trading?',
      body: `Switch ${strategyId} to LIVE. From the next signal this book places real orders on the NIFTY future.`,
      strategyId,
      action: 'PAPER → LIVE',
      mode: 'LIVE',
      warn: 'Real money. The book keeps its own stop and risk limits, but fills are final.',
      okLabel: 'Arm LIVE',
    });
    if (!ok) return;
  }
  button.disabled = true;
  try {
    const result = await api('/api/books/mode', {
      method: 'POST',
      body: JSON.stringify({ strategy_id: strategyId, mode: target, confirm: true }),
    });
    toast(`${strategyId} switched ${result.previous} → ${result.mode}`, 'ok');
    await poll();
  } catch (error) {
    toast(`Could not switch ${strategyId}: ${error.message}`, 'err');
    button.disabled = false;
  }
}

/**
 * Square off one book, then pause it.
 *
 * Confirmed because it closes a real position in LIVE. The server scopes the
 * exit to this book, so the other book's leg in the same contract is untouched.
 */
async function squareOffBook(event) {
  const button = event.target.closest('[data-book-square-off]');
  if (!button || button.disabled) return;
  const strategyId = button.dataset.bookSquareOff;
  const mode = button.dataset.bookSquareOffMode || 'PAPER';
  const ok = await confirmDialog({
    title: 'Square off this book?',
    body: `Close ${strategyId}'s open position at market and pause the book. The other book's leg in the same contract is left alone.`,
    strategyId,
    action: 'Square off + pause',
    mode,
    warn: mode === 'LIVE' ? 'This sends a real market order.' : '',
    okLabel: 'Square off',
    danger: mode === 'LIVE',
  });
  if (!ok) return;
  button.disabled = true;
  try {
    const result = await api('/api/books/square-off', {
      method: 'POST',
      body: JSON.stringify({ strategy_id: strategyId, confirm: true }),
    });
    toast(
      result.ok ? `${strategyId} squared off` : `${strategyId}: ${result.reason}`,
      result.ok ? 'ok' : 'err',
    );
    await poll();
  } catch (error) {
    toast(`Could not square off ${strategyId}: ${error.message}`, 'err');
    button.disabled = false;
  }
}

/**
 * Place a manual BUY or SELL for one book.
 *
 * Confirmed first: in LIVE this is a real order. The server routes it through
 * the same path as a signal, so the session gate, pause switch, duplicate-entry
 * guard, lot/margin limits and the book's initial stop all still apply - the
 * operator picks the direction, not the risk rules.
 */
async function manualBookOrder(event) {
  const button = event.target.closest('[data-book-manual]');
  if (!button || button.disabled) return;
  const strategyId = button.dataset.bookManual;
  const action = button.dataset.bookManualAction;
  const mode = button.dataset.bookManualMode || 'PAPER';
  const ok = await confirmDialog({
    title: `Manual ${action}?`,
    body: `Place a manual ${action} for ${strategyId}. It goes through the same risk checks as a signal, and the book's initial stop is applied on fill.`,
    strategyId,
    action: `Manual ${action}`,
    mode,
    warn: mode === 'LIVE' ? 'This is a real order.' : '',
    okLabel: `Send ${action}`,
    danger: mode === 'LIVE',
  });
  if (!ok) return;
  button.disabled = true;
  try {
    const result = await api('/api/books/manual-order', {
      method: 'POST',
      body: JSON.stringify({ strategy_id: strategyId, action, confirm: true }),
    });
    toast(
      result.ok
        ? `${strategyId}: manual ${action} filled`
        : `${strategyId}: ${result.reason}`,
      result.ok ? 'ok' : 'err',
    );
    await poll();
  } catch (error) {
    toast(`Could not place ${action} for ${strategyId}: ${error.message}`, 'err');
    button.disabled = false;
  }
}

/**
 * The five summary figures at the top of the dashboard./**
 * The five summary figures at the top of the dashboard.
 *
 * Everything here is derived from payloads the page already fetches, so this
 * adds no request: per-book performance for the realised side, the open legs for
 * the unrealised side. They are kept separate rather than summed into one number
 * because a realised loss and an open loss are different facts - one is money
 * spent, the other is a decision still running.
 */
function renderSummary(payload) {
  const runs = (payload && payload.runs) || [];
  const perf = (payload && payload.performance) || {};
  const open = (state.positions || []).filter((p) => p.status === 'OPEN');

  let realised = 0;
  let closed = 0;
  let wins = 0;
  let losses = 0;
  for (const run of runs) {
    const book = perf[run.id] || {};
    realised += Number(book.net_pnl) || 0;
    closed += Number(book.trades) || 0;
    wins += Number(book.wins) || 0;
    losses += Number(book.losses) || 0;
  }

  let unrealised = 0;
  for (const row of open) unrealised += Number(row.unrealized_pnl) || 0;

  const total = realised + unrealised;
  setText('sum-pnl', signedInr(total), pnlClass(total));
  setText('sum-pnl-sub', `${signedInr(realised)} realised · ${signedInr(unrealised)} open`);
  setText('sum-unrealised', open.length ? signedInr(unrealised) : '—',
    open.length ? pnlClass(unrealised) : 'flat');
  setText('sum-unrealised-sub', open.length
    ? `${open.length} leg${open.length === 1 ? '' : 's'} marked live`
    : 'no open positions');
  setText('sum-open', String(open.length));
  setText('sum-open-sub', open.length
    ? [...new Set(open.map((p) => p.symbol))].join(', ')
    : 'flat');
  setText('sum-closed', String(closed));
  setText('sum-closed-sub', closed
    ? `${wins}W · ${losses}L`
    : 'no closed trades yet');

  const ready = runs.filter((r) => (r.state || {}).ready).length;
  const paused = runs.filter((r) => (r.state || {}).paused).length;
  setText('sum-books', `${ready}/${runs.length}`);
  setText('sum-books-sub', paused
    ? `${paused} paused`
    : runs.length ? 'all evaluating' : 'none configured');
}

/**
 * Fill the book filter with the configured books.
 *
 * Rebuilt only when the set of ids changes, so a poll every 1.5s does not reset
 * the operator's selection. The current value is restored by id, and an id that
 * no longer exists falls back to "All books" rather than leaving a dangling
 * filter that matches nothing.
 */
function syncBookFilter(runs) {
  const el = $('orders-book');
  if (!el) return;
  const wanted = ['', ...runs.map((r) => r.id)].join('|');
  if (el.dataset.books !== wanted) {
    const previous = el.value;
    el.dataset.books = wanted;
    el.replaceChildren(
      new Option('All books', ''),
      ...runs.map((r) => new Option(r.name || r.id, r.id)),
    );
    el.value = runs.some((r) => r.id === previous) ? previous : '';
  }
}

/**
 * The group's shared facts, stated once above its tiles.
 *
 * Both books trade the same contract, so the instrument, the venue and the
 * combined exposure are properties of the *group*, not of either tile. Repeating
 * them in both tiles made two strategies on one instrument read as two unrelated
 * things; stating them once here is what makes the pair legible as one position
 * at the broker.
 *
 * The figures are summed across books but never across modes: a LIVE leg and a
 * PAPER leg are different money, and adding them would produce a number that is
 * true of neither.
 */
function renderGroupSummary(books, perf, accounts) {
  if (!books.length) return '';
  const modes = new Set(books.map((r) => (accounts[r.id] || {}).mode || r.mode || 'PAPER'));
  const mixed = modes.size > 1;
  const modeLabel = mixed ? 'MIXED' : [...modes][0];

  let netPnl = 0;
  let openUnits = 0;
  let trades = 0;
  let havePnl = false;
  for (const run of books) {
    const book = perf[run.id] || {};
    const pnl = num(book.net_pnl);
    if (pnl !== null) { netPnl += pnl; havePnl = true; }
    trades += Number(book.trades) || 0;
    openUnits += Number((run.state || {}).open_units) || 0;
  }

  const positionText = openUnits === 0
    ? 'Flat'
    : `${openUnits > 0 ? 'Long' : 'Short'} ${Math.abs(openUnits)}`;
  const pnlClass = netPnl > 0 ? 'pos' : netPnl < 0 ? 'neg' : '';

  return `
    <div class="book-group-summary">
      <span class="book-group-summary-item">
        <span class="book-group-summary-label">Venue</span>
        <span class="book-group-summary-value">NIFTY futures</span>
      </span>
      <span class="book-group-summary-item">
        <span class="book-group-summary-label">Mode</span>
        <span class="book-group-summary-value">${escapeHtml(modeLabel)}</span>
      </span>
      <span class="book-group-summary-item">
        <span class="book-group-summary-label">Net position</span>
        <span class="book-group-summary-value">${escapeHtml(positionText)}</span>
      </span>
      <span class="book-group-summary-item">
        <span class="book-group-summary-label">Combined P&amp;L</span>
        <span class="book-group-summary-value ${pnlClass}">${havePnl ? signedInr(netPnl) : '—'}</span>
      </span>
      <span class="book-group-summary-item">
        <span class="book-group-summary-label">Trades</span>
        <span class="book-group-summary-value">${trades}</span>
      </span>
    </div>`;
}

function renderStrategies(payload) {
  const host = $('book-bands');
  if (!host) return;
  const runs = (payload && payload.runs) || [];
  // The ledger tabs share this list, so it is kept in step here rather than in a
  // second place that could disagree.
  syncBookFilter(runs);
  const perf = (payload && payload.performance) || {};
  const accounts = (payload && payload.accounts) || {};

  if (!runs.length) {
    host.innerHTML = '<p class="condition-empty">No strategy is configured.</p>';
    return;
  }

  // Both strategies execute on the NIFTY future, so there is one column. The
  // signal series differs (GIFT 15m vs NIFTY 60m) and each tile names its own,
  // but the venue the operator is exposed to is the same for both.
  //
  // Within the column the books are ordered by timeframe, so 15m and 60m sit one
  // above the other.
  const columns = [
    { key: 'nifty', title: 'NIFTY 50', note: 'NSE · traded via the future' },
  ];

  const buckets = new Map(columns.map((c) => [c.key, []]));
  for (const run of runs) {
    buckets.get('nifty').push(run);
  }

  host.innerHTML = columns.map((column) => {
    const books = (buckets.get(column.key) || [])
      .slice()
      .sort((a, b) => (a.seconds || 0) - (b.seconds || 0));
    const body = books.length
      ? books.map((run) => renderBookBand(run, perf, accounts, column.key)).join('')
      : '<p class="condition-empty">No book on this instrument.</p>';
    return `
      <section class="book-group">
        <div class="book-group-head">
          <div>
            <h3 class="book-group-title">${escapeHtml(column.title)}</h3>
            <span class="book-group-note">${escapeHtml(column.note)}</span>
          </div>
          <span class="book-group-count">${books.length} book${books.length === 1 ? '' : 's'}</span>
        </div>
        ${renderGroupSummary(books, perf, accounts)}
        <div class="book-group-tiles">
          ${body}
        </div>
      </section>`;
  }).join('');
}

/**
 * One tile: the whole state of a single strategy book, kept light.
 *
 * Everything a book owns is inside its own tile - signal, the two rule checks,
 * position and account - because two books are two independent strategies and a
 * shared panel would have to be read twice and still not say which figure
 * belonged to which. What changed is the density: the same numbers in four
 * compact rows instead of four boxed sections, so four tiles fit on one screen.
 */
function renderBookBand(run, perf, accounts, columnKey) {
  const st = run.state || {};
  const src = run.source || {};
  const values = st.last_values || {};
  const conditions = st.last_conditions || {};
  const signal = st.last_signal || 'NO_SIGNAL';
  const ready = !!st.ready;
  const err = st.last_error || src.error || null;

  const tone = signal === 'BULL' ? 'is-bull' : signal === 'BEAR' ? 'is-bear' : '';
  const signalClass = signal === 'BULL' ? 'pos' : signal === 'BEAR' ? 'neg' : 'flat';
  const signalNote = signal === 'BULL' ? 'both rules long'
    : signal === 'BEAR' ? 'both rules short'
      : 'rules disagree';

  // A paused book is not "running": it is evaluating and deliberately not
  // entering, which is a different thing to say and the reason the toggle must
  // be visible on the tile rather than only in the page header.
  const paused = !!st.paused;
  const stateText = paused ? 'paused' : err ? 'error' : !ready ? 'warming up' : 'running';
  const stateVariant = paused ? 'paper' : err ? 'bad' : ready ? 'live' : 'idle';

  // The *signal* series, which is not the venue the book is exposed to. A
  // GIFT-sourced book that trades the Nifty future reads GIFT and holds NIFTY,
  // and the tile has to say both or the reader cannot tell which is which.
  const venue = (src.symbol || run.symbol || '')
    .replace(/^NSEIX:/i, '').replace(/^NSE:/i, '');
  const signalVenue = /gift/i.test(venue) ? 'GIFT' : /nifty/i.test(venue) ? 'NIFTY' : '';
  // Shown only when the signal series differs from the column it sits in, so a
  // NIFTY-sourced book in the NIFTY column does not repeat itself. Compared
  // case-insensitively: the column key is lowercase and the label is not, and a
  // strict compare printed "signal NIFTY" on the NIFTY tile.
  const venueTag = signalVenue && signalVenue.toLowerCase() !== String(columnKey || '').toLowerCase()
    ? `signal ${signalVenue}`
    : '';

  const g = flipGaps(num(run.last_close), values);

  // The levels below are the *signal series*, which is an index and cannot be
  // traded. The book is exposed to the NIFTY future, and the two differ by the
  // basis - a few hundred points on a normal day. Showing the index levels with
  // no label read as "these are my levels" and they never matched the price on
  // Zerodha, which quotes the future. So the series is named, and the basis to
  // the future is stated beside it: that gap is what the trade has to overcome
  // before it is profitable, and it is the number that explains the mismatch.
  const exec = (state.status && state.status.feed && state.status.feed.exec) || null;
  const execPrice = exec ? num(exec.price) : null;
  const basis = execPrice !== null && g.close !== null ? execPrice - g.close : null;
  const seriesLabel = signalVenue || 'signal';
  const basisNote = basis === null
    ? ''
    : ` · future ${level(execPrice)} (${basis >= 0 ? '+' : ''}${level(basis, 1)})`;

  // Indicators and rule checks are the same two numbers read two ways, so they
  // share one row: each states the indicator's level, its own direction and how
  // far price is from flipping it. A single "2 of 2 confirmed" count hides which
  // indicator voted which way, so a split bar would read as a weaker version of
  // an agreeing one. Naming each rule and colouring it by its own direction
  // makes the disagreement the visible thing, which is what decides whether the
  // next bar flips the book.
  const total = num(values.confirmations_total) || 2;
  const buyN = num(values.buy_confirmations) || 0;
  const sellN = num(values.sell_confirmations) || 0;
  const stDir = conditions.buy_supertrend ? 'BULL'
    : conditions.sell_supertrend ? 'BEAR' : 'FLAT';
  const psarDir = conditions.buy_psar ? 'BULL'
    : conditions.sell_psar ? 'BEAR' : 'FLAT';
  const dirClass = (dir) => (dir === 'BULL' ? 'is-bull' : dir === 'BEAR' ? 'is-bear' : 'is-flat');
  const agrees = (dir) => (signal === 'NO_SIGNAL' || dir === 'FLAT' ? null : dir === signal);
  // Each rule is one line: name, its level, which way it points, and how far
  // price has to move to turn it. The direction is stated once per rule rather
  // than repeated as an agree/disagree verdict - the headline already says what
  // the pair decided, so "agrees" next to both was two words saying nothing.
  const ruleRow = (name, value, dir, gap, near) => {
    const same = agrees(dir);
    // A split bar has no verdict to agree with, so say "split" rather than
    // leaving a dash that reads as missing data.
    const note = same === null ? 'split' : same ? '' : 'against';
    return `
      <div class="book-band-rule ${dirClass(dir)}">
        <span class="book-band-rule-name">${escapeHtml(name)}</span>
        <span class="book-band-rule-dir">${escapeHtml(dir)}</span>
        <span class="book-band-rule-value">${escapeHtml(value)}</span>
        <span class="book-band-rule-gap${near ? ' is-near' : ''}"
          title="${escapeHtml(near
            ? 'Price is inside a quarter of the last bar\'s range of this level - a flip is close'
            : 'How far price must move to turn this rule')}">${
          gap === '—' ? '' : `${escapeHtml(gap)} to flip`}</span>
        ${note ? `<span class="book-band-rule-note">${escapeHtml(note)}</span>` : ''}
      </div>`;
  };

  // Account and score: what the book holds and what it has made.
  const acct = accounts[run.id] || {};
  const book = perf[run.id] || {};
  const pnl = num(book.net_pnl);
  const equity = num(acct.equity);
  const trades = book.trades ?? 0;
  // The mode this book is *actually* running, which is not the bot's mode: one
  // book can be live while another keeps its paper ledger. Read from the account
  // first because that is the ledger the figures beside it came from - a
  // live-looking label on a paper balance is the number a trader would misread.
  const bookMode = acct.mode || run.mode || 'PAPER';
  const acctSource = acct.source === 'zerodha' ? 'Zerodha' : 'ledger';

  const open = num(st.open_units) || 0;
  const positionText = open === 0 ? 'Flat' : `${open > 0 ? 'Long' : 'Short'} ${Math.abs(open)}`;

  // What the book is doing next, in words. The verdict ships a compact string
  // ("close=… psar=… ST bullish confirm BUY 2/2 SELL 0/2") which is useful in a
  // tooltip and unreadable as the last line of a tile.
  //
  // The level named here is the *signal series* level, not a price the position
  // can be closed at. The book holds the future, so the exit happens when the
  // future's own price crosses the equivalent level - the index level plus the
  // basis. Saying "exits at <index level>" beside a future position was the
  // claim that made the tile look wrong against Zerodha.
  const singleLevels = [g.st, g.psar].filter((v) => v !== null);
  const singleFlip = singleLevels.length === 0
    ? null
    : (signal === 'BEAR' ? Math.max(...singleLevels) : Math.min(...singleLevels));
  const flipOnFuture = singleFlip === null || basis === null
    ? null
    : singleFlip + basis;
  const flipText = singleFlip === null
    ? ''
    : `${seriesLabel} ${level(singleFlip)}`
      + (flipOnFuture === null ? '' : ` (future ≈ ${level(flipOnFuture)})`);
  const singleFooter = singleFlip === null
    ? 'No level to act on yet - the indicators have not produced a value.'
    : open === 0
      ? (signal === 'NO_SIGNAL'
        ? 'Waiting for SuperTrend and PSAR to agree.'
        : `Rules already ${signal === 'BULL' ? 'long' : 'short'} - `
          + `enters on the next flip, at ${flipText}.`)
      : `Holding ${open > 0 ? 'long' : 'short'} ${Math.abs(open)}; exits at ${flipText} `
        + 'or on the next flip.';

  // There is no stop. The signal is the only exit, so the tile shows the level
  // the signal would close at rather than a stop that does not exist.
  const openRow = (state.positions || []).find(
    (p) => p.status === 'OPEN' && (p.strategy_id || '') === run.id,
  );

  const detail = err || st.last_detail || 'no evaluation yet';
  const counts = signal === 'BEAR' ? `${sellN}/${total} short`
    : signal === 'BULL' ? `${buyN}/${total} long`
      : `${buyN} long · ${sellN} short`;

  return `
    <article class="book-band ${tone}">
      <div class="book-band-head">
        <div class="book-band-headline">
          <span class="book-band-title">${escapeHtml(run.name || run.id)}</span>
          ${venueTag ? `<span class="book-band-venue">${escapeHtml(venueTag)}</span>` : ''}
          <span class="book-band-rule-tag" title="${escapeHtml(
            `SuperTrend ${values.supertrend_config?.length ?? 14}×${values.supertrend_config?.multiplier ?? 3} + PSAR · ${run.exit || 'flip'} exit`)}"
          >ST+PSAR · ${escapeHtml(run.exit || 'flip')}</span>
          <span class="book-band-mode ${bookMode === 'LIVE' ? 'is-live' : 'is-paper'}"
            title="${escapeHtml(bookMode === 'LIVE'
              ? `Real orders. Equity is ${acctSource}.`
              : run.live
                // Armed but not yet live: the bot is still in PAPER, so this
                // book will place real orders the moment the switch is thrown.
                // Saying only "PAPER" hid that, and the operator could not tell
                // an armed book from one that will never trade real money.
                ? 'Armed for LIVE - real orders once the bot is switched to LIVE. '
                  + 'Simulated until then.'
                : 'Simulated. Its orders, fills and P&L stay in the paper ledger.')}"
          >${escapeHtml(bookMode)}${run.live && bookMode !== 'LIVE'
            ? '<span class="book-band-armed" title="Armed for LIVE">armed</span>'
            : ''}</span>
        </div>
        <span class="book-band-actions">
          <span class="pill pill-${stateVariant}">${escapeHtml(stateText)}</span>
          <button
            class="book-mode-toggle ${bookMode === 'LIVE' ? 'is-live' : 'is-paper'}"
            type="button"
            data-book-mode="${escapeHtml(run.id)}"
            data-book-mode-current="${escapeHtml(bookMode)}"
            title="${escapeHtml(bookMode === 'LIVE'
              ? `Switch ${run.name || run.id} to PAPER. Requires its live position to be flat.`
              : `Switch ${run.name || run.id} to LIVE. Real orders will be placed.`)}"
          >${escapeHtml(bookMode)}</button>
          <button
            class="book-pause ${paused ? 'is-paused' : ''}"
            type="button"
            data-book-pause="${escapeHtml(run.id)}"
            aria-pressed="${paused ? 'true' : 'false'}"
            title="${escapeHtml(paused
              ? `Resume ${run.name || run.id}. Entries resume on its next signal.`
              : `Pause ${run.name || run.id}. Its exits stay available.`)}"
          >${paused ? 'Resume' : 'Pause'}</button>
          <button
            class="book-square-off"
            type="button"
            data-book-square-off="${escapeHtml(run.id)}"
            data-book-square-off-mode="${escapeHtml(bookMode)}"
            title="${escapeHtml(`Square off ${run.name || run.id} and pause it. Never touches the other book.`)}"
          >Square off</button>
          <span class="book-manual" role="group" aria-label="Manual orders">
            <button
              class="book-manual-buy"
              type="button"
              data-book-manual="${escapeHtml(run.id)}"
              data-book-manual-action="BUY"
              data-book-manual-mode="${escapeHtml(bookMode)}"
              title="${escapeHtml(`Manual BUY for ${run.name || run.id}. Same risk rules as an automated signal.`)}"
            >Buy</button>
            <button
              class="book-manual-sell"
              type="button"
              data-book-manual="${escapeHtml(run.id)}"
              data-book-manual-action="SELL"
              data-book-manual-mode="${escapeHtml(bookMode)}"
              title="${escapeHtml(`Manual SELL for ${run.name || run.id}. Same risk rules as an automated signal.`)}"
            >Sell</button>
          </span>
        </span>
      </div>

      <div class="book-band-signal">
        <span class="book-band-signal-value ${signalClass}">${escapeHtml(signal)}</span>
        <span class="book-band-signal-note">${escapeHtml(signalNote)}</span>
        <span class="book-band-close-inline" title="${escapeHtml(
          `${seriesLabel} close ${g.close === null ? '—' : level(g.close)}`
          + ` · bar range ${g.range ? level(g.range, 1) : '—'}`
          + (basis === null ? '' : ` · NIFTY future ${level(execPrice)} (basis ${basis >= 0 ? '+' : ''}${level(basis, 1)})`)
          + '. The levels below are the signal series, not the future you trade.')}">
          <span class="book-band-close-series">${escapeHtml(seriesLabel)}</span>
          ${g.close === null ? '—' : level(g.close)}${basisNote
            ? `<span class="book-band-close-basis">${escapeHtml(basisNote)}</span>` : ''}
        </span>
      </div>

      <div class="book-band-rules">
        ${ruleRow('SuperTrend', g.st === null ? '—' : level(g.st), stDir,
          g.stGap === null ? '—' : `${level(g.stGap, 1)}`, g.stNear)}
        ${ruleRow('PSAR', g.psar === null ? '—' : level(g.psar), psarDir,
          g.psarGap === null ? '—' : `${level(g.psarGap, 1)}`, g.psarNear)}
        <div class="book-band-rules-note">${escapeHtml(
          `${seriesLabel} levels · the book trades the NIFTY future`
          + (basis === null ? '' : `, currently ${basis >= 0 ? '+' : ''}${level(basis, 1)} away`))}</div>
      </div>

      <div class="book-band-foot">
        <span class="book-band-foot-item" title="${escapeHtml(counts)}">
          <span class="book-band-foot-label">Position</span>
          <span class="book-band-foot-value ${open === 0 ? 'is-flat' : ''}">${escapeHtml(positionText)}</span>
        </span>
        <span class="book-band-foot-item" title="${escapeHtml(
          'The signal is the only exit. There is no stop-loss and no trailing stop.')}">
          <span class="book-band-foot-label">Exit</span>
          <span class="book-band-foot-value">${escapeHtml(run.exit === 'flat' ? 'NO_SIGNAL' : 'flip')}</span>
        </span>
        <span class="book-band-foot-item">
          <span class="book-band-foot-label">P&amp;L</span>
          <span class="book-band-foot-value ${pnlClass(pnl)}">${pnl === null ? '—' : signedInr(pnl)}</span>
        </span>
        <span class="book-band-foot-item" title="${escapeHtml(
          `${trades} trade${trades === 1 ? '' : 's'}${trades ? ` · win ${book.win_rate ?? 0}%` : ''}`
          + ` · equity from ${acctSource}`)}">
          <span class="book-band-foot-label">Equity</span>
          <span class="book-band-foot-value">${equity === null ? '—' : inr(equity)}</span>
        </span>
      </div>

      <p class="strategy-detail${err ? ' is-bad' : ''}" title="${escapeHtml(detail)}">
        ${escapeHtml(err ? detail : singleFooter)}
      </p>
    </article>`;
}


/**
 * Fill the alerting panel: where alerts would go, and whether they are armed.
 *
 * Shows the *target* rather than just "configured", because a channel pointing at
 * the wrong chat looks exactly like a working one until a real alert is missed.
 */
function renderAlerts(status) {
  const alerts = status.alerts || {};
  const wd = status.watchdog || {};
  const cfg = alerts.alerting || {};
  // Two different questions, and conflating them is what made a fully set-up
  // channel report "not configured" once it was switched off:
  //   ready   - the channel has its token/url and could deliver
  //   armed   - an operator has actually switched alerting on
  const ready = cfg.channel_ready !== undefined ? !!cfg.channel_ready : !!cfg.configured;
  const armed = !!cfg.enabled;
  const on = ready && armed;

  setText('alerts-target', ready
    ? `${cfg.channel} · ${cfg.target || 'configured'}`
    : `${cfg.channel || 'none'} · not configured`);

  const pill = $('alerts-state');
  if (pill) {
    // "Armed" only when a message would actually go out; "Off" when the switch
    // is the only thing holding it back.
    pill.textContent = on ? 'Armed' : (ready ? 'Off' : 'No channel');
    pill.className = `pill ${on ? 'pill-live' : (ready ? 'pill-idle' : 'pill-bad')}`;
  }

  setText('alerts-sent', wd.alerts_sent ?? 0);
  const last = wd.last_alert;
  setText('alerts-last', last ? String(last.kind || '—') : 'none yet');
  setText('alerts-health', wd.healthy === false ? 'degraded' : 'healthy',
    wd.healthy === false ? 'neg' : 'pos');

  // The switch reflects the *switch*, not whether the channel is configured.
  // They are different questions: a channel can be fully configured with the
  // switch off, which is the normal resting state, and conflating them would
  // make the control read off on a bot that is already armed. Mirrors the mode
  // switch in the header so the two read the same way at a glance.
  const toggle = $('btn-alert-toggle');
  if (toggle) {
    toggle.setAttribute('aria-checked', String(armed));
    toggle.classList.toggle('mode-toggle-live', armed);
    toggle.disabled = !!state.busy;
    toggle.title = armed
      ? 'Stop sending alerts. The channel stays configured.'
      : (ready ? 'Start sending alerts on the configured channel.'
        : 'Start sending alerts. The channel has no token or URL yet, so nothing will arrive.');
    setText('alert-toggle-label', armed ? 'ON' : 'OFF');
    setText('alert-toggle-caption', armed ? 'Disable alerts' : 'Enable alerts');
  }

  const btn = $('btn-alerts-test');
  if (btn) btn.disabled = !on;
}

/**
 * Switch alerting on or off.
 *
 * The switch is stored server-side rather than in the browser, so it survives a
 * reload and a container restart, and so the watchdog and the lifecycle notices
 * obey the same decision the operator just made.
 */
async function toggleAlerting() {
  const toggle = $('btn-alert-toggle');
  if (!toggle || toggle.disabled) return;
  // Read the current state from the switch itself, like the mode toggle does,
  // so a click during a poll cannot send the same direction twice.
  const armed = toggle.getAttribute('aria-checked') === 'true';
  const next = !armed;
  toggle.disabled = true;
  try {
    const result = await api('/api/alerts/enabled', {
      method: 'POST',
      body: JSON.stringify({ enabled: next }),
    });
    toast(result.enabled ? 'Alerting enabled' : 'Alerting disabled',
      result.enabled ? 'ok' : 'info');
    await poll();
  } catch (err) {
    toast(`Could not change alerting: ${err.message}`, 'err');
    toggle.disabled = false;
  }
  // Re-enabled by the next renderAlerts, which also settles the label from the
  // server's answer rather than from what was optimistically sent.
}

/**
 * Send one alert on the configured channel.
 *
 * Exists because a channel that is silently misconfigured looks exactly like a
 * quiet market: the only way to tell "nothing happened" from "nothing arrived"
 * is to ask for a message and see whether it lands.
 */
async function sendTestAlert() {
  const btn = $('btn-alerts-test');
  btn.disabled = true;
  try {
    const result = await api('/api/alerts/test', { method: 'POST' });
    toast(result.delivered ? 'Test alert sent' : `Delivery failed: ${result.detail}`,
      result.delivered ? 'ok' : 'err');
    await poll();
  } catch (err) {
    toast(`Test alert failed: ${err.message}`, 'err');
  } finally {
    btn.disabled = false;
  }
}

function exportCsv() {
  const rows = filteredOrders();
  if (!rows.length) {
    toast('Nothing to export', 'info');
    return;
  }
  // strategy_id is exported along with the rest: an order without its book is
  // unattributable once it leaves the page, and the two books' rows are
  // otherwise indistinguishable in a spreadsheet.
  const headers = ['ts', 'strategy_id', 'symbol', 'transaction_type', 'quantity', 'price', 'status', 'mode', 'source', 'exchange_order_id', 'message'];
  const csv = [
    headers.join(','),
    ...rows.map((row) => headers
      .map((key) => `"${String(row[key] ?? '').replace(/"/g, '""')}"`)
      .join(',')),
  ].join('\n');

  const url = URL.createObjectURL(new Blob([csv], { type: 'text/csv;charset=utf-8' }));
  const link = document.createElement('a');
  link.href = url;
  // The book filter is reflected in the name, or two exports taken with
  // different filters look like the same file.
  const book = selectedBook() ? `_${selectedBook()}` : '';
  link.download = `order_book_nifty_future${book}_${new Date().toISOString().slice(0, 16).replace(/[:T]/g, '')}.csv`;
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
  // Warnings get their own tone. They were rendered in the same grey as INFO, so
  // the one level that exists to be noticed was the one you had to hunt for.
  const warn = parsed.level === 'WARNING';
  return `<div class="log-line${severe ? ' has-error' : warn ? ' has-warning' : ''}">
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
  // Count what is actually on screen, not what the whole tail holds: the
  // question the counts answer is "is there something in what I am looking at",
  // and a total including filtered-out lines would contradict the list.
  let errors = 0;
  let warnings = 0;
  for (const line of state.logsRaw) {
    const parsed = parseLogLine(line);
    if (parsed.level === 'ERROR' || parsed.level === 'CRITICAL') errors += 1;
    else if (parsed.level === 'WARNING') warnings += 1;
  }
  const badges = [
    errors ? `${errors} error${errors === 1 ? '' : 's'}` : null,
    warnings ? `${warnings} warning${warnings === 1 ? '' : 's'}` : null,
  ].filter(Boolean).join(' · ');
  setText('logs-caption', `${payload.count} of ${payload.scanned} tail lines${level}${suffix}`
    + (badges ? ` · ${badges}` : ''));
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
  if (tab === 'orders') {
    renderOrders();
    renderSignals();
  }
  if (tab === 'analytics') {
    renderCalendar();
    renderTrades();
    renderAnalyticsTiles(null);
  }
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
  $('btn-stop-all').addEventListener('click', stopAll);
  $('btn-logout').addEventListener('click', signOut);
  $('btn-risk-clear').addEventListener('click', clearRiskBreach);

  $('btn-orders-csv').addEventListener('click', exportCsv);
  $('btn-orders-reconcile').addEventListener('click', reconcileOrders);
  $('book-bands').addEventListener('click', toggleBookPause);
  $('book-bands').addEventListener('click', toggleBookMode);
  $('book-bands').addEventListener('click', squareOffBook);
  $('book-bands').addEventListener('click', manualBookOrder);
  $('btn-alerts-test').addEventListener('click', sendTestAlert);
  $('btn-alert-toggle').addEventListener('click', toggleAlerting);

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
  $('orders-mode').addEventListener('change', () => {
    renderOrders();
    // The pending strip is mode-scoped too, so it has to be refetched rather
    // than re-rendered from the previous mode's rows.
    loadLedger().catch((error) => toast(`Ledger: ${error.message}`, 'err'));
  });
  // The book filter narrows both the order book and the signal trail, so both
  // are repainted: leaving the signal trail showing every book's decisions while
  // the order book showed one book's would make the two panels disagree.
  $('orders-book').addEventListener('change', () => {
    renderOrders();
    renderSignals();
  });

  // Pause polling while the tab is hidden; refresh immediately on return so
  // the dashboard is never showing stale numbers when the user looks at it.
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) {
      clearInterval(state.timer);
      clearInterval(state.logTimer);
    } else {
      poll();
      state.timer = setInterval(poll, POLL_MS);
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
}

document.addEventListener('DOMContentLoaded', init);
