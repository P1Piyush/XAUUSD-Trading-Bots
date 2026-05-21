"""
Alpha Model module for XAUUSD Institutional Trading System.

Orchestrates the Smart Money Confluence signal generation pipeline, combining
multi-timeframe structure analysis, Order Block/FVG zone detection, Asian session
liquidity sweeps, and M1 Market Structure Shift execution triggers. Includes
Fractional Kelly Criterion position sizing.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional

import numpy as np

from src.config import AlphaConfig, OrderConfig, PropFirmConfig, RiskConfig, SymbolConfig
from src.database import TradingDatabase
from src.market_intelligence import (
    Direction,
    FVG,
    MarketIntelligence,
    MarketStructure,
    OrderBlock,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class TradeSignal:
    """Generated trade signal with full confluence details."""

    direction: Direction
    entry_price: float
    stop_loss: float
    take_profit_1: float
    take_profit_2: float
    lot_size: float
    confidence_score: float
    confluence_factors: List[str] = field(default_factory=list)
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# AlphaModel Class
# ---------------------------------------------------------------------------


class AlphaModel:
    """Smart Money Confluence signal generator.

    Orchestrates the full institutional trading logic:
    1. Multi-timeframe structure analysis (H4 -> H1 alignment)
    2. HTF Order Block and FVG zone identification
    3. Asian session range and liquidity sweep detection
    4. M1 Market Structure Shift with displacement confirmation
    5. Entry/SL/TP calculation with Kelly Criterion sizing
    """

    def __init__(
        self,
        market_intelligence: MarketIntelligence,
        database: TradingDatabase,
        alpha_config: Optional[AlphaConfig] = None,
        risk_config: Optional[RiskConfig] = None,
        prop_config: Optional[PropFirmConfig] = None,
        order_config: Optional[OrderConfig] = None,
        symbol_config: Optional[SymbolConfig] = None,
    ) -> None:
        self._mi = market_intelligence
        self._db = database
        self._alpha = alpha_config or AlphaConfig()
        self._risk = risk_config or RiskConfig()
        self._prop = prop_config or PropFirmConfig()
        self._order = order_config or OrderConfig()
        self._symbol = symbol_config or SymbolConfig()

    # ------------------------------------------------------------------
    # Signal Generation (Main Orchestration)
    # ------------------------------------------------------------------

    async def generate_signal(self) -> Optional[TradeSignal]:
        """Generate a trade signal through the full confluence pipeline.

        Steps:
            1. Fetch H4 candles, determine market structure
            2. Fetch H1 candles, confirm alignment with H4
            3. Detect Order Blocks and FVGs on HTF
            4. Fetch M15 candles, calculate Asian session range
            5. Fetch M5/M15 candles, check for liquidity sweep
            6. Verify sweep within HTF OB or FVG zone
            7. If confirmed, fetch M1 candles
            8. Detect Market Structure Shift (break + displacement)
            9. Calculate entry/SL/TP and return TradeSignal

        Returns:
            TradeSignal if all confluence conditions are met, None otherwise.
        """
        symbol = self._symbol.symbol
        confluence_factors: List[str] = []

        # Step 1: H4 structure
        logger.info("Step 1: Analyzing H4 market structure")
        h4_candles = await self._mi.fetch_candles(symbol, "H4", 100)
        if h4_candles is None or len(h4_candles) < 20:
            logger.warning("Insufficient H4 data for structure analysis")
            return None

        h4_structure = self._mi.detect_market_structure(h4_candles)
        if h4_structure == MarketStructure.RANGING:
            logger.info("H4 structure is RANGING - no clear bias, skipping")
            return None

        confluence_factors.append(f"H4 structure: {h4_structure.value}")

        # Step 2: H1 alignment
        logger.info("Step 2: Confirming H1 alignment with H4")
        h1_candles = await self._mi.fetch_candles(symbol, "H1", 100)
        if h1_candles is None or len(h1_candles) < 20:
            logger.warning("Insufficient H1 data for structure confirmation")
            return None

        h1_structure = self._mi.detect_market_structure(h1_candles)
        if h1_structure != h4_structure:
            logger.info(
                "H1 (%s) does not align with H4 (%s) - no signal",
                h1_structure.value,
                h4_structure.value,
            )
            return None

        confluence_factors.append(f"H1 structure aligned: {h1_structure.value}")

        # Step 3: Detect OBs and FVGs on HTF
        logger.info("Step 3: Detecting HTF Order Blocks and FVGs")
        h4_atr = self._mi.calculate_atr(h4_candles, self._alpha.atr_period)
        h1_atr = self._mi.calculate_atr(h1_candles, self._alpha.atr_period)

        h4_obs = self._mi.detect_order_blocks(h4_candles, h4_structure, "H4")
        h1_obs = self._mi.detect_order_blocks(h1_candles, h1_structure, "H1")
        all_obs = h4_obs + h1_obs

        h4_fvgs = self._mi.detect_fair_value_gaps(h4_candles, h4_atr)
        h1_fvgs = self._mi.detect_fair_value_gaps(h1_candles, h1_atr)
        all_fvgs = h4_fvgs + h1_fvgs

        if not all_obs and not all_fvgs:
            logger.info("No Order Blocks or FVGs detected on HTF - no signal")
            return None

        confluence_factors.append(
            f"HTF zones: {len(all_obs)} OBs, {len(all_fvgs)} FVGs"
        )

        # Step 4: Asian session range
        logger.info("Step 4: Calculating Asian session range")
        m15_candles = await self._mi.fetch_candles(symbol, "M15", 200)
        if m15_candles is None or len(m15_candles) < 32:
            logger.warning("Insufficient M15 data for Asian range calculation")
            return None

        asian_range = self._mi.get_asian_session_range(m15_candles)
        if asian_range is None:
            logger.info("Could not determine Asian session range")
            return None

        asian_high, asian_low = asian_range
        confluence_factors.append(
            f"Asian range: {asian_high:.5f} - {asian_low:.5f}"
        )

        # Step 5: Check for liquidity sweep
        logger.info("Step 5: Checking for liquidity sweep at Asian range")
        m5_candles = await self._mi.fetch_candles(symbol, "M5", 100)
        if m5_candles is None or len(m5_candles) < 20:
            logger.warning("Insufficient M5 data for sweep detection")
            return None

        m15_atr = self._mi.calculate_atr(m15_candles, self._alpha.atr_period)
        if m15_atr <= 0:
            logger.warning("Invalid M15 ATR for sweep detection")
            return None

        # Check recent M5 candles for sweep
        sweep_direction: Optional[Direction] = None
        sweep_candle_idx: Optional[int] = None

        for idx in range(len(m5_candles) - 1, max(len(m5_candles) - 20, -1), -1):
            result = self._mi.detect_liquidity_sweep(
                m5_candles[idx : idx + 1], asian_high, asian_low, m15_atr
            )
            if result is not None:
                sweep_direction = result
                sweep_candle_idx = idx
                break

        if sweep_direction is None:
            logger.info("No liquidity sweep detected at Asian range")
            return None

        confluence_factors.append(f"Liquidity sweep: {sweep_direction.value}")

        # Step 6: Verify sweep within HTF OB or FVG zone
        logger.info("Step 6: Verifying sweep within HTF zone")
        sweep_price = float(m5_candles[sweep_candle_idx]["high"]) if sweep_direction == Direction.SELL else float(m5_candles[sweep_candle_idx]["low"])

        in_zone = self._is_price_in_zone(sweep_price, all_obs, all_fvgs, sweep_direction)
        if not in_zone:
            logger.info("Sweep not within an HTF OB or FVG zone - no signal")
            return None

        confluence_factors.append("Sweep confirmed within HTF zone")

        # Step 7: Fetch M1 candles for MSS detection
        logger.info("Step 7: Fetching M1 candles for MSS detection")
        m1_candles = await self._mi.fetch_candles(symbol, "M1", 100)
        if m1_candles is None or len(m1_candles) < 30:
            logger.warning("Insufficient M1 data for MSS detection")
            return None

        # Step 8: Detect Market Structure Shift with displacement
        logger.info("Step 8: Detecting Market Structure Shift on M1")
        mss_confirmed = self._detect_mss(m1_candles, sweep_direction)
        if not mss_confirmed:
            logger.info("No Market Structure Shift detected on M1")
            return None

        has_displacement = self._mi.detect_displacement(m1_candles, lookback=10)
        if not has_displacement:
            logger.info("No displacement confirmed on M1")
            return None

        confluence_factors.append("M1 MSS + displacement confirmed")

        # Step 9: Calculate entry, SL, TP and build signal
        logger.info("Step 9: Calculating trade parameters")
        m1_atr = self._mi.calculate_atr(m1_candles, self._alpha.atr_period)
        if m1_atr <= 0:
            logger.warning("Invalid M1 ATR for trade calculation")
            return None

        entry_price, stop_loss, tp1, tp2 = self._calculate_trade_levels(
            m1_candles, sweep_direction, m1_atr
        )

        # Calculate lot size
        lot_size = self.calculate_kelly_lot_size(
            account_balance=10000.0,  # Will be overridden by caller with actual balance
            sl_distance=abs(entry_price - stop_loss),
        )

        # Confidence score based on confluence count
        confidence = min(1.0, len(confluence_factors) / 8.0)

        signal = TradeSignal(
            direction=sweep_direction,
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit_1=tp1,
            take_profit_2=tp2,
            lot_size=lot_size,
            confidence_score=confidence,
            confluence_factors=confluence_factors,
            timestamp=datetime.now(timezone.utc),
        )

        logger.info(
            "Trade signal generated: %s @ %.5f, SL=%.5f, TP1=%.5f, TP2=%.5f, "
            "lot=%.2f, confidence=%.2f",
            signal.direction.value,
            signal.entry_price,
            signal.stop_loss,
            signal.take_profit_1,
            signal.take_profit_2,
            signal.lot_size,
            signal.confidence_score,
        )
        return signal

    # ------------------------------------------------------------------
    # Kelly Criterion Lot Sizing
    # ------------------------------------------------------------------

    def calculate_kelly_lot_size(
        self,
        account_balance: float,
        sl_distance: float = 0.0,
    ) -> float:
        """Calculate position size using Fractional Kelly Criterion.

        Pulls trade history from database to compute win rate and average
        win/loss ratio. Applies fractional Kelly with a cap at
        max_risk_per_trade_pct.

        Cold-start behavior:
            When trade history contains fewer than 10 samples, the Kelly
            formula cannot produce a statistically meaningful estimate and
            returns 0.0. In this case, the position sizer falls back to
            ``max_risk_per_trade_pct * kelly_cold_start_fraction`` (default
            0.5, yielding 0.25% risk per trade with default config). This
            conservative default prevents oversized positions during early
            live operation while still allowing trades. The fallback fraction
            is configurable via ``RiskConfig.kelly_cold_start_fraction``.

        Args:
            account_balance: Current account balance.
            sl_distance: Stop loss distance in price units.

        Returns:
            Lot size (minimum 0.01).
        """
        # Default to minimum if we cannot compute
        if sl_distance <= 0 or account_balance <= 0:
            return 0.01

        # Attempt to get trade history (synchronous fallback for calculation)
        # In async context, caller should pre-fetch history
        kelly_pct = self._compute_kelly_percentage()

        # Apply fractional Kelly
        position_risk = kelly_pct * self._risk.kelly_fraction

        # Cap at max risk per trade
        max_risk = self._prop.max_risk_per_trade_pct / 100.0
        position_risk = min(position_risk, max_risk)

        # Cold-start fallback: when Kelly returns 0 (insufficient history),
        # use a configurable fraction of max risk to allow trading at reduced size.
        if position_risk <= 0:
            cold_start_fraction = self._risk.kelly_cold_start_fraction
            position_risk = max_risk * cold_start_fraction

        # Convert risk percentage to dollar risk
        dollar_risk = account_balance * position_risk

        # Convert to lot size based on SL distance
        # For XAUUSD: 1 lot = 100 oz, pip_value depends on broker
        # Standard: risk_amount / (sl_pips * pip_value_per_lot)
        point_value = self._symbol.point_value
        if point_value <= 0:
            point_value = 0.01

        sl_points = sl_distance / point_value
        if sl_points <= 0:
            return 0.01

        # Standard lot calculation: dollar_risk / (sl_points * dollar_per_point_per_lot)
        # For XAUUSD typically $1 per point per 0.01 lot (micro), $100 per point per 1.0 lot
        dollar_per_point_per_lot = 100.0 * point_value  # Simplified
        lot_size = dollar_risk / (sl_points * dollar_per_point_per_lot)

        # Enforce minimum and round to 2 decimals
        lot_size = max(0.01, round(lot_size, 2))

        logger.info(
            "Kelly lot size: %.2f (kelly_pct=%.4f, risk=%.4f, balance=%.2f, sl_dist=%.5f)",
            lot_size,
            kelly_pct,
            position_risk,
            account_balance,
            sl_distance,
        )
        return lot_size

    def _compute_kelly_percentage(self) -> float:
        """Compute raw Kelly percentage from trade history.

        Kelly % = (win_rate * avg_win - (1 - win_rate) * avg_loss) / avg_win

        When fewer than 10 trades are available, returns 0.0 to signal
        that the Kelly estimate is unreliable. The caller should apply
        the cold-start fallback (see calculate_kelly_lot_size).

        Returns:
            Kelly percentage (0.0 if insufficient history or losing strategy).
        """
        # This is called synchronously - use a cached/preloaded history
        # In production, history would be pre-fetched async before calling
        if not hasattr(self, "_trade_history_cache"):
            self._trade_history_cache: List[dict] = []

        history = self._trade_history_cache
        if len(history) < 10:
            logger.debug(
                "Insufficient trade history (%d trades) for Kelly calculation",
                len(history),
            )
            return 0.0

        wins = [t for t in history if t.get("pnl", 0) > 0]
        losses = [t for t in history if t.get("pnl", 0) < 0]

        total_trades = len(wins) + len(losses)
        if total_trades == 0:
            return 0.0

        win_rate = len(wins) / total_trades

        avg_win = (
            sum(t["pnl"] for t in wins) / len(wins) if wins else 0.0
        )
        avg_loss = (
            abs(sum(t["pnl"] for t in losses) / len(losses)) if losses else 0.0
        )

        if avg_win <= 0:
            return 0.0

        kelly_pct = (win_rate * avg_win - (1 - win_rate) * avg_loss) / avg_win

        # Kelly can be negative (meaning don't trade) - floor at 0
        kelly_pct = max(0.0, kelly_pct)

        logger.debug(
            "Kelly calculation: win_rate=%.3f, avg_win=%.2f, avg_loss=%.2f, kelly=%.4f",
            win_rate,
            avg_win,
            avg_loss,
            kelly_pct,
        )
        return kelly_pct

    async def load_trade_history(self) -> None:
        """Pre-load trade history from database for Kelly calculations.

        Should be called before generate_signal() to ensure synchronous
        Kelly computation has access to history data.
        """
        try:
            self._trade_history_cache = await self._db.get_trade_history(
                limit=100, symbol=self._symbol.symbol
            )
            logger.info(
                "Loaded %d trades for Kelly calculation",
                len(self._trade_history_cache),
            )
        except Exception as exc:
            logger.error("Failed to load trade history: %s", exc)
            self._trade_history_cache = []

    # ------------------------------------------------------------------
    # Internal Helpers
    # ------------------------------------------------------------------

    def _is_price_in_zone(
        self,
        price: float,
        order_blocks: List[OrderBlock],
        fvgs: List[FVG],
        direction: Direction,
    ) -> bool:
        """Check if a price is within any HTF Order Block or FVG zone.

        Args:
            price: Price to check.
            order_blocks: List of detected order blocks.
            fvgs: List of detected fair value gaps.
            direction: Expected trade direction.

        Returns:
            True if price falls within a matching zone.
        """
        # Check Order Blocks
        for ob in order_blocks:
            if ob.direction == direction:
                if ob.price_low <= price <= ob.price_high:
                    logger.debug(
                        "Price %.5f within OB zone [%.5f - %.5f]",
                        price,
                        ob.price_low,
                        ob.price_high,
                    )
                    return True

        # Check FVGs
        for fvg in fvgs:
            if fvg.direction == direction:
                if fvg.low <= price <= fvg.high:
                    logger.debug(
                        "Price %.5f within FVG zone [%.5f - %.5f]",
                        price,
                        fvg.low,
                        fvg.high,
                    )
                    return True

        return False

    def _detect_mss(
        self, m1_candles: np.ndarray, expected_direction: Direction
    ) -> bool:
        """Detect Market Structure Shift on M1 candles.

        An MSS is a break of the most recent swing point in the direction
        opposite to the prior micro-structure, confirming reversal.

        Args:
            m1_candles: M1 numpy structured array.
            expected_direction: The direction the MSS should confirm.

        Returns:
            True if MSS detected in the expected direction.
        """
        n = len(m1_candles)
        if n < 10:
            return False

        highs = m1_candles["high"]
        lows = m1_candles["low"]
        closes = m1_candles["close"]

        lookback = 3  # Swing detection window for M1

        # Find recent swing points
        recent_swing_highs: List[float] = []
        recent_swing_lows: List[float] = []

        for i in range(lookback, n - lookback):
            if highs[i] == max(highs[i - lookback : i + lookback + 1]):
                recent_swing_highs.append(float(highs[i]))
            if lows[i] == min(lows[i - lookback : i + lookback + 1]):
                recent_swing_lows.append(float(lows[i]))

        if not recent_swing_highs or not recent_swing_lows:
            return False

        last_close = float(closes[-1])

        # For a BUY MSS: price breaks above most recent swing high
        if expected_direction == Direction.BUY:
            if len(recent_swing_highs) >= 1:
                last_swing_high = recent_swing_highs[-1]
                if last_close > last_swing_high:
                    logger.debug(
                        "M1 MSS BUY: close %.5f > swing high %.5f",
                        last_close,
                        last_swing_high,
                    )
                    return True

        # For a SELL MSS: price breaks below most recent swing low
        if expected_direction == Direction.SELL:
            if len(recent_swing_lows) >= 1:
                last_swing_low = recent_swing_lows[-1]
                if last_close < last_swing_low:
                    logger.debug(
                        "M1 MSS SELL: close %.5f < swing low %.5f",
                        last_close,
                        last_swing_low,
                    )
                    return True

        return False

    def _calculate_trade_levels(
        self,
        m1_candles: np.ndarray,
        direction: Direction,
        atr: float,
    ) -> tuple:
        """Calculate entry, stop loss, and take profit levels.

        Args:
            m1_candles: M1 numpy structured array.
            direction: Trade direction.
            atr: M1 ATR for SL/TP distance.

        Returns:
            Tuple of (entry_price, stop_loss, take_profit_1, take_profit_2).
        """
        last_candle = m1_candles[-1]
        entry_price = float(last_candle["close"])

        if direction == Direction.BUY:
            # SL below recent low + ATR buffer
            recent_low = float(np.min(m1_candles["low"][-10:]))
            stop_loss = recent_low - atr * 0.5
            sl_distance = entry_price - stop_loss

            # TP1 at partial_close_rr, TP2 at final_target_rr
            tp1 = entry_price + sl_distance * self._order.partial_close_rr
            tp2 = entry_price + sl_distance * self._order.final_target_rr_min
        else:
            # SL above recent high + ATR buffer
            recent_high = float(np.max(m1_candles["high"][-10:]))
            stop_loss = recent_high + atr * 0.5
            sl_distance = stop_loss - entry_price

            # TP1 and TP2 below entry
            tp1 = entry_price - sl_distance * self._order.partial_close_rr
            tp2 = entry_price - sl_distance * self._order.final_target_rr_min

        return (entry_price, stop_loss, tp1, tp2)
