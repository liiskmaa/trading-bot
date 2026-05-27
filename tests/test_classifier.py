"""
Unit tests for ai_filter/classifier.py — ML-based market regime classifier.
"""

import numpy as np
import pytest
from ai_filter.classifier import MarketClassifier, _FALLBACK
from ai_filter.features import FEATURE_WINDOW
from ai_filter.trainer import LABELS


def flat_candles(n, price=50000.0):
    return [
        {"open": price, "high": price + 10, "low": price - 10,
         "close": price, "volume": 0.0}
        for _ in range(n)
    ]


class FakeCache:
    def __init__(self, stored_regime=None):
        self._regime = stored_regime
        self.written = {}

    async def get_ai_regime(self, symbol):
        return self._regime

    async def set_ai_regime(self, symbol, regime):
        self.written[symbol] = regime


class FakeModel:
    def __init__(self, prediction_idx=0):
        self._pred = prediction_idx

    def predict(self, X):
        return np.array([self._pred])


def _classifier_with_model(pred_idx=0, call_interval=0):
    import ai_filter.classifier as mod
    bundle = {"model": FakeModel(pred_idx), "labels": LABELS}
    clf = MarketClassifier.__new__(MarketClassifier)
    clf._cache_ttl = 60
    clf._call_interval = call_interval
    clf._cache = None
    clf._symbol = ""
    clf._last_call = 0.0
    clf._bundle = bundle
    return clf


class TestPredictFallback:
    def test_no_model_returns_fallback(self, monkeypatch):
        import ai_filter.classifier as mod
        monkeypatch.setattr(mod, "load_model", lambda: None)
        clf = MarketClassifier()
        assert clf._predict(flat_candles(FEATURE_WINDOW + 10)) == _FALLBACK

    def test_too_few_candles_returns_fallback(self, monkeypatch):
        import ai_filter.classifier as mod
        monkeypatch.setattr(mod, "load_model", lambda: {"model": FakeModel(0), "labels": LABELS})
        clf = MarketClassifier()
        assert clf._predict(flat_candles(FEATURE_WINDOW - 1)) == _FALLBACK

    def test_model_inference_error_returns_fallback(self, monkeypatch):
        import ai_filter.classifier as mod

        class BrokenModel:
            def predict(self, X):
                raise RuntimeError("inference failed")

        monkeypatch.setattr(mod, "load_model", lambda: {"model": BrokenModel(), "labels": LABELS})
        clf = MarketClassifier()
        result = clf._predict(flat_candles(FEATURE_WINDOW + 10))
        assert result == _FALLBACK


class TestPredictOutput:
    def test_returns_ranging_for_prediction_0(self, monkeypatch):
        import ai_filter.classifier as mod
        monkeypatch.setattr(mod, "load_model", lambda: {"model": FakeModel(0), "labels": LABELS})
        clf = MarketClassifier()
        assert clf._predict(flat_candles(FEATURE_WINDOW + 10)) == "ranging"

    def test_returns_trending_for_prediction_1(self, monkeypatch):
        import ai_filter.classifier as mod
        monkeypatch.setattr(mod, "load_model", lambda: {"model": FakeModel(1), "labels": LABELS})
        clf = MarketClassifier()
        assert clf._predict(flat_candles(FEATURE_WINDOW + 10)) == "trending"

    def test_returns_high_volatility_for_prediction_2(self, monkeypatch):
        import ai_filter.classifier as mod
        monkeypatch.setattr(mod, "load_model", lambda: {"model": FakeModel(2), "labels": LABELS})
        clf = MarketClassifier()
        assert clf._predict(flat_candles(FEATURE_WINDOW + 10)) == "high_volatility"


class TestClassifyCache:
    async def test_returns_cached_regime_without_calling_model(self):
        cache = FakeCache(stored_regime="ranging")
        clf = _classifier_with_model(pred_idx=2)  # model would say high_volatility
        clf.inject(cache, "BTCUSDT")
        result = await clf.classify(flat_candles(FEATURE_WINDOW + 10))
        assert result == "ranging"

    async def test_ignores_unknown_cached_regime(self, monkeypatch):
        import ai_filter.classifier as mod
        monkeypatch.setattr(mod, "load_model", lambda: {"model": FakeModel(0), "labels": LABELS})
        cache = FakeCache(stored_regime="banana")  # invalid value
        clf = MarketClassifier(call_interval_seconds=0)
        clf.inject(cache, "BTCUSDT")
        result = await clf.classify(flat_candles(FEATURE_WINDOW + 10))
        assert result in LABELS

    async def test_writes_result_to_cache(self, monkeypatch):
        import ai_filter.classifier as mod
        monkeypatch.setattr(mod, "load_model", lambda: {"model": FakeModel(0), "labels": LABELS})
        cache = FakeCache()
        clf = MarketClassifier(call_interval_seconds=0)
        clf.inject(cache, "BTCUSDT")
        await clf.classify(flat_candles(FEATURE_WINDOW + 10))
        assert cache.written.get("BTCUSDT") == "ranging"


class TestClassifyRateLimiting:
    async def test_returns_fallback_within_call_interval(self):
        clf = _classifier_with_model(pred_idx=0, call_interval=3600)
        clf._last_call = 1e18  # far in the future
        result = await clf.classify(flat_candles(FEATURE_WINDOW + 10))
        assert result == _FALLBACK

    async def test_calls_model_when_interval_expired(self, monkeypatch):
        import ai_filter.classifier as mod
        monkeypatch.setattr(mod, "load_model", lambda: {"model": FakeModel(0), "labels": LABELS})
        clf = MarketClassifier(call_interval_seconds=0)
        clf._last_call = 0.0
        result = await clf.classify(flat_candles(FEATURE_WINDOW + 10))
        assert result == "ranging"


class TestReload:
    def test_reload_loads_new_bundle(self, monkeypatch, tmp_path):
        import ai_filter.classifier as mod
        call_count = 0

        def counting_load():
            nonlocal call_count
            call_count += 1
            return None

        monkeypatch.setattr(mod, "load_model", counting_load)
        clf = MarketClassifier()   # first load at __init__
        clf.reload()               # second load
        assert call_count == 2
