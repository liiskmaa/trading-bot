"""
Pure, stateless grid level math.
No I/O — all functions are deterministic and easy to unit-test.
"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class GridLevel:
    idx: int
    price: float
    side: str              # "BUY" | "SELL"
    status: str = "PENDING"
    # PENDING | BUY_OPEN | BUY_FILLED | SELL_OPEN | SELL_FILLED | DISABLED
    client_order_id: Optional[str] = None
    exchange_order_id: Optional[str] = None
    quantity: float = 0.0


def compute_levels(
    current_price: float,
    range_percent: float,
    num_levels: int,
    order_size_usdt: float,
) -> list[GridLevel]:
    """
    Returns num_levels evenly spaced grid levels spanning
    ±range_percent around current_price.

    Levels below current_price receive side=BUY.
    Levels above (or equal to) current_price receive side=SELL.
    Quantity is computed as order_size_usdt / level_price.
    """
    if num_levels < 2:
        raise ValueError("Need at least 2 grid levels")

    lower = current_price * (1.0 - range_percent / 100.0)
    upper = current_price * (1.0 + range_percent / 100.0)
    step = (upper - lower) / (num_levels - 1)

    levels: list[GridLevel] = []
    for i in range(num_levels):
        price = lower + i * step
        side = "BUY" if price < current_price else "SELL"
        qty = order_size_usdt / price
        levels.append(GridLevel(idx=i, price=price, side=side, quantity=qty))

    return levels


def round_price(price: float, precision: int) -> float:
    return round(price, precision)


def round_qty(qty: float, precision: int) -> float:
    return round(qty, precision)


def grid_profit_per_cycle(
    buy_price: float,
    sell_price: float,
    quantity: float,
    fee_rate: float = 0.001,
) -> float:
    """
    Net profit for one complete buy → sell cycle after Binance fees.
    fee_rate is the taker/maker rate (default 0.1%).
    """
    cost = buy_price * quantity * (1 + fee_rate)
    revenue = sell_price * quantity * (1 - fee_rate)
    return revenue - cost


def is_price_in_range(
    current_price: float,
    reference_price: float,
    range_percent: float,
) -> bool:
    deviation = abs(current_price - reference_price) / reference_price * 100
    return deviation <= range_percent
