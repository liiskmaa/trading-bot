"""
Backtest result metrics.  Pure functions, no I/O.
"""

from dataclasses import dataclass
from typing import Sequence


@dataclass
class BacktestMetrics:
    total_trades: int
    winning_trades: int
    losing_trades: int
    total_profit_usdt: float
    max_drawdown_percent: float
    win_rate_percent: float
    avg_profit_per_trade: float
    profit_factor: float   # gross_profit / gross_loss
    grid_cycles_completed: int
    start_price: float
    end_price: float
    price_change_percent: float

    def __str__(self) -> str:
        return (
            f"Trades       : {self.total_trades} "
            f"(W:{self.winning_trades} L:{self.losing_trades})\n"
            f"Win rate     : {self.win_rate_percent:.1f}%\n"
            f"Net profit   : {self.total_profit_usdt:+.2f} USDT\n"
            f"Avg/trade    : {self.avg_profit_per_trade:+.4f} USDT\n"
            f"Profit factor: {self.profit_factor:.2f}\n"
            f"Max drawdown : {self.max_drawdown_percent:.2f}%\n"
            f"Grid cycles  : {self.grid_cycles_completed}\n"
            f"Price Δ      : {self.price_change_percent:+.2f}% "
            f"({self.start_price:.2f} → {self.end_price:.2f})"
        )


def compute_metrics(
    trades: Sequence[dict],
    initial_capital: float,
    equity_curve: Sequence[float],
) -> BacktestMetrics:
    if not trades:
        return BacktestMetrics(
            total_trades=0, winning_trades=0, losing_trades=0,
            total_profit_usdt=0.0, max_drawdown_percent=0.0,
            win_rate_percent=0.0, avg_profit_per_trade=0.0,
            profit_factor=0.0, grid_cycles_completed=0,
            start_price=0.0, end_price=0.0, price_change_percent=0.0,
        )

    pnls = [t.get("realized_pnl", 0.0) for t in trades]
    winning = [p for p in pnls if p > 0]
    losing = [p for p in pnls if p <= 0]
    gross_profit = sum(winning)
    gross_loss = abs(sum(losing))

    max_dd = _max_drawdown(equity_curve) if equity_curve else 0.0
    start_price = trades[0].get("price", 0.0)
    end_price = trades[-1].get("price", 0.0)
    price_chg = (end_price - start_price) / start_price * 100 if start_price else 0.0

    # Grid cycles = pairs of buy→sell completions
    completed_sells = [t for t in trades if t.get("side") == "SELL"]

    return BacktestMetrics(
        total_trades=len(trades),
        winning_trades=len(winning),
        losing_trades=len(losing),
        total_profit_usdt=sum(pnls),
        max_drawdown_percent=max_dd,
        win_rate_percent=len(winning) / len(pnls) * 100,
        avg_profit_per_trade=sum(pnls) / len(pnls),
        profit_factor=gross_profit / gross_loss if gross_loss > 0 else float("inf"),
        grid_cycles_completed=len(completed_sells),
        start_price=start_price,
        end_price=end_price,
        price_change_percent=price_chg,
    )


def _max_drawdown(equity: Sequence[float]) -> float:
    peak = equity[0]
    max_dd = 0.0
    for v in equity:
        if v > peak:
            peak = v
        dd = (peak - v) / peak * 100 if peak > 0 else 0.0
        max_dd = max(max_dd, dd)
    return max_dd
