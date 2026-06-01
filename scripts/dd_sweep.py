"""Does the 8% drawdown stop kill the grid? Sweep DD limit on the ranging window."""
import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from market_data.binance_rest import BinanceRest          # noqa: E402
from backtesting.engine import BacktestEngine             # noqa: E402
from backtesting.metrics import _max_drawdown             # noqa: E402
from scripts.strategy_compare import fetch_klines, _ms, ACTIVE_CAPITAL  # noqa: E402


async def main():
    rest = BinanceRest(api_key="", api_secret="", testnet=False)
    await rest.open()
    try:
        candles = await fetch_klines(rest, "BTCUSDT", "1m",
                                     _ms("2025-01-30"), _ms("2025-05-29"))
    finally:
        await rest.close()
    print(f"{len(candles):,} candles, ranging window 2025-01-30 -> 2025-05-29\n")
    print(f"{'DD limit':>9}{'return':>9}{'final $':>10}{'maxDD':>8}{'r-trips':>9}")
    print("-" * 45)
    for dd in (8.0, 12.0, 16.0, 25.0, 100.0):
        eng = BacktestEngine(rest=None, symbol="BTCUSDT", range_percent=3.0,
                             num_levels=10, order_size_usdt=29.0,
                             active_capital_usdt=ACTIVE_CAPITAL,
                             max_drawdown_percent=dd)
        eng.run_candles(candles)
        eq = eng.equity_curve
        final = eq[-1]
        ret = (final - ACTIVE_CAPITAL) / ACTIVE_CAPITAL * 100
        sells = sum(1 for t in eng._trades if t["side"] == "SELL")
        print(f"{dd:>8.0f}%{ret:>8.2f}%{final:>10.2f}{_max_drawdown(eq):>7.2f}%{sells:>9}")


if __name__ == "__main__":
    asyncio.run(main())
