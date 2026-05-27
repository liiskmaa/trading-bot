"""
Trains a GradientBoosting classifier to predict market regime from 1-min candles.

Each training sample uses FEATURE_WINDOW candles of history. The label is derived
from the NEXT _LABEL_WINDOW candles:
  ranging        — default (price stayed flat)
  trending       — abs price change over label window exceeded _TREND_RETURN_PCT
  high_volatility— average ATR over label window exceeded _VOL_SPIKE_RATIO * dataset baseline

Run via CLI:
  python main.py train-regime
"""

import logging
import pickle
from pathlib import Path

import numpy as np

from ai_filter.features import compute_features, FEATURE_WINDOW

logger = logging.getLogger(__name__)

MODEL_PATH = Path("data/regime_model.pkl")
LABELS = ["ranging", "trending", "high_volatility"]

_LABEL_WINDOW     = 30    # candles ahead used to derive each label
_TREND_RETURN_PCT = 1.5   # abs % move over label window → trending
_VOL_SPIKE_RATIO  = 2.0   # future ATR / dataset baseline ATR → high_volatility


def train(candles: list[dict]) -> None:
    from sklearn.ensemble import GradientBoostingClassifier
    from sklearn.model_selection import train_test_split
    from sklearn.preprocessing import StandardScaler
    from sklearn.pipeline import Pipeline
    from sklearn.metrics import classification_report

    X, y = _build_dataset(candles)

    if len(X) < 200:
        raise ValueError(
            f"Only {len(X)} labeled samples — need at least 200. "
            "Run the bot longer or backtest to collect more candle history."
        )

    distribution = {LABELS[i]: int((y == i).sum()) for i in range(len(LABELS))}
    logger.info("Training on %d samples. Label distribution: %s", len(X), distribution)

    n_classes = int((np.bincount(y) > 0).sum())
    if n_classes < 2:
        raise ValueError(
            f"All {len(X)} samples have the same label '{LABELS[int(y[0])]}'. "
            "The dataset needs at least two distinct regimes to train. "
            "Collect more candle history covering a trending or volatile period."
        )

    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=0.2, shuffle=False
    )

    model = Pipeline([
        ("scaler", StandardScaler()),
        ("clf", GradientBoostingClassifier(
            n_estimators=300,
            max_depth=4,
            learning_rate=0.05,
            subsample=0.8,
        )),
    ])
    model.fit(X_train, y_train)

    report = classification_report(
        y_test, model.predict(X_test),
        labels=list(range(len(LABELS))),
        target_names=LABELS,
        zero_division=0,
    )
    logger.info("Validation results:\n%s", report)

    MODEL_PATH.parent.mkdir(exist_ok=True)
    with open(MODEL_PATH, "wb") as f:
        pickle.dump({"model": model, "labels": LABELS}, f)
    logger.info("Model saved to %s", MODEL_PATH)


def load_model() -> dict | None:
    if not MODEL_PATH.exists():
        return None
    try:
        with open(MODEL_PATH, "rb") as f:
            return pickle.load(f)
    except Exception as e:
        logger.warning("Failed to load regime model: %s", e)
        return None


def _build_dataset(candles: list[dict]) -> tuple[np.ndarray, np.ndarray]:
    min_required = FEATURE_WINDOW + _LABEL_WINDOW
    if len(candles) < min_required:
        raise ValueError(f"Need at least {min_required} candles, got {len(candles)}")

    baseline_atr = float(np.mean([c["high"] - c["low"] for c in candles])) or 1e-9

    X, y = [], []
    for i in range(FEATURE_WINDOW, len(candles) - _LABEL_WINDOW):
        features = compute_features(candles[i - FEATURE_WINDOW: i])
        if features is None:
            continue
        label = _make_label(candles[i], candles[i: i + _LABEL_WINDOW], baseline_atr)
        X.append(features)
        y.append(label)

    return np.array(X, dtype=np.float32), np.array(y, dtype=np.int32)


def _make_label(current: dict, future: list[dict], baseline_atr: float) -> int:
    close_now = current["close"]
    future_return = (
        abs((future[-1]["close"] - close_now) / close_now * 100)
        if close_now else 0.0
    )
    future_atr = float(np.mean([c["high"] - c["low"] for c in future]))

    if future_atr > _VOL_SPIKE_RATIO * baseline_atr:
        return LABELS.index("high_volatility")
    if future_return > _TREND_RETURN_PCT:
        return LABELS.index("trending")
    return LABELS.index("ranging")
