# Safety Checklist

Work through this list in order before touching real capital.

---

## Phase 1 — Unit Tests

- [ ] `pytest` passes with zero failures
- [ ] Grid level count, range, and side assignment verified
- [ ] Risk drawdown and consecutive-loss triggers verified
- [ ] Backtesting metrics calculations verified

---

## Phase 2 — Backtesting

Run at least 3 months of historical data before paper trading:

```bash
python main.py backtest --start 2024-01-01 --end 2024-06-30
```

Accept results only if:
- [ ] Max drawdown < 8% during the test period
- [ ] Win rate > 50%
- [ ] Profit factor > 1.0
- [ ] Grid cycles completed > 20 (shows the range assumption was valid)

If the backtest shows high drawdown, widen `grid.range_percent` or reduce
`capital.order_size_usdt` before proceeding.

---

## Phase 3 — Paper Trading (minimum 7 days)

1. Set `trading.mode: paper` in `config/config.yaml`
2. Confirm `trading.live_confirmation: false`
3. Start the bot:
   ```bash
   docker compose up -d
   docker compose logs -f bot
   ```
4. Verify over 7 days:
   - [ ] Bot starts and restarts cleanly (simulate by stopping/starting container)
   - [ ] Grid orders appear in logs with correct prices
   - [ ] Paper fills trigger the paired order at the next level
   - [ ] Risk pauses trigger correctly when AI returns non-ranging regime
   - [ ] Monitoring endpoint responds: `curl http://localhost:8080/status`
   - [ ] Metrics visible: `curl http://localhost:8080/metrics`
   - [ ] No uncaught exceptions in `logs/bot.log`
   - [ ] Database contains expected rows (`data/trading_bot.db`)
   - [ ] Redis keys are set and expire as expected

---

## Phase 4 — Testnet Live Trading (minimum 3 days)

1. Obtain Binance **testnet** API keys from `testnet.binance.vision`
2. Add keys to `.env`:
   ```
   BINANCE_API_KEY=your_testnet_key
   BINANCE_API_SECRET=your_testnet_secret
   ```
3. Set in `config/config.yaml`:
   ```yaml
   binance:
     testnet: true
   trading:
     mode: live
     live_confirmation: true
   ```
4. Start and verify:
   - [ ] Bot creates listen key successfully (check logs)
   - [ ] Orders appear on testnet exchange UI
   - [ ] Order fills reflected in DB within 1–2 seconds
   - [ ] Paired orders placed correctly after each fill
   - [ ] Emergency stop cancels all orders (test by temporarily setting
         `risk.emergency_price_move_percent: 0.1`)
   - [ ] Bot recovers grid state after a restart

---

## Phase 5 — Live Trading

**Only proceed when all Phase 1–4 checks pass.**

1. Obtain **live** Binance API keys
   - Enable Spot & Margin Trading permission only
   - Disable Withdrawals (critical)
   - Whitelist your Raspberry Pi's IP address
2. Update `.env` with live keys
3. Set in `config/config.yaml`:
   ```yaml
   binance:
     testnet: false
   trading:
     mode: live
     live_confirmation: true
   capital:
     # Start with a fraction of your intended capital
     active_trading_usdt: 50.0
     order_size_usdt: 5.0
   ```
4. Deploy on Raspberry Pi:
   ```bash
   docker compose up -d
   ```
5. First week live checks:
   - [ ] First real order placed and confirmed
   - [ ] Monitor drawdown daily via `/status` endpoint
   - [ ] Verify no duplicate orders after container restart
   - [ ] Check Binance app: open orders match bot's DB
   - [ ] Reserve capital (200 EUR) kept in separate account/wallet

---

## Ongoing Operations

- [ ] Review `logs/bot.log` weekly for warnings and errors
- [ ] Run `curl http://localhost:8080/status` and verify `drawdown_percent`
- [ ] After any config change, restart in `paper` mode for 24h before going live
- [ ] Re-run backtesting quarterly to validate current grid parameters
- [ ] Monitor Raspberry Pi temperature (`vcgencmd measure_temp`) — throttling causes missed ticks
- [ ] Keep Raspberry Pi OS and Docker updated monthly

---

## Emergency Procedures

**If bot stops unexpectedly:**
1. `docker compose logs bot` — read the last 50 lines
2. Check `data/trading_bot.db` grid_levels table for open orders
3. Verify open orders on Binance manually before restarting
4. Restart: `docker compose restart bot`

**If you need to cancel all orders immediately:**
```bash
docker compose exec bot python -c "
import asyncio
from config import Config
from market_data import BinanceRest
cfg = Config()
rest = BinanceRest(cfg.str('binance','api_key'), cfg.str('binance','api_secret'), testnet=False)
async def cancel():
    await rest.open()
    result = await rest.cancel_all_orders('BTCUSDT')
    print(result)
    await rest.close()
asyncio.run(cancel())
"
```

**Hard limits — never change these without re-running all phases:**
- `risk.max_drawdown_percent` — do not raise above 10%
- `capital.reserve_usdt` — never include in active trading
- `binance.api_key` — never grant withdrawal permissions
