import numpy as np
from typing import Optional

FEATURE_WINDOW = 60  # candles of history required per sample


def compute_features(candles: list[dict]) -> Optional[np.ndarray]:
    """
    Extract a fixed-length feature vector from the last FEATURE_WINDOW candles.
    Returns None if insufficient data.
    """
    if len(candles) < FEATURE_WINDOW:
        return None

    w = candles[-FEATURE_WINDOW:]
    closes = np.array([c["close"] for c in w], dtype=np.float64)
    highs  = np.array([c["high"]  for c in w], dtype=np.float64)
    lows   = np.array([c["low"]   for c in w], dtype=np.float64)
    opens  = np.array([c["open"]  for c in w], dtype=np.float64)

    price = closes[-1]
    trs = highs - lows  # True Range (no overnight gaps for 1-min bars)

    feats: list[float] = []

    # Returns at 5, 15, 30, 60 candles (%)
    for n in [5, 15, 30, 60]:
        ref = closes[-n]
        feats.append((price - ref) / ref * 100 if ref != 0 else 0.0)

    # ATR as % of price at 5, 15, 30, 60 candles
    for n in [5, 15, 30, 60]:
        feats.append(float(np.mean(trs[-n:])) / price * 100 if price > 0 else 0.0)

    # ATR spike ratio: 5-bar vs 30-bar (detects sudden volatility expansion)
    atr_30 = float(np.mean(trs[-30:]))
    feats.append(float(np.mean(trs[-5:])) / atr_30 if atr_30 > 0 else 1.0)

    # Bollinger Band width (20-period, 2σ, as % of mid-band)
    ma20  = float(np.mean(closes[-20:]))
    std20 = float(np.std(closes[-20:]))
    feats.append(2.0 * std20 / ma20 * 100 if ma20 > 0 else 0.0)

    # RSI (14-period)
    feats.append(_rsi(closes, 14))

    # Linear regression slope over last 20 candles, normalised (% per candle)
    x = np.arange(20, dtype=np.float64)
    slope = float(np.polyfit(x, closes[-20:], 1)[0])
    feats.append(slope / closes[-20] * 100 if closes[-20] != 0 else 0.0)

    # Average candle body ratio over last 5 candles (body / full range)
    bodies = np.abs(closes[-5:] - opens[-5:])
    feats.append(float(np.mean(bodies / np.maximum(trs[-5:], 1e-9))))

    # Directional streak over last 10 candles (positive = up run, negative = down run)
    feats.append(float(_streak(closes[-10:])))

    return np.array(feats, dtype=np.float32)


def _rsi(closes: np.ndarray, period: int) -> float:
    if len(closes) < period + 1:
        return 50.0
    deltas = np.diff(closes[-(period + 1):])
    gains  = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_loss = float(np.mean(losses))
    if avg_loss == 0.0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + float(np.mean(gains)) / avg_loss)


def _streak(closes: np.ndarray) -> int:
    if len(closes) < 2:
        return 0
    diffs = np.diff(closes)
    direction = int(np.sign(diffs[-1]))
    count = 0
    for d in reversed(diffs):
        if int(np.sign(d)) == direction:
            count += 1
        else:
            break
    return count * direction
