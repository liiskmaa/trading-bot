# BTC/USDT Trading Bot

Grid trading bot for Binance BTC/USDT with an optional moving-average crossover strategy. Runs unattended on a Raspberry Pi. Designed for small capital (~500 EUR) with an emphasis on not blowing up.

<a href="https://ko-fi.com/jaanek">
  <img src="https://ko-fi.com/img/githubbutton_sm.svg" alt="Buy Me a Coffee at ko-fi.com" />
</a>

---

## Quick start (Raspberry Pi)

**Important: build the Docker image on your Mac/PC, not on the Pi.** Compiling numpy and scikit-learn from source on the Pi will freeze it. The Mac cross-compiles for ARM in a few minutes; the Pi just runs the finished image.

**1. On the Pi — pull the code and add API keys**
```bash
git clone https://github.com/your-repo/trading-bot.git
cd trading-bot
cp .env.example .env
nano .env   # paste your Binance testnet keys, save with Ctrl+X → Y → Enter
```
Get testnet keys at [testnet.binance.vision](https://testnet.binance.vision) — no real money involved.

**2. Check your Pi's architecture**
```bash
dpkg --print-architecture
```
- `arm64` → 64-bit OS (most Raspberry Pi OS installs from 2023+)
- `armhf` → 32-bit OS (older installs)

**3. On your Mac — build for the correct platform**

For 64-bit Pi:
```bash
docker buildx build --platform linux/arm64 -t gridbot:latest -f docker/Dockerfile .
```

For 32-bit Pi:
```bash
docker buildx build --platform linux/arm/v7 -t gridbot:latest -f docker/Dockerfile .
```

**4. On your Mac — transfer the image to the Pi**
```bash
docker save gridbot:latest | gzip | ssh <user>@<pi-ip> 'docker load'
```

**5. On the Pi — start everything**
```bash
docker compose -f docker/docker-compose.yml up -d
```
This starts Redis, the bot, Prometheus, and Grafana all at once.

**6. Check it's working**
```bash
docker compose -f docker/docker-compose.yml logs -f bot
```
You should see `Redis connected`, `Grid built`, `Ticker WS connected`, and `Bot RUNNING`. Press `Ctrl+C` to stop following logs — the bot keeps running.

**Open the dashboards** (replace `<pi-ip>` with your Pi's IP — find it with `hostname -I`):

| What | URL |
|---|---|
| Bot status | `http://<pi-ip>:8088` |
| Grafana | `http://<pi-ip>:3000` — login: admin / admin |

> Port 8088 is used because 8080 is a common conflict (Pi-hole, Home Assistant, etc.). Change `"8088:8080"` in `docker-compose.yml` if needed.

**After ~8 hours — train the AI classifier**
```bash
docker compose -f docker/docker-compose.yml exec bot python main.py train-regime
docker compose -f docker/docker-compose.yml restart bot
```

Training prints a label distribution and validation report. If `trending` shows zero samples, BTC hasn't trended during your collection window — that's fine, retrain after a week when you have more diverse data.

After restart the bot loads the model and logs `ML regime: ranging` (or `high_volatility`) every minute. When it logs `ranging`, it is actively placing grid orders.

The bot runs safely without a trained model — it defaults to pausing trading until training is done.

---

## How it works

Grid trading doesn't try to predict where price is going. Instead, it places a ladder of limit orders above and below the current price — buy orders below, sell orders above — and profits from the market oscillating back and forth.

When a buy order fills, the bot immediately places a sell order one step higher. When that sell fills, it places another buy at the original level. Each completed cycle captures the price difference between adjacent levels as profit, minus fees.

Here's what the grid looks like around a $74,000 BTC price with 20 levels and ±3% range (current defaults):

```
Level 19 — $76,220  → SELL
Level 18 — $75,986  → SELL
...
Level 11 — $74,296  → SELL
─ ─ ─ ─ ─  $74,000  (current price)
Level 10 — $73,778  → BUY
...
Level  1 — $72,236  → BUY
Level  0 — $71,780  → BUY
```

Step between levels: ~$222 (~0.3%). BTC drops $222 → buy fills → sell placed $222 higher. BTC recovers → sell fills → buy placed again. Net profit per cycle: roughly ~0.1% after fees (~$0.03 per $29 order).

All 20 levels run in parallel, 24 hours a day.

**What kills this strategy:** a strong trend in one direction. In a downtrend, buy orders keep filling but sells don't, leaving you holding BTC that's worth less than what you paid. The AI filter watches for this and pauses the bot when conditions turn unfavourable.

**A note on fees:** Binance charges 0.1% per side (0.2% round-trip). With a default grid step of ~0.3% (±3% range / 20 levels), each completed buy→sell cycle earns roughly 0.1% after fees. Tighten the grid below 0.2% step and fees exceed profit — you'd be trading to pay Binance.

---

## Moving-average crossover strategy

An alternative to the grid for trending markets. Instead of many small orders, it takes a single all-in / all-out position based on two moving averages of the daily closing price.

**Signal:**
- Fast MA (20-day) crosses **above** slow MA (50-day) → buy all active capital (golden cross)
- Fast MA crosses **below** slow MA → sell everything (death cross)

**Why this can outperform the grid on BTC:** BTC has historically trended for months at a time. The grid earns small amounts oscillating sideways but gives it all back when price moves directionally. The MA strategy catches the big multi-month moves and sits in USDT during downtrends.

**Trade-offs:**
- Only 2–4 trades per year — long periods of doing nothing
- Needs ~50 days of candle history before it can generate a signal
- Checks every 4 hours; does not run on every price tick

**Enable it** in `config/config.yaml`:
```yaml
ma_strategy:
  enabled: true
```

> **Note:** both strategies share the same capital pool and the same `OrderExecutor`. Running both at once means they compete for the same USDT. Pick one.

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

**`paper`** — Connects to the real Binance WebSocket for live prices, but uses virtual balances. The starting capital is split roughly 50/50 between USDT and BTC so both the buy orders below price and the sell orders above price have capital backing them from the start. Orders are simulated: when the live price crosses a level, the bot treats it as filled and places the next order. Everything is saved to the database so it survives restarts. No API keys needed.

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

All 206 tests should pass.

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
  range_percent: 3.0               # grid spans ±3% around current price
  levels: 20                       # number of price rungs
  rebuild_threshold_percent: 3.0   # rebuild when price drifts this far from center
```

The grid is automatically rebuilt when price moves far enough that most levels are out of range. It cancels all open orders, recalculates around the new price, and places fresh orders.

Wider range → fewer rebuilds, smaller profit per cycle. Tighter range → more cycles but more rebuilds if price trends.

**Step size matters for fees:** step size = (2 × range_percent) / levels. At 0.1% fee per side, you need a step larger than 0.2% to make any profit at all. The defaults give ~0.3% per step, leaving ~0.1% profit after fees.

### MA crossover strategy

```yaml
ma_strategy:
  enabled: false   # set true to use instead of the grid
  fast_period: 20  # days for the fast moving average
  slow_period: 50  # days for the slow moving average
```

Requires ~`slow_period` days of candle history before it can trade. Until then it logs "not enough daily history" and waits. The bot accumulates history automatically while running in paper mode; alternatively populate the database with a backtest first.

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

Open `http://<host>:8088/` in any browser while the bot is running and you get a live dashboard — no setup, no external services, just the built-in HTTP server.

The dashboard is a single page that polls the bot every 3 seconds. It shows:

- **Price chart** — the last 30 minutes of BTC price, with every grid level overlaid as a horizontal dashed line. Buy levels are green, sell levels are red. Only active (open) orders get a price label so the chart doesn't get cluttered.
- **Equity curve** — portfolio value over the same 30-minute window, as a filled area chart below the status cards.
- **Status cards** — portfolio value (USDT + BTC mark-to-market, with a breakdown line showing each separately), drawdown (with a progress bar relative to the 8% limit), number of open orders, consecutive losses, and cooldown countdown if one is active.
- **Recent trades** — last 50 fills with time, side, price, quantity, and realised P&L. Buy rows show a dash for P&L since the profit is only realised when the paired sell fills.

The header shows bot state (running / paused / cooldown / emergency stop), current price, trading mode, AI regime classification, and how long the bot has been up.

For scripting and external monitoring, the underlying endpoints are also available:

```
GET http://<host>:8088/status   → current bot state as JSON
GET http://<host>:8088/history  → price history, portfolio history, recent trades as JSON
GET http://<host>:8088/metrics  → Prometheus metrics
```

### Grafana dashboard

The Docker Compose setup includes Prometheus and Grafana pre-configured with a trading dashboard. Open `http://<host>:3000` (login: `admin` / `admin`) after starting the stack — the dashboard loads automatically.

Panels included:

| Panel | What it shows |
|---|---|
| Drawdown % | Current drawdown with colour thresholds (green → yellow at −4%, red at −8%) |
| Portfolio Value | Total mark-to-market value (USDT + BTC × price) |
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

Four containers start together: Redis, the bot, Prometheus, and Grafana. The AI classifier runs inside the bot container — no additional services needed.

### Build on your Mac, not the Pi

The Pi will freeze if you try to compile numpy/scikit-learn from source. Always build on your Mac and transfer the finished image.

First check your Pi's OS type:
```bash
# on the Pi
dpkg --print-architecture   # arm64 = 64-bit,  armhf = 32-bit
```

Build on the Mac:
```bash
# 64-bit Pi (arm64)
docker buildx build --platform linux/arm64 -t gridbot:latest -f docker/Dockerfile .

# 32-bit Pi (armhf)
docker buildx build --platform linux/arm/v7 -t gridbot:latest -f docker/Dockerfile .
```

Transfer to the Pi:
```bash
docker save gridbot:latest | gzip | ssh <user>@<pi-ip> 'docker load'
```

### Start / update

```bash
# on the Pi — first start or after loading a new image
docker compose -f docker/docker-compose.yml up -d
```

> **`up -d` vs `restart`**: Always use `up -d` after transferring a new image. `docker compose restart` only restarts the existing container process — it does not pick up a new image. `up -d` recreates the container when the image has changed.

### Useful commands

```bash
# follow logs
docker compose -f docker/docker-compose.yml logs -f bot

# built-in dashboard — http://<pi-ip>:8088
curl http://localhost:8088/status

# Grafana — http://<pi-ip>:3000  (admin / admin)
# Prometheus — http://<pi-ip>:9090

# graceful stop (state is preserved)
docker compose -f docker/docker-compose.yml stop bot

# restart after training or config change (image unchanged)
docker compose -f docker/docker-compose.yml restart bot

# deploy a newly built image (recreates the container — use this, not restart)
docker compose -f docker/docker-compose.yml up -d bot

# full stop
docker compose -f docker/docker-compose.yml down
```

Data (database, logs, trained model) is stored in named volumes and survives restarts and image rebuilds. Don't run `docker compose down -v` unless you mean to wipe everything.

### Known issues

**Port conflict on 8088 (or any port)**
The bot monitoring port defaults to 8088. If another service on the Pi uses that port, change the left side of `"8088:8080"` in `docker-compose.yml` to any free port.

**Memory limit warnings**
```
Your kernel does not support memory limit capabilities
```
This appears on Raspberry Pi OS because cgroup memory is disabled by default. It's harmless — the memory limits are just ignored. To enable properly, add `cgroup_enable=memory cgroup_memory=1` to the end of `/boot/firmware/cmdline.txt` (one line, no newline) and reboot.

**Bot PAUSED — HIGH VOLATILITY on first start**
Expected. The AI classifier has no trained model yet and defaults to pausing trading. Run for ~8 hours then train with `docker compose exec bot python main.py train-regime`.

**Upgrading to 64-bit Raspberry Pi OS**
If your Pi is running 32-bit OS (`armhf`) and you want to upgrade to 64-bit for better compatibility, flash a fresh 64-bit Raspberry Pi OS image from [raspberrypi.com/software](https://www.raspberrypi.com/software/). This is a clean install — back up your data first.

### Training the AI filter on the Pi

After the bot has been running for at least 8 hours (or after a backtest has populated the database), train the regime classifier:

```bash
docker compose -f docker/docker-compose.yml exec bot python main.py train-regime
docker compose -f docker/docker-compose.yml restart bot
```

Training reads all stored 1-minute candles, labels each point by what actually happened in the following 30 minutes, and fits a gradient-boosted classifier. The model is saved to `data/regime_model.pkl` (inside the `bot_data` volume) and loaded automatically on the next bot start.

**Reading the training output:**
```
Training on 622 candles...
Label distribution: {'ranging': 482, 'trending': 0, 'high_volatility': 50}
```
- All three labels should ideally have samples. `trending: 0` means the market hasn't trended during your collection window — the bot will still work, it just won't recognise trends yet. Retrain after a week of data.
- After restart you should see `ML regime: ranging` in the logs. The bot will start placing orders.

**Note:** use `restart` here (not `up -d`) — training writes to the data volume, not the image, so no container recreation is needed.

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

The command prints the label distribution and a validation report. You want all three classes represented — if `trending` or `high_volatility` show zero samples, the dataset doesn't yet cover a diverse enough market period. This is normal on first train after 8 hours; retrain after a week.

If you want better initial coverage, run a backtest first to populate the candle database with months of history, then train:
```bash
docker compose -f docker/docker-compose.yml exec bot python main.py backtest --start 2024-01-01 --end 2024-12-31
docker compose -f docker/docker-compose.yml exec bot python main.py train-regime
docker compose -f docker/docker-compose.yml restart bot
```

Minimum: 500 candles (~8 hours). Recommended: several weeks or months covering both quiet and volatile periods.

Retrain periodically — monthly is a reasonable cadence, or after any significant market regime change. The old model stays active and safe until you replace it.

---

## Going live

Work through these phases in order. The full checklist with checkboxes is in `SAFETY_CHECKLIST.md`.

**Phase 1 — Tests.** Run `pytest` and make sure all 206 pass.

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

## Future ideas

These are not yet implemented but are reasonable next steps once you have validated the strategy with real money.

**Dynamic position sizing**
Instead of a fixed `active_trading_usdt`, the bot reads your actual USDT balance from Binance on each grid rebuild and sizes the grid as a percentage of it:

```yaml
capital:
  active_trading_percent: 80  # use 80% of available USDT
  order_size_percent: 8       # each order = 8% of active capital
```

This means profits compound automatically — a larger balance deploys more capital. Losses also shrink the position automatically, which is actually a safety property.

**When to add this:** only after you've validated the strategy is net positive over several weeks of live trading. Compounding amplifies both gains and losses — don't enable it before you trust the strategy.

**Implementation:** ~30 lines. Fetch USDT balance via `BinanceRest` on grid rebuild, calculate `active_capital = balance * active_trading_percent / 100`, pass it into `GridManager` and `RiskManager` instead of the fixed config value.

---

**Multi-symbol support**
Run independent bot instances per symbol (e.g. ETHUSDT alongside BTCUSDT) using separate Docker Compose services with separate config files. Each bot has its own grid, database, and risk manager. No code changes needed — just add a second service to `docker-compose.yml`.

---

## Limits to keep in mind

- `max_drawdown_percent` — don't raise above 10%
- `reserve_usdt` — never include in `active_trading_usdt`
- Binance API key — never enable withdrawal permissions on the key the bot uses

---

**Disclaimer:** This is not financial advice. Cryptocurrency trading involves significant risk of loss. Backtest results do not predict future performance. Only use capital you can afford to lose entirely.
