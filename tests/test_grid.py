"""
Unit tests for grid calculator and manager logic.
"""

import pytest
from grid_engine.calculator import (
    compute_levels,
    grid_profit_per_cycle,
    is_price_in_range,
    round_price,
    round_qty,
)


class TestComputeLevels:
    def test_correct_level_count(self):
        levels = compute_levels(50_000, 5.0, 10, 29.0)
        assert len(levels) == 10

    def test_levels_span_correct_range(self):
        price = 50_000.0
        levels = compute_levels(price, 5.0, 10, 29.0)
        assert abs(levels[0].price - price * 0.95) < 1.0
        assert abs(levels[-1].price - price * 1.05) < 1.0

    def test_buy_below_sell_above(self):
        price = 50_000.0
        levels = compute_levels(price, 5.0, 10, 29.0)
        buys = [lv for lv in levels if lv.side == "BUY"]
        sells = [lv for lv in levels if lv.side == "SELL"]
        assert all(lv.price < price for lv in buys)
        assert all(lv.price >= price for lv in sells)

    def test_quantities_positive(self):
        levels = compute_levels(50_000, 5.0, 10, 29.0)
        for lv in levels:
            assert lv.quantity > 0

    def test_quantity_approximately_correct(self):
        price = 50_000.0
        order_size = 29.0
        levels = compute_levels(price, 5.0, 10, order_size)
        for lv in levels:
            notional = lv.price * lv.quantity
            assert abs(notional - order_size) / order_size < 0.15  # within 15% of target

    def test_indices_are_sequential(self):
        levels = compute_levels(50_000, 5.0, 10, 29.0)
        assert [lv.idx for lv in levels] == list(range(10))

    def test_minimum_two_levels_required(self):
        with pytest.raises(ValueError):
            compute_levels(50_000, 5.0, 1, 29.0)

    def test_levels_are_sorted_ascending(self):
        levels = compute_levels(50_000, 5.0, 10, 29.0)
        prices = [lv.price for lv in levels]
        assert prices == sorted(prices)


class TestGridProfitPerCycle:
    def test_profitable_cycle(self):
        profit = grid_profit_per_cycle(47_500, 48_000, 0.0006, 0.001)
        assert profit > 0

    def test_profit_increases_with_spread(self):
        p1 = grid_profit_per_cycle(47_000, 48_000, 0.0006, 0.001)
        p2 = grid_profit_per_cycle(46_000, 48_000, 0.0006, 0.001)
        assert p2 > p1

    def test_fee_reduces_profit(self):
        p_low_fee = grid_profit_per_cycle(47_500, 48_000, 0.0006, 0.0005)
        p_high_fee = grid_profit_per_cycle(47_500, 48_000, 0.0006, 0.002)
        assert p_low_fee > p_high_fee


class TestIsInRange:
    def test_within_range(self):
        assert is_price_in_range(50_100, 50_000, 5.0) is True

    def test_outside_range(self):
        assert is_price_in_range(54_000, 50_000, 5.0) is False

    def test_exactly_at_boundary(self):
        assert is_price_in_range(52_500, 50_000, 5.0) is True


class TestRounding:
    def test_round_price(self):
        assert round_price(50_000.12345, 2) == 50_000.12

    def test_round_qty(self):
        assert round_qty(0.000123456, 5) == 0.00012
