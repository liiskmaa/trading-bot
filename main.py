"""
Entry point.

Usage:
  python main.py                          # run bot (paper mode by default)
  python main.py --mode dry_run           # log only, no orders
  python main.py --config my_config.yaml  # use a custom config file
  python main.py backtest --start 2024-01-01 --end 2024-06-30
"""

import asyncio
import logging
import logging.handlers
import signal
import sys
from pathlib import Path

import click

from config import Config
from core.bot import Bot
from core.state import BotState
from market_data import BinanceRest, BinanceWebSocket, CandleAggregator
from grid_engine import GridManager
from execution import OrderExecutor, Reconciler
from risk import RiskManager
from ai_filter import MarketClassifier
from backtesting import BacktestEngine
from database import Repository
from cache import Cache
from monitoring import MonitoringServer


def setup_logging(cfg: Config) -> None:
    log_dir = Path(cfg.str("logging", "path", default="logs/"))
    log_dir.mkdir(exist_ok=True)

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-8s [%(name)s] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    level = getattr(logging, cfg.str("logging", "level", default="INFO").upper(), logging.INFO)

    root = logging.getLogger()
    root.setLevel(level)

    # Console
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(sh)

    # Rotating file
    fh = logging.handlers.RotatingFileHandler(
        log_dir / "bot.log",
        maxBytes=cfg.int("logging", "max_bytes", default=10_485_760),
        backupCount=cfg.int("logging", "backup_count", default=5),
    )
    fh.setFormatter(fmt)
    root.addHandler(fh)


def build_bot(cfg: Config, mode_override: str | None = None) -> Bot:
    mode = mode_override or cfg.str("trading", "mode", default="paper")
    symbol = cfg.str("trading", "symbol", default="BTCUSDT")
    testnet = cfg.bool("binance", "testnet", default=True)

    # Infrastructure
    repo = Repository(cfg.str("database", "path", default="data/trading_bot.db"))
    cache = Cache(
        host=cfg.str("redis", "host", default="localhost"),
        port=cfg.int("redis", "port", default=6379),
        db=cfg.int("redis", "db", default=0),
        password=cfg.str("redis", "password", default=""),
        ttl_config=cfg.get("redis", "ttl") or {},
    )

    rest = BinanceRest(
        api_key=cfg.str("binance", "api_key"),
        api_secret=cfg.str("binance", "api_secret"),
        testnet=testnet,
        retries=cfg.int("binance", "order_retry_attempts", default=3),
        retry_delay=cfg.float("binance", "order_retry_delay_seconds", default=2.0),
    )

    candles = CandleAggregator(symbol)
    candles.set_repo(repo)

    # Grid
    grid_manager = GridManager(
        symbol=symbol,
        range_percent=cfg.float("grid", "range_percent", default=5.0),
        num_levels=cfg.int("grid", "levels", default=10),
        order_size_usdt=cfg.float("capital", "order_size_usdt", default=29.0),
        rebuild_threshold_percent=cfg.float("grid", "rebuild_threshold_percent", default=3.0),
        price_precision=2,
        qty_precision=5,
    )

    # Execution
    executor = OrderExecutor(
        symbol=symbol,
        mode=mode,
        rest=rest if mode == "live" else None,
        repo=repo,
    )

    # Risk
    risk = RiskManager(
        active_capital_usdt=cfg.float("capital", "active_trading_usdt", default=324.0),
        max_drawdown_percent=cfg.float("risk", "max_drawdown_percent", default=8.0),
        consecutive_loss_limit=cfg.int("risk", "consecutive_loss_limit", default=3),
        cooldown_minutes=cfg.float("risk", "cooldown_minutes", default=20.0),
        emergency_price_move_percent=cfg.float("risk", "emergency_price_move_percent", default=7.0),
        emergency_window_seconds=cfg.float("risk", "emergency_price_window_seconds", default=300.0),
    )

    # AI filter
    ai = MarketClassifier(
        cache_ttl_seconds=cfg.int("ai_filter", "cache_ttl_seconds", default=60),
        call_interval_seconds=cfg.int("ai_filter", "call_interval_seconds", default=60),
    )
    ai.inject(cache, symbol)

    # WebSocket (callbacks wired after Bot is created)
    ws = BinanceWebSocket(
        symbol=symbol,
        testnet=testnet,
        on_price=lambda p: None,    # replaced below after Bot init
        on_execution=lambda e: None,
        reconnect_delay=cfg.float("binance", "ws_reconnect_delay_seconds", default=5.0),
        max_reconnect_delay=cfg.float("binance", "ws_max_reconnect_delay_seconds", default=60.0),
    )

    # Reconciler (live only)
    reconciler = Reconciler(symbol, rest, repo, grid_manager)

    # Monitoring
    monitoring = None
    if cfg.bool("monitoring", "enabled", default=True):
        monitoring = MonitoringServer(
            host=cfg.str("monitoring", "host", default="0.0.0.0"),
            port=cfg.int("monitoring", "port", default=8080),
        )

    # Wire grid manager deps
    grid_manager.inject(repo, cache, executor)

    bot = Bot(
        config=cfg,
        repo=repo,
        cache=cache,
        rest=rest,
        ws=ws,
        candles=candles,
        grid_manager=grid_manager,
        executor=executor,
        reconciler=reconciler,
        risk=risk,
        ai_classifier=ai,
        monitoring=monitoring,
    )

    # Wire WebSocket callbacks to bot handlers
    ws._on_price = bot.on_price
    ws._on_execution = bot.on_execution_report

    return bot


# ------------------------------------------------------------------ #
# CLI
# ------------------------------------------------------------------ #

@click.group(invoke_without_command=True)
@click.pass_context
@click.option("--config", "-c", default="config/config.yaml", show_default=True)
@click.option(
    "--mode", "-m",
    type=click.Choice(["dry_run", "paper", "live"]),
    default=None,
    help="Override trading mode from config",
)
def cli(ctx, config, mode):
    """BTC/USDT Grid Trading Bot"""
    if ctx.invoked_subcommand is None:
        cfg = Config(config)
        setup_logging(cfg)
        bot = build_bot(cfg, mode_override=mode)
        _run_bot(bot)


@cli.command()
@click.option("--config", "-c", default="config/config.yaml", show_default=True)
@click.option("--start", required=True, help="Start date YYYY-MM-DD")
@click.option("--end", required=True, help="End date YYYY-MM-DD")
def backtest(config, start, end):
    """Run backtesting simulation against historical data."""
    cfg = Config(config)
    setup_logging(cfg)

    rest = BinanceRest(
        api_key="",
        api_secret="",
        testnet=cfg.bool("binance", "testnet", default=True),
    )

    engine = BacktestEngine(
        rest=rest,
        symbol=cfg.str("trading", "symbol", default="BTCUSDT"),
        range_percent=cfg.float("grid", "range_percent", default=5.0),
        num_levels=cfg.int("grid", "levels", default=10),
        order_size_usdt=cfg.float("capital", "order_size_usdt", default=29.0),
        active_capital_usdt=cfg.float("capital", "active_trading_usdt", default=324.0),
        max_drawdown_percent=cfg.float("risk", "max_drawdown_percent", default=8.0),
    )

    async def _run():
        await rest.open()
        metrics = await engine.run(start, end)
        print("\n=== Backtest Results ===")
        print(metrics)
        await rest.close()

    asyncio.run(_run())


@cli.command("train-regime")
@click.option("--config", "-c", default="config/config.yaml", show_default=True)
@click.option("--min-candles", default=500, show_default=True,
              help="Minimum candles required before training starts")
def train_regime(config, min_candles):
    """Train the market regime classifier from stored candle data.

    The bot must have been running long enough to accumulate candle history
    (or backtest first to populate the database). Retrain whenever you have
    significantly more data.
    """
    from ai_filter.trainer import train

    cfg = Config(config)
    setup_logging(cfg)

    repo = Repository(cfg.str("database", "path", default="data/trading_bot.db"))
    symbol = cfg.str("trading", "symbol", default="BTCUSDT")

    async def _run():
        await repo.open()
        candles = await repo.get_candles(symbol, "1m", limit=100_000)
        await repo.close()

        if len(candles) < min_candles:
            click.echo(
                f"Not enough candles: {len(candles)} available, {min_candles} required.\n"
                "Run the bot in paper mode or run a backtest first."
            )
            return

        click.echo(f"Training on {len(candles)} candles...")
        train(candles)
        click.echo("Done. Model saved to data/regime_model.pkl")

    asyncio.run(_run())


def _run_bot(bot: Bot) -> None:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def _handle_signal():
        logging.getLogger(__name__).info("Shutdown signal received")
        for task in asyncio.all_tasks(loop):
            task.cancel()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _handle_signal)

    try:
        loop.run_until_complete(bot.run())
    except KeyboardInterrupt:
        pass
    finally:
        loop.close()


if __name__ == "__main__":
    cli()
