"""
Unit tests for ai_filter/trainer.py — label generation and dataset building.

Training (sklearn fit) is not exercised here — that's slow and belongs in a
separate integration/smoke test. We test the pure logic: labelling and dataset shape.
"""

import numpy as np
import pytest
from ai_filter.trainer import (
    _make_label, _build_dataset, load_model,
    LABELS, _TREND_RETURN_PCT, _VOL_SPIKE_RATIO, _LABEL_WINDOW,
)
from ai_filter.features import FEATURE_WINDOW

FEATURE_COUNT = 14


def flat_candle(price=50000.0, atr=10.0):
    return {
        "open": price, "high": price + atr / 2,
        "low": price - atr / 2, "close": price, "volume": 0.0,
    }


def flat_candles(n, price=50000.0, atr=10.0):
    return [flat_candle(price, atr) for _ in range(n)]


class TestMakeLabel:
    def _future(self, n, close=50000.0, atr=10.0):
        return flat_candles(n, price=close, atr=atr)

    def test_flat_market_is_ranging(self):
        current = flat_candle(50000.0)
        future = self._future(_LABEL_WINDOW, close=50000.0, atr=10.0)
        label = _make_label(current, future, baseline_atr=10.0)
        assert LABELS[label] == "ranging"

    def test_large_price_move_is_trending(self):
        current = flat_candle(50000.0)
        end_price = 50000.0 * (1 + (_TREND_RETURN_PCT + 1.0) / 100)
        future = self._future(_LABEL_WINDOW - 1, 50000.0) + [flat_candle(end_price)]
        label = _make_label(current, future, baseline_atr=10.0)
        assert LABELS[label] == "trending"

    def test_negative_price_move_is_also_trending(self):
        current = flat_candle(50000.0)
        end_price = 50000.0 * (1 - (_TREND_RETURN_PCT + 1.0) / 100)
        future = self._future(_LABEL_WINDOW - 1, 50000.0) + [flat_candle(end_price)]
        label = _make_label(current, future, baseline_atr=10.0)
        assert LABELS[label] == "trending"

    def test_high_atr_is_high_volatility(self):
        baseline_atr = 10.0
        current = flat_candle(50000.0)
        future = self._future(_LABEL_WINDOW, 50000.0, atr=baseline_atr * (_VOL_SPIKE_RATIO + 1))
        label = _make_label(current, future, baseline_atr)
        assert LABELS[label] == "high_volatility"

    def test_high_volatility_beats_trending(self):
        # Both conditions met — volatility takes priority
        current = flat_candle(50000.0)
        end_price = 50000.0 * 1.05
        future = (
            self._future(_LABEL_WINDOW - 1, 50000.0, atr=10000.0)
            + [flat_candle(end_price, atr=10000.0)]
        )
        label = _make_label(current, future, baseline_atr=10.0)
        assert LABELS[label] == "high_volatility"

    def test_just_below_trend_threshold_is_ranging(self):
        current = flat_candle(50000.0)
        # Move just under the threshold
        end_price = 50000.0 * (1 + (_TREND_RETURN_PCT - 0.1) / 100)
        future = self._future(_LABEL_WINDOW - 1, 50000.0) + [flat_candle(end_price)]
        label = _make_label(current, future, baseline_atr=10.0)
        assert LABELS[label] == "ranging"


class TestBuildDataset:
    def test_raises_for_too_few_candles(self):
        with pytest.raises(ValueError, match="Need at least"):
            _build_dataset(flat_candles(FEATURE_WINDOW + _LABEL_WINDOW - 1))

    def test_returns_arrays_at_minimum_candles(self):
        candles = flat_candles(FEATURE_WINDOW + _LABEL_WINDOW)
        X, y = _build_dataset(candles)
        assert isinstance(X, np.ndarray)
        assert isinstance(y, np.ndarray)

    def test_feature_vector_length_correct(self):
        candles = flat_candles(300)
        X, _ = _build_dataset(candles)
        assert X.shape[1] == FEATURE_COUNT

    def test_x_and_y_same_length(self):
        candles = flat_candles(300)
        X, y = _build_dataset(candles)
        assert len(X) == len(y)

    def test_labels_are_valid_indices(self):
        candles = flat_candles(300)
        _, y = _build_dataset(candles)
        assert np.all(y >= 0)
        assert np.all(y < len(LABELS))

    def test_sample_count_bounded_by_windows(self):
        n = 400
        candles = flat_candles(n)
        X, _ = _build_dataset(candles)
        max_possible = n - FEATURE_WINDOW - _LABEL_WINDOW
        assert len(X) <= max_possible

    def test_x_dtype_is_float32(self):
        X, _ = _build_dataset(flat_candles(300))
        assert X.dtype == np.float32

    def test_y_dtype_is_int32(self):
        _, y = _build_dataset(flat_candles(300))
        assert y.dtype == np.int32


class TestLoadModel:
    def test_returns_none_when_file_missing(self, tmp_path, monkeypatch):
        import ai_filter.trainer as mod
        monkeypatch.setattr(mod, "MODEL_PATH", tmp_path / "no_model.pkl")
        assert load_model() is None

    def test_returns_none_for_corrupt_file(self, tmp_path, monkeypatch):
        import ai_filter.trainer as mod
        corrupt = tmp_path / "bad.pkl"
        corrupt.write_bytes(b"this is not a pickle")
        monkeypatch.setattr(mod, "MODEL_PATH", corrupt)
        assert load_model() is None

    def test_save_and_load_round_trip(self, tmp_path, monkeypatch):
        import pickle, ai_filter.trainer as mod
        path = tmp_path / "model.pkl"
        monkeypatch.setattr(mod, "MODEL_PATH", path)
        bundle = {"model": "fake_model", "labels": LABELS}
        with open(path, "wb") as f:
            pickle.dump(bundle, f)
        loaded = load_model()
        assert loaded is not None
        assert loaded["labels"] == LABELS
        assert loaded["model"] == "fake_model"
