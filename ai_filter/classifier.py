"""
Market regime classifier using a trained GradientBoosting model.

Loads data/regime_model.pkl at startup. Returns "high_volatility" (safe fallback)
when the model has not been trained yet or there are fewer than FEATURE_WINDOW candles.

Train the model first:
  python main.py train-regime

Valid regimes:
  "ranging"         — sideways price action, grid trading allowed
  "trending"        — directional momentum, grid trading paused
  "high_volatility" — extreme swings, grid trading paused
"""

import logging
import time

from ai_filter.features import compute_features, FEATURE_WINDOW
from ai_filter.trainer import load_model, LABELS

logger = logging.getLogger(__name__)

_FALLBACK = "high_volatility"


class MarketClassifier:
    def __init__(self, cache_ttl_seconds: int = 60, call_interval_seconds: int = 60):
        self._cache_ttl = cache_ttl_seconds
        self._call_interval = call_interval_seconds
        self._cache = None
        self._symbol = ""
        self._last_call: float = 0.0
        self._bundle = load_model()  # {"model": Pipeline, "labels": [...]}
        if self._bundle:
            logger.info("Regime classifier loaded from %s", "data/regime_model.pkl")
        else:
            logger.warning(
                "No trained regime model found — returning '%s' until trained. "
                "Run: python main.py train-regime",
                _FALLBACK,
            )

    def inject(self, cache, symbol: str) -> None:
        self._cache = cache
        self._symbol = symbol

    def reload(self) -> None:
        self._bundle = load_model()
        if self._bundle:
            logger.info("Regime model reloaded")

    async def classify(self, candles: list[dict]) -> str:
        if self._cache:
            cached = await self._cache.get_ai_regime(self._symbol)
            if cached and cached in set(LABELS):
                return cached

        if time.time() - self._last_call < self._call_interval:
            return _FALLBACK

        regime = self._predict(candles)
        self._last_call = time.time()

        if self._cache:
            await self._cache.set_ai_regime(self._symbol, regime)

        return regime

    def _predict(self, candles: list[dict]) -> str:
        if not self._bundle or len(candles) < FEATURE_WINDOW:
            return _FALLBACK

        features = compute_features(candles)
        if features is None:
            return _FALLBACK

        try:
            model  = self._bundle["model"]
            labels = self._bundle["labels"]
            pred   = int(model.predict(features.reshape(1, -1))[0])
            regime = labels[pred]
            logger.info("ML regime: %s", regime)
            return regime
        except Exception as e:
            logger.warning("Model inference failed: %s", e)
            return _FALLBACK
