import React from 'react';
import Link from '@docusaurus/Link';
import Layout from '@theme/Layout';
import styles from './index.module.css';

type Feature = {
  icon: string;
  title: string;
  body: string;
};

const FEATURES: Feature[] = [
  {
    icon: '📐',
    title: 'Exact on 1119 real bars',
    body: 'SuperTrend and PSAR match their Pine references with zero line differences and zero direction differences across 1119 real GIFT Nifty bars.',
  },
  {
    icon: '🔍',
    title: 'PSAR computed locally',
    body: "The library's psar diverged on ~30% of bars, so it is implemented here against the canonical Wilder definition instead.",
  },
  {
    icon: '🧩',
    title: 'Configurable SuperTrends',
    body: 'Three instances as atr_length:multiplier pairs. The warm-up requirement follows the config automatically.',
  },
  {
    icon: '📊',
    title: 'Every verdict logged',
    body: 'One line per completed candle with the OHLC input and every indicator value — enough to reproduce a decision offline.',
  },
  {
    icon: '🛡️',
    title: 'Refusals are recorded',
    body: 'Blocked signals are stored with their reason, so a quiet bot is explainable rather than mysterious.',
  },
  {
    icon: '⚡',
    title: '1% CPU',
    body: 'Profiled and optimised from 8.91% to 1.00% by caching broker reads and memoising verdicts per bar.',
  },
];

const STATS = [
  {value: '2 of 3', label: 'SuperTrend agreement'},
  {value: '14 bars', label: 'Warm-up required'},
  {value: '1.00%', label: 'Average CPU'},
  {value: '52', label: 'Tests passing'},
];

const DOC_LINKS = [
  {to: '/setup', title: 'Setup', body: 'Install, Kite credentials, first run'},
  {to: '/architecture', title: 'Architecture', body: 'How the pieces fit together'},
  {to: '/strategy', title: 'Strategy', body: 'The entry rules in detail'},
  {to: '/execution', title: 'Execution', body: 'Order path and every safety gate'},
  {to: '/database', title: 'Database', body: 'What is stored, and why'},
  {to: '/database/schema', title: 'Schema reference', body: 'Every table, column and index'},
  {to: '/configuration', title: 'Configuration', body: 'Every environment variable'},
  {to: '/api', title: 'API', body: 'The JSON endpoints'},
  {to: '/performance', title: 'Performance', body: 'Measured resource use'},
];

const STRATEGY_CODE = `buyST   = supertrendBullCount >= 2      # 2 of 3 SuperTrends bullish
buyPSAR = close > psar                  # price above Parabolic SAR

longCondition  = buyST and buyPSAR
shortCondition = sellST and sellPSAR

# Fires once per transition, mirroring the Pine not longCondition[1] guard
buySignal = barstate.isconfirmed and longCondition and not longCondition[1]`;

const LOG_CODE = `Indicator values source=NSEIX:GIFT NIFTY
  interval_seconds=300
  candle_at=2026-11-06T23:25:00+05:30
  history_bars=1166
  ohlc={"close": 24976.0, "high": 24977.5,
        "low": 24961.5, "open": 24963.0}
  indicators={"psar": 24926.5,
              "st_11_1": 24985.26,
              "st_12_2": 25014.33,
              "st_13_3": 25043.42,
              "supertrend_bull": 0,
              "supertrend_bear": 3}
  signal=NEUTRAL`;

function CodeBlock({label, code}: {label: string; code: string}) {
  return (
    <div className={styles.code}>
      <div className={styles.codeBar}>
        <span className={styles.dots}>
          <i />
          <i />
          <i />
        </span>
        <span>{label}</span>
      </div>
      <pre>
        <code>{code}</code>
      </pre>
    </div>
  );
}

export default function Home(): React.ReactElement {
  return (
    <Layout
      title="Algorithmic execution for GIFT Nifty"
      description="Belaku reads GIFT Nifty, evaluates a triple-SuperTrend + Parabolic SAR strategy on completed candles, and trades the front-month NFO Nifty future.">
      <main className={styles.page}>
        <section className={styles.hero}>
          <div className={styles.wrap}>
            <div className={styles.eyebrow}>
              <span className={styles.dot} /> GIFT Nifty → Nifty Futures · PAPER and LIVE
            </div>
            <h1 className={styles.h1}>
              Algorithmic execution for
              <br />
              <span className={styles.grad}>GIFT Nifty</span>
            </h1>
            <p className={styles.lede}>
              Belaku reads GIFT Nifty, evaluates a triple-SuperTrend + Parabolic SAR
              strategy on completed candles, and trades the front-month NFO Nifty future.
              Indicators are computed locally and verified against the Pine reference — no
              TradingView in the loop.
            </p>
            <div className={styles.cta}>
              <Link className={`${styles.btn} ${styles.btnPrimary}`} to="/setup">
                Get started
              </Link>
              <Link className={`${styles.btn} ${styles.btnGhost}`} to="/architecture">
                Architecture
              </Link>
            </div>
            <div className={styles.stats}>
              {STATS.map((s) => (
                <div className={styles.stat} key={s.label}>
                  <b>{s.value}</b>
                  <span>{s.label}</span>
                </div>
              ))}
            </div>
          </div>
        </section>

        <section className={styles.section}>
          <div className={styles.wrap}>
            <h2 className={styles.h2}>Signal source ≠ execution venue</h2>
            <p className={styles.sub}>
              GIFT Nifty trades ~20 hours a day and is the standard pre-market proxy for
              Nifty 50 — but it is an index, and cannot be traded. So Belaku reads signals
              from GIFT Nifty and places orders on the NFO Nifty future. Everything else
              follows from that split.
            </p>
            <CodeBlock label="bot/strategy.py" code={STRATEGY_CODE} />
          </div>
        </section>

        <section className={styles.section}>
          <div className={styles.wrap}>
            <h2 className={styles.h2}>Built to be verified, not trusted</h2>
            <p className={styles.sub}>
              The indicators are the part that must not drift, so they are pinned against
              independently transcribed Pine references rather than against a library's own
              output.
            </p>
            <div className={styles.grid}>
              {FEATURES.map((f) => (
                <div className={styles.card} key={f.title}>
                  <div className={styles.icon}>{f.icon}</div>
                  <h3>{f.title}</h3>
                  <p>{f.body}</p>
                </div>
              ))}
            </div>
          </div>
        </section>

        <section className={styles.section}>
          <div className={styles.wrap}>
            <div className={styles.split}>
              <div>
                <h3 className={styles.h3}>Two clocks, one bot</h3>
                <p className={styles.splitP}>
                  GIFT Nifty trades ~20 hours; NSE F&amp;O trades 6 h 15 m. A signal that
                  fires at 02:00 has no venue until 09:15.
                </p>
                <p className={styles.splitP}>
                  Belaku records it as <strong>deferred</strong> and drops it. It is not
                  queued for the open — a signal computed on a 02:00 bar is not a signal at
                  09:15.
                </p>
                <ul className={styles.list}>
                  <li>GIFT session: evaluate on every closed bar</li>
                  <li>NSE F&amp;O session: LIVE orders may be placed</li>
                  <li>Outside hours: PAPER simulates, LIVE defers</li>
                  <li>Deferred signals are never auto-executed</li>
                </ul>
              </div>
              <CodeBlock label="engine log" code={LOG_CODE} />
            </div>
          </div>
        </section>

        <section className={styles.section}>
          <div className={styles.wrap}>
            <h2 className={styles.h2}>Documentation</h2>
            <p className={styles.sub}>Everything from first run to the database schema.</p>
            <div className={styles.links}>
              {DOC_LINKS.map((l) => (
                <Link className={styles.link} to={l.to} key={l.to}>
                  <strong>{l.title}</strong>
                  <span>{l.body}</span>
                </Link>
              ))}
            </div>
          </div>
        </section>

        <section className={styles.section}>
          <div className={styles.wrap}>
            <div className={styles.warn}>
              <span className={styles.warnIcon}>⚠️</span>
              <p>
                <strong>No stop-loss, no profit target.</strong> Exits are an opposite
                signal or a manual square-off. Size positions accordingly — this is a
                property of the strategy, not a missing feature. The dashboard is also
                unauthenticated: keep it on loopback or behind an authenticating proxy.
              </p>
            </div>
          </div>
        </section>
      </main>
    </Layout>
  );
}
