"""
Backtesting engine unit tests.
Uses a synthetic candle sequence so no network calls are needed.
"""

import asyncio
import pytest
from backtesting.metrics import compute_metrics, _max_drawdown, BacktestMetrics
from backtesting.engine import BacktestEngine, _needs_rebuild, _date_to_ms


class TestMetrics:
    def test_empty_trades(self):
        m = compute_metrics([], 300.0, [])
        assert m.total_trades == 0
        assert m.win_rate_percent == 0.0

    def test_win_rate(self):
        trades = [
            {"side": "SELL", "price": 50_000, "quantity": 0.0006, "realized_pnl": 1.0},
            {"side": "SELL", "price": 50_000, "quantity": 0.0006, "realized_pnl": -0.5},
            {"side": "SELL", "price": 50_000, "quantity": 0.0006, "realized_pnl": 0.8},
        ]
        m = compute_metrics(trades, 300.0, [300, 301, 300.5, 301.3])
        assert abs(m.win_rate_percent - 66.67) < 0.1

    def test_profit_factor_infinite_when_no_losses(self):
        trades = [
            {"side": "SELL", "price": 50_000, "quantity": 0.0006, "realized_pnl": 1.0},
        ]
        m = compute_metrics(trades, 300.0, [300, 301])
        assert m.profit_factor == float("inf")

    def test_total_profit_sums_pnl(self):
        trades = [
            {"side": "SELL", "price": 50_000, "quantity": 0.0006, "realized_pnl": 1.5},
            {"side": "SELL", "price": 50_000, "quantity": 0.0006, "realized_pnl": -0.3},
        ]
        m = compute_metrics(trades, 300.0, [300, 301.5, 301.2])
        assert abs(m.total_profit_usdt - 1.2) < 1e-9

    def test_price_change_computed(self):
        trades = [
            {"side": "BUY",  "price": 40_000, "quantity": 0.001, "realized_pnl": 0},
            {"side": "SELL", "price": 50_000, "quantity": 0.001, "realized_pnl": 10},
        ]
        m = compute_metrics(trades, 300.0, [300, 310])
        assert abs(m.price_change_percent - 25.0) < 0.01


class TestMaxDrawdown:
    def test_flat_equity(self):
        assert _max_drawdown([100, 100, 100]) == 0.0

    def test_simple_drawdown(self):
        # Peak 110, trough 99 → dd = (110-99)/110 ≈ 10%
        dd = _max_drawdown([100, 110, 105, 99, 103])
        assert abs(dd - (110 - 99) / 110 * 100) < 0.01

    def test_monotone_increase_no_drawdown(self):
        assert _max_drawdown([100, 101, 102, 103]) == 0.0

    def test_monotone_decrease_full_drawdown(self):
        dd = _max_drawdown([100, 90, 80, 70])
        assert abs(dd - 30.0) < 0.01


class TestNeedsRebuild:
    def test_needs_rebuild_when_drifted(self):
        assert _needs_rebuild(55_000, 50_000, 3.0) is True  # 10% drift

    def test_no_rebuild_within_threshold(self):
        assert _needs_rebuild(51_000, 50_000, 3.0) is False  # 2% drift

    def test_rebuild_when_no_reference(self):
        assert _needs_rebuild(50_000, 0, 3.0) is True


class TestDateToMs:
    def test_known_date(self):
        ms = _date_to_ms("2024-01-01")
        assert ms == 1_704_067_200_000

    def test_monotone(self):
        assert _date_to_ms("2024-01-01") < _date_to_ms("2024-06-01")


class TestBacktestEngineReplay:
    """Integration test against a synthetic flat then rising price series."""

    def _make_candles(self, prices: list[float]) -> list[dict]:
        # Use ±3% intra-candle swings so grid levels are crossed and fills happen.
        return [
            {
                "open_time": i * 60_000,
                "open": p,
                "high": p * 1.03,
                "low": p * 0.97,
                "close": p,
                "volume": 10.0,
            }
            for i, p in enumerate(prices)
        ]

    def _make_engine(self) -> BacktestEngine:
        return BacktestEngine(
            rest=None,
            symbol="BTCUSDT",
            range_percent=5.0,
            num_levels=10,
            order_size_usdt=29.0,
            active_capital_usdt=300.0,
            max_drawdown_percent=8.0,
        )

    def test_no_crash_on_flat_market(self):
        engine = self._make_engine()
        prices = [50_000.0] * 100
        candles = self._make_candles(prices)
        engine._replay(candles)
        assert len(engine._equity_curve) > 0

    def test_equity_curve_not_empty(self):
        engine = self._make_engine()
        prices = [50_000 + i * 10 for i in range(200)]
        engine._replay(self._make_candles(prices))
        assert len(engine._equity_curve) == 200

    def test_drawdown_stop_halts_replay(self):
        engine = self._make_engine()
        # Phase 1: 30 flat candles at 50 000.  With ±3% intra-candle swings the
        # buy levels at ~48 611, ~49 167, ~49 722 are crossed and BTC is acquired.
        phase1 = [50_000] * 30
        # Phase 2: price halves — the BTC inventory marks down heavily, pushing
        # drawdown well past the 8% limit on the very first crash candle.
        phase2 = [25_000] * 300
        engine._replay(self._make_candles(phase1 + phase2))
        total = len(phase1) + len(phase2)
        assert len(engine._equity_curve) < total
