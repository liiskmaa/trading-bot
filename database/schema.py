"""
SQLite schema.  All writes use INSERT OR REPLACE / ON CONFLICT IGNORE so every
operation is idempotent.
"""

SCHEMA_SQL = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS orders (
    client_order_id  TEXT PRIMARY KEY,
    exchange_order_id TEXT,
    symbol           TEXT    NOT NULL,
    side             TEXT    NOT NULL,          -- BUY | SELL
    order_type       TEXT    NOT NULL DEFAULT 'LIMIT',
    price            REAL    NOT NULL,
    quantity         REAL    NOT NULL,
    executed_qty     REAL    NOT NULL DEFAULT 0.0,
    status           TEXT    NOT NULL DEFAULT 'NEW',
    -- NEW | OPEN | PARTIALLY_FILLED | FILLED | CANCELLED | PAPER_OPEN | PAPER_FILLED
    grid_level_idx   INTEGER,
    created_at       REAL    NOT NULL,
    updated_at       REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS trades (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    exchange_trade_id TEXT UNIQUE,
    client_order_id  TEXT    NOT NULL,
    symbol           TEXT    NOT NULL,
    side             TEXT    NOT NULL,
    price            REAL    NOT NULL,
    quantity         REAL    NOT NULL,
    fee              REAL    NOT NULL DEFAULT 0.0,
    fee_asset        TEXT    NOT NULL DEFAULT 'USDT',
    realized_pnl     REAL    NOT NULL DEFAULT 0.0,
    timestamp        REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS grid_levels (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol           TEXT    NOT NULL,
    level_idx        INTEGER NOT NULL,
    price            REAL    NOT NULL,
    side             TEXT    NOT NULL,          -- current expected side
    status           TEXT    NOT NULL DEFAULT 'PENDING',
    -- PENDING | BUY_OPEN | BUY_FILLED | SELL_OPEN | SELL_FILLED | DISABLED
    client_order_id  TEXT,
    created_at       REAL    NOT NULL,
    updated_at       REAL    NOT NULL,
    UNIQUE(symbol, level_idx)
);

CREATE TABLE IF NOT EXISTS balances (
    asset            TEXT    PRIMARY KEY,
    free             REAL    NOT NULL,
    locked           REAL    NOT NULL,
    updated_at       REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS candles (
    symbol           TEXT    NOT NULL,
    interval         TEXT    NOT NULL,
    open_time        INTEGER NOT NULL,          -- Unix ms
    open             REAL    NOT NULL,
    high             REAL    NOT NULL,
    low              REAL    NOT NULL,
    close            REAL    NOT NULL,
    volume           REAL    NOT NULL,
    PRIMARY KEY (symbol, interval, open_time)
);

CREATE TABLE IF NOT EXISTS system_events (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type       TEXT    NOT NULL,
    severity         TEXT    NOT NULL DEFAULT 'INFO',
    message          TEXT    NOT NULL,
    data             TEXT,                      -- JSON blob
    timestamp        REAL    NOT NULL
);

-- Performance indexes
CREATE INDEX IF NOT EXISTS idx_orders_status       ON orders(status);
CREATE INDEX IF NOT EXISTS idx_orders_level        ON orders(grid_level_idx);
CREATE INDEX IF NOT EXISTS idx_trades_timestamp    ON trades(timestamp);
CREATE INDEX IF NOT EXISTS idx_candles_lookup      ON candles(symbol, interval, open_time DESC);
CREATE INDEX IF NOT EXISTS idx_events_type_time    ON system_events(event_type, timestamp DESC);
"""


async def init_schema(db) -> None:
    """Run schema creation on an open aiosqlite connection."""
    await db.executescript(SCHEMA_SQL)
    await db.commit()
