"""
Order Router module for XAUUSD Institutional Trading System.

Handles all order execution, retry logic with exponential backoff,
requote handling with dynamic deviation adjustment, partial closes,
break-even modifications, and open position management. Works in
coordination with RiskGuardian to refuse orders when trading is locked.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

try:
    import MetaTrader5 as mt5  # type: ignore[import-untyped]

    MT5_AVAILABLE = True
except ImportError:
    mt5 = None  # type: ignore[assignment]
    MT5_AVAILABLE = False

from src.alpha_model import TradeSignal
from src.config import OrderConfig, SessionConfig, SymbolConfig
from src.database import TradingDatabase
from src.market_intelligence import Direction
from src.risk_guardian import RiskGuardian

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data Classes
# ---------------------------------------------------------------------------


@dataclass
class OrderResult:
    """Result of an order execution attempt.

    Attributes:
        success: Whether the order was filled successfully.
        ticket: MT5 ticket number (0 if failed).
        fill_price: Actual fill price (0.0 if failed).
        slippage: Difference between requested and fill price.
        retries_used: Number of retry attempts before success/failure.
        error_code: MT5 return code (0 if successful).
        error_message: Human-readable error description.
    """

    success: bool
    ticket: int
    fill_price: float
    slippage: float
    retries_used: int
    error_code: int
    error_message: str


# ---------------------------------------------------------------------------
# OrderRouter Class
# ---------------------------------------------------------------------------


class OrderRouter:
    """Async order execution engine with retry logic and position management.

    Coordinates with RiskGuardian to enforce risk limits before sending
    orders. Implements exponential backoff retry, requote handling with
    dynamic deviation adjustment during volatile sessions, partial close
    at 1.5 RR, break-even modification, and full position lifecycle.
    """

    def __init__(
        self,
        risk_guardian: RiskGuardian,
        database: TradingDatabase,
        order_config: Optional[OrderConfig] = None,
        symbol_config: Optional[SymbolConfig] = None,
        session_config: Optional[SessionConfig] = None,
    ) -> None:
        """Initialize the OrderRouter.

        Args:
            risk_guardian: RiskGuardian instance for lock-state checks.
            database: TradingDatabase instance for trade persistence.
            order_config: Order execution parameters.
            symbol_config: Symbol-specific configuration.
            session_config: Trading session time windows.
        """
        self._guardian = risk_guardian
        self._db = database
        self._order = order_config or OrderConfig()
        self._symbol = symbol_config or SymbolConfig()
        self._session = session_config or SessionConfig()
        self._partially_closed_tickets: set = set()

    async def load_partially_closed_tickets(self) -> None:
        """Populate the in-memory set of partially closed tickets from the database.

        Should be called on startup so that the management loop does not
        re-attempt partial closes for tickets already processed.
        """
        try:
            active_trades = await self._db.get_active_trades()
            self._partially_closed_tickets = {
                t["ticket"] for t in active_trades if t.get("partial_closed", 0) == 1
            }
            logger.info(
                "Loaded %d partially closed tickets from database",
                len(self._partially_closed_tickets),
            )
        except Exception as exc:
            logger.error("Failed to load partially closed tickets: %s", exc)
            self._partially_closed_tickets = set()

    # ------------------------------------------------------------------
    # Order Execution
    # ------------------------------------------------------------------

    async def execute_trade(self, signal: TradeSignal) -> OrderResult:
        """Execute a trade based on an alpha model signal.

        Checks risk guardian lock status, builds the MT5 order request,
        sends with retry logic, and persists the trade on success.

        Args:
            signal: TradeSignal from the alpha model.

        Returns:
            OrderResult with execution details.
        """
        # Check risk lock
        if self._guardian.is_locked:
            logger.warning(
                "Order refused: trading is locked by RiskGuardian"
            )
            return OrderResult(
                success=False,
                ticket=0,
                fill_price=0.0,
                slippage=0.0,
                retries_used=0,
                error_code=-1,
                error_message="Trading locked by RiskGuardian",
            )

        # Build MT5 order request
        order_type = self._get_order_type(signal.direction)
        request = {
            "action": self._get_trade_action_deal(),
            "symbol": self._symbol.symbol,
            "volume": signal.lot_size,
            "type": order_type,
            "price": signal.entry_price,
            "sl": signal.stop_loss,
            "tp": signal.take_profit_1,
            "deviation": self._order.max_deviation_points,
            "magic": self._symbol.magic_number,
            "comment": f"ALPHA_{signal.direction.value}_{signal.confidence_score:.2f}",
            "type_time": self._get_order_time_gtc(),
            "type_filling": self._get_order_filling_ioc(),
        }

        logger.info(
            "Executing trade: %s %s @ %.5f, SL=%.5f, TP=%.5f, lot=%.2f",
            signal.direction.value,
            self._symbol.symbol,
            signal.entry_price,
            signal.stop_loss,
            signal.take_profit_1,
            signal.lot_size,
        )

        # Send with retry
        try:
            result = await self._send_with_retry(request)
        except OrderExecutionError as exc:
            logger.error("Order execution failed after retries: %s", exc)
            return OrderResult(
                success=False,
                ticket=0,
                fill_price=0.0,
                slippage=0.0,
                retries_used=self._order.retry_max_attempts,
                error_code=exc.error_code,
                error_message=str(exc),
            )

        # Calculate slippage
        fill_price = result.get("price", signal.entry_price)
        slippage = abs(fill_price - signal.entry_price)
        ticket = result.get("order", 0)

        # Save trade to database
        trade_record = {
            "ticket": ticket,
            "magic": self._symbol.magic_number,
            "symbol": self._symbol.symbol,
            "direction": signal.direction.value,
            "entry_price": fill_price,
            "sl": signal.stop_loss,
            "tp": signal.take_profit_1,
            "lot": signal.lot_size,
            "status": "open",
            "open_time": datetime.now(timezone.utc).isoformat(),
            "close_time": None,
            "pnl": 0.0,
            "partial_closed": 0,
        }
        await self._db.save_trade(trade_record)

        logger.info(
            "Trade executed successfully: ticket=%d, fill=%.5f, slippage=%.5f",
            ticket,
            fill_price,
            slippage,
        )

        return OrderResult(
            success=True,
            ticket=ticket,
            fill_price=fill_price,
            slippage=slippage,
            retries_used=result.get("retries_used", 0),
            error_code=0,
            error_message="",
        )

    async def _send_with_retry(self, request: dict) -> dict:
        """Send an order request to MT5 with exponential backoff retry.

        Implements retry logic with configurable max attempts and base delay.
        Handles specific MT5 return codes differently:
          - 10009 (DONE): Success, return immediately.
          - 10013 (INVALID): Abort, do not retry.
          - 10004 (REQUOTE): Adjust deviation, retry.
          - Other errors: Retry with backoff.

        Args:
            request: MT5 order request dictionary.

        Returns:
            Dictionary with order result details including 'order', 'price',
            and 'retries_used'.

        Raises:
            OrderExecutionError: If max retries exhausted or unrecoverable error.
        """
        max_attempts = self._order.retry_max_attempts
        base_delay_ms = self._order.retry_base_delay_ms

        for attempt in range(max_attempts):
            if not MT5_AVAILABLE or mt5 is None:
                logger.warning("MT5 not available - cannot send order")
                raise OrderExecutionError(
                    "MT5 not available", error_code=-2
                )

            try:
                result = mt5.order_send(request)
            except Exception as exc:
                logger.error(
                    "MT5 order_send() exception on attempt %d: %s",
                    attempt + 1,
                    exc,
                )
                if attempt < max_attempts - 1:
                    delay = (base_delay_ms * (2 ** attempt)) / 1000.0
                    await asyncio.sleep(delay)
                    continue
                raise OrderExecutionError(
                    f"MT5 exception: {exc}", error_code=-3
                ) from exc

            if result is None:
                logger.error(
                    "MT5 order_send() returned None on attempt %d",
                    attempt + 1,
                )
                if attempt < max_attempts - 1:
                    delay = (base_delay_ms * (2 ** attempt)) / 1000.0
                    await asyncio.sleep(delay)
                    continue
                raise OrderExecutionError(
                    "MT5 returned None", error_code=-4
                )

            retcode = result.retcode

            # 10009 - TRADE_RETCODE_DONE (success)
            if retcode == 10009:
                logger.info(
                    "Order filled: ticket=%d, price=%.5f, volume=%.2f, "
                    "retcode=%d, attempt=%d",
                    result.order,
                    result.price,
                    result.volume,
                    retcode,
                    attempt + 1,
                )
                return {
                    "order": result.order,
                    "price": result.price,
                    "volume": result.volume,
                    "retcode": retcode,
                    "retries_used": attempt,
                }

            # 10013 - TRADE_RETCODE_INVALID (unrecoverable)
            if retcode == 10013:
                logger.error(
                    "Order INVALID (retcode=10013): %s. Aborting.",
                    result.comment if hasattr(result, "comment") else "no comment",
                )
                raise OrderExecutionError(
                    f"Invalid order: {getattr(result, 'comment', 'unknown')}",
                    error_code=10013,
                )

            # 10004 - TRADE_RETCODE_REQUOTE
            if retcode == 10004:
                logger.warning(
                    "Requote received (attempt %d/%d). Adjusting deviation.",
                    attempt + 1,
                    max_attempts,
                )
                # Get current spread for deviation adjustment
                current_spread = self._get_current_spread()
                new_deviation = await self._adjust_deviation(current_spread)
                request["deviation"] = new_deviation
                logger.info(
                    "Deviation adjusted to %d points after requote",
                    new_deviation,
                )
                if attempt < max_attempts - 1:
                    delay = (base_delay_ms * (2 ** attempt)) / 1000.0
                    await asyncio.sleep(delay)
                    continue
                raise OrderExecutionError(
                    "Max retries exhausted after requotes",
                    error_code=10004,
                )

            # Other errors - retry with backoff
            comment = getattr(result, "comment", "unknown")
            logger.warning(
                "Order failed (retcode=%d, comment=%s) on attempt %d/%d",
                retcode,
                comment,
                attempt + 1,
                max_attempts,
            )
            if attempt < max_attempts - 1:
                delay = (base_delay_ms * (2 ** attempt)) / 1000.0
                logger.info(
                    "Retrying in %.1fms (attempt %d/%d)",
                    delay * 1000,
                    attempt + 2,
                    max_attempts,
                )
                await asyncio.sleep(delay)
            else:
                raise OrderExecutionError(
                    f"Max retries exhausted. Last retcode={retcode}, comment={comment}",
                    error_code=retcode,
                )

        # Should not reach here, but safety net
        raise OrderExecutionError(
            "Unexpected exit from retry loop", error_code=-5
        )

    async def _adjust_deviation(self, current_spread: float) -> int:
        """Adjust deviation based on current session volatility.

        During NY/London overlap (13:00-16:00 UTC), increases deviation
        based on current spread. Outside overlap, uses standard config value.

        Args:
            current_spread: Current bid-ask spread in points.

        Returns:
            Adjusted deviation in points.
        """
        now_utc = datetime.now(timezone.utc)
        current_hour = now_utc.hour
        current_minute = now_utc.minute

        ny_start_hour, ny_start_min = self._session.ny_start
        london_end_hour, london_end_min = self._session.london_end

        # Check if in NY/London overlap (13:00 - 16:00 UTC)
        current_time_mins = current_hour * 60 + current_minute
        overlap_start_mins = ny_start_hour * 60 + ny_start_min
        overlap_end_mins = london_end_hour * 60 + london_end_min

        if overlap_start_mins <= current_time_mins < overlap_end_mins:
            # During overlap: increase deviation = spread * 2
            adjusted = int(current_spread * 2)
            # Cap at max_deviation_points
            adjusted = min(adjusted, self._order.max_deviation_points)
            # Ensure at least the standard deviation
            adjusted = max(adjusted, self._order.max_deviation_points // 2)
            logger.debug(
                "NY/London overlap active: deviation=%d (spread=%.1f)",
                adjusted,
                current_spread,
            )
            return adjusted

        # Outside overlap: use standard deviation
        logger.debug(
            "Outside overlap: using standard deviation=%d",
            self._order.max_deviation_points,
        )
        return self._order.max_deviation_points

    # ------------------------------------------------------------------
    # Partial Close and Break-Even
    # ------------------------------------------------------------------

    async def execute_partial_close(
        self, ticket: int, percentage: float = 0.5
    ) -> bool:
        """Close a percentage of an open position at market.

        Args:
            ticket: MT5 position ticket to partially close.
            percentage: Fraction of volume to close (default 0.5 = 50%).

        Returns:
            True if partial close succeeded, False otherwise.
        """
        if not MT5_AVAILABLE or mt5 is None:
            logger.warning("MT5 not available - cannot execute partial close")
            return False

        try:
            # Get position info
            positions = mt5.positions_get(ticket=ticket)
            if positions is None or len(positions) == 0:
                logger.error(
                    "Position ticket=%d not found for partial close", ticket
                )
                return False

            position = positions[0]
            close_volume = round(position.volume * percentage, 2)

            # Ensure minimum lot
            if close_volume < 0.01:
                close_volume = 0.01

            # Determine close order type
            close_type = (
                mt5.ORDER_TYPE_SELL
                if position.type == mt5.ORDER_TYPE_BUY
                else mt5.ORDER_TYPE_BUY
            )

            request = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": position.symbol,
                "volume": close_volume,
                "type": close_type,
                "position": ticket,
                "deviation": self._order.max_deviation_points,
                "magic": position.magic,
                "comment": f"PARTIAL_CLOSE_{percentage*100:.0f}%",
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": mt5.ORDER_FILLING_IOC,
            }

            result = await self._send_with_retry(request)

            logger.info(
                "Partial close executed: ticket=%d, closed_volume=%.2f, "
                "remaining=%.2f",
                ticket,
                close_volume,
                position.volume - close_volume,
            )

            # Update trade in database and in-memory tracking set
            await self._db.update_trade(ticket, {"partial_closed": 1})
            self._partially_closed_tickets.add(ticket)

            return True

        except OrderExecutionError as exc:
            logger.error(
                "Partial close failed for ticket=%d: %s", ticket, exc
            )
            return False
        except Exception as exc:
            logger.error(
                "Unexpected error in partial close for ticket=%d: %s",
                ticket,
                exc,
            )
            return False

    async def modify_to_breakeven(
        self,
        ticket: int,
        entry_price: float,
        commission_buffer: float = 0.0,
    ) -> bool:
        """Modify position stop loss to break-even plus commission buffer.

        For BUY positions: new SL = entry_price + commission_buffer
        For SELL positions: new SL = entry_price - commission_buffer

        Uses _send_with_retry() for resilient execution against transient
        MT5 failures (requotes, timeouts).

        Args:
            ticket: MT5 position ticket to modify.
            entry_price: Original entry price of the position.
            commission_buffer: Buffer to cover commissions and swaps.

        Returns:
            True if modification succeeded, False otherwise.
        """
        if not MT5_AVAILABLE or mt5 is None:
            logger.warning("MT5 not available - cannot modify to breakeven")
            return False

        try:
            # Get position to determine direction
            positions = mt5.positions_get(ticket=ticket)
            if positions is None or len(positions) == 0:
                logger.error(
                    "Position ticket=%d not found for breakeven modification",
                    ticket,
                )
                return False

            position = positions[0]

            # Calculate new SL based on direction
            if position.type == mt5.ORDER_TYPE_BUY:
                new_sl = entry_price + commission_buffer
            else:
                new_sl = entry_price - commission_buffer

            request = {
                "action": mt5.TRADE_ACTION_SLTP,
                "symbol": position.symbol,
                "position": ticket,
                "sl": new_sl,
                "tp": position.tp,
            }

            await self._send_with_retry(request)

            logger.info(
                "Modified to breakeven: ticket=%d, new_sl=%.5f "
                "(entry=%.5f, buffer=%.5f)",
                ticket,
                new_sl,
                entry_price,
                commission_buffer,
            )
            return True

        except OrderExecutionError as exc:
            logger.error(
                "Breakeven modification failed for ticket=%d: %s", ticket, exc
            )
            return False
        except Exception as exc:
            logger.error(
                "Error modifying to breakeven for ticket=%d: %s", ticket, exc
            )
            return False

    # ------------------------------------------------------------------
    # Position Management
    # ------------------------------------------------------------------

    async def manage_open_positions(self) -> None:
        """Monitor and manage all open positions for partial close and targets.

        For each open position:
          - If RR >= partial_close_rr (1.5) and not partially closed:
            execute partial close (50%) and modify to breakeven.
          - If RR >= final_target_rr (4.0): close remaining position.

        This method should be called periodically by the core engine.
        """
        if not MT5_AVAILABLE or mt5 is None:
            logger.debug("MT5 not available - skipping position management")
            return

        try:
            positions = mt5.positions_get(
                symbol=self._symbol.symbol
            )
            if positions is None or len(positions) == 0:
                return

            for position in positions:
                # Only manage positions with our magic number
                if position.magic != self._symbol.magic_number:
                    continue

                # Calculate current RR ratio
                entry_price = position.price_open
                current_price = position.price_current
                sl = position.sl

                if sl == 0.0 or entry_price == sl:
                    continue

                sl_distance = abs(entry_price - sl)

                if position.type == mt5.ORDER_TYPE_BUY:
                    current_profit_distance = current_price - entry_price
                else:
                    current_profit_distance = entry_price - current_price

                current_rr = current_profit_distance / sl_distance

                # Check if position needs partial close
                if (
                    current_rr >= self._order.partial_close_rr
                    and not self._is_partially_closed(position.ticket)
                ):
                    logger.info(
                        "Position ticket=%d reached %.2f RR - executing partial close",
                        position.ticket,
                        current_rr,
                    )
                    success = await self.execute_partial_close(
                        position.ticket,
                        percentage=self._order.partial_close_pct,
                    )
                    if success:
                        await self.modify_to_breakeven(
                            position.ticket, entry_price
                        )

                # Check if position reached final target
                elif current_rr >= self._order.final_target_rr_min:
                    logger.info(
                        "Position ticket=%d reached %.2f RR (final target) - closing",
                        position.ticket,
                        current_rr,
                    )
                    await self.close_position(position.ticket)

        except Exception as exc:
            logger.error("Error managing open positions: %s", exc)

    async def close_position(self, ticket: int) -> bool:
        """Send a market close order for a specific position.

        Args:
            ticket: MT5 position ticket to close.

        Returns:
            True if close succeeded, False otherwise.
        """
        if not MT5_AVAILABLE or mt5 is None:
            logger.warning("MT5 not available - cannot close position")
            return False

        try:
            positions = mt5.positions_get(ticket=ticket)
            if positions is None or len(positions) == 0:
                logger.error("Position ticket=%d not found for close", ticket)
                return False

            position = positions[0]

            close_type = (
                mt5.ORDER_TYPE_SELL
                if position.type == mt5.ORDER_TYPE_BUY
                else mt5.ORDER_TYPE_BUY
            )

            request = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": position.symbol,
                "volume": position.volume,
                "type": close_type,
                "position": ticket,
                "deviation": self._order.max_deviation_points,
                "magic": position.magic,
                "comment": "CLOSE_POSITION",
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": mt5.ORDER_FILLING_IOC,
            }

            result = await self._send_with_retry(request)

            # Update trade in database
            await self._db.update_trade(
                ticket,
                {
                    "status": "closed",
                    "close_time": datetime.now(timezone.utc).isoformat(),
                    "pnl": position.profit,
                },
            )

            logger.info(
                "Position closed: ticket=%d, pnl=%.2f", ticket, position.profit
            )
            return True

        except OrderExecutionError as exc:
            logger.error("Close position failed for ticket=%d: %s", ticket, exc)
            return False
        except Exception as exc:
            logger.error(
                "Unexpected error closing position ticket=%d: %s", ticket, exc
            )
            return False

    async def cancel_pending_order(self, ticket: int) -> bool:
        """Remove a specific pending order.

        Args:
            ticket: MT5 pending order ticket to cancel.

        Returns:
            True if cancellation succeeded, False otherwise.
        """
        if not MT5_AVAILABLE or mt5 is None:
            logger.warning("MT5 not available - cannot cancel pending order")
            return False

        try:
            request = {
                "action": mt5.TRADE_ACTION_REMOVE,
                "order": ticket,
            }

            result = mt5.order_send(request)

            if result is not None and result.retcode == 10009:
                logger.info("Pending order cancelled: ticket=%d", ticket)
                return True
            else:
                retcode = result.retcode if result else "None"
                logger.error(
                    "Failed to cancel pending order ticket=%d, retcode=%s",
                    ticket,
                    retcode,
                )
                return False

        except Exception as exc:
            logger.error(
                "Error cancelling pending order ticket=%d: %s", ticket, exc
            )
            return False

    # ------------------------------------------------------------------
    # Internal Helpers
    # ------------------------------------------------------------------

    def _get_order_type(self, direction: Direction) -> int:
        """Map Direction enum to MT5 order type constant.

        Args:
            direction: BUY or SELL direction.

        Returns:
            MT5 order type constant (integer).
        """
        if MT5_AVAILABLE and mt5 is not None:
            if direction == Direction.BUY:
                return mt5.ORDER_TYPE_BUY
            return mt5.ORDER_TYPE_SELL
        # Fallback constants when MT5 is not available
        if direction == Direction.BUY:
            return 0  # ORDER_TYPE_BUY
        return 1  # ORDER_TYPE_SELL

    def _get_trade_action_deal(self) -> int:
        """Get MT5 TRADE_ACTION_DEAL constant.

        Returns:
            MT5 trade action deal constant.
        """
        if MT5_AVAILABLE and mt5 is not None:
            return mt5.TRADE_ACTION_DEAL
        return 1  # TRADE_ACTION_DEAL fallback

    def _get_order_time_gtc(self) -> int:
        """Get MT5 ORDER_TIME_GTC constant.

        Returns:
            MT5 order time GTC constant.
        """
        if MT5_AVAILABLE and mt5 is not None:
            return mt5.ORDER_TIME_GTC
        return 0  # ORDER_TIME_GTC fallback

    def _get_order_filling_ioc(self) -> int:
        """Get MT5 ORDER_FILLING_IOC constant.

        Returns:
            MT5 order filling IOC constant.
        """
        if MT5_AVAILABLE and mt5 is not None:
            return mt5.ORDER_FILLING_IOC
        return 1  # ORDER_FILLING_IOC fallback

    def _get_current_spread(self) -> float:
        """Get the current bid-ask spread for the symbol.

        Returns:
            Current spread in points, or a default value if unavailable.
        """
        if not MT5_AVAILABLE or mt5 is None:
            return 10.0  # Default spread estimate

        try:
            tick = mt5.symbol_info_tick(self._symbol.symbol)
            if tick is not None:
                spread = (tick.ask - tick.bid) / self._symbol.point_value
                return spread
        except Exception as exc:
            logger.error("Error getting spread: %s", exc)

        return 10.0  # Default fallback

    def _is_partially_closed(self, ticket: int) -> bool:
        """Check if a position has already been partially closed.

        Uses the in-memory set populated on startup from the database
        and updated after each successful partial close.

        Args:
            ticket: Position ticket to check.

        Returns:
            True if position was already partially closed.
        """
        return ticket in self._partially_closed_tickets


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class OrderExecutionError(Exception):
    """Raised when an order cannot be executed after all retry attempts.

    Attributes:
        error_code: The MT5 return code or internal error code.
    """

    def __init__(self, message: str, error_code: int = 0) -> None:
        super().__init__(message)
        self.error_code = error_code
