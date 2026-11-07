---
title: Testing
---

# Testing

```bash
.venv/bin/python -m pytest tests -q
```

52 tests, no network access required.

## What is pinned

The indicators are the part that must not drift, so they are pinned against
independently transcribed Pine references rather than against the library's own
output.

| Test | Pins |
|---|---|
| `test_supertrend_matches_the_pine_reference_on_a_long_series` | SuperTrend lines and directions, all three instances, 1119 bars |
| `test_psar_matches_the_pine_reference_on_a_long_series` | Wilder PSAR, 1119 bars |
| `test_psar_initializes_direction_from_first_close_change` | PSAR seeding rule |
| `test_psar_follows_wilder_reversal_and_two_bar_clamp` | Reversal and clamp behaviour |
| `test_pandas_ta_indicators_warm_up_from_real_candles` | Warm-up requirement and verdict shape |
| `test_configured_supertrends_drive_the_vote_and_warmup` | Config drives vote count and warm-up |
| `test_supertrend_config_is_parsed_from_the_environment` | `STRATEGY_SUPERTRENDS` parsing |
| `test_supertrend_config_falls_back_when_the_spec_is_unusable` | Bad spec falls back |

Both indicators match their references **exactly** — zero line differences and
zero direction differences across 1119 real GIFT Nifty bars.

## Caching and performance

| Test | Pins |
|---|---|
| `test_profile_is_cached_between_dashboard_polls` | `profile()` TTL, and `max_age=0` bypass |
| `test_margins_are_cached_between_dashboard_polls` | `margins()` TTL |
| `test_quote_cache_is_shared_but_orders_can_bypass_it` | Quote cache + order bypass |
| `test_failed_quote_is_cached_so_a_dead_token_is_not_retried` | Negative caching |
| `test_engine_status_reuses_the_verdict_until_a_new_bar` | Verdict memoisation |
| `test_history_fetch_backs_off_after_a_failure` | Exponential backoff |

## Market calendar

| Test | Pins |
|---|---|
| `test_market_status_blocks_a_trading_holiday` | Holiday gating |
| `test_market_status_reports_unverified_without_a_calendar` | Unknown calendar is not "open" |
| `test_market_status_opens_on_a_normal_session` | Normal session |

## Data quality

| Test | Pins |
|---|---|
| `test_bar_joined_late_is_excluded_from_indicator_history` | Mid-bar join is marked partial and excluded |

## Writing a test

Tests live in `tests/test_regressions.py` and use `monkeypatch` for isolation.
The database is redirected to a temp path:

```python
def use_test_database(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        db, "settings",
        SimpleNamespace(db_path=str(tmp_path / "test.db"), virtual_balance=1_000_000.0),
    )
    db.init_db()
```

For a client test, stub the SDK rather than the network:

```python
client = KiteClient(api_key="key", access_token="token")
client._kite = SimpleNamespace(profile=lambda: {"user_id": "AB1234"})
```

## Next

- [Performance](/performance) — measured resource use
