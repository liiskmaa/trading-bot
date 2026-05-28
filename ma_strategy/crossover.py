"""
Moving-average crossover strategy.

Buys all active capital when the fast MA crosses above the slow MA (golden
cross) and sells everything when it crosses below (death cross).  Intended
as an alternative to the grid strategy for trending markets.

Runs as a periodic check (check_interval_hours) rather than on every tick.
"""

import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

_FEE_RATE = 0.001
# Client-order-ID prefix distinguishes MA orders from grid orders ("G…")
_ID_PREFIX = "MA"


class MACrossover:
    def __init__(
        self,
        repo,
        executor,
        symbol: str,
        fast_period: int = 20,
        slow_period: int = 50,
        capital_usdt: float = 324.0,
        mode: str = "paper",
    ):
        self._repo = repo
        self._executor = executor
        self._symbol = symbol
        self._fast = fast_period
        self._slow = slow_period
        self._capital_usdt = capital_usdt
        self._mode = mode

        self._position: str = "OUT"   # "OUT" | "IN"
        self._entry_price: float = 0.0
        self._qty: float = 0.0

    # ------------------------------------------------------------------ #
    # Startup restore
    # ------------------------------------------------------------------ #

    async def restore(self) -> None:
        """Reload position state from the last MA_POSITION event in the DB."""
        event = await self._repo.get_last_event("MA_POSITION")
        if not event:
            return
        data = event.get("data") or {}
        self._position = data.get("position", "OUT")
        self._entry_price = float(data.get("price", 0.0))
        self._qty = float(data.get("qty", 0.0))
        logger.info(
            "MA strategy restored: position=%s entry=%.2f qty=%.5f",
            self._position, self._entry_price, self._qty,
        )

    # ------------------------------------------------------------------ #
    # Main check (called periodically from bot heartbeat)
    # ------------------------------------------------------------------ #

    async def check(self, current_price: float) -> None:
        closes = await self._repo.get_daily_closes(self._symbol, self._slow)
        if len(closes) < self._slow:
            logger.info(
                "MA strategy: not enough daily history (%d/%d days) — waiting",
                len(closes), self._slow,
            )
            return

        fast_ma = sum(closes[-self._fast :]) / self._fast
        slow_ma = sum(closes) / self._slow

        logger.info(
            "MA check: price=%.2f fast%d=%.2f slow%d=%.2f position=%s",
            current_price, self._fast, fast_ma, self._slow, slow_ma, self._position,
        )

        if fast_ma > slow_ma and self._position == "OUT":
            await self._enter(current_price)
        elif fast_ma < slow_ma and self._position == "IN":
            await self._exit(current_price)

    # ------------------------------------------------------------------ #
    # Signal handlers
    # ------------------------------------------------------------------ #

    async def _enter(self, price: float) -> None:
        qty = round(self._capital_usdt / price * (1 - _FEE_RATE), 5)
        client_id = f"{_ID_PREFIX}{self._symbol[:3]}{int(time.time()) % 10_000_000:07d}B"
        result = await self._executor.place_buy(price, qty, client_id, grid_level_idx=-1)
        if result is not None or self._mode == "dry_run":
            self._position = "IN"
            self._entry_price = price
            self._qty = qty
            await self._repo.log_event(
                "MA_POSITION",
                f"ENTER buy qty={qty:.5f} @ {price:.2f}",
                data={"position": "IN", "price": price, "qty": qty},
            )
            logger.info("MA strategy: ENTER — bought %.5f BTC @ %.2f", qty, price)

    async def _exit(self, price: float) -> None:
        qty = self._qty
        if qty <= 0:
            logger.warning("MA strategy: EXIT signal but qty=0, skipping")
            self._position = "OUT"
            return
        client_id = f"{_ID_PREFIX}{self._symbol[:3]}{int(time.time()) % 10_000_000:07d}S"
        result = await self._executor.place_sell(price, qty, client_id, grid_level_idx=-1)
        if result is not None or self._mode == "dry_run":
            pnl = (price - self._entry_price) * qty
            self._position = "OUT"
            self._qty = 0.0
            await self._repo.log_event(
                "MA_POSITION",
                f"EXIT sell qty={qty:.5f} @ {price:.2f} pnl={pnl:.2f}",
                data={"position": "OUT", "price": price, "pnl": pnl},
            )
            logger.info(
                "MA strategy: EXIT — sold %.5f BTC @ %.2f  pnl=%.2f USDT",
                qty, price, pnl,
            )

    # ------------------------------------------------------------------ #
    # Properties
    # ------------------------------------------------------------------ #

    @property
    def position(self) -> str:
        return self._position

    @property
    def entry_price(self) -> float:
        return self._entry_price
