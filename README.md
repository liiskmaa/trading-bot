# BTC/USDT Grid Trading Bot

Grid trading bot for Binance BTC/USDT. Runs unattended on a Raspberry Pi. Designed for small capital (~500 EUR) with an emphasis on not blowing up.

<a href="https://ko-fi.com/jaanek">
  <img src="https://ko-fi.com/img/githubbutton_sm.svg" alt="Buy Me a Coffee at ko-fi.com" />
</a>

---

## How it works

Grid trading doesn't try to predict where price is going. Instead, it places a ladder of limit orders above and below the current price — buy orders below, sell orders above — and profits from the market oscillating back and forth.

When a buy order fills, the bot immediately places a sell order one step higher. When that sell fills, it places another buy at the original level. Each completed cycle captures the price difference between adjacent levels as profit, minus fees.

Here's what the grid looks like around a $50,000 BTC price with 10 levels and ±5% range:

```
Level 9 — $52,500  → SELL
Level 8 — $51,944  → SELL
Level 7 — $51,389  → SELL
Level 6 — $50,833  → SELL
Level 5 — $50,278  → SELL
─ ─ ─ ─ ─ $50,000  (current price)
Level 4 — $49,722  → BUY
Level 3 — $49,167  → BUY
Level 2 — $48,611  → BUY
Level 1 — $48,056  → BUY
Level 0 — $47,500  → BUY
```

BTC drops to $49,722 → buy fills → sell placed at $50,278.  
BTC recovers to $50,278 → sell fills → buy placed again at $49,722.  
Net profit per cycle: roughly $0.15–0.30 after fees, depending on order size.

All 10 levels run in parallel, 24 hours a day.

**What kills this strategy:** a strong trend in one direction. In a downtrend, buy orders keep filling but sells don't, leaving you holding BTC that's worth less than what you paid. The AI filter watches for this and pauses the bot when conditions turn unfavourable.

---

## Risk rules

Four hard rules that cannot be disabled or overridden. They fire before any grid logic runs.

**Drawdown limit (8%)** — If the portfolio value drops 8% below its peak, the bot cancels all open orders and stops. With 324 USDT active capital that's a ~26 USDT loss ceiling. It will not restart automatically.

**Consecutive losses (3)** — After 3 back-to-back losing cycles, the bot pauses for 20 minutes, then resumes on its own. This is a cooldown, not a full stop.

**Price velocity (7% in 5 minutes)** — If BTC moves more than 7% in either direction within 5 minutes, the bot cancels everything and stops. Flash crashes, exchange glitches, sudden news events — this rule handles all of them.

**AI filter** — A trained machine learning classifier reads the last 60 minutes of candle data on every price tick (rate-limited to once per minute) and labels the current market regime: `ranging`, `trending`, or `high_volatility`. If conditions are unfavourable, the bot pauses grid operations until the regime returns to ranging. It never touches existing orders, just stops placing new ones. The model is trained on your own historical candle data — see [Training the AI filter](#training-the-ai-filter).

All percentages and thresholds are configurable.

---

## Capital split

The config separates capital into two pools that never mix:

- **Active trading** (~300 EUR / 324 USDT) — the only money the bot ever touches
- **Reserve** (~200 EUR / 216 USDT) — kept offline, completely outside the bot's reach

If the worst happens and the drawdown limit fires, you lose at most 8% of the active pool, not the full investment.

---

## Modes

**`dry_run`** — Logs what it would do. No orders placed, no state saved. Useful for verifying your config and watching the grid level calculations.

**`paper`** — Connects to the real Binance WebSocket for live prices, but uses virtual balances. Orders are simulated: when the live price crosses a level, the bot treats it as filled and places the next order. Everything is saved to the database so it survives restarts. No API keys needed.

**`live`** — Real orders, real money. Requires `live_confirmation: true` explicitly set in the config. The bot refuses to start in live mode without this flag — it's not a soft warning.

---

## Before you start

You'll need:

- Python 3.11
- A Binance account — testnet for testing, real account for live
- Redis (optional — the bot runs fine without it, just no caching)

For Binance API keys, whether testnet or live: enable **Spot & Margin Trading** only. No withdrawal permissions. If your Pi has a static IP, whitelist it.

Testnet keys: [testnet.binance.vision](https://testnet.binance.vision)

---

## Installation

```bash
python3.11 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# edit .env and add your API keys
```

Run the tests to make sure everything is in order:

```bash
pytest
```

You should see 45 tests pass.

---

## Configuration

Everything lives in `config/config.yaml`. Keys in `.env` take priority for credentials.

### Trading

```yaml
trading:
  symbol: "BTCUSDT"
  mode: "paper"            # dry_run | paper | live
  live_confirmation: false # must be true to run in live mode
```

### Capital

```yaml
capital:
  total_usdt: 540.0          # informational only, bot doesn't use this
  active_trading_usdt: 324.0 # the only money the bot can deploy
  reserve_usdt: 216.0        # kept outside the bot, never touched
  order_size_usdt: 29.0      # how much USDT to put on each grid level
```

`order_size_usdt` times the number of levels gives you the maximum capital the bot will have deployed at once. With 29 USDT and 10 levels, that's 290 USDT — safely within the 324 USDT active pool.

### Grid

```yaml
grid:
  range_percent: 5.0               # grid spans ±5% around current price
  levels: 10                       # number of price rungs
  rebuild_threshold_percent: 3.0   # rebuild when price drifts this far from center
```

The grid is automatically rebuilt when price moves far enough that most levels are out of range. It cancels all open orders, recalculates around the new price, and places fresh orders.

Wider range → fewer rebuilds, smaller profit per cycle. Tighter range → more cycles but more rebuilds if price trends.

### Risk

```yaml
risk:
  max_drawdown_percent: 8.0          # emergency stop threshold
  consecutive_loss_limit: 3          # losses before cooldown
  cooldown_minutes: 20               # how long the cooldown lasts
  emergency_price_move_percent: 7.0  # velocity trigger
  emergency_price_window_seconds: 300
```

Don't raise `max_drawdown_percent` above 10%. Don't lower `consecutive_loss_limit` below 2 or you'll be pausing after every bad tick.

### AI filter

```yaml
ai_filter:
  enabled: true
  call_interval_seconds: 60   # minimum time between classifier calls
  cache_ttl_seconds: 60       # how long to cache the regime result in Redis
```

The classifier runs locally — no external services needed. It returns `high_volatility` (trading paused) whenever the trained model file doesn't exist yet, so the bot is always safe to start even before training.

To disable the filter entirely:

```yaml
ai_filter:
  enabled: false
```

The bot will run grid logic continuously without any regime check. Only do this if you're comfortable with the grid running through trending markets.

### Binance

```yaml
binance:
  testnet: true   # set false when going live
  api_key: ""     # or set BINANCE_API_KEY in .env
  api_secret: ""  # or set BINANCE_API_SECRET in .env
  order_retry_attempts: 3
  order_retry_delay_seconds: 2.0
```

### Redis

```yaml
redis:
  host: "localhost"
  port: 6379
  db: 0
  password: ""
```

Redis is used as a fast cache for the current price and grid state. If Redis isn't running, the bot logs a warning and continues without it. Nothing breaks, it just reads from the database instead.

### Logging and monitoring

```yaml
logging:
  level: "INFO"   # use DEBUG when troubleshooting
  path: "logs/"

monitoring:
  enabled: true
  host: "0.0.0.0"
  port: 8080
```

---

## Running it

```bash
source venv/bin/activate

# paper mode (default)
python main.py

# explicit mode
python main.py --mode dry_run
python main.py --mode paper
python main.py --mode live   # requires live_confirmation: true in config

# custom config
python main.py --config /path/to/config.yaml

# backtest
python main.py backtest --start 2024-01-01 --end 2024-06-30

# train the AI regime classifier (see Training the AI filter below)
python main.py train-regime
```

On a clean start you'll see the grid being built and the price levels logged. On a restart, it reloads the saved grid from the database and in live mode reconciles the current state against the exchange before resuming.

---

## Backtesting

Run a backtest before paper trading to check that your grid parameters are reasonable for the period you're looking at:

```bash
python main.py backtest --start 2024-01-01 --end 2024-06-30
```

It fetches 1-minute candles from Binance (no API key required), replays them through the grid logic, and prints a summary:

```
=== Backtest Results ===
Trades       : 847 (W:612 L:235)
Win rate     : 72.3%
Net profit   : +42.18 USDT
Avg/trade    : +0.0498 USDT
Profit factor: 2.14
Max drawdown : 4.87%
Grid cycles  : 612
```

What to look for:
- **Max drawdown under 8%** — if it's higher, widen `range_percent` or reduce `order_size_usdt`
- **Win rate above 50%** — anything below means the grid is mostly fighting the trend
- **Profit factor above 1.0** — total wins vs total losses
- **Grid cycles above 20** — if the grid barely cycled, the market was trending the whole time and grid trading wasn't a fit for that period

Run backtest across multiple date ranges, not just a single favourable one.

---

## Monitoring

### Built-in dashboard

Open `http://<host>:8080/` in any browser while the bot is running and you get a live dashboard — no setup, no external services, just the built-in HTTP server.

The dashboard is a single page that polls the bot every 3 seconds. It shows:

- **Price chart** — the last 30 minutes of BTC price, with every grid level overlaid as a horizontal dashed line. Buy levels are green, sell levels are red. Only active (open) orders get a price label so the chart doesn't get cluttered.
- **Equity curve** — portfolio value over the same 30-minute window, as a filled area chart below the status cards.
- **Status cards** — portfolio value, drawdown (with a progress bar relative to the 8% limit), number of open orders, consecutive losses, and cooldown countdown if one is active.
- **Recent trades** — last 50 fills with time, side, price, quantity, and realised P&L. Buy rows show a dash for P&L since the profit is only realised when the paired sell fills.

The header shows bot state (running / paused / cooldown / emergency stop), current price, trading mode, AI regime classification, and how long the bot has been up.

For scripting and external monitoring, the underlying endpoints are also available:

```
GET http://<host>:8080/status   → current bot state as JSON
GET http://<host>:8080/history  → price history, portfolio history, recent trades as JSON
GET http://<host>:8080/metrics  → Prometheus metrics
```

### Grafana dashboard

The Docker Compose setup includes Prometheus and Grafana pre-configured with a trading dashboard. Open `http://<host>:3000` (login: `admin` / `admin`) after starting the stack — the dashboard loads automatically.

Panels included:

| Panel | What it shows |
|---|---|
| Drawdown % | Current drawdown with colour thresholds (green → yellow at −4%, red at −8%) |
| Portfolio Value | Current USDT portfolio value |
| BTC Price | Last known price |
| Market Regime | Green = ranging (trading active), orange = paused |
| Open Orders | Number of live limit orders on the grid |
| Uptime | Time since bot started |
| BTC Price over time | Price chart for the selected time range |
| Drawdown over time | Drawdown curve with the 8% emergency-stop line marked |
| Portfolio value over time | Equity curve |
| Trade fill rate | BUY and SELL fills per minute (from Prometheus counters) |

Prometheus stores 30 days of history in its own volume, so you can look back at past performance even after restarting the bot.

If you're accessing Grafana from another machine on the network, replace `localhost` with the Pi's IP address. The default `admin` password can be changed via `GF_SECURITY_ADMIN_PASSWORD` in `docker-compose.yml`.

---

## Raspberry Pi deployment

The recommended setup is Docker Compose. Four containers start together: Redis, the bot, Prometheus, and Grafana. The AI classifier runs inside the bot container — no additional services needed beyond what's in the compose file.

### Build the image

From your development machine (cross-compiling for ARM):

```bash
docker buildx build --platform linux/arm64 -t gridbot:latest -f docker/Dockerfile .
```

Or build directly on the Pi:

```bash
docker build -t gridbot:latest -f docker/Dockerfile .
```

### Start

```bash
docker compose -f docker/docker-compose.yml up -d
```

### Useful commands

```bash
# follow logs
docker compose logs -f bot

# built-in dashboard (from another machine, use the Pi's IP)
# http://localhost:8080/

# Grafana dashboard — login: admin / admin
# http://localhost:3000/

# Prometheus raw query UI
# http://localhost:9090/

# pull the bot status as JSON
curl http://localhost:8080/status

# graceful stop (state is preserved)
docker compose stop bot

# restart
docker compose restart bot

# full stop
docker compose down
```

Data (database, logs) is stored in named volumes and survives restarts and image rebuilds. Don't run `docker compose down -v` unless you mean to wipe everything.

### Training the AI filter on the Pi

After the bot has been running for at least 8 hours (or after a backtest has populated the database), train the regime classifier:

```bash
# directly on the Pi
python main.py train-regime

# or inside Docker
docker compose exec bot python main.py train-regime
```

Training reads all stored 1-minute candles, labels each point by what actually happened in the following 30 minutes, and fits a gradient-boosted classifier. The model is saved to `data/regime_model.pkl` (inside the `bot_data` volume) and loaded automatically on the next bot start.

The bot does not need to be stopped to train — but you need to restart it (or send it a signal) to reload the new model. The simplest approach is to train, then `docker compose restart bot`.

Retrain whenever you've collected significantly more history, or if the bot seems to be pausing/resuming at the wrong times.

### Auto-start on boot

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

## Training the AI filter

The AI filter uses a gradient-boosted classifier trained on your own candle history. It learns what `ranging`, `trending`, and `high_volatility` look like in terms of price behaviour, then applies that knowledge to incoming ticks.

### How labels are generated

The trainer doesn't need human annotation. For each point in history it looks 30 minutes ahead and labels it automatically:

- **`trending`** — price moved more than 1.5% in a single direction over the next 30 candles
- **`high_volatility`** — average candle range (ATR) over the next 30 candles was more than 2× the dataset average
- **`ranging`** — everything else

### What the model learns from

14 features extracted from the last 60 1-minute candles:

- Price returns at 5, 15, 30 and 60-candle windows
- Average candle range (ATR) at those same windows
- ATR spike ratio (sudden volatility expansion)
- Bollinger Band width (range tightness)
- RSI (momentum)
- Linear regression slope (directional drift)
- Average candle body ratio (trending vs indecisive candles)
- Directional streak count

### When to train

```bash
python main.py train-regime
```

The command prints the label distribution and a validation report. You want all three classes represented — if `trending` or `high_volatility` show zero samples, the dataset doesn't yet cover a diverse enough market period. Run a backtest first (`python main.py backtest --start 2024-01-01 --end 2024-12-31`) to populate the candle database before training.

Minimum: 500 candles (~8 hours). Recommended: several weeks or months covering both quiet and volatile periods.

Retrain periodically — monthly is a reasonable cadence. The old model stays active and safe until you replace it.

---

## Going live

Work through these phases in order. The full checklist with checkboxes is in `SAFETY_CHECKLIST.md`.

**Phase 1 — Tests.** Run `pytest` and make sure all 45 pass.

**Phase 2 — Backtest.** At least 3 months of data, ideally a period that includes both ranging and trending conditions. Check the numbers above.

**Phase 3 — Paper trading (at least 7 days).** Set `mode: paper` and let it run. After a day of data, train the AI classifier (`python main.py train-regime`) and watch that it pauses trading during trending or volatile periods. Verify that grid fills trigger paired orders and that restarting the bot restores the correct state.

**Phase 4 — Testnet live (at least 3 days).** Get testnet keys from [testnet.binance.vision](https://testnet.binance.vision), set `testnet: true`, `mode: live`, `live_confirmation: true`. Verify that orders appear on the testnet UI and that fills land in the database.

**Phase 5 — Live, small.** Start with a fraction of the intended capital — 50 USDT, `order_size_usdt: 5.0`. Run for a week before scaling up.

---

## When things go wrong

**Bot stopped unexpectedly:**

```bash
docker compose logs --tail=100 bot
```

Look for `CRITICAL` or `EMERGENCY` in the output. The database also keeps a full event log:

```bash
sqlite3 data/trading_bot.db \
  "SELECT event_type, message, datetime(timestamp,'unixepoch') \
   FROM system_events ORDER BY timestamp DESC LIMIT 20;"
```

After reading the logs, restart with `docker compose restart bot`. The bot picks up from where it left off using the saved grid state.

**Emergency stop fired:**

Don't restart immediately. Read the stop reason from the logs first. If it was a genuine market event (flash crash, exchange outage), wait for things to settle. If it looks like a false positive, review the relevant threshold in config before restarting.

After an emergency stop, restart in **paper mode** for at least 24 hours before going live again.

**Check open orders:**

```bash
sqlite3 data/trading_bot.db \
  "SELECT level_idx, side, status, price FROM grid_levels ORDER BY level_idx;"
```

---

## Routine maintenance

**Weekly:** Check logs for warnings. Hit `/status` and verify drawdown is reasonable. Check Pi temperature (`vcgencmd measure_temp`) — sustained temperatures above 80°C will eventually cause problems.

**Monthly:** Re-run backtest against the last 3 months to verify the current grid parameters still make sense. Update the Pi OS and Docker images.

**After any config change:** Stop the bot, apply the change, restart in paper mode for at least 24 hours, then switch back to live.

---

## Limits to keep in mind

- `max_drawdown_percent` — don't raise above 10%
- `reserve_usdt` — never include in `active_trading_usdt`
- Binance API key — never enable withdrawal permissions on the key the bot uses

---

**Disclaimer:** This is not financial advice. Cryptocurrency trading involves significant risk of loss. Backtest results do not predict future performance. Only use capital you can afford to lose entirely.
