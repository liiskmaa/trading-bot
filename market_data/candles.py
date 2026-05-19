"""
Aggregates real-time price ticks into 1-minute OHLC candles and persists them.
"""

import asyncio
import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

_MINUTE_MS = 60_000


class CandleAggregator:
    def __init__(self, symbol: str, interval_seconds: int = 60):
        self._symbol = symbol
        self._interval = interval_seconds
        self._current: Optional[dict] = None
        self._repo = None  # injected after init

    def set_repo(self, repo) -> None:
        self._repo = repo

    def _candle_bucket(self, ts_ms: int) -> int:
        bucket_ms = self._interval * 1000
        return (ts_ms // bucket_ms) * bucket_ms

    async def on_price(self, price: float) -> None:
        now_ms = int(time.time() * 1000)
        bucket = self._candle_bucket(now_ms)

        if self._current is None or self._current["open_time"] != bucket:
            if self._current is not None:
                await self._flush(self._current)
            self._current = {
                "symbol": self._symbol,
                "interval": "1m",
                "open_time": bucket,
                "open": price,
                "high": price,
                "low": price,
                "close": price,
                "volume": 0.0,
            }
        else:
            c = self._current
            c["high"] = max(c["high"], price)
            c["low"] = min(c["low"], price)
            c["close"] = price

    async def _flush(self, candle: dict) -> None:
        if self._repo:
            try:
                await self._repo.upsert_candle(candle)
            except Exception as e:
                logger.error("Failed to persist candle: %s", e)
