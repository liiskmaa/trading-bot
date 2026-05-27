"""
Unit tests for market_data/candles.py — 1-minute OHLC aggregator.
"""

import pytest
from market_data.candles import CandleAggregator


class FakeRepo:
    def __init__(self):
        self.flushed: list[dict] = []

    async def upsert_candle(self, candle: dict) -> None:
        self.flushed.append(dict(candle))


def _make_agg(interval=60) -> tuple[CandleAggregator, FakeRepo]:
    repo = FakeRepo()
    agg = CandleAggregator("BTCUSDT", interval_seconds=interval)
    agg.set_repo(repo)
    return agg, repo


class TestFirstTick:
    async def test_creates_candle_on_first_tick(self):
        agg, _ = _make_agg()
        await agg.on_price(50000.0)
        assert agg._current is not None

    async def test_open_set_from_first_tick(self):
        agg, _ = _make_agg()
        await agg.on_price(50000.0)
        assert agg._current["open"] == 50000.0

    async def test_close_set_from_first_tick(self):
        agg, _ = _make_agg()
        await agg.on_price(50000.0)
        assert agg._current["close"] == 50000.0

    async def test_high_equals_price_on_first_tick(self):
        agg, _ = _make_agg()
        await agg.on_price(50000.0)
        assert agg._current["high"] == 50000.0

    async def test_low_equals_price_on_first_tick(self):
        agg, _ = _make_agg()
        await agg.on_price(50000.0)
        assert agg._current["low"] == 50000.0

    async def test_symbol_set_correctly(self):
        agg, _ = _make_agg()
        await agg.on_price(50000.0)
        assert agg._current["symbol"] == "BTCUSDT"

    async def test_interval_label_is_1m(self):
        agg, _ = _make_agg()
        await agg.on_price(50000.0)
        assert agg._current["interval"] == "1m"


class TestOhlcWithinSameBucket:
    async def test_high_tracks_maximum(self):
        agg, _ = _make_agg()
        await agg.on_price(50000.0)
        await agg.on_price(51000.0)
        assert agg._current["high"] == 51000.0

    async def test_low_tracks_minimum(self):
        agg, _ = _make_agg()
        await agg.on_price(50000.0)
        await agg.on_price(49000.0)
        assert agg._current["low"] == 49000.0

    async def test_close_tracks_latest(self):
        agg, _ = _make_agg()
        await agg.on_price(50000.0)
        await agg.on_price(51000.0)
        await agg.on_price(49500.0)
        assert agg._current["close"] == 49500.0

    async def test_open_stays_at_first_tick(self):
        agg, _ = _make_agg()
        await agg.on_price(50000.0)
        await agg.on_price(51000.0)
        await agg.on_price(49000.0)
        assert agg._current["open"] == 50000.0

    async def test_high_not_lowered_by_subsequent_tick(self):
        agg, _ = _make_agg()
        await agg.on_price(50000.0)
        await agg.on_price(55000.0)
        await agg.on_price(48000.0)
        assert agg._current["high"] == 55000.0

    async def test_low_not_raised_by_subsequent_tick(self):
        agg, _ = _make_agg()
        await agg.on_price(50000.0)
        await agg.on_price(45000.0)
        await agg.on_price(52000.0)
        assert agg._current["low"] == 45000.0


class TestBucketRollover:
    async def test_new_bucket_flushes_previous_candle(self):
        agg, repo = _make_agg()
        await agg.on_price(50000.0)
        # Force an old bucket
        agg._current["open_time"] = 0
        await agg.on_price(51000.0)
        assert len(repo.flushed) == 1

    async def test_flushed_candle_has_correct_close(self):
        agg, repo = _make_agg()
        await agg.on_price(50000.0)
        agg._current["open_time"] = 0
        await agg.on_price(51000.0)
        assert repo.flushed[0]["close"] == 50000.0

    async def test_new_candle_starts_fresh_after_rollover(self):
        agg, _ = _make_agg()
        await agg.on_price(50000.0)
        agg._current["open_time"] = 0
        await agg.on_price(51000.0)
        assert agg._current["open"] == 51000.0

    async def test_no_flush_on_first_tick(self):
        agg, repo = _make_agg()
        await agg.on_price(50000.0)
        assert len(repo.flushed) == 0

    async def test_no_repo_does_not_raise_on_flush(self):
        agg = CandleAggregator("BTCUSDT")
        await agg.on_price(50000.0)
        agg._current["open_time"] = 0
        await agg.on_price(51000.0)  # triggers flush; no repo set


class TestBucketAlignment:
    def test_bucket_aligns_to_minute_boundary(self):
        agg = CandleAggregator("BTCUSDT", interval_seconds=60)
        ts_ms = 1_700_000_075_000  # 75 s into a minute
        bucket = agg._candle_bucket(ts_ms)
        assert bucket % 60_000 == 0

    def test_bucket_is_before_timestamp(self):
        agg = CandleAggregator("BTCUSDT", interval_seconds=60)
        ts_ms = 1_700_000_075_000
        assert agg._candle_bucket(ts_ms) < ts_ms

    def test_next_bucket_is_exactly_one_interval_later(self):
        agg = CandleAggregator("BTCUSDT", interval_seconds=60)
        ts_ms = 1_700_000_000_000
        b1 = agg._candle_bucket(ts_ms)
        b2 = agg._candle_bucket(ts_ms + 60_000)
        assert b2 - b1 == 60_000

    def test_two_ticks_in_same_minute_share_bucket(self):
        agg = CandleAggregator("BTCUSDT", interval_seconds=60)
        base = 1_700_000_000_000
        assert agg._candle_bucket(base) == agg._candle_bucket(base + 30_000)
