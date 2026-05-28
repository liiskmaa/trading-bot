"""
Main bot orchestrator.

Wires together all modules and owns the main event loop.
Price ticks arrive from the WebSocket and fan out to:
  1. CandleAggregator    — OHLC persistence
  2. Cache               — latest price
  3. RiskManager         — velocity check
  4. AIFilter            — regime classification (rate-limited)
  5. GridManager         — fill simulation (paper) / order management (live)
  6. MonitoringServer    — metrics snapshot
"""

import asyncio
import logging
import time
from typing import Optional

from core.state import BotState
from risk.manager import RiskState

logger = logging.getLogger(__name__)

# How often to run the listen-key keepalive (seconds)
_KEEPALIVE_INTERVAL  = 25 * 60
_HEARTBEAT_INTERVAL  = 30
_AI_POLL_INTERVAL    = 60
_DB_HEARTBEAT_INTERVAL = 60 * 60  # write a DB heartbeat once per hour


class Bot:
    def __init__(
        self,
        config,
        repo,
        cache,
        rest,
        ws,
        candles,
        grid_manager,
        executor,
        reconciler,
        risk,
        ai_classifier,
        monitoring: Optional[object] = None,
    ):
        self._cfg = config
        self._repo = repo
        self._cache = cache
        self._rest = rest
        self._ws = ws
        self._candles = candles
        self._grid = grid_manager
        self._executor = executor
        self._reconciler = reconciler
        self._risk = risk
        self._ai = ai_classifier
        self._monitoring = monitoring

        self._state = BotState.STARTING
        self._mode = config.str("trading", "mode", default="paper")
        self._symbol = config.str("trading", "symbol", default="BTCUSDT")
        self._last_ai_call: float = 0.0
        self._last_price: float = 0.0
        self._ai_regime: str = "unknown"
        self._listen_key: Optional[str] = None
        self._listen_key_refreshed_at: float = 0.0

        self._tick_in_flight: bool = False
        self._last_db_heartbeat: float = 0.0

        self._risk.set_stop_callback(self._on_risk_stop)
        self._executor.set_fill_callback(self._on_paper_fill)

    # ------------------------------------------------------------------ #
    # Entry point
    # ------------------------------------------------------------------ #

    async def run(self) -> None:
        logger.info("Bot starting — mode=%s symbol=%s", self._mode, self._symbol)

        # Open infrastructure first — log_event requires an open DB
        await self._repo.open()
        await self._repo.log_event("BOT_START", f"mode={self._mode}")
        await self._cache.connect()

        if self._mode == "live":
            await self._rest.open()
            await self._setup_live_prerequisites()
        elif self._mode == "paper":
            await self._rest.open()
            self._executor.init_paper_balances(
                usdt=self._cfg.float("capital", "active_trading_usdt", default=324.0)
            )
        # dry_run: no REST needed

        # Restore grid from DB or build fresh before WebSocket starts delivering ticks
        restored = await self._grid.restore()
        if restored and self._mode == "live":
            await self._reconciler.run()
        elif not restored:
            price = await self._get_initial_price()
            await self._grid.build(price, reason="initial")

        # Start WebSocket streams after grid is ready
        await self._ws.start()

        # Start background tasks
        tasks = [
            asyncio.create_task(self._heartbeat_loop(), name="heartbeat"),
        ]
        if self._mode == "live" and self._listen_key:
            tasks.append(
                asyncio.create_task(self._keepalive_loop(), name="listen-key-keepalive")
            )
        if self._monitoring:
            tasks.append(
                asyncio.create_task(self._monitoring.start(), name="monitoring")
            )

        self._state = BotState.RUNNING
        logger.info("Bot RUNNING")

        try:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for r in results:
                if isinstance(r, Exception) and not isinstance(r, asyncio.CancelledError):
                    logger.error("Background task error: %s", r)
        except asyncio.CancelledError:
            pass
        finally:
            await self._shutdown()

    # ------------------------------------------------------------------ #
    # Price tick handler (called from WebSocket thread-safe callback)
    # ------------------------------------------------------------------ #

    def on_price(self, price: float) -> None:
        """
        Synchronous — called from WS message handler.
        Schedules async work without blocking the WS receive loop.

        The risk velocity check runs synchronously on every tick so no price
        point is ever skipped from the history window. The heavier async work
        (Redis, SQLite, AI, fill simulation) is skipped when a previous tick
        is still being processed — only the latest price ever matters for grid
        logic, so there is no value in queuing stale ticks.
        """
        self._last_price = price
        # Risk check is sync and must see every price point.
        risk_state = self._risk.on_price(price)
        if risk_state == RiskState.EMERGENCY_STOP:
            return

        if self._tick_in_flight:
            return
        self._tick_in_flight = True
        asyncio.get_running_loop().create_task(self._process_tick(price))

    async def _process_tick(self, price: float) -> None:
        try:
            if self._state in (BotState.EMERGENCY_STOP, BotState.STOPPING):
                return

            await self._cache.set_price(self._symbol, price)
            await self._candles.on_price(price)

            # Paper fill simulation — only when actively running (not PAUSED/COOLDOWN)
            if self._mode in ("paper", "dry_run") and self._state == BotState.RUNNING:
                await self._executor.simulate_fills(price)

            # AI regime check (rate-limited)
            await self._maybe_update_regime(price)

            # Push monitoring snapshot
            if self._monitoring:
                self._monitoring.update(self._build_snapshot())
        finally:
            self._tick_in_flight = False

    # ------------------------------------------------------------------ #
    # Order execution event handler
    # ------------------------------------------------------------------ #

    def on_execution_report(self, msg: dict) -> None:
        asyncio.get_running_loop().create_task(self._process_execution(msg))

    async def _process_execution(self, msg: dict) -> None:
        status = msg.get("X")
        cid = msg.get("c")
        fill_price = float(msg.get("L", 0))
        fill_qty = float(msg.get("l", 0))
        side = msg.get("S")

        if status not in ("FILLED", "PARTIALLY_FILLED"):
            return

        logger.info(
            "Execution report: %s %s price=%.2f qty=%.5f",
            side, cid, fill_price, fill_qty,
        )

        await self._repo.upsert_order(
            {
                "client_order_id": cid,
                "exchange_order_id": str(msg.get("i", "")),
                "symbol": self._symbol,
                "side": side,
                "price": float(msg.get("p", 0)),
                "quantity": float(msg.get("q", 0)),
                "executed_qty": float(msg.get("z", 0)),
                "status": "FILLED" if status == "FILLED" else "PARTIALLY_FILLED",
            }
        )

        if status == "FILLED":
            await self._repo.insert_trade(
                {
                    "client_order_id": cid,
                    "symbol": self._symbol,
                    "side": side,
                    "price": fill_price,
                    "quantity": fill_qty,
                    "fee": float(msg.get("n", 0)),
                    "fee_asset": msg.get("N", "USDT"),
                    "timestamp": time.time(),
                }
            )
            await self._grid.on_order_filled(cid, fill_price, fill_qty)

            if self._monitoring:
                self._monitoring.record_trade(side, fill_price, fill_qty)

    # ------------------------------------------------------------------ #
    # Paper fill callback (from OrderExecutor)
    # ------------------------------------------------------------------ #

    async def _on_paper_fill(self, client_order_id: str, fill_price: float, fill_qty: float) -> None:
        order = await self._repo.get_order(client_order_id)
        if order:
            side = order["side"]
            realized_pnl = 0.0
            if side == "SELL":
                # Realize PnL: sell at fill_price, paired buy was at the level below
                level_idx = order.get("grid_level_idx") or 0
                levels = self._grid.levels
                if 0 < level_idx < len(levels):
                    buy_price = levels[level_idx - 1].price
                    realized_pnl = (
                        fill_price * fill_qty * (1 - 0.001)
                        - buy_price * fill_qty * (1 + 0.001)
                    )
                portfolio_value = (
                    self._executor.paper_balances.get("USDT", 0)
                    + self._executor.paper_balances.get("BTC", 0) * fill_price
                )
                self._risk.on_trade_result(realized_pnl, portfolio_value)
            if self._monitoring:
                self._monitoring.record_trade(side, fill_price, fill_qty, realized_pnl)
        await self._grid.on_order_filled(client_order_id, fill_price, fill_qty)

    # ------------------------------------------------------------------ #
    # AI regime management
    # ------------------------------------------------------------------ #

    async def _maybe_update_regime(self, price: float) -> None:
        if not self._cfg.bool("ai_filter", "enabled", default=True):
            return
        if time.time() - self._last_ai_call < _AI_POLL_INTERVAL:
            return

        candles: list = []
        try:
            candles = await self._repo.get_candles(self._symbol, "1m", limit=100)
        except Exception as e:
            logger.warning("Failed to fetch candles for AI classifier: %s", e)

        regime = await self._ai.classify(candles)
        self._ai_regime = regime
        self._last_ai_call = time.time()

        if regime in ("trending", "high_volatility"):
            if self._state == BotState.RUNNING:
                self._state = BotState.PAUSED
                logger.warning("Trading PAUSED — AI regime: %s", regime)
                await self._repo.log_event(
                    "AI_PAUSE", f"regime={regime}", severity="WARNING"
                )
        elif regime == "ranging":
            if self._state == BotState.PAUSED:
                self._state = BotState.RUNNING
                logger.info("Trading RESUMED — AI regime: ranging")
                await self._repo.log_event("AI_RESUME", "regime=ranging")

    # ------------------------------------------------------------------ #
    # Risk stop callback
    # ------------------------------------------------------------------ #

    async def _on_risk_stop(self, reason: str) -> None:
        logger.critical("Risk stop triggered: %s", reason)
        await self._repo.log_event("RISK_STOP", reason, severity="CRITICAL")
        await self._cache.set_risk_flag("stop_reason", reason)

        if self._risk.state == RiskState.EMERGENCY_STOP:
            self._state = BotState.EMERGENCY_STOP
            await self._grid.cancel_all()
            await self._executor.cancel_all()
        elif self._risk.state == RiskState.COOLDOWN:
            self._state = BotState.COOLDOWN

    # ------------------------------------------------------------------ #
    # Background tasks
    # ------------------------------------------------------------------ #

    async def _heartbeat_loop(self) -> None:
        while self._state not in (BotState.STOPPING, BotState.EMERGENCY_STOP):
            await asyncio.sleep(_HEARTBEAT_INTERVAL)
            # Lift cooldown if expired
            if self._state == BotState.COOLDOWN:
                risk_state = self._risk.check_cooldown()
                if risk_state == RiskState.OK:
                    self._state = BotState.RUNNING
                    logger.info("Cooldown lifted — RUNNING")

            # Rebuild grid if price drifted
            if self._state == BotState.RUNNING and self._last_price > 0:
                if self._grid.needs_rebuild(self._last_price):
                    logger.info("Grid drift detected — rebuilding")
                    await self._grid.cancel_all()
                    await self._grid.build(self._last_price, reason="drift")

            logger.debug(
                "Heartbeat: state=%s price=%.2f regime=%s dd=%.2f%%",
                self._state.value,
                self._last_price,
                self._ai_regime,
                self._risk.drawdown_percent,
            )

            now = time.time()
            if now - self._last_db_heartbeat >= _DB_HEARTBEAT_INTERVAL:
                open_orders = sum(
                    1 for lv in self._grid.levels
                    if lv.status in ("BUY_OPEN", "SELL_OPEN")
                )
                await self._repo.log_event(
                    "HEARTBEAT",
                    f"state={self._state.value} regime={self._ai_regime} "
                    f"price={self._last_price:.2f} dd={self._risk.drawdown_percent:.2f}% "
                    f"open_orders={open_orders}",
                    data={
                        "state": self._state.value,
                        "regime": self._ai_regime,
                        "price": self._last_price,
                        "drawdown_percent": self._risk.drawdown_percent,
                        "open_orders": open_orders,
                    },
                )
                self._last_db_heartbeat = now

    async def _keepalive_loop(self) -> None:
        while self._state not in (BotState.STOPPING, BotState.EMERGENCY_STOP):
            await asyncio.sleep(_KEEPALIVE_INTERVAL)
            try:
                await self._rest.keepalive_listen_key(self._listen_key)
                self._listen_key_refreshed_at = time.time()
            except Exception as e:
                logger.error("Listen key keepalive failed: %s", e)
                try:
                    self._listen_key = await self._rest.create_listen_key()
                    self._ws.update_listen_key(self._listen_key)
                except Exception as e2:
                    logger.error("Failed to refresh listen key: %s", e2)

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    async def _setup_live_prerequisites(self) -> None:
        if not self._cfg.bool("trading", "live_confirmation"):
            raise RuntimeError(
                "Live mode requires 'live_confirmation: true' in config. "
                "Set this only after testing in paper mode."
            )
        self._listen_key = await self._rest.create_listen_key()
        self._ws.set_listen_key(self._listen_key)
        self._ws._on_execution = self.on_execution_report
        logger.info("Live mode: listen key obtained")

    async def _get_initial_price(self) -> float:
        cached = await self._cache.get_price(self._symbol)
        if cached:
            return cached
        price = await self._rest.get_price(self._symbol)
        await self._cache.set_price(self._symbol, price)
        return price

    def _build_snapshot(self) -> dict:
        return {
            "state": self._state.value,
            "mode": self._mode,
            "symbol": self._symbol,
            "last_price": self._last_price,
            "ai_regime": self._ai_regime,
            "drawdown_percent": round(self._risk.drawdown_percent, 3),
            "open_orders": len(
                [lv for lv in self._grid.levels if lv.status in ("BUY_OPEN", "SELL_OPEN")]
            ),
            "portfolio_value": self._executor.paper_balances.get("USDT", 0)
            if self._mode in ("paper", "dry_run")
            else 0,
            "consecutive_losses": self._risk.consecutive_losses,
            "cooldown_remaining": round(self._risk.cooldown_remaining_seconds, 0),
            "grid_levels": [
                {"idx": lv.idx, "price": lv.price, "side": lv.side, "status": lv.status}
                for lv in self._grid.levels
            ],
        }

    async def _shutdown(self) -> None:
        logger.info("Bot shutting down...")
        self._state = BotState.STOPPING
        await self._ws.stop()
        if self._rest:
            await self._rest.close()
        await self._cache.close()
        await self._repo.log_event("BOT_STOP", "graceful shutdown")
        await self._repo.close()
        logger.info("Bot stopped")
