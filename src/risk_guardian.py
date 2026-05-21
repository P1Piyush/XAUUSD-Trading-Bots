"""
Risk Guardian module for XAUUSD Institutional Trading System.

Provides continuous background equity monitoring that enforces prop firm risk
limits (4% daily max loss, 9% total max drawdown). Operates as an independent
asyncio task that polls account equity every 500ms and triggers emergency
close-all if thresholds are breached.
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

try:
    import MetaTrader5 as mt5  # type: ignore[import-untyped]

    MT5_AVAILABLE = True
except ImportError:
    mt5 = None  # type: ignore[assignment]
    MT5_AVAILABLE = False

from src.config import DatabaseConfig, PropFirmConfig, RiskConfig
from src.database import TradingDatabase

logger = logging.getLogger(__name__)


class RiskGuardian:
    """Continuous background risk monitoring and enforcement.

    Runs an independent asyncio task that polls MT5 account equity every
    500ms. Enforces two hard limits:
      - Daily max loss: 4% from day-start balance triggers emergency close.
      - Total max drawdown: 9% from initial deposit triggers terminal failure.

    When either threshold is breached, all open positions and pending orders
    are immediately closed and trading is locked until the condition is reset.
    """

    def __init__(
        self,
        database: TradingDatabase,
        prop_config: Optional[PropFirmConfig] = None,
        risk_config: Optional[RiskConfig] = None,
        db_config: Optional[DatabaseConfig] = None,
    ) -> None:
        """Initialize the RiskGuardian.

        Args:
            database: TradingDatabase instance for persistence.
            prop_config: Prop firm risk limits configuration.
            risk_config: Risk monitoring configuration.
            db_config: Database configuration reference.
        """
        self._db = database
        self._prop = prop_config or PropFirmConfig()
        self._risk = risk_config or RiskConfig()
        self._db_config = db_config or DatabaseConfig()
        self._locked: bool = False
        self._terminal_failure: bool = False
        self._monitoring_task: Optional[asyncio.Task] = None  # type: ignore[type-arg]
        self._current_day: Optional[str] = None

    @property
    def is_locked(self) -> bool:
        """Indicate whether trading is halted due to risk breach.

        Returns:
            True if daily loss or total drawdown threshold has been breached.
        """
        return self._locked or self._terminal_failure

    async def start_monitoring(self) -> asyncio.Task:  # type: ignore[type-arg]
        """Launch the 500ms equity polling loop as an asyncio task.

        Returns:
            The asyncio.Task reference so the caller can cancel it.
        """
        self._monitoring_task = asyncio.create_task(
            self._equity_poll_loop(), name="risk_guardian_poll"
        )
        logger.info(
            "RiskGuardian monitoring started (poll interval: %dms)",
            self._risk.equity_poll_interval_ms,
        )
        return self._monitoring_task

    async def _equity_poll_loop(self) -> None:
        """Run indefinitely, polling equity every 500ms.

        Queries MT5 account_info() for balance, equity, and margin.
        Saves equity snapshot to database and checks risk thresholds.
        """
        poll_interval_s = self._risk.equity_poll_interval_ms / 1000.0

        while True:
            try:
                balance, equity, margin = await self._get_account_info()

                if balance is None or equity is None:
                    logger.warning("Failed to retrieve account info from MT5")
                    await asyncio.sleep(poll_interval_s)
                    continue

                # Save equity snapshot
                timestamp = datetime.now(timezone.utc).isoformat()
                await self._db.save_equity_snapshot(
                    timestamp=timestamp,
                    balance=balance,
                    equity=equity,
                    margin=margin or 0.0,
                )

                # Check risk thresholds
                await self._check_daily_loss(equity, balance)
                await self._check_total_drawdown(equity)

            except asyncio.CancelledError:
                logger.info("RiskGuardian polling loop cancelled")
                raise
            except Exception as exc:
                logger.error(
                    "Error in equity poll loop: %s", exc, exc_info=True
                )

            await asyncio.sleep(poll_interval_s)

    async def _get_account_info(self) -> tuple:
        """Retrieve account balance, equity, and margin from MT5.

        Returns:
            Tuple of (balance, equity, margin) or (None, None, None) on failure.
        """
        if not MT5_AVAILABLE or mt5 is None:
            logger.debug("MT5 not available - returning None for account info")
            return (None, None, None)

        try:
            account = mt5.account_info()
            if account is None:
                return (None, None, None)
            return (account.balance, account.equity, account.margin)
        except Exception as exc:
            logger.error("MT5 account_info() failed: %s", exc)
            return (None, None, None)

    async def _check_daily_loss(self, equity: float, balance: float) -> None:
        """Check if daily loss threshold has been breached.

        At broker rollover (00:00 GMT), detects new day and caches
        start_balance/start_equity in the daily_metrics table.
        If equity drops 4% below day-start balance, triggers emergency close.

        Args:
            equity: Current account equity.
            balance: Current account balance.
        """
        today = datetime.now(timezone.utc).date().isoformat()

        # Detect new day (broker rollover)
        if self._current_day != today:
            self._current_day = today
            self._locked = False  # Reset daily lock on new day

            # Cache start-of-day balance/equity
            metrics = {
                "date": today,
                "start_balance": balance,
                "start_equity": equity,
                "max_equity": equity,
                "min_equity": equity,
                "realized_pnl": 0.0,
                "is_locked": 0,
            }
            await self._db.save_daily_metrics(metrics)
            await self._db.set_system_state("daily_lock", "false")
            logger.info(
                "New trading day %s: start_balance=%.2f, start_equity=%.2f",
                today,
                balance,
                equity,
            )
            return

        # Get day-start balance
        daily_metrics = await self._db.get_daily_metrics(today)
        if daily_metrics is None:
            logger.warning("No daily metrics found for %s", today)
            return

        start_balance = daily_metrics["start_balance"]

        # Calculate daily drawdown
        if start_balance <= 0:
            return

        daily_drawdown_pct = (start_balance - equity) / start_balance * 100.0

        # Check threshold
        if daily_drawdown_pct >= self._prop.daily_max_loss_pct:
            logger.critical(
                "DAILY LOSS LIMIT BREACHED: %.2f%% (threshold: %.2f%%). "
                "Equity=%.2f, Start Balance=%.2f",
                daily_drawdown_pct,
                self._prop.daily_max_loss_pct,
                equity,
                start_balance,
            )
            await self.emergency_close_all()
            self._locked = True
            await self._db.set_system_state("daily_lock", "true")
            await self._db.set_system_state(
                "daily_lock_reason",
                f"Daily loss {daily_drawdown_pct:.2f}% exceeded {self._prop.daily_max_loss_pct}%",
            )
            logger.critical("Trading LOCKED for the day due to daily loss breach")

    async def _check_total_drawdown(self, equity: float) -> None:
        """Check if total drawdown threshold has been breached.

        Compares current equity against the initial deposit stored in
        system_state table. If 9% total drawdown is breached, triggers
        terminal failure.

        Args:
            equity: Current account equity.
        """
        if self._terminal_failure:
            return

        # Get initial deposit (set once at first run)
        initial_deposit_str = await self._db.get_system_state("initial_deposit")

        if initial_deposit_str is None:
            # First run: store current equity as initial deposit
            await self._db.set_system_state("initial_deposit", str(equity))
            logger.info("Initial deposit recorded: %.2f", equity)
            return

        initial_deposit = float(initial_deposit_str)

        if initial_deposit <= 0:
            return

        total_drawdown_pct = (initial_deposit - equity) / initial_deposit * 100.0

        if total_drawdown_pct >= self._prop.total_max_drawdown_pct:
            logger.critical(
                "TOTAL DRAWDOWN LIMIT BREACHED: %.2f%% (threshold: %.2f%%). "
                "Equity=%.2f, Initial Deposit=%.2f",
                total_drawdown_pct,
                self._prop.total_max_drawdown_pct,
                equity,
                initial_deposit,
            )
            await self.emergency_close_all()
            self._terminal_failure = True
            self._locked = True
            await self._db.set_system_state("terminal_failure", "true")
            await self._db.set_system_state(
                "terminal_failure_reason",
                f"Total drawdown {total_drawdown_pct:.2f}% exceeded {self._prop.total_max_drawdown_pct}%",
            )
            logger.critical(
                "TERMINAL FAILURE: Trading permanently locked. "
                "Total drawdown breached max allowed."
            )

    async def emergency_close_all(self) -> None:
        """Close all open positions and cancel all pending orders.

        Iterates all open positions from MT5, sends market close orders
        for each ticket, cancels all pending orders, and sets is_locked.
        All MT5 calls are wrapped in asyncio.to_thread() to avoid blocking
        the event loop during the critical close-all operation.
        """
        logger.critical("EMERGENCY CLOSE ALL triggered")

        if not MT5_AVAILABLE or mt5 is None:
            logger.warning(
                "MT5 not available - cannot execute emergency close"
            )
            self._locked = True
            return

        # Close all open positions
        try:
            positions = await asyncio.to_thread(mt5.positions_get)
            if positions is not None and len(positions) > 0:
                logger.info(
                    "Closing %d open positions", len(positions)
                )
                for pos in positions:
                    try:
                        close_type = (
                            mt5.ORDER_TYPE_SELL
                            if pos.type == mt5.ORDER_TYPE_BUY
                            else mt5.ORDER_TYPE_BUY
                        )
                        request = {
                            "action": mt5.TRADE_ACTION_DEAL,
                            "symbol": pos.symbol,
                            "volume": pos.volume,
                            "type": close_type,
                            "position": pos.ticket,
                            "deviation": 50,
                            "magic": pos.magic,
                            "comment": "EMERGENCY_CLOSE",
                            "type_time": mt5.ORDER_TIME_GTC,
                            "type_filling": mt5.ORDER_FILLING_IOC,
                        }
                        result = await asyncio.to_thread(mt5.order_send, request)
                        if result is not None and result.retcode == 10009:
                            logger.info(
                                "Emergency closed position ticket=%d, volume=%.2f",
                                pos.ticket,
                                pos.volume,
                            )
                        else:
                            retcode = result.retcode if result else "None"
                            logger.error(
                                "Failed to close position ticket=%d, retcode=%s",
                                pos.ticket,
                                retcode,
                            )
                    except Exception as exc:
                        logger.error(
                            "Error closing position ticket=%d: %s",
                            pos.ticket,
                            exc,
                        )
            else:
                logger.info("No open positions to close")
        except Exception as exc:
            logger.error("Error getting positions for emergency close: %s", exc)

        # Cancel all pending orders
        await self._cancel_all_pending()

        self._locked = True
        await self._db.set_system_state("is_locked", "true")
        logger.critical("EMERGENCY CLOSE ALL completed - trading locked")

    async def _cancel_all_pending(self) -> None:
        """Cancel all pending orders from MT5.

        Fetches all pending orders and removes each one.
        All MT5 calls are wrapped in asyncio.to_thread() to avoid blocking
        the event loop.
        """
        if not MT5_AVAILABLE or mt5 is None:
            logger.warning("MT5 not available - cannot cancel pending orders")
            return

        try:
            orders = await asyncio.to_thread(mt5.orders_get)
            if orders is not None and len(orders) > 0:
                logger.info("Cancelling %d pending orders", len(orders))
                for order in orders:
                    try:
                        request = {
                            "action": mt5.TRADE_ACTION_REMOVE,
                            "order": order.ticket,
                        }
                        result = await asyncio.to_thread(mt5.order_send, request)
                        if result is not None and result.retcode == 10009:
                            logger.info(
                                "Cancelled pending order ticket=%d",
                                order.ticket,
                            )
                        else:
                            retcode = result.retcode if result else "None"
                            logger.error(
                                "Failed to cancel order ticket=%d, retcode=%s",
                                order.ticket,
                                retcode,
                            )
                    except Exception as exc:
                        logger.error(
                            "Error cancelling order ticket=%d: %s",
                            order.ticket,
                            exc,
                        )
            else:
                logger.info("No pending orders to cancel")
        except Exception as exc:
            logger.error("Error getting pending orders: %s", exc)

    def reset_daily_lock(self) -> None:
        """Reset the daily lock flag.

        Called at broker rollover to allow trading on a new day.
        Does not reset terminal failure.
        """
        if self._terminal_failure:
            logger.warning(
                "Cannot reset daily lock - terminal failure is active"
            )
            return

        self._locked = False
        self._current_day = None
        logger.info("Daily lock reset - trading enabled for new day")

    async def get_daily_drawdown_pct(self) -> float:
        """Return the current day's drawdown percentage.

        Returns:
            Float representing the percentage drawdown from day-start balance.
            Returns 0.0 if data is unavailable.
        """
        today = datetime.now(timezone.utc).date().isoformat()
        daily_metrics = await self._db.get_daily_metrics(today)

        if daily_metrics is None:
            return 0.0

        start_balance = daily_metrics["start_balance"]
        if start_balance <= 0:
            return 0.0

        # Get current equity from MT5
        _, equity, _ = await self._get_account_info()
        if equity is None:
            return 0.0

        drawdown_pct = (start_balance - equity) / start_balance * 100.0
        return max(0.0, drawdown_pct)
