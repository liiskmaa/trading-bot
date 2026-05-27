"""
Unit tests for ai_filter/features.py — feature extraction from 1-min candles.
"""

import numpy as np
import pytest
from ai_filter.features import compute_features, FEATURE_WINDOW, _rsi, _streak

FEATURE_COUNT = 14


def flat_candles(n, price=50000.0, spread=10.0):
    return [
        {"open": price, "high": price + spread, "low": price - spread,
         "close": price, "volume": 0.0}
        for _ in range(n)
    ]


def trending_candles(n, start=50000.0, step=60.0):
    candles, price = [], start
    for _ in range(n):
        candles.append({
            "open": price, "high": price + 20, "low": price - 5,
            "close": price + step, "volume": 0.0,
        })
        price += step
    return candles


def volatile_candles(n, price=50000.0, swing=400.0):
    import random
    random.seed(42)
    candles = []
    for _ in range(n):
        candles.append({
            "open": price,
            "high": price + swing,
            "low": price - swing,
            "close": price + random.uniform(-100, 100),
            "volume": 0.0,
        })
    return candles


class TestComputeFeatures:
    def test_returns_none_below_feature_window(self):
        assert compute_features(flat_candles(FEATURE_WINDOW - 1)) is None

    def test_returns_array_at_exact_window(self):
        result = compute_features(flat_candles(FEATURE_WINDOW))
        assert result is not None
        assert isinstance(result, np.ndarray)

    def test_feature_vector_has_correct_length(self):
        result = compute_features(flat_candles(FEATURE_WINDOW + 10))
        assert result.shape == (FEATURE_COUNT,)

    def test_all_features_finite(self):
        result = compute_features(flat_candles(FEATURE_WINDOW))
        assert np.all(np.isfinite(result))

    def test_all_features_finite_for_volatile_candles(self):
        result = compute_features(volatile_candles(FEATURE_WINDOW))
        assert np.all(np.isfinite(result))

    def test_flat_market_returns_near_zero(self):
        result = compute_features(flat_candles(FEATURE_WINDOW, price=50000.0))
        # All four return features (indices 0-3) should be ~0 for a flat market
        for i in range(4):
            assert abs(result[i]) < 0.01

    def test_uptrend_gives_positive_returns(self):
        result = compute_features(trending_candles(FEATURE_WINDOW, step=50.0))
        # All return features should be positive for a sustained uptrend
        for i in range(4):
            assert result[i] > 0

    def test_atr_spike_ratio_positive(self):
        result = compute_features(volatile_candles(FEATURE_WINDOW))
        atr_spike_idx = 8  # 9th feature
        assert result[atr_spike_idx] > 0

    def test_flat_market_atr_spike_ratio_near_one(self):
        result = compute_features(flat_candles(FEATURE_WINDOW, spread=10.0))
        atr_spike_idx = 8
        assert abs(result[atr_spike_idx] - 1.0) < 0.5

    def test_uses_only_last_feature_window_candles(self):
        # Prepend garbage candles — result should match using only the last FEATURE_WINDOW
        tail = flat_candles(FEATURE_WINDOW, price=50000.0)
        extra = flat_candles(20, price=99000.0)
        result_with_extra = compute_features(extra + tail)
        result_tail_only = compute_features(tail)
        np.testing.assert_array_almost_equal(result_with_extra, result_tail_only)

    def test_dtype_is_float32(self):
        result = compute_features(flat_candles(FEATURE_WINDOW))
        assert result.dtype == np.float32


class TestRsi:
    def test_returns_50_when_insufficient_data(self):
        assert _rsi(np.array([50000.0] * 5), 14) == 50.0

    def test_returns_100_for_monotone_increase(self):
        closes = np.arange(1.0, 17.0)  # 16 values, 15 deltas all positive
        assert _rsi(closes, 14) == 100.0

    def test_in_valid_range(self):
        import random
        random.seed(99)
        closes = np.array([50000.0 + random.gauss(0, 50) for _ in range(30)])
        val = _rsi(closes, 14)
        assert 0.0 <= val <= 100.0

    def test_above_50_for_uptrend(self):
        closes = np.array([float(i) for i in range(1, 32)])  # 31 values
        assert _rsi(closes, 14) > 50.0


class TestStreak:
    def test_empty_returns_zero(self):
        assert _streak(np.array([])) == 0

    def test_single_value_returns_zero(self):
        assert _streak(np.array([50000.0])) == 0

    def test_all_up_returns_positive(self):
        closes = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
        assert _streak(closes) == 4

    def test_all_down_returns_negative(self):
        closes = np.array([5.0, 4.0, 3.0, 2.0, 1.0])
        assert _streak(closes) == -4

    def test_streak_breaks_on_reversal(self):
        # 3 up, then 1 down
        closes = np.array([1.0, 2.0, 3.0, 4.0, 3.5])
        assert _streak(closes) == -1

    def test_magnitude_reflects_run_length(self):
        closes = np.array([1.0, 2.0, 3.0, 4.0, 5.0, 6.0])
        assert abs(_streak(closes)) == 5
