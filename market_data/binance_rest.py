"""
Binance REST API wrapper.
Handles authentication, request signing, and basic retry logic.
"""

import hashlib
import hmac
import logging
import time
from typing import Any, Optional
from urllib.parse import urlencode

import httpx

logger = logging.getLogger(__name__)

_LIVE_BASE = "https://api.binance.com"
_TEST_BASE = "https://testnet.binance.vision"


class BinanceRest:
    def __init__(
        self,
        api_key: str,
        api_secret: str,
        testnet: bool = True,
        retries: int = 3,
        retry_delay: float = 2.0,
    ):
        self._key = api_key
        self._secret = api_secret
        self._base = _TEST_BASE if testnet else _LIVE_BASE
        self._retries = retries
        self._retry_delay = retry_delay
        self._client: Optional[httpx.AsyncClient] = None

    async def open(self) -> None:
        self._client = httpx.AsyncClient(
            base_url=self._base,
            timeout=10.0,
            headers={"X-MBX-APIKEY": self._key},
        )

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()

    # ------------------------------------------------------------------ #
    # Signature
    # ------------------------------------------------------------------ #

    def _sign(self, params: dict) -> str:
        qs = urlencode(params)
        return hmac.new(
            self._secret.encode(), qs.encode(), hashlib.sha256
        ).hexdigest()

    def _signed_params(self, params: dict) -> dict:
        params["timestamp"] = int(time.time() * 1000)
        params["signature"] = self._sign(params)
        return params

    # ------------------------------------------------------------------ #
    # HTTP helpers
    # ------------------------------------------------------------------ #

    async def _get(self, path: str, params: dict | None = None) -> Any:
        for attempt in range(self._retries):
            try:
                r = await self._client.get(path, params=params or {})
                r.raise_for_status()
                return r.json()
            except Exception as e:
                logger.warning("GET %s attempt %d failed: %s", path, attempt + 1, e)
                if attempt < self._retries - 1:
                    await _sleep(self._retry_delay * (attempt + 1))
        raise RuntimeError(f"GET {path} failed after {self._retries} attempts")

    async def _signed_get(self, path: str, params: dict | None = None) -> Any:
        return await self._get(path, self._signed_params(params or {}))

    async def _signed_post(self, path: str, params: dict) -> Any:
        for attempt in range(self._retries):
            try:
                signed = self._signed_params(params)
                r = await self._client.post(path, data=signed)
                r.raise_for_status()
                return r.json()
            except Exception as e:
                logger.warning("POST %s attempt %d failed: %s", path, attempt + 1, e)
                if attempt < self._retries - 1:
                    await _sleep(self._retry_delay * (attempt + 1))
        raise RuntimeError(f"POST {path} failed after {self._retries} attempts")

    async def _signed_put(self, path: str, params: dict) -> Any:
        for attempt in range(self._retries):
            try:
                signed = self._signed_params(params)
                r = await self._client.put(path, data=signed)
                r.raise_for_status()
                return r.json()
            except Exception as e:
                logger.warning("PUT %s attempt %d failed: %s", path, attempt + 1, e)
                if attempt < self._retries - 1:
                    await _sleep(self._retry_delay * (attempt + 1))
        raise RuntimeError(f"PUT {path} failed after {self._retries} attempts")

    async def _signed_delete(self, path: str, params: dict) -> Any:
        for attempt in range(self._retries):
            try:
                signed = self._signed_params(params)
                r = await self._client.delete(path, params=signed)
                r.raise_for_status()
                return r.json()
            except Exception as e:
                logger.warning("DELETE %s attempt %d failed: %s", path, attempt + 1, e)
                if attempt < self._retries - 1:
                    await _sleep(self._retry_delay * (attempt + 1))
        raise RuntimeError(f"DELETE {path} failed after {self._retries} attempts")

    # ------------------------------------------------------------------ #
    # Public endpoints
    # ------------------------------------------------------------------ #

    async def get_price(self, symbol: str) -> float:
        data = await self._get("/api/v3/ticker/price", {"symbol": symbol})
        return float(data["price"])

    async def get_ticker_24h(self, symbol: str) -> dict:
        return await self._get("/api/v3/ticker/24hr", {"symbol": symbol})

    async def get_exchange_info(self, symbol: str) -> dict:
        data = await self._get("/api/v3/exchangeInfo", {"symbol": symbol})
        for s in data.get("symbols", []):
            if s["symbol"] == symbol:
                return s
        raise ValueError(f"Symbol {symbol} not found in exchange info")

    async def get_klines(
        self, symbol: str, interval: str = "1m", limit: int = 500
    ) -> list[list]:
        """Returns list of OHLCV rows: [open_time, o, h, l, c, volume, ...]"""
        return await self._get(
            "/api/v3/klines",
            {"symbol": symbol, "interval": interval, "limit": limit},
        )

    # ------------------------------------------------------------------ #
    # Authenticated endpoints
    # ------------------------------------------------------------------ #

    async def get_account(self) -> dict:
        return await self._signed_get("/api/v3/account")

    async def get_open_orders(self, symbol: str) -> list[dict]:
        return await self._signed_get("/api/v3/openOrders", {"symbol": symbol})

    async def place_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        price: float,
        client_order_id: str,
        qty_precision: int = 5,
        price_precision: int = 2,
    ) -> dict:
        params = {
            "symbol": symbol,
            "side": side,
            "type": "LIMIT",
            "timeInForce": "GTC",
            "quantity": f"{quantity:.{qty_precision}f}",
            "price": f"{price:.{price_precision}f}",
            "newClientOrderId": client_order_id,
        }
        result = await self._signed_post("/api/v3/order", params)
        logger.info(
            "Order placed: %s %s qty=%s price=%s id=%s status=%s",
            side, symbol, quantity, price, client_order_id, result.get("status"),
        )
        return result

    async def cancel_order(self, symbol: str, client_order_id: str) -> dict:
        result = await self._signed_delete(
            "/api/v3/order",
            {"symbol": symbol, "origClientOrderId": client_order_id},
        )
        logger.info("Order cancelled: %s", client_order_id)
        return result

    async def cancel_all_orders(self, symbol: str) -> list[dict]:
        result = await self._signed_delete(
            "/api/v3/openOrders", {"symbol": symbol}
        )
        logger.warning("All orders cancelled for %s", symbol)
        return result

    # ------------------------------------------------------------------ #
    # User data stream
    # ------------------------------------------------------------------ #

    async def create_listen_key(self) -> str:
        data = await self._signed_post("/api/v3/userDataStream", {})
        return data["listenKey"]

    async def keepalive_listen_key(self, listen_key: str) -> None:
        await self._signed_put("/api/v3/userDataStream", {"listenKey": listen_key})

    async def delete_listen_key(self, listen_key: str) -> None:
        params = self._signed_params({"listenKey": listen_key})
        await self._client.delete("/api/v3/userDataStream", params=params)


async def _sleep(seconds: float) -> None:
    import asyncio
    await asyncio.sleep(seconds)
