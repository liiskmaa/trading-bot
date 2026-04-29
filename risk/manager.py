"""
Risk management.

Enforces hard stops that override all other logic.
Each check is synchronous so it can be called inline in the hot price path
without awaiting I/O (Redis writes happen asynchronously after).

Rules:
  1. Max drawdown 8% of active capital → EMERGENCY_STOP
  2. 3 consecutive losing trades → COOLDOWN (15–30 min)
  3. Price moves >7% within 5 min → EMERGENCY_STOP
"""

import asyncio
import logging
import time
from collections import deque
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


class RiskState(Enum):
    OK = "ok"
    COOLDOWN = "cooldown"
    EMERGENCY_STOP = "emergency_stop"


class RiskManager:
    def __init__(
        self,
        active_capital_usdt: float,
        max_drawdown_percent: float = 8.0,
        consecutive_loss_limit: int = 3,
        cooldown_minutes: float = 20.0,
        emergency_price_move_percent: float = 7.0,
        emergency_window_seconds: float = 300.0,
    ):
        self._active_capital = active_capital_usdt
        self._max_dd_pct = max_drawdown_percent
        self._consec_loss_limit = consecutive_loss_limit
        self._cooldown_seconds = cooldown_minutes * 60
        self._emergency_pct = emergency_price_move_percent
        self._emergency_window = emergency_window_seconds

        # Runtime state
        self._peak_value: float = active_capital_usdt
        self._current_value: float = active_capital_usdt
        self._consecutive_losses: int = 0
        self._cooldown_until: float = 0.0
        self._state: RiskState = RiskState.OK
        self._stop_reason: str = ""

        # Circular buffer of (timestamp, price) for velocity check
        self._price_history: deque = deque(maxlen=600)

        # Async callback to notify the bot of state changes
        self._on_stop: Optional[callable] = None

    def set_stop_callback(self, cb) -> None:
        self._on_stop = cb

    # ------------------------------------------------------------------ #
    # Public checks — called on every price tick
    # ------------------------------------------------------------------ #

    def on_price(self, price: float) -> RiskState:
        """Update price history and check velocity rule."""
        self._price_history.append((time.time(), price))
        if self._state == RiskState.OK:
            if self._check_price_velocity(price):
                self._trigger_emergency("Price moved >%.1f%% in %.0fs" % (
                    self._emergency_pct, self._emergency_window
                ))
        return self._state

    def on_trade_result(self, realized_pnl: float, portfolio_value: float) -> RiskState:
        """Call after every completed buy→sell cycle."""
        self._current_value = portfolio_value
        self._peak_value = max(self._peak_value, portfolio_value)

        drawdown = (self._peak_value - self._current_value) / self._peak_value * 100
        if drawdown >= self._max_dd_pct:
            self._trigger_emergency(
                f"Max drawdown reached: {drawdown:.2f}% >= {self._max_dd_pct}%"
            )
            return self._state

        if realized_pnl < 0:
            self._consecutive_losses += 1
            logger.info(
                "Consecutive losses: %d / %d",
                self._consecutive_losses, self._consec_loss_limit,
            )
            if self._consecutive_losses >= self._consec_loss_limit:
                self._trigger_cooldown()
        else:
            self._consecutive_losses = 0

        return self._state

    def check_cooldown(self) -> RiskState:
        """Lift cooldown if the pause period has elapsed."""
        if self._state == RiskState.COOLDOWN and time.time() >= self._cooldown_until:
            self._state = RiskState.OK
            self._consecutive_losses = 0
            logger.info("Cooldown expired — resuming grid trading")
        return self._state

    @property
    def state(self) -> RiskState:
        return self._state

    @property
    def stop_reason(self) -> str:
        return self._stop_reason

    @property
    def drawdown_percent(self) -> float:
        if self._peak_value == 0:
            return 0.0
        return (self._peak_value - self._current_value) / self._peak_value * 100

    @property
    def cooldown_remaining_seconds(self) -> float:
        return max(0.0, self._cooldown_until - time.time())

    def update_portfolio_value(self, value: float) -> None:
        self._current_value = value
        self._peak_value = max(self._peak_value, value)

    # ------------------------------------------------------------------ #
    # Internal
    # ------------------------------------------------------------------ #

    def _check_price_velocity(self, current_price: float) -> bool:
        cutoff = time.time() - self._emergency_window
        window_prices = [p for ts, p in self._price_history if ts >= cutoff]
        if len(window_prices) < 2:
            return False
        oldest = window_prices[0]
        move_pct = abs(current_price - oldest) / oldest * 100
        return move_pct >= self._emergency_pct

    def _trigger_emergency(self, reason: str) -> None:
        self._state = RiskState.EMERGENCY_STOP
        self._stop_reason = reason
        logger.critical("EMERGENCY STOP: %s", reason)
        if self._on_stop:
            asyncio.create_task(self._on_stop(reason))

    def _trigger_cooldown(self) -> None:
        self._state = RiskState.COOLDOWN
        self._cooldown_until = time.time() + self._cooldown_seconds
        self._stop_reason = (
            f"{self._consecutive_losses} consecutive losing trades"
        )
        logger.warning(
            "Entering COOLDOWN for %.0f minutes: %s",
            self._cooldown_seconds / 60,
            self._stop_reason,
        )
        if self._on_stop:
            asyncio.create_task(self._on_stop(self._stop_reason))
