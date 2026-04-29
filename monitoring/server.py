"""
Lightweight HTTP monitoring server.

GET /status  → JSON snapshot of bot state
GET /metrics → Prometheus-compatible text metrics

Intentionally minimal — uses only stdlib asyncio HTTP server
so there are no extra dependencies beyond prometheus-client.
"""

import asyncio
import json
import logging
import time
from typing import Optional

from prometheus_client import (
    Counter, Gauge, generate_latest, CONTENT_TYPE_LATEST
)

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------ #
# Prometheus metrics
# ------------------------------------------------------------------ #

TRADES_TOTAL = Counter(
    "gridbot_trades_total", "Total number of filled orders", ["side"]
)
DRAWDOWN_GAUGE = Gauge(
    "gridbot_drawdown_percent", "Current drawdown as % of peak capital"
)
OPEN_ORDERS_GAUGE = Gauge(
    "gridbot_open_orders", "Number of open grid orders"
)
UPTIME_GAUGE = Gauge(
    "gridbot_uptime_seconds", "Bot uptime in seconds"
)
LAST_PRICE_GAUGE = Gauge(
    "gridbot_last_price_usdt", "Last observed BTC price in USDT"
)
REGIME_GAUGE = Gauge(
    "gridbot_regime_ranging", "1 if AI regime is 'ranging', else 0"
)
PORTFOLIO_GAUGE = Gauge(
    "gridbot_portfolio_usdt", "Estimated portfolio value in USDT"
)


class MonitoringServer:
    def __init__(self, host: str, port: int):
        self._host = host
        self._port = port
        self._start_time = time.time()
        self._server: Optional[asyncio.Server] = None

        # State injected by the bot on each tick
        self._state_snapshot: dict = {}

    def update(self, snapshot: dict) -> None:
        """Called by the bot on each price tick to push metrics."""
        self._state_snapshot = snapshot
        DRAWDOWN_GAUGE.set(snapshot.get("drawdown_percent", 0))
        OPEN_ORDERS_GAUGE.set(snapshot.get("open_orders", 0))
        LAST_PRICE_GAUGE.set(snapshot.get("last_price", 0))
        PORTFOLIO_GAUGE.set(snapshot.get("portfolio_value", 0))
        regime = snapshot.get("ai_regime", "")
        REGIME_GAUGE.set(1 if regime == "ranging" else 0)

    def record_trade(self, side: str) -> None:
        TRADES_TOTAL.labels(side=side).inc()

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle, self._host, self._port
        )
        logger.info("Monitoring server on %s:%s", self._host, self._port)
        async with self._server:
            await self._server.serve_forever()

    async def stop(self) -> None:
        if self._server:
            self._server.close()

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            data = await reader.read(1024)
            request_line = data.decode(errors="ignore").split("\r\n")[0]
            path = request_line.split(" ")[1] if len(request_line.split(" ")) > 1 else "/"

            if path.startswith("/metrics"):
                body = generate_latest()
                content_type = CONTENT_TYPE_LATEST
                writer.write(
                    f"HTTP/1.1 200 OK\r\nContent-Type: {content_type}\r\n"
                    f"Content-Length: {len(body)}\r\n\r\n".encode()
                )
                writer.write(body)

            elif path.startswith("/status"):
                UPTIME_GAUGE.set(time.time() - self._start_time)
                body = json.dumps(
                    {**self._state_snapshot, "uptime_seconds": time.time() - self._start_time},
                    indent=2,
                ).encode()
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                    + f"Content-Length: {len(body)}\r\n\r\n".encode()
                )
                writer.write(body)

            else:
                writer.write(b"HTTP/1.1 404 Not Found\r\n\r\n")

            await writer.drain()
        except Exception as e:
            logger.debug("Monitoring request error: %s", e)
        finally:
            writer.close()
