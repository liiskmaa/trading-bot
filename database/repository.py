"""
Thin async data-access layer over SQLite (via aiosqlite).
Every public method is idempotent so callers can retry safely.
"""

import json
import time
import logging
from pathlib import Path
from typing import Optional

import aiosqlite

from .schema import init_schema

logger = logging.getLogger(__name__)


class Repository:
    def __init__(self, db_path: str):
        self._path = db_path
        self._db: Optional[aiosqlite.Connection] = None

    async def open(self) -> None:
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self._path)
        self._db.row_factory = aiosqlite.Row
        await init_schema(self._db)
        logger.info("Database opened: %s", self._path)

    async def close(self) -> None:
        if self._db:
            await self._db.close()

    # ------------------------------------------------------------------ #
    # Orders
    # ------------------------------------------------------------------ #

    async def upsert_order(self, order: dict) -> None:
        now = time.time()
        await self._db.execute(
            """
            INSERT INTO orders
                (client_order_id, exchange_order_id, symbol, side, order_type,
                 price, quantity, executed_qty, status, grid_level_idx,
                 created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(client_order_id) DO UPDATE SET
                exchange_order_id = excluded.exchange_order_id,
                status            = excluded.status,
                executed_qty      = excluded.executed_qty,
                updated_at        = excluded.updated_at
            """,
            (
                order["client_order_id"],
                order.get("exchange_order_id"),
                order["symbol"],
                order["side"],
                order.get("order_type", "LIMIT"),
                order["price"],
                order["quantity"],
                order.get("executed_qty", 0.0),
                order.get("status", "NEW"),
                order.get("grid_level_idx"),
                order.get("created_at", now),
                now,
            ),
        )
        await self._db.commit()

    async def get_order(self, client_order_id: str) -> Optional[dict]:
        async with self._db.execute(
            "SELECT * FROM orders WHERE client_order_id = ?", (client_order_id,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def get_open_orders(self, symbol: str) -> list[dict]:
        async with self._db.execute(
            "SELECT * FROM orders WHERE symbol = ? AND status IN ('OPEN','PARTIALLY_FILLED','PAPER_OPEN')",
            (symbol,),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def mark_order_filled(
        self, client_order_id: str, executed_qty: float, status: str = "FILLED"
    ) -> None:
        await self._db.execute(
            "UPDATE orders SET status=?, executed_qty=?, updated_at=? WHERE client_order_id=?",
            (status, executed_qty, time.time(), client_order_id),
        )
        await self._db.commit()

    # ------------------------------------------------------------------ #
    # Trades
    # ------------------------------------------------------------------ #

    async def insert_trade(self, trade: dict) -> None:
        await self._db.execute(
            """
            INSERT OR IGNORE INTO trades
                (exchange_trade_id, client_order_id, symbol, side,
                 price, quantity, fee, fee_asset, realized_pnl, timestamp)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (
                trade.get("exchange_trade_id"),
                trade["client_order_id"],
                trade["symbol"],
                trade["side"],
                trade["price"],
                trade["quantity"],
                trade.get("fee", 0.0),
                trade.get("fee_asset", "USDT"),
                trade.get("realized_pnl", 0.0),
                trade.get("timestamp", time.time()),
            ),
        )
        await self._db.commit()

    async def get_recent_trades(self, symbol: str, limit: int = 50) -> list[dict]:
        async with self._db.execute(
            "SELECT * FROM trades WHERE symbol=? ORDER BY timestamp DESC LIMIT ?",
            (symbol, limit),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    # ------------------------------------------------------------------ #
    # Grid levels
    # ------------------------------------------------------------------ #

    async def upsert_grid_level(self, level: dict) -> None:
        now = time.time()
        await self._db.execute(
            """
            INSERT INTO grid_levels
                (symbol, level_idx, price, side, status, client_order_id,
                 created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(symbol, level_idx) DO UPDATE SET
                price           = excluded.price,
                side            = excluded.side,
                status          = excluded.status,
                client_order_id = excluded.client_order_id,
                updated_at      = excluded.updated_at
            """,
            (
                level["symbol"],
                level["level_idx"],
                level["price"],
                level["side"],
                level.get("status", "PENDING"),
                level.get("client_order_id"),
                level.get("created_at", now),
                now,
            ),
        )
        await self._db.commit()

    async def get_grid_levels(self, symbol: str) -> list[dict]:
        async with self._db.execute(
            "SELECT * FROM grid_levels WHERE symbol=? ORDER BY level_idx",
            (symbol,),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def clear_grid_levels(self, symbol: str) -> None:
        await self._db.execute(
            "DELETE FROM grid_levels WHERE symbol=?", (symbol,)
        )
        await self._db.commit()

    # ------------------------------------------------------------------ #
    # Balances
    # ------------------------------------------------------------------ #

    async def upsert_balance(self, asset: str, free: float, locked: float) -> None:
        await self._db.execute(
            """
            INSERT INTO balances (asset, free, locked, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(asset) DO UPDATE SET
                free=excluded.free, locked=excluded.locked, updated_at=excluded.updated_at
            """,
            (asset, free, locked, time.time()),
        )
        await self._db.commit()

    async def get_balance(self, asset: str) -> Optional[dict]:
        async with self._db.execute(
            "SELECT * FROM balances WHERE asset=?", (asset,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    # ------------------------------------------------------------------ #
    # Candles
    # ------------------------------------------------------------------ #

    async def upsert_candle(self, candle: dict) -> None:
        await self._db.execute(
            """
            INSERT OR REPLACE INTO candles
                (symbol, interval, open_time, open, high, low, close, volume)
            VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                candle["symbol"],
                candle["interval"],
                candle["open_time"],
                candle["open"],
                candle["high"],
                candle["low"],
                candle["close"],
                candle["volume"],
            ),
        )
        await self._db.commit()

    async def get_candles(
        self, symbol: str, interval: str, limit: int = 500
    ) -> list[dict]:
        async with self._db.execute(
            """SELECT * FROM candles WHERE symbol=? AND interval=?
               ORDER BY open_time DESC LIMIT ?""",
            (symbol, interval, limit),
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in reversed(rows)]

    # ------------------------------------------------------------------ #
    # System events
    # ------------------------------------------------------------------ #

    async def log_event(
        self,
        event_type: str,
        message: str,
        severity: str = "INFO",
        data: Optional[dict] = None,
    ) -> None:
        await self._db.execute(
            "INSERT INTO system_events (event_type, severity, message, data, timestamp) VALUES (?,?,?,?,?)",
            (
                event_type,
                severity,
                message,
                json.dumps(data) if data else None,
                time.time(),
            ),
        )
        await self._db.commit()
