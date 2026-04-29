"""
Backtesting engine.

Replays historical 1-minute candles through the grid logic and
reports performance metrics.  No real orders are placed.

Usage:
    engine = BacktestEngine(config)
    await engine.run("BTCUSDT", "2024-01-01", "2024-03-31")
    print(engine.metrics)
"""

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Optional

from grid_engine.calculator import compute_levels, grid_profit_per_cycle, round_price, round_qty
from .metrics import BacktestMetrics, compute_metrics

logger = logging.getLogger(__name__)

_FEE_RATE = 0.001
_KLINE_INTERVAL = "1m"


class BacktestEngine:
    def __init__(
        self,
        rest,                        # BinanceRest (public endpoints only)
        symbol: str,
        range_percent: float,
        num_levels: int,
        order_size_usdt: float,
        active_capital_usdt: float,
        max_drawdown_percent: float = 8.0,
        rebuild_threshold_percent: float = 3.0,
        price_precision: int = 2,
        qty_precision: int = 5,
    ):
        self._rest = rest
        self._symbol = symbol
        self._range_pct = range_percent
        self._num_levels = num_levels
        self._order_size = order_size_usdt
        self._active_capital = active_capital_usdt
        self._max_dd_pct = max_drawdown_percent
        self._rebuild_threshold = rebuild_threshold_percent
        self._price_prec = price_precision
        self._qty_prec = qty_precision

        self._trades: list[dict] = []
        self._equity_curve: list[float] = []
        self._metrics: Optional[BacktestMetrics] = None

    async def run(self, start_date: str, end_date: str) -> BacktestMetrics:
        logger.info(
            "Backtest: %s  %s → %s", self._symbol, start_date, end_date
        )
        candles = await self._fetch_candles(start_date, end_date)
        if len(candles) < 2:
            raise ValueError("Insufficient candle data for backtest")

        logger.info("Replaying %d candles...", len(candles))
        self._replay(candles)
        self._metrics = compute_metrics(
            self._trades, self._active_capital, self._equity_curve
        )
        logger.info("\n%s", self._metrics)
        return self._metrics

    @property
    def metrics(self) -> Optional[BacktestMetrics]:
        return self._metrics

    # ------------------------------------------------------------------ #
    # Data fetch
    # ------------------------------------------------------------------ #

    async def _fetch_candles(self, start_date: str, end_date: str) -> list[dict]:
        start_ms = _date_to_ms(start_date)
        end_ms = _date_to_ms(end_date)
        all_candles: list[dict] = []
        current_start = start_ms

        while current_start < end_ms:
            raw = await self._rest._get(
                "/api/v3/klines",
                {
                    "symbol": self._symbol,
                    "interval": _KLINE_INTERVAL,
                    "startTime": current_start,
                    "endTime": end_ms,
                    "limit": 1000,
                },
            )
            if not raw:
                break
            for k in raw:
                all_candles.append(
                    {
                        "open_time": k[0],
                        "open": float(k[1]),
                        "high": float(k[2]),
                        "low": float(k[3]),
                        "close": float(k[4]),
                        "volume": float(k[5]),
                    }
                )
            current_start = raw[-1][0] + 60_000
            if len(raw) < 1000:
                break

        logger.info("Fetched %d candles", len(all_candles))
        return all_candles

    # ------------------------------------------------------------------ #
    # Replay engine
    # ------------------------------------------------------------------ #

    def _replay(self, candles: list[dict]) -> None:
        usdt = self._active_capital
        btc = 0.0
        reference_price: float = 0.0
        levels: list = []
        peak_value = usdt

        for candle in candles:
            close = candle["close"]
            low = candle["low"]
            high = candle["high"]

            # Rebuild grid if price drifted too far
            if not levels or _needs_rebuild(close, reference_price, self._rebuild_threshold):
                # Cancel remaining open orders at mid price
                levels = compute_levels(close, self._range_pct, self._num_levels, self._order_size)
                for lv in levels:
                    lv.price = round_price(lv.price, self._price_prec)
                    lv.quantity = round_qty(lv.quantity, self._qty_prec)
                reference_price = close

            # Simulate fills within this candle (low→high sweep)
            for lv in levels:
                if lv.status == "PENDING":
                    lv.status = "BUY_OPEN" if lv.side == "BUY" else "SELL_OPEN"

                if lv.status == "BUY_OPEN" and low <= lv.price:
                    cost = lv.price * lv.quantity * (1 + _FEE_RATE)
                    if usdt >= cost:
                        usdt -= cost
                        btc += lv.quantity
                        lv.status = "BUY_FILLED"
                        self._trades.append(
                            {
                                "side": "BUY",
                                "price": lv.price,
                                "quantity": lv.quantity,
                                "realized_pnl": 0.0,
                                "timestamp": candle["open_time"],
                            }
                        )

                elif lv.status == "SELL_OPEN" and high >= lv.price:
                    if btc >= lv.quantity:
                        revenue = lv.price * lv.quantity * (1 - _FEE_RATE)
                        btc -= lv.quantity
                        usdt += revenue
                        pnl = grid_profit_per_cycle(
                            lv.price * (1 - 1 / (1 + self._range_pct / 100 / self._num_levels)),
                            lv.price, lv.quantity, _FEE_RATE,
                        )
                        lv.status = "SELL_FILLED"
                        self._trades.append(
                            {
                                "side": "SELL",
                                "price": lv.price,
                                "quantity": lv.quantity,
                                "realized_pnl": pnl,
                                "timestamp": candle["open_time"],
                            }
                        )

            # Reset filled levels for next tick
            for lv in levels:
                if lv.status == "BUY_FILLED":
                    # Activate paired sell at level+1
                    next_idx = lv.idx + 1
                    if next_idx < len(levels):
                        levels[next_idx].status = "SELL_OPEN"
                    lv.status = "PENDING"
                elif lv.status == "SELL_FILLED":
                    prev_idx = lv.idx - 1
                    if prev_idx >= 0:
                        levels[prev_idx].status = "BUY_OPEN"
                    lv.status = "PENDING"

            portfolio_value = usdt + btc * close
            peak_value = max(peak_value, portfolio_value)
            self._equity_curve.append(portfolio_value)

            # Hard stop on drawdown breach
            dd = (peak_value - portfolio_value) / peak_value * 100
            if dd >= self._max_dd_pct:
                logger.warning(
                    "Backtest drawdown limit hit: %.2f%% at candle %s",
                    dd, candle["open_time"],
                )
                break


def _needs_rebuild(price: float, reference: float, threshold: float) -> bool:
    if reference == 0:
        return True
    return abs(price - reference) / reference * 100 > threshold


def _date_to_ms(date_str: str) -> int:
    dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)
