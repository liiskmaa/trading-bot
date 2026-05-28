"""
Grid state machine.

Responsibilities:
- Build and store the grid levels
- Assign client order IDs
- React to order fill events (place the paired order)
- Persist every state transition to the database
- Invalidate the Redis grid cache on every change

State transitions per level:
  PENDING   → BUY_OPEN    (buy order placed)
  BUY_OPEN  → BUY_FILLED  (fill event received)
  BUY_FILLED→ SELL_OPEN   (sell placed at level+1)
  SELL_OPEN → SELL_FILLED (fill event received)
  SELL_FILLED→ BUY_OPEN   (buy placed at level, cycle repeats)

  PENDING   → SELL_OPEN   (initial sell above market)
  SELL_OPEN → SELL_FILLED
  SELL_FILLED→ BUY_OPEN   (buy placed at level-1)
  BUY_OPEN  → BUY_FILLED
  BUY_FILLED→ SELL_OPEN   (cycle repeats)
"""

import asyncio
import logging
import time
from typing import Optional

from .calculator import GridLevel, compute_levels, round_price, round_qty

logger = logging.getLogger(__name__)


class GridManager:
    def __init__(
        self,
        symbol: str,
        range_percent: float,
        num_levels: int,
        order_size_usdt: float,
        rebuild_threshold_percent: float,
        price_precision: int,
        qty_precision: int,
    ):
        self._symbol = symbol
        self._range_pct = range_percent
        self._num_levels = num_levels
        self._order_size = order_size_usdt
        self._rebuild_threshold = rebuild_threshold_percent
        self._price_prec = price_precision
        self._qty_prec = qty_precision

        self._levels: list[GridLevel] = []
        self._reference_price: float = 0.0  # price at last grid build

        # Injected dependencies
        self._repo = None
        self._cache = None
        self._executor = None   # execution.orders.OrderExecutor

    def inject(self, repo, cache, executor) -> None:
        self._repo = repo
        self._cache = cache
        self._executor = executor

    # ------------------------------------------------------------------ #
    # Grid lifecycle
    # ------------------------------------------------------------------ #

    async def build(self, current_price: float, reason: str = "initial") -> None:
        """Create a fresh grid and place all initial orders."""
        prev_ref = self._reference_price
        logger.info("Building grid around %.2f USDT (reason=%s)", current_price, reason)
        self._levels = compute_levels(
            current_price, self._range_pct, self._num_levels, self._order_size
        )
        self._reference_price = current_price

        # Persist levels and place orders
        await self._repo.clear_grid_levels(self._symbol)
        for level in self._levels:
            level.price = round_price(level.price, self._price_prec)
            level.quantity = round_qty(level.quantity, self._qty_prec)
            level.client_order_id = self._make_client_id(level)
            await self._persist_level(level)
            await self._place_initial_order(level)

        await self._cache.invalidate_grid_state(self._symbol)
        await self._cache.set_grid_state(self._symbol, self._serialise())

        lower = self._levels[0].price
        upper = self._levels[-1].price
        logger.info(
            "Grid built: %d levels, lower=%.2f upper=%.2f",
            self._num_levels, lower, upper,
        )
        await self._repo.log_event(
            "GRID_BUILD",
            f"reason={reason} price={current_price:.2f} levels={self._num_levels} "
            f"lower={lower:.2f} upper={upper:.2f} prev_ref={prev_ref:.2f}",
            data={
                "reason": reason,
                "price": current_price,
                "prev_reference_price": prev_ref,
                "lower": lower,
                "upper": upper,
                "levels": self._num_levels,
            },
        )

    async def restore(self) -> bool:
        """Reload grid state from the database after a restart."""
        rows = await self._repo.get_grid_levels(self._symbol)
        if not rows:
            return False
        self._levels = [
            GridLevel(
                idx=r["level_idx"],
                price=r["price"],
                side=r["side"],
                status=r["status"],
                client_order_id=r.get("client_order_id"),
                quantity=r.get("quantity") or 0.0,
            )
            for r in rows
        ]
        if self._levels:
            # Recompute reference price as mid of grid
            self._reference_price = (
                self._levels[0].price + self._levels[-1].price
            ) / 2
        logger.info("Grid restored from DB: %d levels", len(self._levels))
        return True

    async def cancel_all(self) -> None:
        """Cancel every open order — used for emergency exits and shutdowns."""
        for level in self._levels:
            if level.status in ("BUY_OPEN", "SELL_OPEN") and level.client_order_id:
                try:
                    await self._executor.cancel(level.client_order_id)
                except Exception as e:
                    logger.error("Cancel failed for %s: %s", level.client_order_id, e)
                level.status = "DISABLED"
                await self._persist_level(level)
        await self._cache.invalidate_grid_state(self._symbol)

    def needs_rebuild(self, current_price: float) -> bool:
        """True when price has drifted far enough that the grid is mostly useless."""
        if not self._reference_price:
            return False
        drift = abs(current_price - self._reference_price) / self._reference_price * 100
        return drift > self._rebuild_threshold

    # ------------------------------------------------------------------ #
    # Fill event handler
    # ------------------------------------------------------------------ #

    async def on_order_filled(self, client_order_id: str, fill_price: float, fill_qty: float) -> None:
        level = self._find_by_client_id(client_order_id)
        if level is None:
            return

        if level.status == "BUY_OPEN":
            logger.info("BUY filled at level %d price=%.2f", level.idx, fill_price)
            level.status = "BUY_FILLED"
            await self._persist_level(level)
            await self._place_sell_above(level)

        elif level.status == "SELL_OPEN":
            logger.info("SELL filled at level %d price=%.2f", level.idx, fill_price)
            level.status = "SELL_FILLED"
            await self._persist_level(level)
            await self._place_buy_below(level)

        await self._cache.invalidate_grid_state(self._symbol)

    # ------------------------------------------------------------------ #
    # Order placement helpers
    # ------------------------------------------------------------------ #

    async def _place_initial_order(self, level: GridLevel) -> None:
        if level.side == "BUY":
            await self._do_place_buy(level)
        else:
            await self._do_place_sell(level)

    async def _place_sell_above(self, filled_buy: GridLevel) -> None:
        next_idx = filled_buy.idx + 1
        if next_idx >= len(self._levels):
            logger.debug("No level above %d for sell — grid ceiling reached", filled_buy.idx)
            return
        target = self._levels[next_idx]
        await self._do_place_sell(target)

    async def _place_buy_below(self, filled_sell: GridLevel) -> None:
        prev_idx = filled_sell.idx - 1
        if prev_idx < 0:
            logger.debug("No level below %d for buy — grid floor reached", filled_sell.idx)
            return
        target = self._levels[prev_idx]
        await self._do_place_buy(target)

    async def _do_place_buy(self, level: GridLevel) -> None:
        cid = self._make_client_id(level, "B")
        level.client_order_id = cid
        level.status = "BUY_OPEN"
        level.side = "BUY"
        await self._persist_level(level)
        await self._executor.place_buy(
            price=level.price,
            quantity=level.quantity,
            client_order_id=cid,
            grid_level_idx=level.idx,
        )

    async def _do_place_sell(self, level: GridLevel) -> None:
        cid = self._make_client_id(level, "S")
        level.client_order_id = cid
        level.status = "SELL_OPEN"
        level.side = "SELL"
        await self._persist_level(level)
        await self._executor.place_sell(
            price=level.price,
            quantity=level.quantity,
            client_order_id=cid,
            grid_level_idx=level.idx,
        )

    # ------------------------------------------------------------------ #
    # Utilities
    # ------------------------------------------------------------------ #

    def _make_client_id(self, level: GridLevel, side_char: str = "") -> str:
        ts_suffix = str(int(time.time() * 1000))[-7:]
        sym = self._symbol[:3]
        return f"G{sym}{level.idx:03d}{side_char}{ts_suffix}"

    def _find_by_client_id(self, cid: str) -> Optional[GridLevel]:
        for level in self._levels:
            if level.client_order_id == cid:
                return level
        return None

    async def _persist_level(self, level: GridLevel) -> None:
        await self._repo.upsert_grid_level(
            {
                "symbol": self._symbol,
                "level_idx": level.idx,
                "price": level.price,
                "quantity": level.quantity,
                "side": level.side,
                "status": level.status,
                "client_order_id": level.client_order_id,
            }
        )

    def _serialise(self) -> list[dict]:
        return [
            {
                "idx": lv.idx,
                "price": lv.price,
                "side": lv.side,
                "status": lv.status,
                "client_order_id": lv.client_order_id,
            }
            for lv in self._levels
        ]

    @property
    def levels(self) -> list[GridLevel]:
        return self._levels
