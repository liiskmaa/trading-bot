"""
Market regime classifier via Ollama.

Calls the local Ollama API at most once per `call_interval_seconds`.
Caches the last result in Redis for `cache_ttl_seconds`.
Always defaults to a safe fallback when Ollama is unreachable.

Valid regimes:
  "ranging"        — sideways price action, grid trading allowed
  "trending"       — directional momentum, grid trading paused
  "high_volatility"— extreme swings, grid trading paused
"""

import asyncio
import logging
import time
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

_VALID_REGIMES = {"ranging", "trending", "high_volatility"}

_PROMPT_TEMPLATE = """You are a market regime classifier for BTC/USDT.

Market data (last reading):
  Current price     : {price:.2f} USDT
  1h price change   : {change_1h:+.2f}%
  4h price change   : {change_4h:+.2f}%
  24h price change  : {change_24h:+.2f}%
  1h volatility ATR : {volatility:.3f}%

Classify the current market regime as exactly one of:
  ranging          — price oscillating in a narrow band, no clear trend
  trending         — sustained directional movement (up or down)
  high_volatility  — sharp, sudden moves that destabilise range patterns

Reply with a single word only. No explanation."""


class MarketClassifier:
    def __init__(
        self,
        model: str,
        base_url: str,
        cache_ttl_seconds: int,
        call_interval_seconds: int,
        fallback_regime: str = "high_volatility",
    ):
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._cache_ttl = cache_ttl_seconds
        self._call_interval = call_interval_seconds
        self._fallback = fallback_regime
        self._last_call: float = 0.0
        self._cache = None   # Cache instance injected after init
        self._symbol: str = ""

    def inject(self, cache, symbol: str) -> None:
        self._cache = cache
        self._symbol = symbol

    async def classify(
        self,
        price: float,
        change_1h: float = 0.0,
        change_4h: float = 0.0,
        change_24h: float = 0.0,
        volatility: float = 0.0,
    ) -> str:
        # 1. Try Redis cache first
        if self._cache:
            cached = await self._cache.get_ai_regime(self._symbol)
            if cached and cached in _VALID_REGIMES:
                return cached

        # 2. Rate-limit API calls
        if time.time() - self._last_call < self._call_interval:
            return self._fallback

        regime = await self._call_ollama(price, change_1h, change_4h, change_24h, volatility)
        self._last_call = time.time()

        if self._cache:
            await self._cache.set_ai_regime(self._symbol, regime)

        return regime

    async def _call_ollama(
        self,
        price: float,
        change_1h: float,
        change_4h: float,
        change_24h: float,
        volatility: float,
    ) -> str:
        prompt = _PROMPT_TEMPLATE.format(
            price=price,
            change_1h=change_1h,
            change_4h=change_4h,
            change_24h=change_24h,
            volatility=volatility,
        )
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    f"{self._base_url}/api/generate",
                    json={
                        "model": self._model,
                        "prompt": prompt,
                        "stream": False,
                        "options": {"temperature": 0.0, "num_predict": 10},
                    },
                )
                resp.raise_for_status()
                raw = resp.json().get("response", "").strip().lower()
                regime = raw.split()[0] if raw else ""
                if regime not in _VALID_REGIMES:
                    logger.warning("Ollama returned unexpected regime '%s' — using fallback", regime)
                    return self._fallback
                logger.info("AI regime: %s", regime)
                return regime
        except Exception as e:
            logger.warning("Ollama unavailable (%s) — using fallback '%s'", e, self._fallback)
            return self._fallback
