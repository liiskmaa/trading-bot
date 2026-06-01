"""Scan BTC daily history to pick the cleanest ranging and uptrend windows."""
import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from market_data.binance_rest import BinanceRest  # noqa: E402

WIN = 120  # ~4-month windows


def _ms(s):
    return int(datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)


def _d(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


async def main():
    rest = BinanceRest(api_key="", api_secret="", testnet=False)
    await rest.open()
    try:
        start, end = _ms("2023-06-01"), _ms("2026-05-31")
        out, cur = [], start
        while cur < end:
            raw = await rest._get("/api/v3/klines", {
                "symbol": "BTCUSDT", "interval": "1d",
                "startTime": cur, "endTime": end, "limit": 1000})
            if not raw:
                break
            out += raw
            cur = raw[-1][0] + 86_400_000
            if len(raw) < 1000:
                break
    finally:
        await rest.close()

    days = [{"t": k[0], "o": float(k[1]), "h": float(k[2]),
             "l": float(k[3]), "c": float(k[4])} for k in out]
    print(f"{len(days)} daily candles {_d(days[0]['t'])} -> {_d(days[-1]['t'])}")

    best_up = None      # max net return
    best_range = None   # min |net return| but with oscillation (high path range)
    for i in range(0, len(days) - WIN):
        w = days[i:i + WIN]
        c0, c1 = w[0]["c"], w[-1]["c"]
        ret = (c1 - c0) / c0 * 100
        hi = max(x["h"] for x in w)
        lo = min(x["l"] for x in w)
        band = (hi - lo) / lo * 100          # peak-to-trough span
        # range score: small net move, but enough intra-window travel to scalp
        if abs(ret) < 8 and band > 15:
            score = band - abs(ret) * 3
            if best_range is None or score > best_range[0]:
                best_range = (score, i, ret, band)
        if best_up is None or ret > best_up[0]:
            best_up = (ret, i, ret, band)

    for tag, sel in [("UPTREND", best_up), ("RANGING", best_range)]:
        _, i, ret, band = sel
        w = days[i:i + WIN]
        print(f"\n{tag}: {_d(w[0]['t'])} -> {_d(w[-1]['t'])}  "
              f"net {ret:+.1f}%  peak-to-trough {band:.0f}%  "
              f"({w[0]['c']:,.0f} -> {w[-1]['c']:,.0f})")


if __name__ == "__main__":
    asyncio.run(main())
