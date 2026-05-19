"""
Binance WebSocket streams.

Opens two concurrent streams:
  1. {symbol}@miniTicker  — live best price, 24h stats
  2. User data stream     — order execution reports

Reconnects automatically with exponential backoff.
Publishes parsed events to asyncio queues consumed by other modules.
"""

import asyncio
import json
import logging
import time
from typing import Callable, Optional

import websockets
from websockets.exceptions import ConnectionClosed

logger = logging.getLogger(__name__)

_LIVE_WS_BASE = "wss://stream.binance.com:9443/ws"
_TEST_WS_BASE = "wss://testnet.binance.vision/ws"


class BinanceWebSocket:
    def __init__(
        self,
        symbol: str,
        testnet: bool,
        on_price: Callable[[float], None],
        on_execution: Callable[[dict], None],
        reconnect_delay: float = 5.0,
        max_reconnect_delay: float = 60.0,
    ):
        self._symbol = symbol.lower()
        self._base = _TEST_WS_BASE if testnet else _LIVE_WS_BASE
        self._on_price = on_price
        self._on_execution = on_execution
        self._reconnect_delay = reconnect_delay
        self._max_reconnect_delay = max_reconnect_delay
        self._listen_key: Optional[str] = None
        self._running = False
        self._tasks: list[asyncio.Task] = []
        self._user_data_task: Optional[asyncio.Task] = None

    def set_listen_key(self, key: str) -> None:
        self._listen_key = key

    async def start(self) -> None:
        self._running = True
        self._tasks = [
            asyncio.create_task(self._ticker_loop(), name="ws-ticker"),
        ]
        if self._listen_key:
            self._user_data_task = asyncio.create_task(
                self._user_data_loop(), name="ws-user-data"
            )
            self._tasks.append(self._user_data_task)

    async def stop(self) -> None:
        self._running = False
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    # ------------------------------------------------------------------ #
    # Market ticker stream
    # ------------------------------------------------------------------ #

    async def _ticker_loop(self) -> None:
        url = f"{self._base}/{self._symbol}@miniTicker"
        delay = self._reconnect_delay
        while self._running:
            try:
                async with websockets.connect(url, ping_interval=20) as ws:
                    logger.info("Ticker WS connected: %s", url)
                    delay = self._reconnect_delay
                    async for raw in ws:
                        if not self._running:
                            return
                        try:
                            msg = json.loads(raw)
                            price = float(msg.get("c", 0))
                            if price > 0:
                                self._on_price(price)
                        except Exception as e:
                            logger.debug("Ticker parse error: %s", e)
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.warning("Ticker WS error: %s — reconnecting in %.0fs", e, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, self._max_reconnect_delay)

    # ------------------------------------------------------------------ #
    # User data stream (order execution reports)
    # ------------------------------------------------------------------ #

    async def _user_data_loop(self) -> None:
        delay = self._reconnect_delay
        while self._running:
            if not self._listen_key:
                await asyncio.sleep(delay)
                continue
            url = f"{self._base}/{self._listen_key}"
            try:
                async with websockets.connect(url, ping_interval=20) as ws:
                    logger.info("User-data WS connected")
                    delay = self._reconnect_delay
                    async for raw in ws:
                        if not self._running:
                            return
                        try:
                            msg = json.loads(raw)
                            if msg.get("e") == "executionReport":
                                self._on_execution(msg)
                        except Exception as e:
                            logger.debug("User-data parse error: %s", e)
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.warning("User-data WS error: %s — reconnecting in %.0fs", e, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, self._max_reconnect_delay)

    def update_listen_key(self, key: str) -> None:
        """Hot-swap the listen key and force an immediate reconnect."""
        self._listen_key = key
        if self._user_data_task and not self._user_data_task.done():
            self._user_data_task.cancel()
