"""Tests for src/alpha_model.py - signal generation and Kelly sizing."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from src.alpha_model import AlphaModel, TradeSignal
from src.config import PropFirmConfig, RiskConfig, SymbolConfig
from src.database import TradingDatabase
from src.market_intelligence import Direction, MarketIntelligence


pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def alpha_model(database):
    """Create an AlphaModel with mocked MarketIntelligence."""
    mi = MarketIntelligence()
    model = AlphaModel(
        market_intelligence=mi,
        database=database,
    )
    return model


class TestGenerateSignal:
    async def test_generate_signal_returns_none_without_data(self, alpha_model):
        """With mocked fetch_candles returning None, generate_signal returns None."""
        # Patch fetch_candles to return None (simulating MT5 unavailable)
        with patch.object(
            alpha_model._mi, "fetch_candles", new_callable=AsyncMock, return_value=None
        ):
            result = await alpha_model.generate_signal()
            assert result is None


class TestKellyLotSize:
    async def test_kelly_lot_size_caps_at_max(self, alpha_model):
        """With very favorable history, lot size is capped at 0.5% risk."""
        # Create a very favorable trade history (high win rate, high avg_win)
        alpha_model._trade_history_cache = [
            {"pnl": 500.0} for _ in range(8)
        ] + [
            {"pnl": -50.0} for _ in range(2)
        ]  # 80% win rate, 10:1 payoff

        # With extreme Kelly, the position_risk should cap at max_risk_per_trade_pct
        # max_risk = 0.5% = 0.005
        lot = alpha_model.calculate_kelly_lot_size(
            account_balance=100000.0,
            sl_distance=5.0,
        )

        # Verify the lot is bounded (cannot exceed what 0.5% risk allows)
        # Dollar risk at cap = 100000 * 0.005 = 500
        # sl_points = 5.0 / 0.01 = 500
        # dollar_per_point_per_lot = 100 * 0.01 = 1.0
        # max lot = 500 / (500 * 1.0) = 1.0
        assert lot <= 1.0
        assert lot >= 0.01

    async def test_kelly_lot_size_minimum(self, alpha_model):
        """With insufficient history, returns minimum 0.01."""
        # Only 5 trades - below the 10 trade minimum for Kelly
        alpha_model._trade_history_cache = [
            {"pnl": 100.0} for _ in range(3)
        ] + [
            {"pnl": -50.0} for _ in range(2)
        ]

        lot = alpha_model.calculate_kelly_lot_size(
            account_balance=10000.0,
            sl_distance=5.0,
        )
        # With insufficient history, Kelly returns 0.0, then fallback logic applies
        # but lot should be at minimum 0.01
        assert lot >= 0.01

    async def test_kelly_formula_correctness(self, alpha_model):
        """Known win_rate=0.6, avg_win=200, avg_loss=100 produces correct Kelly %."""
        # Kelly % = (win_rate * avg_win - (1 - win_rate) * avg_loss) / avg_win
        # = (0.6 * 200 - 0.4 * 100) / 200 = (120 - 40) / 200 = 80 / 200 = 0.4
        history = []
        # 60 wins of $200 each
        for _ in range(60):
            history.append({"pnl": 200.0})
        # 40 losses of $100 each
        for _ in range(40):
            history.append({"pnl": -100.0})

        alpha_model._trade_history_cache = history
        kelly_pct = alpha_model._compute_kelly_percentage()

        assert kelly_pct == pytest.approx(0.4, abs=0.001)

    async def test_kelly_returns_zero_for_losing_strategy(self, alpha_model):
        """A strategy with more losses than gains returns Kelly = 0."""
        # 30% win rate, avg_win=100, avg_loss=200
        # Kelly = (0.3 * 100 - 0.7 * 200) / 100 = (30 - 140) / 100 = -1.1 -> clamped to 0
        history = []
        for _ in range(30):
            history.append({"pnl": 100.0})
        for _ in range(70):
            history.append({"pnl": -200.0})

        alpha_model._trade_history_cache = history
        kelly_pct = alpha_model._compute_kelly_percentage()

        assert kelly_pct == 0.0
