"""Re-run grid/MA/combo with the new static wide-grid config across 3 regimes,
at the config's 8% drawdown stop AND a wider 30% stop (to show the interaction)."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from market_data.binance_rest import BinanceRest          # noqa: E402
from ai_filter.trainer import load_model                  # noqa: E402
from scripts.strategy_compare import (                    # noqa: E402
    fetch_klines, _ms, run_comparison, ACTIVE_CAPITAL,
    RANGE_PCT, LEVELS, ORDER_SIZE, REBUILD_THRESHOLD,
)

WINDOWS = [
    ("DOWN   ", "2025-12-01", "2026-05-31"),
    ("RANGING", "2025-01-30", "2025-05-29"),
    ("UPTREND", "2023-11-14", "2024-03-12"),
]
DD_LEVELS = [8.0, 30.0]


async def main():
    print(f"Static wide grid: range={RANGE_PCT:.0f}% levels={LEVELS} "
          f"order=${ORDER_SIZE:.0f} rebuild={REBUILD_THRESHOLD:.0f} (never)")
    bundle = load_model()
    model, labels = bundle["model"], bundle["labels"]

    rest = BinanceRest(api_key="", api_secret="", testnet=False)
    await rest.open()
    try:
        for tag, s, e in WINDOWS:
            warm = _ms(s[:8] + "01")  # rough warmup start (month-01); MA fetches need history
            candles = await fetch_klines(rest, "BTCUSDT", "1m", _ms(s), _ms(e))
            daily = await fetch_klines(rest, "BTCUSDT", "1d",
                                       _ms("2023-09-01"), _ms(e))
            bh = (candles[-1]["close"] - candles[0]["close"]) / candles[0]["close"] * 100
            print(f"\n=== {tag.strip()}  ({s} -> {e})   buy&hold {bh:+.1f}% ===")
            print(f"{'DDstop':>7} | {'grid':>8}{'MA':>8}{'combo':>8}")
            print("-" * 34)
            for dd in DD_LEVELS:
                rows, _ = run_comparison(candles, daily, model, labels, dd)
                r = {x["name"]: x["ret"] for x in rows}
                print(f"{dd:>6.0f}% | {r['grid-only']:>+7.1f}%"
                      f"{r['MA-only']:>+7.1f}%{r['combo']:>+7.1f}%")
    finally:
        await rest.close()


if __name__ == "__main__":
    asyncio.run(main())
