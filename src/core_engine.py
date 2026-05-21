"""
Core Engine module for XAUUSD Institutional Trading System.

Provides the main async event loop orchestrator that ties all modules together:
MarketIntelligence, AlphaModel, RiskGuardian, OrderRouter, and TradingDatabase.
Handles startup, state recovery, signal generation, trade management, and
graceful shutdown.
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional

try:
    import MetaTrader5 as mt5  # type: ignore[import-untyped]

    MT5_AVAILABLE = True
except ImportError:
    mt5 = None  # type: ignore[assignment]
    MT5_AVAILABLE = False

from src.alpha_model import AlphaModel
from src.config import (
    AlphaConfig,
    DatabaseConfig,
    OrderConfig,
    PropFirmConfig,
    RiskConfig,
    SessionConfig,
    SymbolConfig,
)
from src.database import TradingDatabase
from src.market_intelligence import MarketIntelligence
from src.order_router import OrderRouter
from src.risk_guardian import RiskGuardian

logger = logging.getLogger(__name__)


class CoreEngine:
    """Main async orchestrator for the institutional trading system.

    Coordinates all modules through startup, state recovery, signal generation,
    trade management, and graceful shutdown. Runs as concurrent asyncio tasks
    for signal scanning and position management.
    """

    def __init__(
        self,
        db_config: Optional[DatabaseConfig] = None,
        prop_config: Optional[PropFirmConfig] = None,
        risk_config: Optional[RiskConfig] = None,
        alpha_config: Optional[AlphaConfig] = None,
        order_config: Optional[OrderConfig] = None,
        session_config: Optional[SessionConfig] = None,
        symbol_config: Optional[SymbolConfig] = None,
        scan_interval: float = 30.0,
        management_interval: float = 5.0,
    ) -> None:
        """Initialize all module instances.

        Args:
            db_config: Database configuration override.
            prop_config: Prop firm risk limits override.
            risk_config: Risk monitoring parameters override.
            alpha_config: Alpha model parameters override.
            order_config: Order execution parameters override.
            session_config: Session time windows override.
            symbol_config: Symbol-specific configuration override.
            scan_interval: Seconds between signal scans (default 30).
            management_interval: Seconds between position management checks (default 5).
        """
        self._db_config = db_config or DatabaseConfig()
        self._prop_config = prop_config or PropFirmConfig()
        self._risk_config = risk_config or RiskConfig()
        self._alpha_config = alpha_config or AlphaConfig()
        self._order_config = order_config or OrderConfig()
        self._session_config = session_config or SessionConfig()
        self._symbol_config = symbol_config or SymbolConfig()

        self._scan_interval = scan_interval
        self._management_interval = management_interval
        self._running = False
        self._tasks: List[asyncio.Task] = []  # type: ignore[type-arg]

        # Module instances
        self._database = TradingDatabase(config=self._db_config)
        self._market_intelligence = MarketIntelligence(
            alpha_config=self._alpha_config,
            session_config=self._session_config,
            symbol_config=self._symbol_config,
        )
        self._alpha_model = AlphaModel(
            market_intelligence=self._market_intelligence,
            database=self._database,
            alpha_config=self._alpha_config,
            risk_config=self._risk_config,
            prop_config=self._prop_config,
            order_config=self._order_config,
            symbol_config=self._symbol_config,
        )
        self._risk_guardian = RiskGuardian(
            database=self._database,
            prop_config=self._prop_config,
            risk_config=self._risk_config,
            db_config=self._db_config,
        )
        self._order_router = OrderRouter(
            risk_guardian=self._risk_guardian,
            database=self._database,
            order_config=self._order_config,
            symbol_config=self._symbol_config,
            session_config=self._session_config,
        )

    # ------------------------------------------------------------------
    # Main Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Main orchestration method.

        Initializes the database, recovers state from any prior session,
        starts risk monitoring, loads trade history for Kelly calculations,
        then launches signal and trade management loops as concurrent tasks.
        """
        logger.info("CoreEngine starting...")
        self._running = True

        # Initialize database
        await self._database.initialize()
        logger.info("Database initialized")

        # Recover state from any prior session
        await self._recover_state()

        # Start risk guardian monitoring as a background task
        risk_task = await self._risk_guardian.start_monitoring()
        self._tasks.append(risk_task)
        logger.info("RiskGuardian monitoring started")

        # Load trade history for Kelly calculations
        await self._alpha_model.load_trade_history()
        logger.info("Trade history loaded for Kelly calculations")

        # Launch signal and management loops
        signal_task = asyncio.create_task(
            self._signal_loop(), name="signal_loop"
        )
        management_task = asyncio.create_task(
            self._trade_management_loop(), name="trade_management_loop"
        )
        self._tasks.append(signal_task)
        self._tasks.append(management_task)

        logger.info(
            "CoreEngine running: signal_interval=%.1fs, management_interval=%.1fs",
            self._scan_interval,
            self._management_interval,
        )

        # Wait for all tasks to complete (or be cancelled)
        try:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        except asyncio.CancelledError:
            logger.info("CoreEngine tasks cancelled")
        except Exception as exc:
            logger.error("CoreEngine error: %s", exc, exc_info=True)
        finally:
            self._running = False
            logger.info("CoreEngine event loop exited")

    async def stop(self) -> None:
        """Graceful shutdown.

        Cancels all asyncio tasks, waits for cleanup, saves final state
        to the database, and closes the database connection.
        """
        logger.info("CoreEngine stopping...")
        self._running = False

        # Cancel all tasks
        for task in self._tasks:
            if not task.done():
                task.cancel()

        # Wait for tasks to finish cancellation
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

        # Save final state
        try:
            await self._database.set_system_state(
                "last_shutdown",
                datetime.now(timezone.utc).isoformat(),
            )
            await self._database.set_system_state("shutdown_clean", "true")
        except Exception as exc:
            logger.error("Error saving final state: %s", exc)

        # Close database connection
        await self._database.close()
        logger.info("CoreEngine shutdown complete")

    # ------------------------------------------------------------------
    # State Recovery
    # ------------------------------------------------------------------

    async def _recover_state(self) -> None:
        """VM reboot recovery.

        Reads all active trades from the database, verifies each ticket
        still exists in MT5, updates fields if partially closed, and marks
        trades as closed if the position no longer exists. Stores
        initial_deposit in system_state if not already set.
        """
        logger.info("Recovering state from database...")

        active_trades = await self._database.get_active_trades()
        if not active_trades:
            logger.info("No active trades to recover")
            await self._ensure_initial_deposit()
            return

        logger.info("Found %d active trades in database", len(active_trades))

        recovered = 0
        reconciled = 0

        for trade in active_trades:
            ticket = trade["ticket"]

            if not MT5_AVAILABLE or mt5 is None:
                logger.warning(
                    "MT5 not available - cannot verify ticket=%d", ticket
                )
                continue

            try:
                positions = mt5.positions_get(ticket=ticket)

                if positions is not None and len(positions) > 0:
                    # Position still exists in MT5
                    position = positions[0]
                    updates: Dict[str, object] = {}

                    # Check if lot size changed (partial close happened externally)
                    if abs(position.volume - trade["lot"]) > 0.001:
                        updates["lot"] = position.volume
                        updates["partial_closed"] = 1
                        logger.info(
                            "Ticket=%d lot updated: %.2f -> %.2f (external partial close)",
                            ticket,
                            trade["lot"],
                            position.volume,
                        )

                    if updates:
                        await self._database.update_trade(ticket, updates)
                        reconciled += 1

                    recovered += 1
                    logger.debug("Ticket=%d verified active in MT5", ticket)

                else:
                    # Position no longer exists - mark as closed
                    await self._database.update_trade(
                        ticket,
                        {
                            "status": "closed",
                            "close_time": datetime.now(timezone.utc).isoformat(),
                        },
                    )
                    reconciled += 1
                    logger.info(
                        "Ticket=%d no longer in MT5 - marked closed (reconciled)",
                        ticket,
                    )

            except Exception as exc:
                logger.error(
                    "Error recovering ticket=%d: %s", ticket, exc
                )

        # Store initial deposit if not set
        await self._ensure_initial_deposit()

        logger.info(
            "State recovery complete: %d recovered, %d reconciled out of %d active",
            recovered,
            reconciled,
            len(active_trades),
        )

    async def _ensure_initial_deposit(self) -> None:
        """Store initial_deposit in system_state if not already set."""
        existing = await self._database.get_system_state("initial_deposit")
        if existing is not None:
            return

        if MT5_AVAILABLE and mt5 is not None:
            try:
                account = mt5.account_info()
                if account is not None:
                    await self._database.set_system_state(
                        "initial_deposit", str(account.balance)
                    )
                    logger.info(
                        "Initial deposit stored: %.2f", account.balance
                    )
                    return
            except Exception as exc:
                logger.error("Error getting account info for initial deposit: %s", exc)

        logger.info("Initial deposit not set (MT5 not available)")

    # ------------------------------------------------------------------
    # Signal Loop
    # ------------------------------------------------------------------

    async def _signal_loop(self) -> None:
        """Main trading signal loop.

        Periodically checks for new trade signals from the alpha model.
        Respects the risk guardian lock state and configurable scan interval.
        """
        logger.info("Signal loop started (interval=%.1fs)", self._scan_interval)

        while self._running:
            try:
                # Check if trading is locked
                if self._risk_guardian.is_locked:
                    logger.debug("Trading locked by RiskGuardian - skipping signal scan")
                    await asyncio.sleep(self._scan_interval)
                    continue

                # Generate signal
                signal = await self._alpha_model.generate_signal()

                if signal is not None:
                    # Check confidence threshold
                    min_confidence = 0.5
                    if signal.confidence_score < min_confidence:
                        logger.info(
                            "Signal confidence %.2f below threshold %.2f - skipping",
                            signal.confidence_score,
                            min_confidence,
                        )
                        await asyncio.sleep(self._scan_interval)
                        continue

                    # Get account balance for lot size recalculation
                    balance = self._get_account_balance()
                    if balance > 0 and signal.stop_loss != 0:
                        sl_distance = abs(signal.entry_price - signal.stop_loss)
                        if sl_distance > 0:
                            signal.lot_size = self._alpha_model.calculate_kelly_lot_size(
                                account_balance=balance,
                                sl_distance=sl_distance,
                            )

                    # Execute trade
                    result = await self._order_router.execute_trade(signal)

                    if result.success:
                        logger.info(
                            "Trade executed: ticket=%d, fill=%.5f, lot=%.2f",
                            result.ticket,
                            result.fill_price,
                            signal.lot_size,
                        )
                    else:
                        logger.warning(
                            "Trade execution failed: %s (code=%d)",
                            result.error_message,
                            result.error_code,
                        )

                await asyncio.sleep(self._scan_interval)

            except asyncio.CancelledError:
                logger.info("Signal loop cancelled")
                raise
            except Exception as exc:
                logger.error("Error in signal loop: %s", exc, exc_info=True)
                await asyncio.sleep(self._scan_interval)

        logger.info("Signal loop stopped")

    # ------------------------------------------------------------------
    # Trade Management Loop
    # ------------------------------------------------------------------

    async def _trade_management_loop(self) -> None:
        """Separate task for managing open positions.

        Periodically calls the order router to check open positions for
        partial close, break-even, and final target conditions.
        """
        logger.info(
            "Trade management loop started (interval=%.1fs)",
            self._management_interval,
        )

        while self._running:
            try:
                await self._order_router.manage_open_positions()
                await asyncio.sleep(self._management_interval)

            except asyncio.CancelledError:
                logger.info("Trade management loop cancelled")
                raise
            except Exception as exc:
                logger.error(
                    "Error in trade management loop: %s", exc, exc_info=True
                )
                await asyncio.sleep(self._management_interval)

        logger.info("Trade management loop stopped")

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def status(self) -> Dict[str, object]:
        """Return current engine state for external monitoring.

        Returns:
            Dictionary with running state, lock status, and task info.
        """
        active_tasks = sum(1 for t in self._tasks if not t.done())
        return {
            "running": self._running,
            "locked": self._risk_guardian.is_locked,
            "active_tasks": active_tasks,
            "total_tasks": len(self._tasks),
            "scan_interval": self._scan_interval,
            "management_interval": self._management_interval,
            "mt5_available": MT5_AVAILABLE,
        }

    # ------------------------------------------------------------------
    # Internal Helpers
    # ------------------------------------------------------------------

    def _get_account_balance(self) -> float:
        """Get account balance from MT5.

        Returns:
            Account balance or 0.0 if unavailable.
        """
        if not MT5_AVAILABLE or mt5 is None:
            return 0.0

        try:
            account = mt5.account_info()
            if account is not None:
                return account.balance
        except Exception as exc:
            logger.error("Error getting account balance: %s", exc)

        return 0.0
