"""Is rebuild-on-drift the grid's value destroyer?

For each regime window, compare (DD stop OFF, to show true strategy edge):
  dynamic-3%   : current grid, range 3%, re-centers every 3% drift
  static-3%    : same grid but rebuild disabled (dormant outside band)
  static-30%   : a wide range-bound grid (range 30%) that spans the swings
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from market_data.binance_rest import BinanceRest          # noqa: E402
from backtesting.engine import BacktestEngine             # noqa: E402
from backtesting.metrics import _max_drawdown             # noqa: E402
from scripts.strategy_compare import fetch_klines, _ms, ACTIVE_CAPITAL  # noqa: E402

WINDOWS = [
    ("DOWN   ", "2025-12-01", "2026-05-31"),
    ("RANGING", "2025-01-30", "2025-05-29"),
    ("UPTREND", "2023-11-14", "2024-03-12"),
]
NO_STOP = 1e9          # effectively no drawdown stop
NO_REBUILD = 1e9       # effectively never re-center


def run(candles, range_pct, levels, rebuild):
    eng = BacktestEngine(rest=None, symbol="BTCUSDT", range_percent=range_pct,
                         num_levels=levels, order_size_usdt=29.0,
                         active_capital_usdt=ACTIVE_CAPITAL,
                         max_drawdown_percent=NO_STOP,
                         rebuild_threshold_percent=rebuild)
    eng.run_candles(candles)
    eq = eng.equity_curve
    ret = (eq[-1] - ACTIVE_CAPITAL) / ACTIVE_CAPITAL * 100
    sells = sum(1 for t in eng._trades if t["side"] == "SELL")
    return ret, _max_drawdown(eq), sells


async def main():
    rest = BinanceRest(api_key="", api_secret="", testnet=False)
    await rest.open()
    try:
        print(f"{'window':<9}{'buy&hold':>9}{'dynamic-3%':>12}{'static-3%':>11}{'static-30%':>12}")
        print("-" * 53)
        for tag, s, e in WINDOWS:
            candles = await fetch_klines(rest, "BTCUSDT", "1m", _ms(s), _ms(e))
            bh = (candles[-1]["close"] - candles[0]["close"]) / candles[0]["close"] * 100
            dyn, _, _ = run(candles, 3.0, 10, 3.0)
            st3, _, _ = run(candles, 3.0, 10, NO_REBUILD)
            st30, _, _ = run(candles, 30.0, 20, NO_REBUILD)
            print(f"{tag:<9}{bh:>+8.1f}%{dyn:>+11.1f}%{st3:>+10.1f}%{st30:>+11.1f}%")
    finally:
        await rest.close()
    print("\n(returns are equity-curve based, drawdown stop OFF for all)")


if __name__ == "__main__":
    asyncio.run(main())
