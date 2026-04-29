"""
Redis-backed cache with structured key namespacing and configurable TTLs.
Falls back gracefully when Redis is unavailable so the bot keeps running.
"""

import json
import logging
from typing import Any, Optional

import redis.asyncio as aioredis

logger = logging.getLogger(__name__)


class Cache:
    def __init__(self, host: str, port: int, db: int, password: str, ttl_config: dict):
        self._host = host
        self._port = port
        self._db = db
        self._password = password or None
        self._ttl = ttl_config  # dict from config redis.ttl
        self._client: Optional[aioredis.Redis] = None

    async def connect(self) -> None:
        self._client = aioredis.Redis(
            host=self._host,
            port=self._port,
            db=self._db,
            password=self._password,
            decode_responses=True,
            socket_connect_timeout=3,
            socket_timeout=3,
        )
        try:
            await self._client.ping()
            logger.info("Redis connected at %s:%s", self._host, self._port)
        except Exception as e:
            logger.warning("Redis unavailable (%s) — running without cache", e)

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    async def _set(self, key: str, value: Any, ttl: int) -> None:
        if not self._client:
            return
        try:
            await self._client.set(key, json.dumps(value), ex=ttl)
        except Exception as e:
            logger.debug("Cache write failed (%s): %s", key, e)

    async def _get(self, key: str) -> Optional[Any]:
        if not self._client:
            return None
        try:
            raw = await self._client.get(key)
            return json.loads(raw) if raw is not None else None
        except Exception as e:
            logger.debug("Cache read failed (%s): %s", key, e)
            return None

    async def _delete(self, key: str) -> None:
        if not self._client:
            return
        try:
            await self._client.delete(key)
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # Latest price
    # ------------------------------------------------------------------ #

    async def set_price(self, symbol: str, price: float) -> None:
        ttl = self._ttl.get("price_seconds", 10)
        await self._set(f"price:{symbol}", price, ttl)

    async def get_price(self, symbol: str) -> Optional[float]:
        v = await self._get(f"price:{symbol}")
        return float(v) if v is not None else None

    # ------------------------------------------------------------------ #
    # Grid state snapshot
    # ------------------------------------------------------------------ #

    async def set_grid_state(self, symbol: str, state: list) -> None:
        ttl = self._ttl.get("grid_state_seconds", 300)
        await self._set(f"grid:{symbol}", state, ttl)

    async def get_grid_state(self, symbol: str) -> Optional[list]:
        return await self._get(f"grid:{symbol}")

    async def invalidate_grid_state(self, symbol: str) -> None:
        await self._delete(f"grid:{symbol}")

    # ------------------------------------------------------------------ #
    # AI regime output
    # ------------------------------------------------------------------ #

    async def set_ai_regime(self, symbol: str, regime: str) -> None:
        ttl = self._ttl.get("ai_output_seconds", 60)
        await self._set(f"ai_regime:{symbol}", regime, ttl)

    async def get_ai_regime(self, symbol: str) -> Optional[str]:
        return await self._get(f"ai_regime:{symbol}")

    # ------------------------------------------------------------------ #
    # Risk flags
    # ------------------------------------------------------------------ #

    async def set_risk_flag(self, flag: str, value: Any) -> None:
        ttl = self._ttl.get("risk_flags_seconds", 3600)
        await self._set(f"risk:{flag}", value, ttl)

    async def get_risk_flag(self, flag: str) -> Optional[Any]:
        return await self._get(f"risk:{flag}")

    async def clear_risk_flag(self, flag: str) -> None:
        await self._delete(f"risk:{flag}")
