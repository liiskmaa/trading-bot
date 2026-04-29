"""
Unit tests for risk management rules.
"""

import time
import pytest
from risk.manager import RiskManager, RiskState


def make_risk(**kwargs) -> RiskManager:
    defaults = dict(
        active_capital_usdt=300.0,
        max_drawdown_percent=8.0,
        consecutive_loss_limit=3,
        cooldown_minutes=0.01,   # 0.6s for fast tests
        emergency_price_move_percent=7.0,
        emergency_window_seconds=300.0,
    )
    defaults.update(kwargs)
    return RiskManager(**defaults)


class TestDrawdownRule:
    def test_no_stop_within_limit(self):
        risk = make_risk()
        state = risk.on_trade_result(realized_pnl=-10.0, portfolio_value=280.0)
        assert state == RiskState.OK

    def test_emergency_stop_at_limit(self):
        risk = make_risk(active_capital_usdt=300.0, max_drawdown_percent=8.0)
        # Drawdown of exactly 8%: 300 * 0.92 = 276
        state = risk.on_trade_result(realized_pnl=-24.0, portfolio_value=276.0)
        assert state == RiskState.EMERGENCY_STOP

    def test_emergency_stop_above_limit(self):
        risk = make_risk()
        state = risk.on_trade_result(realized_pnl=-50.0, portfolio_value=250.0)
        assert state == RiskState.EMERGENCY_STOP

    def test_drawdown_percent_calculation(self):
        risk = make_risk(active_capital_usdt=300.0)
        risk.on_trade_result(realized_pnl=0.0, portfolio_value=270.0)
        assert abs(risk.drawdown_percent - 10.0) < 0.01

    def test_peak_tracks_maximum(self):
        risk = make_risk(active_capital_usdt=300.0)
        risk.on_trade_result(realized_pnl=10.0, portfolio_value=310.0)
        risk.on_trade_result(realized_pnl=-5.0, portfolio_value=305.0)
        assert abs(risk.drawdown_percent - (310 - 305) / 310 * 100) < 0.01


class TestConsecutiveLossRule:
    def test_cooldown_after_limit(self):
        risk = make_risk(consecutive_loss_limit=3)
        for _ in range(3):
            state = risk.on_trade_result(realized_pnl=-1.0, portfolio_value=299.0)
        assert state == RiskState.COOLDOWN

    def test_not_triggered_before_limit(self):
        risk = make_risk(consecutive_loss_limit=3)
        for _ in range(2):
            state = risk.on_trade_result(realized_pnl=-1.0, portfolio_value=299.0)
        assert state == RiskState.OK

    def test_counter_resets_on_win(self):
        risk = make_risk(consecutive_loss_limit=3)
        risk.on_trade_result(realized_pnl=-1.0, portfolio_value=299.0)
        risk.on_trade_result(realized_pnl=-1.0, portfolio_value=298.0)
        risk.on_trade_result(realized_pnl=+2.0, portfolio_value=300.0)  # win resets
        state = risk.on_trade_result(realized_pnl=-1.0, portfolio_value=299.0)
        assert state == RiskState.OK  # only 1 loss since last win

    def test_cooldown_lifts_after_period(self):
        risk = make_risk(consecutive_loss_limit=3, cooldown_minutes=0.005)
        for _ in range(3):
            risk.on_trade_result(realized_pnl=-1.0, portfolio_value=299.0)
        assert risk.state == RiskState.COOLDOWN
        time.sleep(0.4)
        state = risk.check_cooldown()
        assert state == RiskState.OK


class TestPriceVelocityRule:
    def test_no_trigger_for_small_move(self):
        risk = make_risk(emergency_price_move_percent=7.0, emergency_window_seconds=300)
        for p in [50_000, 50_100, 50_200]:
            state = risk.on_price(p)
        assert state != RiskState.EMERGENCY_STOP

    def test_trigger_for_large_move(self):
        risk = make_risk(emergency_price_move_percent=7.0, emergency_window_seconds=300)
        risk.on_price(50_000)
        state = risk.on_price(54_000)  # 8% move
        assert state == RiskState.EMERGENCY_STOP

    def test_no_trigger_when_already_stopped(self):
        risk = make_risk(emergency_price_move_percent=7.0)
        risk.on_price(50_000)
        risk.on_price(54_000)  # triggers stop
        # Subsequent calls should not crash
        state = risk.on_price(56_000)
        assert state == RiskState.EMERGENCY_STOP
