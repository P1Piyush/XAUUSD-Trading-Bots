"""
Main entry point for the XAUUSD Institutional Trading System.

Bootstraps the entire system with proper startup/shutdown lifecycle,
argument parsing, logging configuration, MT5 initialization, and
signal handling for graceful shutdown.
"""

import argparse
import asyncio
import logging
import os
import signal
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

# Ensure project root is on sys.path for direct script execution
_project_root = str(Path(__file__).resolve().parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

try:
    import MetaTrader5 as mt5  # type: ignore[import-untyped]

    MT5_AVAILABLE = True
except ImportError:
    mt5 = None  # type: ignore[assignment]
    MT5_AVAILABLE = False

from src.config import DatabaseConfig, SymbolConfig
from src.core_engine import CoreEngine

logger = logging.getLogger(__name__)


def parse_arguments() -> argparse.Namespace:
    """Parse command-line arguments.

    Returns:
        Parsed arguments namespace.
    """
    parser = argparse.ArgumentParser(
        description="XAUUSD Institutional Trading System",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python src/main.py --dry-run                 # Test without MT5\n"
            "  python src/main.py --log-level DEBUG         # Verbose logging\n"
            "  python src/main.py --db-path data/test.db    # Custom DB path\n"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Run without MT5 connection (for testing module integration)",
    )
    parser.add_argument(
        "--log-level",
        type=str,
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default="INFO",
        help="Logging level (default: INFO)",
    )
    parser.add_argument(
        "--db-path",
        type=str,
        default=None,
        help="Override database file path (default: data/trading_state.db)",
    )
    parser.add_argument(
        "--scan-interval",
        type=float,
        default=30.0,
        help="Signal scan interval in seconds (default: 30)",
    )
    return parser.parse_args()


def setup_logging(log_level: str) -> None:
    """Configure logging with rotating file handler and console output.

    Args:
        log_level: Logging level string (DEBUG, INFO, WARNING, ERROR).
    """
    log_format = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
    level = getattr(logging, log_level.upper(), logging.INFO)

    # Create logs directory
    log_dir = Path("logs")
    log_dir.mkdir(parents=True, exist_ok=True)

    # Root logger configuration
    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    # Clear any existing handlers
    root_logger.handlers.clear()

    # Rotating file handler: 10MB max, 5 backups
    file_handler = RotatingFileHandler(
        filename=str(log_dir / "trading_system.log"),
        maxBytes=10 * 1024 * 1024,  # 10MB
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(logging.Formatter(log_format))
    root_logger.addHandler(file_handler)

    # Console (stdout) handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level)
    console_handler.setFormatter(logging.Formatter(log_format))
    root_logger.addHandler(console_handler)


def initialize_mt5(symbol: str) -> bool:
    """Initialize MetaTrader 5 connection.

    Args:
        symbol: Trading symbol to verify availability.

    Returns:
        True if MT5 initialized successfully, False otherwise.
    """
    if not MT5_AVAILABLE or mt5 is None:
        logger.warning("MetaTrader5 package not available (expected on Linux)")
        return False

    try:
        if not mt5.initialize():
            logger.error("MT5 initialize() failed: %s", mt5.last_error())
            return False

        # Log account info
        account = mt5.account_info()
        if account is not None:
            logger.info(
                "MT5 connected: server=%s, login=%d, balance=%.2f, leverage=%d",
                account.server,
                account.login,
                account.balance,
                account.leverage,
            )
        else:
            logger.warning("MT5 initialized but account_info() returned None")

        # Verify symbol is available
        symbol_info = mt5.symbol_info(symbol)
        if symbol_info is None:
            logger.error("Symbol %s not found in MT5", symbol)
            mt5.shutdown()
            return False

        if not symbol_info.visible:
            # Try to enable the symbol
            if not mt5.symbol_select(symbol, True):
                logger.error("Failed to enable symbol %s", symbol)
                mt5.shutdown()
                return False

        logger.info(
            "Symbol %s available: bid=%.5f, ask=%.5f, spread=%d",
            symbol,
            symbol_info.bid,
            symbol_info.ask,
            symbol_info.spread,
        )
        return True

    except Exception as exc:
        logger.error("MT5 initialization error: %s", exc)
        return False


async def run_dry_mode(engine: CoreEngine) -> None:
    """Run the engine in dry-run mode for testing.

    Starts the engine, waits briefly to demonstrate the lifecycle,
    then shuts down gracefully.

    Args:
        engine: CoreEngine instance to run.
    """
    logger.info("DRY-RUN MODE: Starting engine for integration test")

    # Start engine in background
    engine_task = asyncio.create_task(engine.start())

    # Let it run briefly to exercise the loops
    await asyncio.sleep(2.0)

    # Graceful shutdown
    logger.info("DRY-RUN MODE: Initiating graceful shutdown")
    await engine.stop()

    # Wait for engine task to finish
    try:
        await asyncio.wait_for(engine_task, timeout=5.0)
    except asyncio.TimeoutError:
        logger.warning("Engine task did not finish within timeout")
        engine_task.cancel()
        try:
            await engine_task
        except asyncio.CancelledError:
            pass

    logger.info("DRY-RUN MODE: Shutdown complete")


async def run_live(engine: CoreEngine) -> None:
    """Run the engine in live trading mode.

    Sets up signal handlers for graceful shutdown via SIGINT/SIGTERM.

    Args:
        engine: CoreEngine instance to run.
    """
    loop = asyncio.get_running_loop()
    shutdown_event = asyncio.Event()

    def _signal_handler() -> None:
        logger.info("Shutdown signal received")
        shutdown_event.set()

    # Register signal handlers
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _signal_handler)

    # Start engine in background
    engine_task = asyncio.create_task(engine.start())

    # Wait for shutdown signal
    await shutdown_event.wait()

    # Graceful shutdown
    logger.info("Shutting down engine...")
    await engine.stop()

    # Wait for engine task to finish
    try:
        await asyncio.wait_for(engine_task, timeout=10.0)
    except asyncio.TimeoutError:
        logger.warning("Engine task did not finish within timeout")
        engine_task.cancel()
        try:
            await engine_task
        except asyncio.CancelledError:
            pass


def main() -> None:
    """Main entry point for the trading system."""
    args = parse_arguments()

    # Setup logging
    setup_logging(args.log_level)
    logger.info("XAUUSD Institutional Trading System starting...")
    logger.info("Arguments: %s", vars(args))

    # Configuration overrides
    db_config: Optional[DatabaseConfig] = None
    if args.db_path:
        db_config = DatabaseConfig(db_path=args.db_path)

    symbol_config = SymbolConfig()
    mt5_initialized = False

    # MT5 initialization (skip in dry-run mode)
    if not args.dry_run:
        mt5_initialized = initialize_mt5(symbol_config.symbol)
        if not mt5_initialized:
            logger.warning(
                "MT5 not initialized - system will run with limited functionality"
            )
    else:
        logger.info("DRY-RUN MODE: Skipping MT5 initialization")
        if not MT5_AVAILABLE:
            logger.info("MetaTrader5 package not available (expected on Linux)")

    # Create CoreEngine
    engine = CoreEngine(
        db_config=db_config,
        scan_interval=args.scan_interval,
        management_interval=5.0,
    )

    # Run
    try:
        if args.dry_run:
            asyncio.run(run_dry_mode(engine))
        else:
            asyncio.run(run_live(engine))
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt received")
    except Exception as exc:
        logger.error("Unhandled exception: %s", exc, exc_info=True)
        sys.exit(1)
    finally:
        # MT5 shutdown
        if mt5_initialized and MT5_AVAILABLE and mt5 is not None:
            try:
                mt5.shutdown()
                logger.info("MT5 shutdown complete")
            except Exception as exc:
                logger.error("Error during MT5 shutdown: %s", exc)

        logger.info("XAUUSD Institutional Trading System terminated")


if __name__ == "__main__":
    main()
