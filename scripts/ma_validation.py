"""Fair MA validation: MA crossover with the SAME drawdown stop the grid got.
Shows how much the original (no-stop) MA numbers were flattered."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from market_data.binance_rest import BinanceRest          # noqa: E402
from backtesting.metrics import _max_drawdown             # noqa: E402
from scripts.strategy_compare import (                    # noqa: E402
    fetch_klines, _ms, ma_targets, sim_ma, ACTIVE_CAPITAL,
)

WINDOWS = [
    ("DOWN   ", "2025-12-01", "2026-05-31"),
    ("RANGING", "2025-01-30", "2025-05-29"),
    ("UPTREND", "2023-11-14", "2024-03-12"),
]
DD_LEVELS = [(1e9, "none"), (30.0, "30%"), (16.0, "16%"), (8.0, "8%")]


def ret(eq):
    return (eq[-1] - ACTIVE_CAPITAL) / ACTIVE_CAPITAL * 100


async def main():
    rest = BinanceRest(api_key="", api_secret="", testnet=False)
    await rest.open()
    try:
        hdr = "window    buy&hold" + "".join(f"{'MA@' + lbl:>10}" for _, lbl in DD_LEVELS)
        print(hdr)
        print("-" * len(hdr))
        for tag, s, e in WINDOWS:
            candles = await fetch_klines(rest, "BTCUSDT", "1m", _ms(s), _ms(e))
            daily = await fetch_klines(rest, "BTCUSDT", "1d", _ms("2023-09-01"), _ms(e))
            targets = ma_targets(daily)
            bh = (candles[-1]["close"] - candles[0]["close"]) / candles[0]["close"] * 100
            cells = []
            for dd, _lbl in DD_LEVELS:
                _, eq = sim_ma(candles, targets, max_dd=dd)
                cells.append(f"{ret(eq):>+9.1f}%")
            print(f"{tag:<9}{bh:>+8.1f}%" + "".join(cells))
    finally:
        await rest.close()
    print("\nMA@none = original (no stop). MA@8% = same emergency stop the grid runs.")


if __name__ == "__main__":
    asyncio.run(main())
