"""Tests for src/order_router.py - order execution and retry logic."""

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from src.alpha_model import TradeSignal
from src.config import OrderConfig, SessionConfig, SymbolConfig
from src.database import TradingDatabase
from src.market_intelligence import Direction
from src.order_router import OrderExecutionError, OrderResult, OrderRouter
from src.risk_guardian import RiskGuardian


pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def router(database):
    """Create an OrderRouter with a mocked RiskGuardian."""
    guardian = RiskGuardian(database=database)
    router = OrderRouter(
        risk_guardian=guardian,
        database=database,
    )
    return router


@pytest_asyncio.fixture
async def locked_router(database):
    """Create an OrderRouter with a locked RiskGuardian."""
    guardian = RiskGuardian(database=database)
    guardian._locked = True
    router = OrderRouter(
        risk_guardian=guardian,
        database=database,
    )
    return router


def _sample_signal():
    """Create a sample TradeSignal."""
    return TradeSignal(
        direction=Direction.BUY,
        entry_price=2050.0,
        stop_loss=2045.0,
        take_profit_1=2060.0,
        take_profit_2=2070.0,
        lot_size=0.10,
        confidence_score=0.85,
        confluence_factors=["H4 BULLISH", "OB confirmed"],
    )


class TestExecuteTrade:
    async def test_execute_trade_respects_lock(self, locked_router):
        """When is_locked=True, returns failed OrderResult."""
        signal = _sample_signal()
        result = await locked_router.execute_trade(signal)

        assert result.success is False
        assert result.ticket == 0
        assert result.error_code == -1
        assert "locked" in result.error_message.lower()

    async def test_execute_trade_success(self, router, mock_mt5):
        """Successful trade execution returns positive OrderResult."""
        signal = _sample_signal()

        # Mock MT5 order_send to return success
        mock_result = MagicMock()
        mock_result.retcode = 10009
        mock_result.order = 12345
        mock_result.price = 2050.10
        mock_result.volume = 0.10
        mock_mt5.order_send.return_value = mock_result

        # Patch MT5_AVAILABLE in order_router module
        with patch("src.order_router.MT5_AVAILABLE", True), \
             patch("src.order_router.mt5", mock_mt5):
            result = await router.execute_trade(signal)

        assert result.success is True
        assert result.ticket == 12345
        assert result.fill_price == 2050.10


class TestSendWithRetry:
    async def test_send_with_retry_exponential_backoff(self, router, mock_mt5):
        """Mock MT5 failing 3 times then succeeding, verify delays increase."""
        # First 3 calls fail with retcode != 10009, 4th succeeds
        fail_result = MagicMock()
        fail_result.retcode = 10006  # Some retriable error
        fail_result.comment = "Trade timeout"

        success_result = MagicMock()
        success_result.retcode = 10009
        success_result.order = 99999
        success_result.price = 2050.0
        success_result.volume = 0.10

        mock_mt5.order_send.side_effect = [
            fail_result, fail_result, fail_result, success_result
        ]

        request = {
            "action": 1,
            "symbol": "XAUUSD",
            "volume": 0.10,
            "type": 0,
            "price": 2050.0,
            "sl": 2045.0,
            "tp": 2060.0,
            "deviation": 20,
            "magic": 202401,
            "comment": "test",
            "type_time": 0,
            "type_filling": 1,
        }

        sleep_calls = []
        original_sleep = asyncio.sleep

        async def mock_sleep(delay):
            sleep_calls.append(delay)

        with patch("src.order_router.MT5_AVAILABLE", True), \
             patch("src.order_router.mt5", mock_mt5), \
             patch("asyncio.sleep", side_effect=mock_sleep):
            result = await router._send_with_retry(request)

        assert result["order"] == 99999
        assert result["retries_used"] == 3
        # Verify exponential backoff: base_delay=100ms -> 0.1, 0.2, 0.4
        assert len(sleep_calls) == 3
        assert sleep_calls[0] == pytest.approx(0.1, abs=0.01)
        assert sleep_calls[1] == pytest.approx(0.2, abs=0.01)
        assert sleep_calls[2] == pytest.approx(0.4, abs=0.01)


class TestPartialClose:
    async def test_partial_close_calculates_50_percent(self, router, mock_mt5):
        """0.10 lot -> close 0.05."""
        # Mock position with 0.10 lot
        mock_position = MagicMock()
        mock_position.volume = 0.10
        mock_position.type = 0  # ORDER_TYPE_BUY
        mock_position.symbol = "XAUUSD"
        mock_position.magic = 202401
        mock_position.tp = 2060.0

        mock_mt5.positions_get.return_value = [mock_position]
        mock_mt5.ORDER_TYPE_BUY = 0
        mock_mt5.ORDER_TYPE_SELL = 1
        mock_mt5.TRADE_ACTION_DEAL = 1
        mock_mt5.ORDER_TIME_GTC = 0
        mock_mt5.ORDER_FILLING_IOC = 1

        # Success on order_send
        mock_send_result = MagicMock()
        mock_send_result.retcode = 10009
        mock_send_result.order = 55555
        mock_send_result.price = 2055.0
        mock_send_result.volume = 0.05
        mock_mt5.order_send.return_value = mock_send_result

        with patch("src.order_router.MT5_AVAILABLE", True), \
             patch("src.order_router.mt5", mock_mt5):
            success = await router.execute_partial_close(ticket=12345, percentage=0.5)

        assert success is True
        # Verify the order was sent with 50% of volume
        call_args = mock_mt5.order_send.call_args[0][0]
        assert call_args["volume"] == 0.05


class TestDeviationAdjustment:
    async def test_deviation_adjustment_during_overlap(self, router):
        """At 14:00 UTC (during NY/London overlap 13:00-16:00), deviation is increased."""
        # Mock datetime to be 14:00 UTC (inside overlap)
        mock_dt = MagicMock()
        mock_dt.hour = 14
        mock_dt.minute = 0

        with patch("src.order_router.datetime") as mock_datetime:
            mock_datetime.now.return_value = mock_dt
            mock_datetime.side_effect = lambda *args, **kw: datetime(*args, **kw)
            mock_datetime.now.return_value = datetime(2024, 1, 15, 14, 0, tzinfo=timezone.utc)

            adjusted = await router._adjust_deviation(current_spread=15.0)

        # During overlap: deviation = int(spread * 2) = 30, capped at max_deviation_points=20
        # min(30, 20) = 20, max(20, 20//2=10) = 20
        assert adjusted >= router._order.max_deviation_points // 2
        assert adjusted <= router._order.max_deviation_points

    async def test_deviation_standard_outside_overlap(self, router):
        """At 10:00 UTC (outside overlap), standard deviation is used."""
        with patch("src.order_router.datetime") as mock_datetime:
            mock_datetime.now.return_value = datetime(2024, 1, 15, 10, 0, tzinfo=timezone.utc)

            adjusted = await router._adjust_deviation(current_spread=15.0)

        # Outside overlap: should return standard max_deviation_points
        assert adjusted == router._order.max_deviation_points
