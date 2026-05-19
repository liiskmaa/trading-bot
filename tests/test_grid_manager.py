"""
Tests for GridManager.

Covers:
- restore() reads quantity from DB rows (C4 fix — was hardcoded to 0.0)
- restore() returns False when no rows exist
- _persist_level() includes quantity in the upserted record
"""

import pytest
from grid_engine.manager import GridManager
from grid_engine.calculator import GridLevel


class FakeRepo:
    """Minimal repo stub that records upsert calls."""

    def __init__(self, rows=None):
        self._rows = rows or []
        self.upserted = []

    async def get_grid_levels(self, symbol):
        return self._rows

    async def upsert_grid_level(self, level: dict):
        self.upserted.append(dict(level))

    async def clear_grid_levels(self, symbol):
        pass


class FakeCache:
    async def invalidate_grid_state(self, symbol):
        pass

    async def set_grid_state(self, symbol, state):
        pass


class FakeExecutor:
    async def place_buy(self, **kwargs):
        pass

    async def place_sell(self, **kwargs):
        pass


def make_manager() -> GridManager:
    return GridManager(
        symbol="BTCUSDT",
        range_percent=5.0,
        num_levels=10,
        order_size_usdt=29.0,
        rebuild_threshold_percent=3.0,
        price_precision=2,
        qty_precision=5,
    )


class TestRestore:
    async def test_restore_returns_false_when_no_rows(self):
        m = make_manager()
        m.inject(FakeRepo(rows=[]), FakeCache(), FakeExecutor())
        result = await m.restore()
        assert result is False

    async def test_restore_returns_true_when_rows_exist(self):
        rows = [
            {"level_idx": 0, "price": 49000.0, "side": "BUY",
             "status": "PENDING", "client_order_id": None, "quantity": 0.00059},
        ]
        m = make_manager()
        m.inject(FakeRepo(rows=rows), FakeCache(), FakeExecutor())
        result = await m.restore()
        assert result is True

    async def test_restore_reads_quantity_from_db(self):
        rows = [
            {"level_idx": 0, "price": 49000.0, "side": "BUY",
             "status": "PENDING", "client_order_id": None, "quantity": 0.00059},
            {"level_idx": 1, "price": 50000.0, "side": "SELL",
             "status": "SELL_OPEN", "client_order_id": "GBTC001S1234567", "quantity": 0.00058},
        ]
        m = make_manager()
        m.inject(FakeRepo(rows=rows), FakeCache(), FakeExecutor())
        await m.restore()

        assert abs(m.levels[0].quantity - 0.00059) < 1e-10
        assert abs(m.levels[1].quantity - 0.00058) < 1e-10

    async def test_restore_handles_missing_quantity_gracefully(self):
        # Rows without a quantity key (very old DB that somehow bypassed migration)
        rows = [
            {"level_idx": 0, "price": 49000.0, "side": "BUY",
             "status": "PENDING", "client_order_id": None},
        ]
        m = make_manager()
        m.inject(FakeRepo(rows=rows), FakeCache(), FakeExecutor())
        await m.restore()
        assert m.levels[0].quantity == 0.0

    async def test_restore_sets_reference_price_to_grid_midpoint(self):
        rows = [
            {"level_idx": 0, "price": 48000.0, "side": "BUY",
             "status": "PENDING", "client_order_id": None, "quantity": 0.001},
            {"level_idx": 1, "price": 52000.0, "side": "SELL",
             "status": "PENDING", "client_order_id": None, "quantity": 0.001},
        ]
        m = make_manager()
        m.inject(FakeRepo(rows=rows), FakeCache(), FakeExecutor())
        await m.restore()
        assert m._reference_price == 50000.0


class TestPersistLevel:
    async def test_persist_level_includes_quantity(self):
        repo = FakeRepo()
        m = make_manager()
        m.inject(repo, FakeCache(), FakeExecutor())
        m._levels = []

        level = GridLevel(idx=3, price=49500.0, side="BUY",
                          status="BUY_OPEN", quantity=0.000587)
        await m._persist_level(level)

        assert len(repo.upserted) == 1
        record = repo.upserted[0]
        assert "quantity" in record
        assert abs(record["quantity"] - 0.000587) < 1e-10

    async def test_persist_level_includes_all_required_fields(self):
        repo = FakeRepo()
        m = make_manager()
        m.inject(repo, FakeCache(), FakeExecutor())
        m._levels = []

        level = GridLevel(idx=2, price=50000.0, side="SELL",
                          status="SELL_OPEN", client_order_id="GBTC002S9999999",
                          quantity=0.00058)
        await m._persist_level(level)

        record = repo.upserted[0]
        assert record["symbol"] == "BTCUSDT"
        assert record["level_idx"] == 2
        assert record["price"] == 50000.0
        assert record["side"] == "SELL"
        assert record["status"] == "SELL_OPEN"
        assert record["client_order_id"] == "GBTC002S9999999"
