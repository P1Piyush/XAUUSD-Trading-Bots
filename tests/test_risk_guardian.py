"""Tests for src/risk_guardian.py - risk limit enforcement."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

from src.config import PropFirmConfig
from src.database import TradingDatabase
from src.risk_guardian import RiskGuardian


pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def guardian(database):
    """Create a RiskGuardian with a real database."""
    g = RiskGuardian(database=database)
    return g


class TestDailyLossLock:
    async def test_daily_loss_locks_at_4_percent(self, guardian, database):
        """start_balance=10000, equity=9600 -> is_locked=True (exactly 4%)."""
        today = datetime.now(timezone.utc).date().isoformat()

        # Set up daily metrics with start_balance=10000
        metrics = {
            "date": today,
            "start_balance": 10000.0,
            "start_equity": 10000.0,
            "max_equity": 10000.0,
            "min_equity": 9600.0,
            "realized_pnl": 0.0,
            "is_locked": 0,
        }
        await database.save_daily_metrics(metrics)
        guardian._current_day = today

        # Check daily loss with equity = 9600 (4% loss from 10000)
        await guardian._check_daily_loss(equity=9600.0, balance=9600.0)
        assert guardian.is_locked is True

    async def test_daily_loss_does_not_lock_below_4_percent(self, guardian, database):
        """equity=9601 -> is_locked=False (3.99% loss)."""
        today = datetime.now(timezone.utc).date().isoformat()

        metrics = {
            "date": today,
            "start_balance": 10000.0,
            "start_equity": 10000.0,
            "max_equity": 10000.0,
            "min_equity": 9601.0,
            "realized_pnl": 0.0,
            "is_locked": 0,
        }
        await database.save_daily_metrics(metrics)
        guardian._current_day = today

        # Check daily loss with equity = 9601 (3.99% loss from 10000)
        await guardian._check_daily_loss(equity=9601.0, balance=9601.0)
        assert guardian.is_locked is False

    async def test_reset_daily_lock(self, guardian, database):
        """After lock, reset clears it."""
        today = datetime.now(timezone.utc).date().isoformat()

        metrics = {
            "date": today,
            "start_balance": 10000.0,
            "start_equity": 10000.0,
            "max_equity": 10000.0,
            "min_equity": 9500.0,
            "realized_pnl": 0.0,
            "is_locked": 0,
        }
        await database.save_daily_metrics(metrics)
        guardian._current_day = today

        # Trigger the lock
        await guardian._check_daily_loss(equity=9500.0, balance=9500.0)
        assert guardian.is_locked is True

        # Reset
        guardian.reset_daily_lock()
        assert guardian.is_locked is False


class TestTotalDrawdown:
    async def test_total_drawdown_locks_at_9_percent(self, guardian, database):
        """initial_deposit=10000, equity=9100 -> terminal failure."""
        # Set initial deposit in system_state
        await database.set_system_state("initial_deposit", "10000.0")

        # Check total drawdown: (10000 - 9100) / 10000 = 9%
        await guardian._check_total_drawdown(equity=9100.0)
        assert guardian._terminal_failure is True
        assert guardian.is_locked is True

    async def test_total_drawdown_does_not_lock_below_9_percent(self, guardian, database):
        """initial_deposit=10000, equity=9101 -> not locked."""
        await database.set_system_state("initial_deposit", "10000.0")

        # (10000 - 9101) / 10000 = 8.99%
        await guardian._check_total_drawdown(equity=9101.0)
        assert guardian._terminal_failure is False
        assert guardian.is_locked is False

    async def test_terminal_failure_prevents_daily_reset(self, guardian, database):
        """Once terminal failure is active, reset_daily_lock does nothing."""
        await database.set_system_state("initial_deposit", "10000.0")
        await guardian._check_total_drawdown(equity=9000.0)
        assert guardian._terminal_failure is True

        guardian.reset_daily_lock()
        # Should still be locked
        assert guardian.is_locked is True
