"""
Tests for GridManager.

Covers:
- restore() reads quantity from DB rows
- _persist_level() includes quantity in the upserted record
- on_order_filled() state machine transitions
- needs_rebuild() drift detection
- cancel_all() cancels open levels
- build() places initial orders
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
    def __init__(self):
        self.placed: list[dict] = []
        self.cancelled: list[str] = []

    async def place_buy(self, **kwargs):
        self.placed.append({"side": "BUY", **kwargs})

    async def place_sell(self, **kwargs):
        self.placed.append({"side": "SELL", **kwargs})

    async def cancel(self, client_order_id: str) -> bool:
        self.cancelled.append(client_order_id)
        return True


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


def _manager_with_levels(statuses: list[str]) -> tuple[GridManager, FakeRepo, FakeExecutor]:
    """Build a GridManager pre-loaded with synthetic levels at fixed prices."""
    repo = FakeRepo()
    executor = FakeExecutor()
    m = make_manager()
    m.inject(repo, FakeCache(), executor)
    prices = [49000.0 + i * 1000 for i in range(len(statuses))]
    m._levels = [
        GridLevel(
            idx=i, price=prices[i], side="BUY" if prices[i] < 51000 else "SELL",
            status=statuses[i], client_order_id=f"GID{i:03d}",
            quantity=0.001,
        )
        for i in range(len(statuses))
    ]
    m._reference_price = 51000.0
    return m, repo, executor


class TestOnOrderFilled:
    async def test_buy_filled_transitions_status_to_buy_filled(self):
        m, _, _ = _manager_with_levels(["BUY_OPEN", "PENDING", "PENDING"])
        await m.on_order_filled("GID000", 49000.0, 0.001)
        assert m._levels[0].status == "BUY_FILLED"

    async def test_buy_filled_places_sell_at_level_above(self):
        m, _, executor = _manager_with_levels(["BUY_OPEN", "PENDING", "PENDING"])
        await m.on_order_filled("GID000", 49000.0, 0.001)
        sell_calls = [p for p in executor.placed if p["side"] == "SELL"]
        assert len(sell_calls) == 1
        assert sell_calls[0]["price"] == m._levels[1].price

    async def test_buy_filled_sets_level_above_to_sell_open(self):
        m, _, _ = _manager_with_levels(["BUY_OPEN", "PENDING", "PENDING"])
        await m.on_order_filled("GID000", 49000.0, 0.001)
        assert m._levels[1].status == "SELL_OPEN"

    async def test_sell_filled_transitions_status_to_sell_filled(self):
        m, _, _ = _manager_with_levels(["PENDING", "PENDING", "SELL_OPEN"])
        await m.on_order_filled("GID002", 51000.0, 0.001)
        assert m._levels[2].status == "SELL_FILLED"

    async def test_sell_filled_places_buy_at_level_below(self):
        m, _, executor = _manager_with_levels(["PENDING", "PENDING", "SELL_OPEN"])
        await m.on_order_filled("GID002", 51000.0, 0.001)
        buy_calls = [p for p in executor.placed if p["side"] == "BUY"]
        assert len(buy_calls) == 1
        assert buy_calls[0]["price"] == m._levels[1].price

    async def test_sell_filled_sets_level_below_to_buy_open(self):
        m, _, _ = _manager_with_levels(["PENDING", "PENDING", "SELL_OPEN"])
        await m.on_order_filled("GID002", 51000.0, 0.001)
        assert m._levels[1].status == "BUY_OPEN"

    async def test_buy_at_ceiling_does_not_crash(self):
        # Level 0 is the only level — no level above
        m, _, executor = _manager_with_levels(["BUY_OPEN"])
        await m.on_order_filled("GID000", 49000.0, 0.001)
        assert len(executor.placed) == 0  # no sell placed

    async def test_sell_at_floor_does_not_crash(self):
        # Level 0 is the only level — no level below
        m, _, executor = _manager_with_levels(["SELL_OPEN"])
        await m.on_order_filled("GID000", 49000.0, 0.001)
        assert len(executor.placed) == 0

    async def test_unknown_client_id_is_ignored(self):
        m, repo, executor = _manager_with_levels(["BUY_OPEN", "PENDING"])
        await m.on_order_filled("UNKNOWN_ID", 49000.0, 0.001)
        assert m._levels[0].status == "BUY_OPEN"
        assert len(executor.placed) == 0


class TestNeedsRebuild:
    def test_returns_false_within_threshold(self):
        m = make_manager()
        m._reference_price = 50000.0
        assert m.needs_rebuild(51000.0) is False  # 2% drift, threshold 3%

    def test_returns_true_beyond_threshold(self):
        m = make_manager()
        m._reference_price = 50000.0
        assert m.needs_rebuild(55000.0) is True   # 10% drift

    def test_returns_false_when_no_reference_price(self):
        m = make_manager()
        m._reference_price = 0.0
        assert m.needs_rebuild(50000.0) is False

    def test_symmetric_for_downward_drift(self):
        m = make_manager()
        m._reference_price = 50000.0
        assert m.needs_rebuild(45000.0) is True   # -10% drift

    def test_exact_threshold_triggers_rebuild(self):
        m = make_manager()
        m._reference_price = 50000.0
        # 3% of 50000 = 1500; price at 51501 > 3%
        assert m.needs_rebuild(51501.0) is True


class TestCancelAll:
    async def test_cancels_all_open_levels(self):
        m, _, executor = _manager_with_levels(["BUY_OPEN", "SELL_OPEN", "PENDING"])
        await m.cancel_all()
        assert len(executor.cancelled) == 2

    async def test_pending_levels_not_cancelled(self):
        m, _, executor = _manager_with_levels(["PENDING", "PENDING"])
        await m.cancel_all()
        assert len(executor.cancelled) == 0

    async def test_open_levels_set_to_disabled(self):
        m, _, _ = _manager_with_levels(["BUY_OPEN", "SELL_OPEN"])
        await m.cancel_all()
        assert all(lv.status == "DISABLED" for lv in m._levels)

    async def test_cancel_continues_on_executor_error(self):
        m, _, executor = _manager_with_levels(["BUY_OPEN", "BUY_OPEN"])

        async def failing_cancel(cid):
            raise RuntimeError("exchange error")

        executor.cancel = failing_cancel
        # Should not raise
        await m.cancel_all()


class TestBuild:
    async def test_build_creates_correct_number_of_levels(self):
        m, _, _ = _manager_with_levels([])
        m._repo = FakeRepo()
        m.inject(m._repo, FakeCache(), FakeExecutor())
        await m.build(50000.0)
        assert len(m._levels) == 10  # make_manager uses num_levels=10

    async def test_build_sets_reference_price(self):
        m = make_manager()
        executor = FakeExecutor()
        m.inject(FakeRepo(), FakeCache(), executor)
        await m.build(50000.0)
        assert m._reference_price == 50000.0

    async def test_build_places_one_order_per_level(self):
        m = make_manager()
        executor = FakeExecutor()
        m.inject(FakeRepo(), FakeCache(), executor)
        await m.build(50000.0)
        assert len(executor.placed) == 10

    async def test_build_places_buys_below_and_sells_above(self):
        m = make_manager()
        executor = FakeExecutor()
        m.inject(FakeRepo(), FakeCache(), executor)
        await m.build(50000.0)
        buys = [p for p in executor.placed if p["side"] == "BUY"]
        sells = [p for p in executor.placed if p["side"] == "SELL"]
        assert all(p["price"] < 50000.0 for p in buys)
        assert all(p["price"] >= 50000.0 for p in sells)
