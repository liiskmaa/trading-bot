"""
Order execution layer.

Abstracts live, paper, and dry_run modes behind a single interface.
The GridManager calls this; it never touches BinanceRest directly.

Live mode  — calls Binance REST API.
Paper mode — records virtual orders; fills are simulated by the price feed.
Dry-run    — logs intended orders and does nothing.
"""

import asyncio
import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

# Binance BTC/USDT maker fee
_FEE_RATE = 0.001


class OrderExecutor:
    def __init__(
        self,
        symbol: str,
        mode: str,  # "live" | "paper" | "dry_run"
        rest=None,  # BinanceRest instance (required for live)
        repo=None,
        price_precision: int = 2,
        qty_precision: int = 5,
    ):
        self._symbol = symbol
        self._mode = mode
        self._rest = rest
        self._repo = repo
        self._price_prec = price_precision
        self._qty_prec = qty_precision

        # Paper trading virtual wallet (USDT, BTC)
        self._paper_balances: dict[str, float] = {}

        # Callback set by GridManager for paper fill simulation
        self._on_fill_cb = None

    def set_fill_callback(self, cb) -> None:
        self._on_fill_cb = cb

    def init_paper_balances(self, usdt: float, btc: float = 0.0) -> None:
        self._paper_balances = {"USDT": usdt, "BTC": btc}

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    async def place_buy(
        self,
        price: float,
        quantity: float,
        client_order_id: str,
        grid_level_idx: int,
    ) -> Optional[dict]:
        return await self._place("BUY", price, quantity, client_order_id, grid_level_idx)

    async def place_sell(
        self,
        price: float,
        quantity: float,
        client_order_id: str,
        grid_level_idx: int,
    ) -> Optional[dict]:
        return await self._place("SELL", price, quantity, client_order_id, grid_level_idx)

    async def cancel(self, client_order_id: str) -> bool:
        if self._mode == "dry_run":
            logger.info("[DRY-RUN] CANCEL %s", client_order_id)
            return True
        if self._mode == "paper":
            await self._repo.mark_order_filled(client_order_id, 0.0, status="CANCELLED")
            return True
        # live
        try:
            await self._rest.cancel_order(self._symbol, client_order_id)
            await self._repo.mark_order_filled(client_order_id, 0.0, status="CANCELLED")
            return True
        except Exception as e:
            logger.error("Cancel %s failed: %s", client_order_id, e)
            return False

    async def cancel_all(self) -> None:
        if self._mode == "live":
            try:
                await self._rest.cancel_all_orders(self._symbol)
            except Exception as e:
                logger.error("cancel_all failed: %s", e)
        open_orders = await self._repo.get_open_orders(self._symbol)
        for o in open_orders:
            await self._repo.mark_order_filled(
                o["client_order_id"], 0.0, status="CANCELLED"
            )

    # ------------------------------------------------------------------ #
    # Paper fill simulation (called by price-tick handler in core.bot)
    # ------------------------------------------------------------------ #

    async def simulate_fills(self, current_price: float) -> None:
        """Check open paper orders and simulate fills when price crosses."""
        if self._mode not in ("paper", "dry_run"):
            return
        open_orders = await self._repo.get_open_orders(self._symbol)
        for order in open_orders:
            filled = False
            side = order["side"]
            price = order["price"]
            qty = order["quantity"]

            if side == "BUY" and current_price <= price:
                filled = True
            elif side == "SELL" and current_price >= price:
                filled = True

            if not filled:
                continue

            # Update virtual balance
            if side == "BUY":
                cost = price * qty * (1 + _FEE_RATE)
                if self._paper_balances.get("USDT", 0) >= cost:
                    self._paper_balances["USDT"] -= cost
                    self._paper_balances["BTC"] = (
                        self._paper_balances.get("BTC", 0) + qty
                    )
                else:
                    logger.warning("Paper: insufficient USDT for BUY at %.2f", price)
                    continue
            else:
                if self._paper_balances.get("BTC", 0) >= qty:
                    self._paper_balances["BTC"] -= qty
                    self._paper_balances["USDT"] = (
                        self._paper_balances.get("USDT", 0)
                        + price * qty * (1 - _FEE_RATE)
                    )
                else:
                    logger.warning("Paper: insufficient BTC for SELL at %.2f", price)
                    continue

            await self._repo.mark_order_filled(
                order["client_order_id"], qty, status="PAPER_FILLED"
            )
            await self._repo.insert_trade(
                {
                    "client_order_id": order["client_order_id"],
                    "symbol": self._symbol,
                    "side": side,
                    "price": price,
                    "quantity": qty,
                    "fee": price * qty * _FEE_RATE,
                    "fee_asset": "USDT",
                    "timestamp": time.time(),
                }
            )
            logger.info(
                "[PAPER] %s filled level=%s price=%.2f qty=%.5f USDT=%.2f BTC=%.5f",
                side,
                order.get("grid_level_idx"),
                price,
                qty,
                self._paper_balances.get("USDT", 0),
                self._paper_balances.get("BTC", 0),
            )
            if self._on_fill_cb:
                await self._on_fill_cb(order["client_order_id"], price, qty)

    @property
    def paper_balances(self) -> dict:
        return dict(self._paper_balances)

    # ------------------------------------------------------------------ #
    # Internal
    # ------------------------------------------------------------------ #

    async def _place(
        self,
        side: str,
        price: float,
        quantity: float,
        client_order_id: str,
        grid_level_idx: int,
    ) -> Optional[dict]:
        order_record = {
            "client_order_id": client_order_id,
            "symbol": self._symbol,
            "side": side,
            "price": price,
            "quantity": quantity,
            "grid_level_idx": grid_level_idx,
        }

        if self._mode == "dry_run":
            logger.info(
                "[DRY-RUN] %s %s qty=%.5f price=%.2f id=%s",
                side, self._symbol, quantity, price, client_order_id,
            )
            return None

        if self._mode == "paper":
            order_record["status"] = "PAPER_OPEN"
            await self._repo.upsert_order(order_record)
            logger.info(
                "[PAPER] Order queued: %s %s qty=%.5f @%.2f id=%s",
                side, self._symbol, quantity, price, client_order_id,
            )
            return order_record

        # live
        order_record["status"] = "OPEN"
        await self._repo.upsert_order(order_record)
        try:
            result = await self._rest.place_order(
                symbol=self._symbol,
                side=side,
                quantity=quantity,
                price=price,
                client_order_id=client_order_id,
                qty_precision=self._qty_prec,
                price_precision=self._price_prec,
            )
            order_record["exchange_order_id"] = str(result.get("orderId", ""))
            order_record["status"] = result.get("status", "OPEN")
            await self._repo.upsert_order(order_record)
            return result
        except Exception as e:
            logger.error("Live order placement failed for %s: %s", client_order_id, e)
            await self._repo.upsert_order({**order_record, "status": "FAILED"})
            return None
