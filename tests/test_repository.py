"""
Tests for the database repository layer.

Covers:
- mark_order_filled returns True on first call, False on duplicate (H2 double-fill guard)
- quantity column is persisted and restored for grid levels (C4)
- schema migration adds quantity column to an existing database (C4)
"""

import time
import pytest
import aiosqlite

from database.repository import Repository
from database.schema import init_schema


@pytest.fixture
async def repo(tmp_path):
    r = Repository(str(tmp_path / "test.db"))
    await r.open()
    yield r
    await r.close()


async def _insert_order(repo: Repository, cid: str, status: str, side: str = "BUY") -> None:
    now = time.time()
    await repo._db.execute(
        "INSERT INTO orders "
        "(client_order_id, exchange_order_id, symbol, side, order_type, "
        "price, quantity, executed_qty, status, grid_level_idx, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (cid, None, "BTCUSDT", side, "LIMIT", 50000.0, 0.001, 0.0, status, 0, now, now),
    )
    await repo._db.commit()


class TestMarkOrderFilled:
    async def test_returns_true_for_paper_open_row(self, repo):
        await _insert_order(repo, "order-1", "PAPER_OPEN")
        result = await repo.mark_order_filled("order-1", 0.001, "PAPER_FILLED")
        assert result is True

    async def test_returns_false_on_duplicate_paper_fill(self, repo):
        await _insert_order(repo, "order-1", "PAPER_OPEN")
        await repo.mark_order_filled("order-1", 0.001, "PAPER_FILLED")
        # Second call — row is no longer PAPER_OPEN so the WHERE clause misses
        result = await repo.mark_order_filled("order-1", 0.001, "PAPER_FILLED")
        assert result is False

    async def test_paper_fill_doesnt_match_already_filled_row(self, repo):
        await _insert_order(repo, "order-1", "PAPER_FILLED")
        result = await repo.mark_order_filled("order-1", 0.001, "PAPER_FILLED")
        assert result is False

    async def test_non_paper_status_always_updates(self, repo):
        await _insert_order(repo, "order-1", "OPEN")
        result = await repo.mark_order_filled("order-1", 0.001, "FILLED")
        assert result is True

    async def test_cancelled_status_always_updates(self, repo):
        await _insert_order(repo, "order-1", "PAPER_OPEN")
        result = await repo.mark_order_filled("order-1", 0.0, "CANCELLED")
        assert result is True

    async def test_status_is_updated_in_db(self, repo):
        await _insert_order(repo, "order-1", "PAPER_OPEN")
        await repo.mark_order_filled("order-1", 0.001, "PAPER_FILLED")
        row = await repo.get_order("order-1")
        assert row["status"] == "PAPER_FILLED"
        assert abs(row["executed_qty"] - 0.001) < 1e-10


class TestGridLevelQuantity:
    async def test_quantity_persisted_and_restored(self, repo):
        now = time.time()
        await repo.upsert_grid_level({
            "symbol": "BTCUSDT",
            "level_idx": 0,
            "price": 50000.0,
            "quantity": 0.00058,
            "side": "BUY",
            "status": "PENDING",
            "created_at": now,
        })
        levels = await repo.get_grid_levels("BTCUSDT")
        assert len(levels) == 1
        assert abs(levels[0]["quantity"] - 0.00058) < 1e-10

    async def test_quantity_updated_on_conflict(self, repo):
        now = time.time()
        base = {"symbol": "BTCUSDT", "level_idx": 0, "price": 50000.0,
                "side": "BUY", "status": "PENDING", "created_at": now}
        await repo.upsert_grid_level({**base, "quantity": 0.001})
        await repo.upsert_grid_level({**base, "quantity": 0.002})
        levels = await repo.get_grid_levels("BTCUSDT")
        assert abs(levels[0]["quantity"] - 0.002) < 1e-10

    async def test_multiple_levels_quantities(self, repo):
        now = time.time()
        for i, qty in enumerate([0.0006, 0.00059, 0.00058]):
            await repo.upsert_grid_level({
                "symbol": "BTCUSDT", "level_idx": i,
                "price": 50000.0 + i * 1000, "quantity": qty,
                "side": "BUY", "status": "PENDING", "created_at": now,
            })
        levels = await repo.get_grid_levels("BTCUSDT")
        assert len(levels) == 3
        assert abs(levels[0]["quantity"] - 0.0006) < 1e-10
        assert abs(levels[2]["quantity"] - 0.00058) < 1e-10


class TestSchemaMigration:
    async def test_migration_adds_quantity_to_existing_db(self, tmp_path):
        db_path = str(tmp_path / "old.db")
        # Create schema without the quantity column (simulates a pre-migration database)
        async with aiosqlite.connect(db_path) as db:
            await db.execute("""
                CREATE TABLE grid_levels (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol          TEXT    NOT NULL,
                    level_idx       INTEGER NOT NULL,
                    price           REAL    NOT NULL,
                    side            TEXT    NOT NULL,
                    status          TEXT    NOT NULL DEFAULT 'PENDING',
                    client_order_id TEXT,
                    created_at      REAL    NOT NULL,
                    updated_at      REAL    NOT NULL,
                    UNIQUE(symbol, level_idx)
                )
            """)
            await db.commit()

        # Opening through Repository runs init_schema + migration
        r = Repository(db_path)
        await r.open()
        await r.upsert_grid_level({
            "symbol": "BTCUSDT", "level_idx": 0, "price": 50000.0,
            "quantity": 0.001, "side": "BUY", "status": "PENDING",
        })
        levels = await r.get_grid_levels("BTCUSDT")
        assert abs(levels[0]["quantity"] - 0.001) < 1e-10
        await r.close()

    async def test_migration_is_idempotent(self, tmp_path):
        db_path = str(tmp_path / "new.db")
        # Fresh DB already has the quantity column; running init_schema again must not error
        r = Repository(db_path)
        await r.open()
        await r.close()
        # Open again — migration runs, should be a no-op
        r2 = Repository(db_path)
        await r2.open()
        await r2.close()
