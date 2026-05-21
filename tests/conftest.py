"""Shared test fixtures for the XAUUSD Institutional Trading System."""

import os
import sys
import tempfile
from datetime import datetime, timezone
from unittest.mock import MagicMock

import numpy as np
import pytest
import pytest_asyncio

from src.config import DatabaseConfig
from src.database import TradingDatabase


# ---------------------------------------------------------------------------
# MT5 Mock Fixture
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def mock_mt5(monkeypatch):
    """Patch the MetaTrader5 module globally so no test needs real MT5."""
    mock = MagicMock()
    mock.ORDER_TYPE_BUY = 0
    mock.ORDER_TYPE_SELL = 1
    mock.TRADE_ACTION_DEAL = 1
    mock.TRADE_ACTION_SLTP = 6
    mock.TRADE_ACTION_REMOVE = 8
    mock.ORDER_TIME_GTC = 0
    mock.ORDER_FILLING_IOC = 1
    mock.TIMEFRAME_M1 = 1
    mock.TIMEFRAME_M5 = 5
    mock.TIMEFRAME_M15 = 15
    mock.TIMEFRAME_H1 = 16385
    mock.TIMEFRAME_H4 = 16388
    monkeypatch.setitem(sys.modules, "MetaTrader5", mock)
    return mock


# ---------------------------------------------------------------------------
# Database Fixture
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def database(tmp_path):
    """Create a TradingDatabase with a temp file, initialize, yield, then cleanup."""
    db_file = tmp_path / "test_trading.db"
    config = DatabaseConfig(db_path=str(db_file))
    db = TradingDatabase(config=config)
    await db.initialize()
    yield db
    await db.close()


# ---------------------------------------------------------------------------
# Candle Data Helpers
# ---------------------------------------------------------------------------

_CANDLE_DTYPE = np.dtype([
    ("time", "i8"),
    ("open", "f8"),
    ("high", "f8"),
    ("low", "f8"),
    ("close", "f8"),
    ("tick_volume", "i8"),
    ("spread", "i4"),
    ("real_volume", "i8"),
])


def _make_candles(opens, highs, lows, closes, volumes=None, start_ts=None):
    """Helper to build a numpy structured array from price lists."""
    n = len(opens)
    if volumes is None:
        rng = np.random.default_rng(42)
        volumes = rng.integers(50, 500, size=n)
    if start_ts is None:
        # Start at 2024-01-15 00:00 UTC
        start_ts = int(datetime(2024, 1, 15, 0, 0, tzinfo=timezone.utc).timestamp())
    candles = np.zeros(n, dtype=_CANDLE_DTYPE)
    for i in range(n):
        candles[i]["time"] = start_ts + i * 3600  # 1-hour bars
        candles[i]["open"] = opens[i]
        candles[i]["high"] = highs[i]
        candles[i]["low"] = lows[i]
        candles[i]["close"] = closes[i]
        candles[i]["tick_volume"] = volumes[i]
        candles[i]["spread"] = 20
        candles[i]["real_volume"] = 0
    return candles


@pytest.fixture
def sample_candles():
    """~50 bars of realistic XAUUSD data around $2000-2100 with mixed structure."""
    rng = np.random.default_rng(123)
    n = 50
    base_price = 2050.0
    opens = np.zeros(n)
    highs = np.zeros(n)
    lows = np.zeros(n)
    closes = np.zeros(n)
    volumes = rng.integers(100, 600, size=n)

    opens[0] = base_price
    for i in range(n):
        if i > 0:
            opens[i] = closes[i - 1] + rng.normal(0, 0.5)
        move = rng.normal(0, 3.0)
        closes[i] = opens[i] + move
        highs[i] = max(opens[i], closes[i]) + rng.uniform(0.5, 3.0)
        lows[i] = min(opens[i], closes[i]) - rng.uniform(0.5, 3.0)

    return _make_candles(opens, highs, lows, closes, volumes)


@pytest.fixture
def sample_candles_bullish():
    """Data showing clear Higher Highs and Higher Lows pattern.

    Construct explicit swing points that the lookback=5 detector will find:
    swing lows at indices 10, 25, 40 (increasing)
    swing highs at indices 17, 32, 47 (increasing)
    """
    n = 50
    rng = np.random.default_rng(42)
    opens = np.zeros(n)
    highs = np.zeros(n)
    lows = np.zeros(n)
    closes = np.zeros(n)
    volumes = rng.integers(100, 500, size=n)

    # Base ascending price
    base = 2000.0

    for i in range(n):
        # General upward trend
        mid = base + i * 1.5
        opens[i] = mid
        closes[i] = mid + 1.0
        highs[i] = mid + 3.0
        lows[i] = mid - 2.0

    # Create explicit swing lows (must be lowest in window of +-5 bars)
    # Swing low at index 10: make lows[10] the minimum in lows[5:16]
    lows[10] = base + 10 * 1.5 - 15.0  # much lower than surroundings

    # Swing low at index 25: higher than swing low at 10
    lows[25] = base + 25 * 1.5 - 12.0  # lower than surroundings but higher than lows[10]

    # Swing low at index 40: higher than swing low at 25
    lows[40] = base + 40 * 1.5 - 10.0

    # Create explicit swing highs (must be highest in window of +-5 bars)
    # Swing high at index 17: make highs[17] the maximum in highs[12:23]
    highs[17] = base + 17 * 1.5 + 15.0

    # Swing high at index 32: higher than swing high at 17
    highs[32] = base + 32 * 1.5 + 18.0

    # Swing high at index 47 is automatically the highest due to trend

    return _make_candles(opens, highs, lows, closes, volumes)


@pytest.fixture
def sample_candles_bearish():
    """Data showing clear Lower Highs and Lower Lows pattern.

    Construct explicit swing points that the lookback=5 detector will find:
    swing highs at indices 10, 25, 40 (decreasing)
    swing lows at indices 17, 32, 47 (decreasing)
    """
    n = 50
    rng = np.random.default_rng(99)
    opens = np.zeros(n)
    highs = np.zeros(n)
    lows = np.zeros(n)
    closes = np.zeros(n)
    volumes = rng.integers(100, 500, size=n)

    # Base descending price
    base = 2100.0

    for i in range(n):
        # General downward trend
        mid = base - i * 1.5
        opens[i] = mid
        closes[i] = mid - 1.0
        highs[i] = mid + 2.0
        lows[i] = mid - 3.0

    # Create explicit swing highs (must be highest in window of +-5 bars)
    # Swing high at index 10
    highs[10] = base - 10 * 1.5 + 15.0

    # Swing high at index 25: lower than swing high at 10
    highs[25] = base - 25 * 1.5 + 12.0

    # Swing high at index 40: lower than swing high at 25
    highs[40] = base - 40 * 1.5 + 10.0

    # Create explicit swing lows (must be lowest in window of +-5 bars)
    # Swing low at index 17
    lows[17] = base - 17 * 1.5 - 15.0

    # Swing low at index 32: lower than swing low at 17
    lows[32] = base - 32 * 1.5 - 18.0

    # Swing low at index 47 is automatically the lowest due to trend

    return _make_candles(opens, highs, lows, closes, volumes)
