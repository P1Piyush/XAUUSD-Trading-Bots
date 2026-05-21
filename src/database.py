"""
Database module for XAUUSD Institutional Trading System.

Provides async SQLite persistence for trade state, equity snapshots,
daily metrics, and system state using aiosqlite. Ensures fault tolerance
and recovery across VM restarts.
"""

import logging
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiosqlite

from src.config import DatabaseConfig

logger = logging.getLogger(__name__)


class TradingDatabase:
    """Async SQLite database for trading state persistence.

    Manages connection lifecycle, schema creation, and provides
    typed async methods for all persistence operations.
    """

    def __init__(self, config: Optional[DatabaseConfig] = None) -> None:
        self._config = config or DatabaseConfig()
        self._db: Optional[aiosqlite.Connection] = None

    async def initialize(self) -> None:
        """Initialize the database connection and create schema."""
        db_path = Path(self._config.db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)

        self._db = await aiosqlite.connect(str(db_path))
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA busy_timeout=5000")
        await self._create_schema()
        logger.info("Database initialized at %s", db_path)

    async def _create_schema(self) -> None:
        """Create all database tables if they do not exist."""
        assert self._db is not None, "Database not initialized"

        await self._db.executescript("""
            CREATE TABLE IF NOT EXISTS trades (
                ticket INTEGER PRIMARY KEY,
                magic INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                direction TEXT NOT NULL,
                entry_price REAL NOT NULL,
                sl REAL NOT NULL,
                tp REAL NOT NULL,
                lot REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                open_time TEXT NOT NULL,
                close_time TEXT,
                pnl REAL DEFAULT 0.0,
                partial_closed INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS equity_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                balance REAL NOT NULL,
                equity REAL NOT NULL,
                margin REAL NOT NULL DEFAULT 0.0
            );

            CREATE TABLE IF NOT EXISTS daily_metrics (
                date TEXT PRIMARY KEY,
                start_balance REAL NOT NULL,
                start_equity REAL NOT NULL,
                max_equity REAL NOT NULL,
                min_equity REAL NOT NULL,
                realized_pnl REAL NOT NULL DEFAULT 0.0,
                is_locked INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS system_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
        """)
        await self._db.commit()
        logger.debug("Database schema created/verified")

    async def close(self) -> None:
        """Close the database connection."""
        if self._db is not None:
            await self._db.close()
            self._db = None
            logger.info("Database connection closed")

    async def save_trade(self, trade: Dict[str, Any]) -> None:
        """Insert a new trade record.

        Args:
            trade: Dictionary with keys matching the trades table columns.
        """
        assert self._db is not None, "Database not initialized"

        await self._db.execute(
            """
            INSERT INTO trades (
                ticket, magic, symbol, direction, entry_price,
                sl, tp, lot, status, open_time, close_time, pnl, partial_closed
            ) VALUES (
                :ticket, :magic, :symbol, :direction, :entry_price,
                :sl, :tp, :lot, :status, :open_time, :close_time, :pnl, :partial_closed
            )
            """,
            {
                "ticket": trade["ticket"],
                "magic": trade["magic"],
                "symbol": trade["symbol"],
                "direction": trade["direction"],
                "entry_price": trade["entry_price"],
                "sl": trade["sl"],
                "tp": trade["tp"],
                "lot": trade["lot"],
                "status": trade.get("status", "open"),
                "open_time": trade["open_time"],
                "close_time": trade.get("close_time"),
                "pnl": trade.get("pnl", 0.0),
                "partial_closed": trade.get("partial_closed", 0),
            },
        )
        await self._db.commit()
        logger.debug("Saved trade ticket=%s", trade["ticket"])

    async def update_trade(self, ticket: int, updates: Dict[str, Any]) -> None:
        """Update an existing trade record.

        Args:
            ticket: The trade ticket number to update.
            updates: Dictionary of column names and new values.
        """
        assert self._db is not None, "Database not initialized"

        if not updates:
            return

        set_clause = ", ".join(f"{key} = :{key}" for key in updates)
        updates["ticket"] = ticket

        await self._db.execute(
            f"UPDATE trades SET {set_clause} WHERE ticket = :ticket",
            updates,
        )
        await self._db.commit()
        logger.debug("Updated trade ticket=%s fields=%s", ticket, list(updates.keys()))

    async def get_active_trades(self) -> List[Dict[str, Any]]:
        """Retrieve all trades with status 'open'.

        Returns:
            List of trade dictionaries.
        """
        assert self._db is not None, "Database not initialized"

        cursor = await self._db.execute(
            "SELECT * FROM trades WHERE status = 'open'"
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def save_equity_snapshot(
        self,
        timestamp: str,
        balance: float,
        equity: float,
        margin: float = 0.0,
    ) -> None:
        """Save an equity snapshot record.

        Args:
            timestamp: ISO format timestamp string.
            balance: Account balance.
            equity: Account equity.
            margin: Used margin.
        """
        assert self._db is not None, "Database not initialized"

        await self._db.execute(
            """
            INSERT INTO equity_snapshots (timestamp, balance, equity, margin)
            VALUES (?, ?, ?, ?)
            """,
            (timestamp, balance, equity, margin),
        )
        await self._db.commit()
        logger.debug("Saved equity snapshot at %s", timestamp)

    async def get_daily_metrics(self, target_date: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Retrieve daily metrics for a given date.

        Args:
            target_date: ISO date string (YYYY-MM-DD). Defaults to today.

        Returns:
            Dictionary of daily metrics or None if not found.
        """
        assert self._db is not None, "Database not initialized"

        if target_date is None:
            target_date = date.today().isoformat()

        cursor = await self._db.execute(
            "SELECT * FROM daily_metrics WHERE date = ?",
            (target_date,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def save_daily_metrics(self, metrics: Dict[str, Any]) -> None:
        """Insert or replace daily metrics record.

        Args:
            metrics: Dictionary with keys matching daily_metrics columns.
        """
        assert self._db is not None, "Database not initialized"

        await self._db.execute(
            """
            INSERT OR REPLACE INTO daily_metrics (
                date, start_balance, start_equity, max_equity,
                min_equity, realized_pnl, is_locked
            ) VALUES (
                :date, :start_balance, :start_equity, :max_equity,
                :min_equity, :realized_pnl, :is_locked
            )
            """,
            metrics,
        )
        await self._db.commit()
        logger.debug("Saved daily metrics for %s", metrics.get("date"))

    async def set_system_state(self, key: str, value: str) -> None:
        """Set a system state key-value pair.

        Args:
            key: State key identifier.
            value: State value as string.
        """
        assert self._db is not None, "Database not initialized"

        now = datetime.utcnow().isoformat()
        await self._db.execute(
            """
            INSERT OR REPLACE INTO system_state (key, value, updated_at)
            VALUES (?, ?, ?)
            """,
            (key, value, now),
        )
        await self._db.commit()
        logger.debug("Set system state %s=%s", key, value)

    async def get_system_state(self, key: str) -> Optional[str]:
        """Retrieve a system state value by key.

        Args:
            key: State key identifier.

        Returns:
            The state value string or None if not found.
        """
        assert self._db is not None, "Database not initialized"

        cursor = await self._db.execute(
            "SELECT value FROM system_state WHERE key = ?",
            (key,),
        )
        row = await cursor.fetchone()
        return row["value"] if row else None

    async def get_trade_history(
        self, limit: int = 100, symbol: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Retrieve closed trade history for Kelly Criterion calculations.

        Args:
            limit: Maximum number of trades to return.
            symbol: Optional symbol filter.

        Returns:
            List of closed trade dictionaries ordered by close_time descending.
        """
        assert self._db is not None, "Database not initialized"

        query = "SELECT * FROM trades WHERE status = 'closed'"
        params: List[Any] = []

        if symbol:
            query += " AND symbol = ?"
            params.append(symbol)

        query += " ORDER BY close_time DESC LIMIT ?"
        params.append(limit)

        cursor = await self._db.execute(query, params)
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]
