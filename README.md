# BTC/USDT Grid Trading Bot

A production-grade, autonomous cryptocurrency grid trading bot designed for
**capital preservation** and 24/7 unattended operation on a Raspberry Pi.

Trades BTC/USDT on Binance. Designed for small capital (~500 EUR). Prioritises
survival over profit at every decision point.

---

## Table of Contents

1. [Overview](#overview)
2. [Trading Strategy](#trading-strategy)
3. [Capital Allocation](#capital-allocation)
4. [Grid Engine](#grid-engine)
5. [Risk Management](#risk-management)
6. [AI Market Filter](#ai-market-filter)
7. [Trading Modes](#trading-modes)
8. [Architecture](#architecture)
9. [Project Structure](#project-structure)
10. [Database Schema](#database-schema)
11. [Redis Cache](#redis-cache)
12. [Monitoring](#monitoring)
13. [Backtesting](#backtesting)
14. [Prerequisites](#prerequisites)
15. [Installation](#installation)
16. [Configuration Reference](#configuration-reference)
17. [Running the Bot](#running-the-bot)
18. [Docker Deployment](#docker-deployment)
19. [Raspberry Pi Deployment](#raspberry-pi-deployment)
20. [Rollout Phases](#rollout-phases)
21. [Emergency Procedures](#emergency-procedures)
22. [Ongoing Operations](#ongoing-operations)

---

## Overview

This bot implements a **grid trading strategy**: it places a ladder of limit buy
orders below the current BTC price and limit sell orders above it. Each time a
buy order fills, it automatically places a sell order one level higher. Each
time a sell fills, it places a buy order one level lower. Every completed
buy→sell cycle captures the grid spacing as profit regardless of which
direction the market moves, as long as it keeps oscillating.

**What it is not:** a predictor, a trend follower, or a leveraged speculator.
The bot makes money from price oscillation in a sideways market and stops
trading when the market is trending or volatile.

### Key properties

| Property | Value |
|---|---|
| Exchange | Binance (testnet and live) |
| Trading pair | BTC/USDT |
| Strategy | Grid trading (limit orders only) |
| Target market | Ranging / sideways |
| Capital range | ~500 EUR |
| Deployment target | Raspberry Pi 4/5 (ARM64) |
| Runtime | Python 3.11, asyncio |
| Persistence | SQLite (WAL mode) |
| Cache | Redis (non-critical, graceful fallback) |
| AI filter | Ollama (local LLM, market regime classification only) |

---

## Trading Strategy

### Grid trading in plain language

Imagine BTC is at $50,000. The bot creates a 10-rung price ladder:

```
Level 9 — $52,500  → SELL order
Level 8 — $51,944  → SELL order
Level 7 — $51,389  → SELL order
Level 6 — $50,833  → SELL order
Level 5 — $50,278  → SELL order
─ ─ ─ ─ ─ $50,000  (current price)
Level 4 — $49,722  → BUY order
Level 3 — $49,167  → BUY order
Level 2 — $48,611  → BUY order
Level 1 — $48,056  → BUY order
Level 0 — $47,500  → BUY order
```

When BTC drops to $49,722 and the BUY fills:
→ A SELL is immediately placed at $50,278 (one level up).

When BTC recovers to $50,278 and the SELL fills:
→ A BUY is placed again at $49,722.

One completed cycle earns approximately:
```
profit = (50,278 − 49,722) × quantity − fees
       ≈ $556 × 0.00058 BTC − fees
       ≈ ~$0.27 net per cycle at 0.1% fee rate
```

The bot runs this cycle simultaneously across all 10 levels, 24 hours a day.

### When grid trading works

Grid trading profits from **price oscillation** — the market bouncing up and
down within a range. It performs poorly when price trends strongly in one
direction, because the bot accumulates a losing inventory (too much BTC in a
downtrend, all cash in an uptrend). The AI filter detects trending and
high-volatility conditions and pauses grid operations until conditions improve.

### Grid parameters (defaults)

| Parameter | Value | Meaning |
|---|---|---|
| Grid range | ±5% | Upper bound: price × 1.05, Lower bound: price × 0.95 |
| Grid levels | 10 | Number of price rungs in the ladder |
| Grid spacing | ~1.1% | Distance between adjacent levels |
| Order size | ~29 USDT | Notional value per order (~27 EUR) |
| Rebuild threshold | 3% | Grid is rebuilt when price drifts >3% from reference |

### Profit per cycle (example at BTC = $50,000)

```
Buy price:   $49,722   Quantity: 0.000583 BTC   Cost:   $28.99
Sell price:  $50,278   Revenue:  $29.25 (before fee)
Fee (0.1%):  ~$0.058
Net profit:  ~$0.19 per cycle
```

With 10 levels active and favourable conditions, expect several cycles per day.

---

## Capital Allocation

Total capital of ~500 EUR is split into two non-mixing pools:

| Pool | Amount | Purpose |
|---|---|---|
| Active trading | ~300 EUR (324 USDT) | The only capital the bot is allowed to deploy |
| Reserve | ~200 EUR (216 USDT) | Kept offline; never used by the bot |

The reserve exists to absorb an extreme scenario (bot hits emergency stop,
all positions liquidated at a loss) without wiping out the entire investment.

**The bot only ever sees `active_trading_usdt`. It has no access to the reserve.**

---

## Grid Engine

### Level calculation

```
lower_bound = current_price × (1 − range_percent / 100)
upper_bound = current_price × (1 + range_percent / 100)
step        = (upper_bound − lower_bound) / (num_levels − 1)
level[i]    = lower_bound + i × step
```

Levels below `current_price` → initial side = BUY  
Levels at or above `current_price` → initial side = SELL  
Quantity per level = `order_size_usdt / level_price`

### State machine per level

Each grid level moves through a deterministic state machine:

```
PENDING ──────► BUY_OPEN ──► BUY_FILLED ──► SELL_OPEN ──► SELL_FILLED ─┐
    │                                                                      │
    └──────► SELL_OPEN ──► SELL_FILLED ──► BUY_OPEN ──► BUY_FILLED ─────┘
                                                           (cycle repeats)
```

State | Meaning
`PENDING` | Level exists but no order has been placed yet
`BUY_OPEN` | A live LIMIT BUY order is on the exchange
`BUY_FILLED` | The buy order was fully filled; paired sell is being placed
`SELL_OPEN` | A live LIMIT SELL order is on the exchange
`SELL_FILLED` | The sell order was fully filled; paired buy is being placed
`DISABLED` | Level cancelled (emergency stop or manual intervention)

### Client order ID format

Every order uses a deterministic client order ID:

```
G{SYM}{idx:03d}{side_char}{timestamp_7digits}
e.g. GBTC004S1234567  →  Grid, BTC, level 4, Sell, timestamp suffix
```

This fits Binance's 36-character limit and makes idempotent retry safe — if
an order placement request is retried after a timeout, the exchange rejects
the duplicate rather than creating a second order.

### Grid rebuild

The grid is rebuilt when `current_price` drifts more than
`rebuild_threshold_percent` (default 3%) from the price at which the grid was
originally built. Rebuild sequence:

1. Cancel all open orders on the grid
2. Recalculate levels around the new current price
3. Place fresh buy and sell orders
4. Persist the new grid state to SQLite

### Restart recovery

On every startup:

1. Attempt to load grid levels from the SQLite database
2. If levels exist and mode is `live`, run the reconciler against the exchange
3. The reconciler fetches all open orders from Binance, compares against the DB,
   and fills in any gaps caused by fills or cancellations that happened while
   the bot was offline
4. No order is ever placed twice for the same client order ID

---

## Risk Management

Three independent rules enforce hard stops. They cannot be overridden at
runtime and they override all other logic including the grid engine and AI filter.

### Rule 1 — Maximum drawdown

```
drawdown = (peak_portfolio_value − current_portfolio_value) / peak_portfolio_value × 100
```

If `drawdown >= max_drawdown_percent` (default **8%**):

- State transitions to `EMERGENCY_STOP`
- All open orders are cancelled
- The async stop callback fires immediately
- The bot logs a CRITICAL event and does not restart automatically

With 324 USDT active capital, an 8% drawdown means a loss of ~26 USDT before
the bot shuts itself down.

### Rule 2 — Consecutive losing trades

A losing trade is a completed buy→sell cycle where the sell price was below the
effective buy cost (including fees).

If **3 consecutive losing trades** occur:

- State transitions to `COOLDOWN`
- Grid operations pause for **20 minutes** (configurable)
- The counter resets to zero after one winning trade or when cooldown expires
- After cooldown, the bot resumes automatically

### Rule 3 — Price velocity (emergency)

On every price tick, the bot inspects a rolling 5-minute price window.

If `|current_price − oldest_price_in_window| / oldest_price × 100 >= 7%`:

- State transitions to `EMERGENCY_STOP`
- All open orders are cancelled immediately
- This fires before any grid logic runs on that tick

This protects against flash crashes and exchange outages that cause sudden
price dislocations.

### State transitions summary

```
STARTING  ──► RUNNING
RUNNING   ──► PAUSED         (AI: trending or high_volatility)
PAUSED    ──► RUNNING         (AI returns ranging)
RUNNING   ──► COOLDOWN        (3 consecutive losses)
COOLDOWN  ──► RUNNING         (cooldown period expired)
*         ──► EMERGENCY_STOP  (drawdown limit or price spike)
*         ──► STOPPING        (graceful shutdown signal)
```

---

## AI Market Filter

The AI filter uses a locally running **Ollama** LLM to classify market
conditions before allowing grid orders to be placed.

### Design principles

- The AI **never predicts prices**
- It only classifies the current market regime into one of three states
- It runs locally on the Raspberry Pi — no external API calls, no cost, no latency
- It uses the smallest capable model (`llama3.2:1b`) to minimise CPU/RAM usage
- It is **rate-limited to one call per 60 seconds**
- Results are **cached in Redis** for 60 seconds
- When Ollama is unreachable, the bot falls back to `high_volatility` (most conservative)

### Regime classification

The classifier receives:
- Current BTC price
- 1h, 4h, 24h price change percentages
- 1h volatility (ATR as % of price)

It returns exactly one word:

| Regime | Grid trading | Meaning |
|---|---|---|
| `ranging` | **Allowed** | Price oscillating in a narrow band, no clear trend |
| `trending` | **Paused** | Sustained directional movement (bull or bear) |
| `high_volatility` | **Paused** | Sharp, sudden moves that destabilise grid patterns |

When the regime changes from `trending`/`high_volatility` back to `ranging`,
the bot automatically resumes without human intervention.

### Prompt structure

```
You are a market regime classifier for BTC/USDT.

Market data (last reading):
  Current price     : {price} USDT
  1h price change   : {change_1h}%
  4h price change   : {change_4h}%
  24h price change  : {change_24h}%
  1h volatility ATR : {volatility}%

Classify the current market regime as exactly one of:
  ranging          — price oscillating in a narrow band, no clear trend
  trending         — sustained directional movement (up or down)
  high_volatility  — sharp, sudden moves that destabilise range patterns

Reply with a single word only. No explanation.
```

The `temperature` is set to 0.0 and `num_predict` to 10, making responses
deterministic and fast.

---

## Trading Modes

The bot supports three operating modes, enforced by the `trading.mode` config key.

### `dry_run`

- Logs what orders **would** be placed — nothing else
- No network calls to the exchange (except WebSocket for price data)
- No state written to the database
- Use for: verifying config, inspecting grid level calculations

### `paper`

- Connects to **real** Binance WebSocket for live price data
- Maintains **virtual** USDT and BTC balances
- Simulates order fills: when the live price crosses a grid level, the order
  is marked filled and the paired order is queued
- Full state persistence to SQLite — survives restarts
- Full monitoring endpoint — shows paper P&L in real time
- No API keys required (uses public WebSocket only)
- Use for: testing strategy logic before risking real money

Paper fill simulation logic:
- BUY simulated when `current_price <= order_price`
- SELL simulated when `current_price >= order_price`
- Fees deducted at 0.1% per trade from virtual balance

### `live`

- Real Binance API — real orders, real money
- Requires `trading.live_confirmation: true` explicitly set in config
  (this is a hard gate in code — the bot refuses to start otherwise)
- Connects user data WebSocket stream for real-time fill notifications
- Refreshes the WebSocket listen key every 25 minutes automatically
- All order placements verified with exchange confirmation before marking open

**Never set `live_confirmation: true` without completing all phases in
`SAFETY_CHECKLIST.md`.**

---

## Architecture

### Component overview

```
main.py (CLI entry point)
   │
   └── Bot (core/bot.py) — main orchestrator
         │
         ├── BinanceWebSocket     market_data/binance_ws.py
         │     ├── miniTicker stream → on_price() callback
         │     └── User data stream → on_execution_report() callback (live only)
         │
         ├── BinanceRest          market_data/binance_rest.py
         │     └── REST API: prices, orders, account, klines
         │
         ├── CandleAggregator     market_data/candles.py
         │     └── Ticks → 1-min OHLC → SQLite
         │
         ├── Cache                cache/redis_cache.py
         │     └── Redis: latest price, grid state, AI output, risk flags
         │
         ├── Repository           database/repository.py
         │     └── Async SQLite: orders, trades, grid_levels, candles, events
         │
         ├── RiskManager          risk/manager.py
         │     └── Sync checks on every price tick — no I/O in hot path
         │
         ├── MarketClassifier     ai_filter/classifier.py
         │     └── Ollama HTTP call (rate-limited) → Redis cache
         │
         ├── GridManager          grid_engine/manager.py
         │     ├── Owns level state machine
         │     └── Calls OrderExecutor on state transitions
         │
         ├── OrderExecutor        execution/orders.py
         │     └── Abstracts live / paper / dry_run behind one interface
         │
         ├── Reconciler           execution/reconciler.py
         │     └── Startup sync: exchange state vs local DB
         │
         └── MonitoringServer     monitoring/server.py
               ├── GET /status  → JSON snapshot
               └── GET /metrics → Prometheus text format
```

### Hot path (every price tick)

```
WebSocket tick arrives
  │
  ├─ Cache.set_price()             (Redis, async)
  ├─ CandleAggregator.on_price()  (in-memory OHLC, flush to SQLite on minute boundary)
  ├─ RiskManager.on_price()       (sync, circular buffer velocity check)
  │    └─ If EMERGENCY_STOP → cancel all orders, halt
  │
  ├─ [paper/dry_run] OrderExecutor.simulate_fills()
  │    └─ For each open paper order: check if price crossed level → simulate fill
  │         └─ → GridManager.on_order_filled() → place paired order
  │
  ├─ MarketClassifier.classify()  (async, rate-limited to 60s, Redis cached)
  │    └─ If not "ranging" → BotState.PAUSED
  │
  └─ MonitoringServer.update()    (in-memory snapshot)
```

### Fill event path (live mode)

```
Binance user-data WebSocket
  └── executionReport (status=FILLED)
        ├── Repository.upsert_order()   (update DB status)
        ├── Repository.insert_trade()   (record the trade)
        └── GridManager.on_order_filled()
              ├── Update level status in DB
              └── OrderExecutor.place_buy() or place_sell() at paired level
```

### Background loops (always running)

| Loop | Interval | Purpose |
|---|---|---|
| Heartbeat | 30s | Check cooldown expiry, detect grid drift, log bot state |
| Listen key keepalive | 25 min | Prevent Binance from invalidating the user-data WS stream |
| Monitoring server | continuous | Serve HTTP requests |

---

## Project Structure

```
trading-bot/
│
├── config/
│   ├── config.yaml          # All runtime parameters
│   └── loader.py            # YAML loader with typed accessors + reload support
│
├── core/
│   ├── bot.py               # Main orchestrator, event routing
│   └── state.py             # BotState enum
│
├── market_data/
│   ├── binance_rest.py      # REST API wrapper (signed requests, retries)
│   ├── binance_ws.py        # WebSocket streams (ticker + user data)
│   └── candles.py           # Tick → 1-min OHLC aggregator
│
├── grid_engine/
│   ├── calculator.py        # Pure math: level calc, profit formula, rounding
│   └── manager.py           # Grid state machine + DB/cache persistence
│
├── execution/
│   ├── orders.py            # Order executor: live / paper / dry_run
│   └── reconciler.py        # Startup reconciler: exchange vs DB sync
│
├── risk/
│   └── manager.py           # Drawdown, consecutive loss, velocity rules
│
├── ai_filter/
│   └── classifier.py        # Ollama market regime classifier
│
├── backtesting/
│   ├── engine.py            # Historical candle replay engine
│   └── metrics.py           # Performance metric calculations (pure functions)
│
├── database/
│   ├── schema.py            # SQLite DDL — idempotent, WAL mode
│   └── repository.py        # Async CRUD layer (aiosqlite)
│
├── cache/
│   └── redis_cache.py       # Redis wrapper with graceful offline fallback
│
├── monitoring/
│   └── server.py            # Async HTTP server: /status + /metrics
│
├── tests/
│   ├── test_grid.py         # Grid calculator unit tests (16 tests)
│   ├── test_risk.py         # Risk manager unit tests (14 tests)
│   └── test_backtesting.py  # Backtesting engine unit tests (15 tests)
│
├── docker/
│   ├── Dockerfile           # Multi-stage build, targets linux/arm64
│   └── docker-compose.yml   # Bot + Redis services
│
├── data/                    # SQLite database (created at runtime)
├── logs/                    # Rotating log files (created at runtime)
│
├── main.py                  # CLI entry point
├── requirements.txt
├── pytest.ini
├── .env.example
└── SAFETY_CHECKLIST.md      # Mandatory pre-live rollout checklist
```

---

## Database Schema

SQLite at `data/trading_bot.db`. WAL journal mode. All writes are idempotent.

### `orders`

Tracks every order the bot has placed (real, paper, or simulated).

| Column | Type | Description |
|---|---|---|
| `client_order_id` | TEXT PK | Deterministic ID in format `G{SYM}{idx}{side}{ts}` |
| `exchange_order_id` | TEXT | Binance's internal order ID (null for paper) |
| `symbol` | TEXT | Trading pair, e.g. `BTCUSDT` |
| `side` | TEXT | `BUY` or `SELL` |
| `order_type` | TEXT | Always `LIMIT` |
| `price` | REAL | Limit price |
| `quantity` | REAL | BTC quantity |
| `executed_qty` | REAL | Filled quantity so far |
| `status` | TEXT | `NEW` / `OPEN` / `PARTIALLY_FILLED` / `FILLED` / `CANCELLED` / `PAPER_OPEN` / `PAPER_FILLED` |
| `grid_level_idx` | INTEGER | Which grid level this order belongs to |
| `created_at` | REAL | Unix timestamp |
| `updated_at` | REAL | Unix timestamp |

### `trades`

One row per completed fill event.

| Column | Type | Description |
|---|---|---|
| `exchange_trade_id` | TEXT UNIQUE | Binance trade ID (null for paper) |
| `client_order_id` | TEXT | Links back to `orders` |
| `price` | REAL | Actual fill price |
| `quantity` | REAL | Filled BTC quantity |
| `fee` | REAL | Fee paid |
| `fee_asset` | TEXT | Usually `USDT` or `BNB` |
| `realized_pnl` | REAL | Net profit/loss for this trade |

### `grid_levels`

Persistent grid state. Survives restarts.

| Column | Type | Description |
|---|---|---|
| `level_idx` | INTEGER | Position in the grid (0 = lowest) |
| `price` | REAL | Price at this level |
| `side` | TEXT | Current expected side (`BUY` or `SELL`) |
| `status` | TEXT | `PENDING` / `BUY_OPEN` / `BUY_FILLED` / `SELL_OPEN` / `SELL_FILLED` / `DISABLED` |
| `client_order_id` | TEXT | Active order ID at this level |

Unique constraint on `(symbol, level_idx)` — prevents duplicate levels.

### `balances`

Snapshot of account balances (updated from exchange or paper simulation).

### `candles`

1-minute OHLCV candles aggregated from WebSocket ticks and stored permanently.
Primary key on `(symbol, interval, open_time)`.

### `system_events`

Audit log for all significant bot events: starts, stops, risk triggers, AI
decisions, errors. Severity levels: `INFO`, `WARNING`, `CRITICAL`.

---

## Redis Cache

Redis is used for fast in-process state sharing and to reduce database reads
on the hot price path. **The bot runs correctly without Redis** — every read
has a graceful `None` fallback.

| Key pattern | TTL | Content |
|---|---|---|
| `price:{symbol}` | 10s | Latest BTC price (float) |
| `grid:{symbol}` | 300s | Serialised grid level list |
| `ai_regime:{symbol}` | 60s | Last AI classification string |
| `risk:{flag}` | 3600s | Risk flag values (stop reason, etc.) |

If Redis is unavailable at startup, a warning is logged and all cache
operations become no-ops for the session.

---

## Monitoring

### HTTP endpoints

Start the bot and access:

```
GET http://<host>:8080/status   → JSON bot snapshot
GET http://<host>:8080/metrics  → Prometheus text format
```

### `/status` response

```json
{
  "state": "running",
  "mode": "paper",
  "symbol": "BTCUSDT",
  "last_price": 67432.50,
  "ai_regime": "ranging",
  "drawdown_percent": 0.412,
  "open_orders": 8,
  "portfolio_value": 321.80,
  "consecutive_losses": 0,
  "cooldown_remaining": 0.0,
  "uptime_seconds": 86432
}
```

### Prometheus metrics

| Metric | Type | Description |
|---|---|---|
| `gridbot_trades_total{side}` | Counter | Total filled orders by side |
| `gridbot_drawdown_percent` | Gauge | Current drawdown % |
| `gridbot_open_orders` | Gauge | Active grid orders on exchange |
| `gridbot_uptime_seconds` | Gauge | Bot uptime |
| `gridbot_last_price_usdt` | Gauge | Latest BTC price |
| `gridbot_regime_ranging` | Gauge | 1 if AI says "ranging", 0 otherwise |
| `gridbot_portfolio_usdt` | Gauge | Estimated portfolio value |

---

## Backtesting

Before paper trading, validate the grid parameters against historical data.

```bash
python main.py backtest --start 2024-01-01 --end 2024-06-30
```

The engine:

1. Fetches 1-minute OHLCV candles from Binance (no API key required)
2. Replays each candle: if the candle low crosses a BUY level, simulates a
   fill; if the candle high crosses a SELL level, simulates a fill
3. Tracks USDT and BTC balances, applies 0.1% fees
4. Rebuilds the grid when price drifts >3% from the reference price
5. Halts replay if drawdown exceeds `max_drawdown_percent`

### Output metrics

```
=== Backtest Results ===
Trades       : 847 (W:612 L:235)
Win rate     : 72.3%
Net profit   : +42.18 USDT
Avg/trade    : +0.0498 USDT
Profit factor: 2.14
Max drawdown : 4.87%
Grid cycles  : 612
Price Δ      : +8.3% (42,100 → 45,600)
```

### Interpreting results

| Metric | Accept if |
|---|---|
| Max drawdown | < 8% |
| Win rate | > 50% |
| Profit factor | > 1.0 |
| Grid cycles | > 20 (confirms market was ranging enough) |

If max drawdown is too high, **widen `grid.range_percent`** or **reduce
`capital.order_size_usdt`** before deploying.

---

## Prerequisites

### Local development

- Python 3.11+
- Redis (optional — bot works without it)
- Ollama with `llama3.2:1b` pulled (optional — fallback to `high_volatility` if absent)

### Docker deployment (Raspberry Pi)

- Docker Engine + Docker Compose plugin
- ARM64 platform (Raspberry Pi 4 or 5 recommended)
- Ollama running separately or on the same host

### Binance

- Testnet keys: register at [testnet.binance.vision](https://testnet.binance.vision)
- Live keys: [Binance API Management](https://www.binance.com/en/my/settings/api-management)
  - Permission: **Spot & Margin Trading only**
  - Withdrawal: **disabled** (mandatory)
  - IP whitelist: set to your Raspberry Pi's IP

---

## Installation

### Local (development)

```bash
# Clone / navigate to project
cd trading-bot

# Create virtual environment
python3.11 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Copy and fill in credentials
cp .env.example .env
# Edit .env: add BINANCE_API_KEY and BINANCE_API_SECRET

# Run tests (should show 45 passed)
pytest
```

### Ollama setup (for AI filter)

```bash
# Install Ollama: https://ollama.ai/download
# Then pull the smallest model
ollama pull llama3.2:1b

# Verify it works
ollama run llama3.2:1b "Reply with one word: ranging"
```

If Ollama is not installed, the bot runs safely — it just defaults to
`high_volatility` fallback (trading paused until you configure it).

---

## Configuration Reference

All configuration lives in `config/config.yaml`. The bot supports runtime
reload (sends a log message but does not require restart for most parameters).

```yaml
trading:
  symbol: "BTCUSDT"
  mode: "paper"              # dry_run | paper | live
  live_confirmation: false   # Must be true for live mode

capital:
  total_usdt: 540.0          # ~500 EUR total (informational)
  active_trading_usdt: 324.0 # ~300 EUR — only amount bot can deploy
  reserve_usdt: 216.0        # ~200 EUR — never touched by bot
  order_size_usdt: 29.0      # ~27 EUR per grid level per order

grid:
  range_percent: 5.0         # ±5% price range for the grid
  levels: 10                 # Number of price levels
  spacing_percent: 1.0       # Informational; actual spacing = range*2/(levels-1)
  rebuild_threshold_percent: 3.0  # Rebuild grid when price drifts this far

risk:
  max_drawdown_percent: 8.0         # Hard stop at 8% drawdown
  consecutive_loss_limit: 3         # Pause after N consecutive losses
  cooldown_minutes: 20              # Pause duration
  emergency_price_move_percent: 7.0 # Emergency stop if price moves this fast
  emergency_price_window_seconds: 300

ai_filter:
  enabled: true
  model: "llama3.2:1b"              # Any Ollama model
  base_url: "http://localhost:11434"
  cache_ttl_seconds: 60             # Cache AI result for 60s
  call_interval_seconds: 60         # Don't call Ollama more than once/min
  fallback_regime: "high_volatility" # Safe default when Ollama is down

binance:
  testnet: true              # Set false for live trading
  api_key: ""                # Or use .env BINANCE_API_KEY
  api_secret: ""             # Or use .env BINANCE_API_SECRET
  ws_reconnect_delay_seconds: 5
  ws_max_reconnect_delay_seconds: 60
  order_retry_attempts: 3
  order_retry_delay_seconds: 2.0
  listen_key_refresh_minutes: 30

redis:
  host: "localhost"
  port: 6379
  db: 0
  password: ""
  ttl:
    price_seconds: 10
    grid_state_seconds: 300
    ai_output_seconds: 60
    risk_flags_seconds: 3600

database:
  path: "data/trading_bot.db"

logging:
  level: "INFO"              # DEBUG for verbose output
  path: "logs/"
  max_bytes: 10485760        # 10 MB per log file
  backup_count: 5            # Keep 5 rotated files = up to 50 MB

monitoring:
  enabled: true
  host: "0.0.0.0"
  port: 8080
```

### Environment variables (.env)

```bash
BINANCE_API_KEY=your_api_key_here
BINANCE_API_SECRET=your_api_secret_here
REDIS_PASSWORD=                      # Leave empty if no Redis auth
```

Values in `.env` override empty strings in `config.yaml` when the Docker
Compose `env_file` directive loads them.

---

## Running the Bot

### Direct (no Docker)

```bash
source venv/bin/activate

# Paper trading (safe default, no keys needed for public WebSocket)
python main.py

# Specify mode explicitly
python main.py --mode paper
python main.py --mode dry_run

# Custom config file
python main.py --config /path/to/my_config.yaml

# Backtesting
python main.py backtest --start 2024-01-01 --end 2024-06-30

# Help
python main.py --help
python main.py backtest --help
```

### Expected startup log

```
2025-04-29T10:00:00 INFO     [core.bot] Bot starting — mode=paper symbol=BTCUSDT
2025-04-29T10:00:00 INFO     [database.repository] Database opened: data/trading_bot.db
2025-04-29T10:00:00 INFO     [cache.redis_cache] Redis connected at localhost:6379
2025-04-29T10:00:00 INFO     [market_data.binance_ws] Ticker WS connected: wss://...
2025-04-29T10:00:01 INFO     [grid_engine.manager] Building grid around 67432.50 USDT
2025-04-29T10:00:01 INFO     [grid_engine.manager] Grid built: 10 levels, lower=64060.88 upper=70804.13
2025-04-29T10:00:01 INFO     [core.bot] Bot RUNNING
2025-04-29T10:00:01 INFO     [monitoring.server] Monitoring server on 0.0.0.0:8080
```

---

## Docker Deployment

### Build

```bash
# Build ARM64 image (for Raspberry Pi) from an x86 machine
docker buildx build \
  --platform linux/arm64 \
  -t gridbot:latest \
  -f docker/Dockerfile \
  .

# Or build natively on the Raspberry Pi
docker build -t gridbot:latest -f docker/Dockerfile .
```

### Start all services

```bash
docker compose -f docker/docker-compose.yml up -d
```

This starts:
- **redis** — Redis 7 Alpine with persistence (`save 60 1`), 64 MB RAM limit
- **bot** — the trading bot, depends on Redis being healthy

### Useful commands

```bash
# Follow live logs
docker compose logs -f bot

# Check bot status
curl http://localhost:8080/status | python3 -m json.tool

# Check Prometheus metrics
curl http://localhost:8080/metrics

# Run a backtest inside the container
docker compose exec bot python main.py backtest --start 2024-01-01 --end 2024-06-30

# Graceful stop (preserves all state)
docker compose stop bot

# Full restart
docker compose restart bot

# Stop everything
docker compose down

# Stop and remove volumes (DESTRUCTIVE — deletes all data)
docker compose down -v
```

### Volume layout

| Volume | Mount | Contents |
|---|---|---|
| `bot_data` | `/app/data` | SQLite database (`trading_bot.db`) |
| `bot_logs` | `/app/logs` | Rotating log files |
| `redis_data` | `/data` | Redis RDB snapshot |
| `../config` | `/app/config` (read-only) | Config YAML |

Data is preserved across restarts and container rebuilds.

---

## Raspberry Pi Deployment

### Hardware recommendations

| Component | Minimum | Recommended |
|---|---|---|
| Model | Raspberry Pi 4 2GB | Raspberry Pi 4 4GB or Pi 5 |
| Storage | 16GB SD card | 32GB+ A2-rated SD or USB SSD |
| Cooling | Passive heatsink | Active fan (bot runs 24/7) |
| Network | WiFi | Ethernet (more reliable) |
| Power | Official PSU | Official PSU with UPS HAT |

### First-time setup

```bash
# On the Pi — install Docker
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
# Log out and back in

# Clone the repo
git clone <your-repo-url> ~/trading-bot
cd ~/trading-bot

# Copy and configure
cp .env.example .env
nano .env   # add API keys

# Pull and start
docker compose -f docker/docker-compose.yml pull
docker compose -f docker/docker-compose.yml up -d

# Verify
docker compose logs -f bot
curl http://localhost:8080/status
```

### Install Ollama on the Pi

```bash
curl -fsSL https://ollama.ai/install.sh | sh
ollama pull llama3.2:1b

# Test
ollama run llama3.2:1b "Is BTC trending or ranging today? One word."
```

Update `config/config.yaml` to point at the local Ollama:

```yaml
ai_filter:
  base_url: "http://localhost:11434"
```

### Monitor Pi health

```bash
# CPU temperature (throttling causes missed price ticks)
vcgencmd measure_temp

# Memory and CPU
free -h && top -bn1 | head -5

# Docker resource usage
docker stats
```

If CPU temperature exceeds 80°C regularly, add a fan or reduce Ollama model
size. The bot itself uses minimal CPU — the model inference is the main load.

### Enable auto-start on boot

```bash
# Add to crontab
crontab -e
# Add this line:
@reboot cd /home/pi/trading-bot && docker compose -f docker/docker-compose.yml up -d
```

Or use systemd (more robust):

```bash
sudo nano /etc/systemd/system/gridbot.service
```

```ini
[Unit]
Description=BTC Grid Trading Bot
After=docker.service
Requires=docker.service

[Service]
WorkingDirectory=/home/pi/trading-bot
ExecStart=docker compose -f docker/docker-compose.yml up
ExecStop=docker compose -f docker/docker-compose.yml down
Restart=on-failure
RestartSec=30s
User=pi

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable gridbot
sudo systemctl start gridbot
```

---

## Rollout Phases

Work through these phases in order. Do not skip any phase. The full checklist
with tick boxes is in `SAFETY_CHECKLIST.md`.

### Phase 1 — Unit tests

```bash
pytest
```

All 45 tests must pass. They cover:
- Grid level count, price range, side assignment, quantity calculation
- Risk drawdown trigger, consecutive loss counter, cooldown expiry
- Price velocity emergency trigger
- Backtesting equity curve, max drawdown guard, metric calculations

### Phase 2 — Backtesting (minimum 3 months)

```bash
python main.py backtest --start 2024-01-01 --end 2024-06-30
```

Accept only if: max drawdown < 8%, win rate > 50%, profit factor > 1.0,
grid cycles > 20. Tune grid parameters if needed.

### Phase 3 — Paper trading (minimum 7 days)

```yaml
trading:
  mode: paper
  live_confirmation: false
```

Verify: grid orders appear in logs, fills trigger paired orders, risk pauses
fire correctly, bot restarts cleanly, monitoring endpoints respond.

### Phase 4 — Testnet live (minimum 3 days)

```yaml
binance:
  testnet: true
trading:
  mode: live
  live_confirmation: true
```

Use keys from [testnet.binance.vision](https://testnet.binance.vision).
Verify: orders appear on testnet UI, fills reflected in DB within 2 seconds,
grid recovers correctly after restart.

### Phase 5 — Live trading

```yaml
binance:
  testnet: false
trading:
  mode: live
  live_confirmation: true
capital:
  active_trading_usdt: 50.0    # Start small
  order_size_usdt: 5.0
```

Start with a small fraction (50 USDT) for the first week. Scale up only after
verifying correct behaviour with real orders.

---

## Emergency Procedures

### Bot stopped unexpectedly

```bash
# Read the last log entries
docker compose logs --tail=100 bot

# Check what's in the database
sqlite3 data/trading_bot.db "SELECT * FROM system_events ORDER BY timestamp DESC LIMIT 20;"

# Check open orders in DB vs Binance
sqlite3 data/trading_bot.db "SELECT client_order_id, status, side, price FROM orders WHERE status IN ('OPEN','PAPER_OPEN');"

# Restart (grid state reloaded from DB, reconciler runs on live mode)
docker compose restart bot
```

### Cancel all orders immediately

```bash
docker compose exec bot python -c "
import asyncio
from config import Config
from market_data import BinanceRest
cfg = Config()
rest = BinanceRest(
    cfg.str('binance','api_key'),
    cfg.str('binance','api_secret'),
    testnet=cfg.bool('binance','testnet')
)
async def run():
    await rest.open()
    result = await rest.cancel_all_orders('BTCUSDT')
    print('Cancelled:', result)
    await rest.close()
asyncio.run(run())
"
```

### Emergency stop was triggered — what now?

1. Check `logs/bot.log` for the CRITICAL line explaining the reason
2. Verify all orders are cancelled on the Binance app
3. Do **not** restart immediately — understand why the stop fired
4. If it was a false positive (e.g., brief network outage causing price spike),
   reduce `emergency_price_move_percent` before restarting
5. If it was a genuine market event, wait for conditions to normalise
6. Re-run backtesting against the recent period to validate parameters
7. Restart in **paper mode** for at least 24h before going live again

### Hard limits — never change without re-running all phases

- `risk.max_drawdown_percent` — do not raise above 10%
- `capital.reserve_usdt` — never include in `active_trading_usdt`
- `binance.api_key` — never grant withdrawal permissions to the trading key

---

## Ongoing Operations

### Weekly

- `docker compose logs bot | grep -E "WARNING|CRITICAL"` — scan for anomalies
- `curl http://localhost:8080/status` — verify drawdown and state
- `vcgencmd measure_temp` — verify Pi temperature

### Monthly

- Re-run backtest against last 3 months to validate current grid parameters
- `sudo apt update && sudo apt upgrade` — update Raspberry Pi OS
- `docker compose pull && docker compose up -d` — update base images

### After any config change

1. Stop the bot: `docker compose stop bot`
2. Apply the change to `config/config.yaml`
3. Restart in paper mode for at least 24h: set `mode: paper`, then restart
4. Only switch back to live after verifying correct behaviour

### Inspect the database

```bash
# All open orders
sqlite3 data/trading_bot.db \
  "SELECT level_idx, side, status, price FROM grid_levels ORDER BY level_idx;"

# Recent trades
sqlite3 data/trading_bot.db \
  "SELECT side, price, quantity, realized_pnl, datetime(timestamp,'unixepoch') 
   FROM trades ORDER BY timestamp DESC LIMIT 20;"

# System events (errors, risk stops)
sqlite3 data/trading_bot.db \
  "SELECT event_type, severity, message, datetime(timestamp,'unixepoch')
   FROM system_events WHERE severity != 'INFO' ORDER BY timestamp DESC LIMIT 20;"

# P&L summary
sqlite3 data/trading_bot.db \
  "SELECT SUM(realized_pnl), COUNT(*) FROM trades WHERE side='SELL';"
```

---

## Development Notes

### Running a single test

```bash
pytest tests/test_grid.py -v
pytest tests/test_risk.py::TestDrawdownRule -v
pytest -k "test_trigger_for_large_move" -v
```

### Adjusting log verbosity

```yaml
logging:
  level: "DEBUG"
```

Or per-module from the shell:

```bash
python main.py --mode dry_run 2>&1 | grep "grid_engine"
```

### Adding a new grid parameter

1. Add the key to `config/config.yaml`
2. Read it via `cfg.float("grid", "new_param")` in `main.py:build_bot()`
3. Pass it into `GridManager.__init__()`
4. Add a unit test in `tests/test_grid.py`
5. Update this README

### Switching to PostgreSQL

The `Repository` class uses raw SQL through `aiosqlite`. To switch to
PostgreSQL, replace `aiosqlite` with `asyncpg` and update:
- `database/schema.py` — adjust `SERIAL`/`AUTOINCREMENT` syntax
- `database/repository.py` — use `asyncpg` connection pool
- `docker/docker-compose.yml` — add a `postgres` service

---

## Disclaimer

This software is provided for educational purposes. Cryptocurrency trading
carries significant financial risk. Past backtest performance does not
guarantee future results. The authors are not responsible for any financial
losses incurred through the use of this software. Never trade with capital
you cannot afford to lose.
