"""Tests for src/database.py - async database operations."""

import pytest
import pytest_asyncio

from src.database import TradingDatabase


pytestmark = pytest.mark.asyncio


def _sample_trade(ticket=1001, status="open", pnl=0.0):
    """Create a sample trade dictionary."""
    return {
        "ticket": ticket,
        "magic": 202401,
        "symbol": "XAUUSD",
        "direction": "BUY",
        "entry_price": 2050.50,
        "sl": 2045.00,
        "tp": 2060.00,
        "lot": 0.10,
        "status": status,
        "open_time": "2024-01-15T10:00:00",
        "close_time": "2024-01-15T12:00:00" if status == "closed" else None,
        "pnl": pnl,
        "partial_closed": 0,
    }


class TestDatabaseInitialize:
    async def test_initialize_creates_tables(self, database: TradingDatabase):
        """After initialize(), all 4 required tables should exist."""
        assert database._db is not None
        cursor = await database._db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        rows = await cursor.fetchall()
        table_names = sorted([row["name"] for row in rows])
        assert "daily_metrics" in table_names
        assert "equity_snapshots" in table_names
        assert "system_state" in table_names
        assert "trades" in table_names


class TestTradeOperations:
    async def test_save_and_get_active_trades(self, database: TradingDatabase):
        """Round-trip: save_trade then get_active_trades returns the trade."""
        trade = _sample_trade(ticket=2001)
        await database.save_trade(trade)

        active = await database.get_active_trades()
        assert len(active) == 1
        assert active[0]["ticket"] == 2001
        assert active[0]["symbol"] == "XAUUSD"
        assert active[0]["status"] == "open"

    async def test_update_trade(self, database: TradingDatabase):
        """Save then update status to closed."""
        trade = _sample_trade(ticket=3001)
        await database.save_trade(trade)

        await database.update_trade(3001, {"status": "closed", "pnl": 150.0})

        # Should no longer appear in active trades
        active = await database.get_active_trades()
        assert len(active) == 0

        # Should appear in trade history
        history = await database.get_trade_history()
        assert len(history) == 1
        assert history[0]["ticket"] == 3001
        assert history[0]["pnl"] == 150.0

    async def test_get_trade_history(self, database: TradingDatabase):
        """Closed trades returned in correct order (most recent first)."""
        for i in range(5):
            trade = _sample_trade(ticket=4000 + i, status="closed", pnl=float(i * 10))
            trade["close_time"] = f"2024-01-{15 + i:02d}T12:00:00"
            await database.save_trade(trade)

        history = await database.get_trade_history(limit=10)
        assert len(history) == 5
        # Most recent close_time first
        assert history[0]["ticket"] == 4004
        assert history[-1]["ticket"] == 4000


class TestEquitySnapshot:
    async def test_equity_snapshot(self, database: TradingDatabase):
        """Save and verify equity snapshot retrieval."""
        await database.save_equity_snapshot(
            timestamp="2024-01-15T10:00:00",
            balance=10000.0,
            equity=9950.0,
            margin=500.0,
        )

        cursor = await database._db.execute(
            "SELECT * FROM equity_snapshots"
        )
        rows = await cursor.fetchall()
        assert len(rows) == 1
        row = dict(rows[0])
        assert row["balance"] == 10000.0
        assert row["equity"] == 9950.0
        assert row["margin"] == 500.0


class TestSystemState:
    async def test_system_state(self, database: TradingDatabase):
        """Set and get key-value pairs."""
        await database.set_system_state("daily_lock", "true")
        value = await database.get_system_state("daily_lock")
        assert value == "true"

    async def test_system_state_overwrite(self, database: TradingDatabase):
        """Setting the same key again overwrites the value."""
        await database.set_system_state("mode", "live")
        await database.set_system_state("mode", "paper")
        value = await database.get_system_state("mode")
        assert value == "paper"

    async def test_system_state_missing_key(self, database: TradingDatabase):
        """Getting a non-existent key returns None."""
        value = await database.get_system_state("nonexistent_key")
        assert value is None
