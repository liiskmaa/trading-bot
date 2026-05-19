"""
Tests for OrderExecutor.

Covers:
- Double-fill protection: concurrent simulate_fills calls on the same order
  fire the fill callback exactly once (H2)
- Normal fill flow: callback fires, balances update, trade is persisted
"""

import asyncio
import time
import pytest

from execution.orders import OrderExecutor
from database.repository import Repository


@pytest.fixture
async def repo(tmp_path):
    r = Repository(str(tmp_path / "test.db"))
    await r.open()
    yield r
    await r.close()


@pytest.fixture
async def executor(repo):
    ex = OrderExecutor(
        symbol="BTCUSDT",
        mode="paper",
        repo=repo,
        price_precision=2,
        qty_precision=5,
    )
    ex.init_paper_balances(usdt=1000.0)
    return ex


async def _insert_paper_order(
    repo: Repository, cid: str, price: float, side: str, qty: float
) -> None:
    now = time.time()
    await repo._db.execute(
        "INSERT INTO orders "
        "(client_order_id, exchange_order_id, symbol, side, order_type, "
        "price, quantity, executed_qty, status, grid_level_idx, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (cid, None, "BTCUSDT", side, "LIMIT", price, qty, 0.0, "PAPER_OPEN", 0, now, now),
    )
    await repo._db.commit()


class TestSimulateFillsNormal:
    async def test_buy_fill_fires_callback(self, repo, executor):
        fills = []
        executor.set_fill_callback(lambda cid, p, q: fills.append(cid) or asyncio.sleep(0))

        async def on_fill(cid, price, qty):
            fills.append(cid)

        executor.set_fill_callback(on_fill)
        await _insert_paper_order(repo, "order-1", 50000.0, "BUY", 0.001)
        await executor.simulate_fills(49999.0)
        assert fills == ["order-1"]

    async def test_sell_fill_fires_callback(self, repo, executor):
        fills = []

        async def on_fill(cid, price, qty):
            fills.append(cid)

        executor.set_fill_callback(on_fill)
        executor._paper_balances["BTC"] = 0.1
        await _insert_paper_order(repo, "order-1", 50000.0, "SELL", 0.001)
        await executor.simulate_fills(50001.0)
        assert fills == ["order-1"]

    async def test_buy_below_price_not_filled(self, repo, executor):
        fills = []

        async def on_fill(cid, price, qty):
            fills.append(cid)

        executor.set_fill_callback(on_fill)
        await _insert_paper_order(repo, "order-1", 50000.0, "BUY", 0.001)
        # Price is above the buy level — should not fill
        await executor.simulate_fills(50500.0)
        assert fills == []

    async def test_buy_updates_paper_balances(self, repo, executor):
        async def on_fill(cid, price, qty):
            pass

        executor.set_fill_callback(on_fill)
        initial_usdt = executor.paper_balances["USDT"]
        await _insert_paper_order(repo, "order-1", 50000.0, "BUY", 0.001)
        await executor.simulate_fills(49999.0)
        assert executor.paper_balances["USDT"] < initial_usdt
        assert executor.paper_balances.get("BTC", 0) > 0.0

    async def test_order_marked_paper_filled_in_db(self, repo, executor):
        async def on_fill(cid, price, qty):
            pass

        executor.set_fill_callback(on_fill)
        await _insert_paper_order(repo, "order-1", 50000.0, "BUY", 0.001)
        await executor.simulate_fills(49999.0)
        row = await repo.get_order("order-1")
        assert row["status"] == "PAPER_FILLED"


class TestSimulateFillsDoubleFill:
    async def test_concurrent_calls_fire_callback_once(self, repo, executor):
        """Two concurrent simulate_fills on the same PAPER_OPEN order must
        call the fill callback exactly once, not twice."""
        fill_count = 0

        async def on_fill(cid, price, qty):
            nonlocal fill_count
            fill_count += 1

        executor.set_fill_callback(on_fill)
        await _insert_paper_order(repo, "order-1", 50000.0, "BUY", 0.001)

        # Both tasks read the order as PAPER_OPEN, but only one should mark it filled
        await asyncio.gather(
            executor.simulate_fills(49999.0),
            executor.simulate_fills(49999.0),
        )

        assert fill_count == 1

    async def test_sequential_calls_dont_double_fill(self, repo, executor):
        fill_count = 0

        async def on_fill(cid, price, qty):
            nonlocal fill_count
            fill_count += 1

        executor.set_fill_callback(on_fill)
        await _insert_paper_order(repo, "order-1", 50000.0, "BUY", 0.001)

        await executor.simulate_fills(49999.0)  # fills
        await executor.simulate_fills(49999.0)  # already PAPER_FILLED, should not re-fire

        assert fill_count == 1

    async def test_two_different_orders_both_fill(self, repo, executor):
        fills = []

        async def on_fill(cid, price, qty):
            fills.append(cid)

        executor.set_fill_callback(on_fill)
        await _insert_paper_order(repo, "order-buy-1", 50000.0, "BUY", 0.0005)
        await _insert_paper_order(repo, "order-buy-2", 49000.0, "BUY", 0.0005)

        await executor.simulate_fills(48500.0)  # crosses both levels

        assert sorted(fills) == ["order-buy-1", "order-buy-2"]
