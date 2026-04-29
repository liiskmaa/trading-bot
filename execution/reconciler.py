"""
Startup reconciler.

On every restart, compares local DB state against the exchange's open orders
and fills any gaps so the grid never has phantom or duplicate orders.
"""

import logging

logger = logging.getLogger(__name__)


class Reconciler:
    def __init__(self, symbol: str, rest, repo, grid_manager):
        self._symbol = symbol
        self._rest = rest
        self._repo = repo
        self._grid = grid_manager

    async def run(self) -> None:
        """
        1. Fetch open orders from exchange.
        2. For each DB order marked OPEN that isn't on the exchange, check
           if it was filled (query order status) and update DB accordingly.
        3. For each exchange order not tracked in DB, insert it (shouldn't
           happen but safe to handle).
        """
        logger.info("Starting reconciliation for %s", self._symbol)

        try:
            exchange_orders = await self._rest.get_open_orders(self._symbol)
        except Exception as e:
            logger.error("Could not fetch open orders from exchange: %s", e)
            return

        exchange_ids = {o["clientOrderId"] for o in exchange_orders}
        db_open = await self._repo.get_open_orders(self._symbol)

        for db_order in db_open:
            cid = db_order["client_order_id"]
            if cid not in exchange_ids:
                # Order disappeared — either filled or cancelled externally
                await self._check_and_reconcile(cid)

        # Register any exchange orders absent from DB (e.g., manual orders)
        db_ids = {o["client_order_id"] for o in db_open}
        for ex_order in exchange_orders:
            if ex_order["clientOrderId"] not in db_ids:
                logger.warning(
                    "Unknown exchange order %s — adding to DB as OPEN",
                    ex_order["clientOrderId"],
                )
                await self._repo.upsert_order(
                    {
                        "client_order_id": ex_order["clientOrderId"],
                        "exchange_order_id": str(ex_order["orderId"]),
                        "symbol": self._symbol,
                        "side": ex_order["side"],
                        "price": float(ex_order["price"]),
                        "quantity": float(ex_order["origQty"]),
                        "executed_qty": float(ex_order["executedQty"]),
                        "status": "OPEN",
                    }
                )

        logger.info("Reconciliation complete")

    async def _check_and_reconcile(self, client_order_id: str) -> None:
        """Query the exchange for this specific order and sync DB."""
        try:
            # We can't query by clientOrderId easily on all endpoints,
            # so we rely on the DB record for the exchange order ID.
            db_rec = await self._repo.get_order(client_order_id)
            if not db_rec:
                return

            ex_oid = db_rec.get("exchange_order_id")
            if not ex_oid:
                # Never confirmed on exchange — mark CANCELLED
                await self._repo.mark_order_filled(client_order_id, 0.0, "CANCELLED")
                return

            # Fetch individual order status
            result = await self._rest._signed_get(
                "/api/v3/order",
                {"symbol": self._symbol, "orderId": ex_oid},
            )
            status = result.get("status", "UNKNOWN")
            exec_qty = float(result.get("executedQty", 0))

            if status in ("FILLED", "PARTIALLY_FILLED"):
                logger.info(
                    "Reconcile: order %s was %s (qty=%.5f) — updating DB and placing paired order",
                    client_order_id, status, exec_qty,
                )
                await self._repo.mark_order_filled(
                    client_order_id, exec_qty, status
                )
                if status == "FILLED":
                    fill_price = float(result.get("price", 0))
                    await self._grid.on_order_filled(client_order_id, fill_price, exec_qty)
            else:
                await self._repo.mark_order_filled(client_order_id, exec_qty, "CANCELLED")

        except Exception as e:
            logger.error("Reconcile failed for %s: %s", client_order_id, e)
