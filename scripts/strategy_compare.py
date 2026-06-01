"""
Strategy comparison harness: grid-only vs MA-only vs regime-routed combo.

Runs all three strategies over the SAME mainnet 1-minute candle set so the
results are apples-to-apples. Grid-only reuses the production BacktestEngine
replay; MA-only and combo are implemented here to mirror the live strategies.

Combo routing (per the live design, made tradeable):
  - regime == ranging          -> run the grid
  - regime == trending         -> follow the daily 20/50 MA direction
                                  (all-in BTC if fast>slow, else cash)
  - regime == high_volatility  -> flat (cash)
Regime is re-classified every 60 one-minute candles using the trained
GradientBoosting model on the trailing 60-candle window (same as live).

Usage:
  ./venv/bin/python scripts/strategy_compare.py --start 2025-12-01 --end 2026-05-31
"""

import argparse
import asyncio
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from market_data.binance_rest import BinanceRest          # noqa: E402
from backtesting.engine import BacktestEngine             # noqa: E402
from backtesting.metrics import _max_drawdown             # noqa: E402
from ai_filter.features import compute_features, FEATURE_WINDOW  # noqa: E402
from ai_filter.trainer import load_model                  # noqa: E402

# Mirror config/config.yaml (static wide grid)
FEE = 0.001
ACTIVE_CAPITAL = 324.0
ORDER_SIZE = 16.0
RANGE_PCT = 30.0
LEVELS = 20
REBUILD_THRESHOLD = 999.0     # 999 = never re-center (static grid)
FAST, SLOW = 20, 50           # MA daily periods
SYMBOL = "BTCUSDT"
CLASSIFY_EVERY = 60           # re-classify regime every N 1m candles


def _ms(date_str: str) -> int:
    dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _day(open_time_ms: int):
    return datetime.fromtimestamp(open_time_ms / 1000, tz=timezone.utc).date()


# --------------------------------------------------------------------------- #
# Data
# --------------------------------------------------------------------------- #

async def fetch_klines(rest, symbol, interval, start_ms, end_ms):
    out, cur = [], start_ms
    while cur < end_ms:
        raw = await rest._get(
            "/api/v3/klines",
            {"symbol": symbol, "interval": interval,
             "startTime": cur, "endTime": end_ms, "limit": 1000},
        )
        if not raw:
            break
        for k in raw:
            out.append({
                "open_time": k[0], "open": float(k[1]), "high": float(k[2]),
                "low": float(k[3]), "close": float(k[4]), "volume": float(k[5]),
            })
        nxt = raw[-1][0] + (60_000 if interval == "1m" else 86_400_000)
        cur = nxt
        if len(raw) < 1000:
            break
    return out


# --------------------------------------------------------------------------- #
# Daily MA crossover -> desired position per day (no lookahead)
# --------------------------------------------------------------------------- #

def ma_targets(daily):
    """Return {date: 'IN'|'OUT'} using closes strictly before each day."""
    closes = [d["close"] for d in daily]
    pos, target = "OUT", {}
    for i, d in enumerate(daily):
        if i >= SLOW:
            fast = sum(closes[i - FAST:i]) / FAST
            slow = sum(closes[i - SLOW:i]) / SLOW
            if fast > slow and pos == "OUT":
                pos = "IN"
            elif fast < slow and pos == "IN":
                pos = "OUT"
        target[d["open_time"] and _day(d["open_time"])] = pos
    return target


# --------------------------------------------------------------------------- #
# MA-only simulation (mark-to-market on 1m closes)
# --------------------------------------------------------------------------- #

def sim_ma(candles, targets, max_dd=1e9):
    """max_dd: emergency drawdown stop (same semantics as grid/combo — liquidate
    and halt). Default 1e9 = no stop (the original, flattered behaviour)."""
    usdt, btc, entry = ACTIVE_CAPITAL, 0.0, 0.0
    trades, equity = [], []
    cur_day = None
    peak = ACTIVE_CAPITAL
    for c in candles:
        d = _day(c["open_time"])
        if d != cur_day:
            cur_day = d
            want = targets.get(d, "OUT")
            price = c["open"]
            if want == "IN" and btc == 0.0:
                btc = usdt * (1 - FEE) / price
                entry = price
                usdt = 0.0
                trades.append({"side": "BUY", "price": price, "realized_pnl": 0.0})
            elif want == "OUT" and btc > 0.0:
                proceeds = btc * price * (1 - FEE)
                pnl = proceeds - (btc * entry)   # vs entry cost (fee already in entry qty)
                usdt = proceeds
                trades.append({"side": "SELL", "price": price, "realized_pnl": pnl})
                btc = 0.0
        val = usdt + btc * c["close"]
        equity.append(val)
        peak = max(peak, val)
        if (peak - val) / peak * 100 >= max_dd:   # emergency stop: liquidate, halt
            if btc > 0:
                usdt += btc * c["close"] * (1 - FEE)
                btc = 0.0
            break
    return trades, equity


# --------------------------------------------------------------------------- #
# Grid stepper (mirrors BacktestEngine._replay fill logic) for the combo
# --------------------------------------------------------------------------- #

from grid_engine.calculator import compute_levels, round_price, round_qty  # noqa: E402


class GridLeg:
    """Self-contained grid that shares one cash/BTC pool with the combo."""
    def __init__(self):
        self.levels = []
        self.ref = 0.0

    def rebuild(self, price):
        self.levels = compute_levels(price, RANGE_PCT, LEVELS, ORDER_SIZE)
        for lv in self.levels:
            lv.price = round_price(lv.price, 2)
            lv.quantity = round_qty(lv.quantity, 5)
        self.ref = price

    def step(self, candle, pool):
        """pool is a dict {'usdt':, 'btc':}; mutates it; returns list of trades."""
        close, low, high = candle["close"], candle["low"], candle["high"]
        if not self.levels or abs(close - self.ref) / self.ref * 100 > REBUILD_THRESHOLD:
            self.rebuild(close)
        trades = []
        for lv in self.levels:
            if lv.status == "PENDING":
                lv.status = "BUY_OPEN" if lv.side == "BUY" else "SELL_OPEN"
            if lv.status == "BUY_OPEN" and low <= lv.price:
                cost = lv.price * lv.quantity * (1 + FEE)
                if pool["usdt"] >= cost:
                    pool["usdt"] -= cost
                    pool["btc"] += lv.quantity
                    lv.status = "BUY_FILLED"
                    trades.append({"side": "BUY", "price": lv.price, "realized_pnl": 0.0})
            elif lv.status == "SELL_OPEN" and high >= lv.price:
                if pool["btc"] >= lv.quantity:
                    pool["btc"] -= lv.quantity
                    pool["usdt"] += lv.price * lv.quantity * (1 - FEE)
                    step = self.ref * (RANGE_PCT / 100) * 2 / (LEVELS - 1)
                    buy_px = lv.price - step
                    pnl = (lv.price * (1 - FEE) - buy_px * (1 + FEE)) * lv.quantity
                    lv.status = "SELL_FILLED"
                    trades.append({"side": "SELL", "price": lv.price, "realized_pnl": pnl})
        for lv in self.levels:
            if lv.status == "BUY_FILLED":
                nxt = lv.idx + 1
                if nxt < len(self.levels) and self.levels[nxt].status == "PENDING":
                    self.levels[nxt].status = "SELL_OPEN"
                lv.status = "PENDING"
            elif lv.status == "SELL_FILLED":
                prv = lv.idx - 1
                if prv >= 0 and self.levels[prv].status == "PENDING":
                    self.levels[prv].status = "BUY_OPEN"
                lv.status = "PENDING"
        return trades

    def liquidate(self, pool, price):
        """Cancel grid, sell any inventory to cash."""
        self.levels = []
        if pool["btc"] > 0:
            pool["usdt"] += pool["btc"] * price * (1 - FEE)
            pool["btc"] = 0.0


# --------------------------------------------------------------------------- #
# Combo simulation
# --------------------------------------------------------------------------- #

def sim_combo(candles, targets, model, labels, max_dd=8.0):
    """Regime-filtered grid, faithful to the live design:
      ranging                  -> grid trades
      trending / high_vol      -> grid PAUSES, inventory HELD (no liquidation)
    Same 8% emergency drawdown stop as grid-only (live risk rule 1)."""
    pool = {"usdt": ACTIVE_CAPITAL, "btc": 0.0}
    grid = GridLeg()
    trades, equity = [], []
    regime = "high_volatility"   # safe default until first classification
    regime_candles = {"ranging": 0, "trending": 0, "high_volatility": 0}
    peak = ACTIVE_CAPITAL

    for i, c in enumerate(candles):
        if i >= FEATURE_WINDOW and i % CLASSIFY_EVERY == 0:
            feats = compute_features(candles[i - FEATURE_WINDOW:i])
            if feats is not None:
                regime = labels[int(model.predict(feats.reshape(1, -1))[0])]
        regime_candles[regime] += 1

        if regime == "ranging":          # only trade in ranging; else hold
            trades += grid.step(c, pool)

        val = pool["usdt"] + pool["btc"] * c["close"]
        equity.append(val)
        peak = max(peak, val)
        if (peak - val) / peak * 100 >= max_dd:   # emergency stop, halt
            break
    return trades, equity, regime_candles


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #

def summarize(name, equity, trades):
    final = equity[-1] if equity else ACTIVE_CAPITAL
    ret = (final - ACTIVE_CAPITAL) / ACTIVE_CAPITAL * 100
    dd = _max_drawdown(equity) if equity else 0.0
    sells = [t for t in trades if t["side"] == "SELL"]
    realized = sum(t["realized_pnl"] for t in trades)
    return {
        "name": name, "final": final, "ret": ret, "dd": dd,
        "trades": len(trades), "round_trips": len(sells), "realized": realized,
    }


def print_table(rows, start_price, end_price, n_candles, span_days):
    bh = (end_price - start_price) / start_price * 100
    print("\n" + "=" * 72)
    print(f"STRATEGY COMPARISON   ({n_candles:,} x 1m candles, ~{span_days} days)")
    print(f"Capital ${ACTIVE_CAPITAL:.0f}   BTC {start_price:,.0f} -> {end_price:,.0f}"
          f"   buy&hold {bh:+.2f}%")
    print("=" * 72)
    h = f"{'strategy':<14}{'final $':>10}{'return':>9}{'net P/L $':>11}{'maxDD':>8}{'trades':>8}{'r-trips':>8}"
    print(h)
    print("-" * 72)
    for r in rows:
        print(f"{r['name']:<14}{r['final']:>10.2f}{r['ret']:>8.2f}%"
              f"{r['final']-ACTIVE_CAPITAL:>11.2f}{r['dd']:>7.2f}%"
              f"{r['trades']:>8}{r['round_trips']:>8}")
    print(f"{'buy & hold':<14}{ACTIVE_CAPITAL*(1+bh/100):>10.2f}{bh:>8.2f}%"
          f"{ACTIVE_CAPITAL*bh/100:>11.2f}{'-':>8}{'-':>8}{'-':>8}")
    print("=" * 72)
    print("return % is equity-curve based (comparable across all three).")


# --------------------------------------------------------------------------- #

def run_comparison(candles, daily, model, labels, max_dd=8.0):
    """Run grid/MA/combo over one candle set at a given drawdown limit."""
    targets = ma_targets(daily)

    eng = BacktestEngine(
        rest=None, symbol=SYMBOL, range_percent=RANGE_PCT, num_levels=LEVELS,
        order_size_usdt=ORDER_SIZE, active_capital_usdt=ACTIVE_CAPITAL,
        max_drawdown_percent=max_dd, rebuild_threshold_percent=REBUILD_THRESHOLD,
    )
    eng.run_candles(candles)
    grid_row = summarize("grid-only", eng.equity_curve, eng._trades)

    ma_trades, ma_eq = sim_ma(candles, targets)
    ma_row = summarize("MA-only", ma_eq, ma_trades)

    cb_trades, cb_eq, regime_candles = sim_combo(candles, targets, model, labels, max_dd)
    cb_row = summarize("combo", cb_eq, cb_trades)
    return [grid_row, ma_row, cb_row], regime_candles


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2025-12-01")
    ap.add_argument("--end", default="2026-05-31")
    ap.add_argument("--max-dd", type=float, default=8.0)
    args = ap.parse_args()

    rest = BinanceRest(api_key="", api_secret="", testnet=False)  # MAINNET data
    await rest.open()
    try:
        start_ms, end_ms = _ms(args.start), _ms(args.end)
        warm_ms = _ms((datetime.strptime(args.start, "%Y-%m-%d") - timedelta(days=70))
                      .strftime("%Y-%m-%d"))

        print(f"Fetching 1m candles {args.start} -> {args.end} (mainnet)...")
        candles = await fetch_klines(rest, SYMBOL, "1m", start_ms, end_ms)
        print(f"  {len(candles):,} 1m candles")
        daily = await fetch_klines(rest, SYMBOL, "1d", warm_ms, end_ms)
    finally:
        await rest.close()

    if len(candles) < 2:
        print("No candle data returned.")
        return

    bundle = load_model()
    rows, regime_candles = run_comparison(
        candles, daily, bundle["model"], bundle["labels"], args.max_dd)
    span_days = (candles[-1]["open_time"] - candles[0]["open_time"]) // 86_400_000
    print_table(rows, candles[0]["close"], candles[-1]["close"], len(candles), span_days)
    tot = sum(regime_candles.values()) or 1
    print("combo regime mix:  " + "  ".join(
        f"{k} {v/tot*100:.0f}%" for k, v in regime_candles.items()))


if __name__ == "__main__":
    asyncio.run(main())
