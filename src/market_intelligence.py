"""
Market Intelligence module for XAUUSD Institutional Trading System.

Provides multi-timeframe market structure analysis, Order Block and Fair Value Gap
detection, session range computation, liquidity sweep identification, displacement
detection, and ATR calculation. All MT5 data access is wrapped for graceful
fallback on platforms where MetaTrader5 is unavailable.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import List, Optional, Tuple

import numpy as np

try:
    import MetaTrader5 as mt5  # type: ignore[import-untyped]

    MT5_AVAILABLE = True
except ImportError:
    mt5 = None  # type: ignore[assignment]
    MT5_AVAILABLE = False

from src.config import AlphaConfig, SessionConfig, SymbolConfig

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums and Dataclasses
# ---------------------------------------------------------------------------


class MarketStructure(Enum):
    """Overall market bias derived from swing point analysis."""

    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    RANGING = "RANGING"


class SwingType(Enum):
    """Type of swing point."""

    HIGH = "HIGH"
    LOW = "LOW"


class Direction(Enum):
    """Trade / block direction."""

    BUY = "BUY"
    SELL = "SELL"


@dataclass
class SwingPoint:
    """A detected swing high or low."""

    price: float
    timestamp: datetime
    type: SwingType


@dataclass
class OrderBlock:
    """Institutional Order Block zone."""

    price_high: float
    price_low: float
    timeframe: str
    direction: Direction
    volume_ratio: float
    is_valid: bool
    timestamp: datetime


@dataclass
class FVG:
    """Fair Value Gap (imbalance zone)."""

    high: float
    low: float
    direction: Direction
    gap_size: float
    timestamp: datetime


# ---------------------------------------------------------------------------
# Timeframe mapping helper
# ---------------------------------------------------------------------------

_MT5_TIMEFRAME_MAP = {
    "M1": 1,
    "M5": 5,
    "M15": 15,
    "M30": 30,
    "H1": 16385,
    "H4": 16388,
    "D1": 16408,
    "W1": 32769,
    "MN1": 49153,
}


def _resolve_timeframe(timeframe: str) -> int:
    """Resolve a string timeframe to MT5 integer constant.

    Falls back to the mt5 module attribute if available.
    """
    if MT5_AVAILABLE and hasattr(mt5, f"TIMEFRAME_{timeframe}"):
        return getattr(mt5, f"TIMEFRAME_{timeframe}")
    return _MT5_TIMEFRAME_MAP.get(timeframe, 16385)


# ---------------------------------------------------------------------------
# MarketIntelligence Class
# ---------------------------------------------------------------------------


class MarketIntelligence:
    """Provides all market analysis primitives for the alpha model.

    Wraps MT5 data retrieval and exposes pure-computation methods for
    structure detection, order blocks, FVGs, session ranges, sweeps, and
    displacement identification.
    """

    def __init__(
        self,
        alpha_config: Optional[AlphaConfig] = None,
        session_config: Optional[SessionConfig] = None,
        symbol_config: Optional[SymbolConfig] = None,
    ) -> None:
        self._alpha = alpha_config or AlphaConfig()
        self._session = session_config or SessionConfig()
        self._symbol = symbol_config or SymbolConfig()

    # ------------------------------------------------------------------
    # MT5 Data Fetching
    # ------------------------------------------------------------------

    async def fetch_candles(
        self, symbol: str, timeframe: str, count: int
    ) -> Optional[np.ndarray]:
        """Fetch historical candles from MT5.

        Args:
            symbol: Trading symbol (e.g. 'XAUUSD').
            timeframe: Timeframe string (M1, M5, M15, H1, H4, etc.).
            count: Number of bars to retrieve.

        Returns:
            Numpy structured array with fields (time, open, high, low, close,
            tick_volume, spread, real_volume) or None if unavailable.
        """
        if not MT5_AVAILABLE or mt5 is None:
            logger.warning(
                "MT5 not available - cannot fetch candles for %s %s", symbol, timeframe
            )
            return None

        tf_value = _resolve_timeframe(timeframe)

        try:
            rates = mt5.copy_rates_from_pos(symbol, tf_value, 0, count)
        except Exception as exc:
            logger.error(
                "MT5 copy_rates_from_pos failed for %s %s: %s",
                symbol,
                timeframe,
                exc,
            )
            return None

        if rates is None or len(rates) == 0:
            logger.warning(
                "No candle data returned for %s %s (count=%d)", symbol, timeframe, count
            )
            return None

        logger.debug(
            "Fetched %d candles for %s %s", len(rates), symbol, timeframe
        )
        return rates

    # ------------------------------------------------------------------
    # Market Structure Detection
    # ------------------------------------------------------------------

    def detect_market_structure(
        self, candles: np.ndarray, lookback: int = 5
    ) -> MarketStructure:
        """Analyze candles to determine institutional market structure.

        Uses swing highs and lows identified with a rolling lookback window.
        - Higher Highs + Higher Lows = BULLISH
        - Lower Highs + Lower Lows = BEARISH
        - Otherwise = RANGING

        Args:
            candles: Numpy structured array with 'high' and 'low' fields.
            lookback: Number of bars on each side to confirm a swing point.

        Returns:
            MarketStructure enum value.
        """
        highs = candles["high"]
        lows = candles["low"]
        n = len(candles)

        swing_highs: List[float] = []
        swing_lows: List[float] = []

        for i in range(lookback, n - lookback):
            # Swing high: highest in the window
            if highs[i] == max(highs[i - lookback : i + lookback + 1]):
                swing_highs.append(float(highs[i]))
            # Swing low: lowest in the window
            if lows[i] == min(lows[i - lookback : i + lookback + 1]):
                swing_lows.append(float(lows[i]))

        if len(swing_highs) < 2 or len(swing_lows) < 2:
            logger.debug("Insufficient swing points for structure detection")
            return MarketStructure.RANGING

        # Check last two swing highs/lows
        hh = swing_highs[-1] > swing_highs[-2]
        hl = swing_lows[-1] > swing_lows[-2]
        lh = swing_highs[-1] < swing_highs[-2]
        ll = swing_lows[-1] < swing_lows[-2]

        if hh and hl:
            logger.info("Market structure: BULLISH (HH + HL)")
            return MarketStructure.BULLISH
        elif lh and ll:
            logger.info("Market structure: BEARISH (LH + LL)")
            return MarketStructure.BEARISH
        else:
            logger.info("Market structure: RANGING")
            return MarketStructure.RANGING

    # ------------------------------------------------------------------
    # Order Block Detection
    # ------------------------------------------------------------------

    def detect_order_blocks(
        self, candles: np.ndarray, structure: MarketStructure, timeframe: str = "H1"
    ) -> List[OrderBlock]:
        """Detect institutional Order Blocks.

        An Order Block is the last opposing candle before a strong displacement
        move. Validated by volume exceeding 1.5x the 20-period average.

        Args:
            candles: Numpy structured array with OHLCV fields.
            structure: Current market structure for directional bias.
            timeframe: Timeframe label for the OrderBlock record.

        Returns:
            List of detected OrderBlock instances.
        """
        n = len(candles)
        if n < 22:
            logger.debug("Insufficient candles for order block detection")
            return []

        opens = candles["open"]
        closes = candles["close"]
        highs = candles["high"]
        lows = candles["low"]
        volumes = candles["tick_volume"]
        times = candles["time"]

        order_blocks: List[OrderBlock] = []
        vol_period = 20

        for i in range(vol_period + 1, n):
            avg_vol = float(np.mean(volumes[i - vol_period : i]))
            if avg_vol == 0:
                continue

            current_vol = float(volumes[i])
            vol_ratio = current_vol / avg_vol

            # Only consider displacement candles (volume > 1.5x average)
            if vol_ratio < 1.5:
                continue

            is_bullish_candle = closes[i] > opens[i]
            is_bearish_candle = closes[i] < opens[i]

            # Bullish OB: bearish candle before a bullish displacement
            if structure in (MarketStructure.BULLISH, MarketStructure.RANGING):
                if is_bullish_candle:
                    # Look for last bearish candle before this displacement
                    for j in range(i - 1, max(i - self._alpha.ob_lookback, 0) - 1, -1):
                        if closes[j] < opens[j]:
                            ob = OrderBlock(
                                price_high=float(highs[j]),
                                price_low=float(lows[j]),
                                timeframe=timeframe,
                                direction=Direction.BUY,
                                volume_ratio=vol_ratio,
                                is_valid=True,
                                timestamp=datetime.fromtimestamp(
                                    int(times[j]), tz=timezone.utc
                                ),
                            )
                            order_blocks.append(ob)
                            break

            # Bearish OB: bullish candle before a bearish displacement
            if structure in (MarketStructure.BEARISH, MarketStructure.RANGING):
                if is_bearish_candle:
                    # Look for last bullish candle before this displacement
                    for j in range(i - 1, max(i - self._alpha.ob_lookback, 0) - 1, -1):
                        if closes[j] > opens[j]:
                            ob = OrderBlock(
                                price_high=float(highs[j]),
                                price_low=float(lows[j]),
                                timeframe=timeframe,
                                direction=Direction.SELL,
                                volume_ratio=vol_ratio,
                                is_valid=True,
                                timestamp=datetime.fromtimestamp(
                                    int(times[j]), tz=timezone.utc
                                ),
                            )
                            order_blocks.append(ob)
                            break

        logger.info("Detected %d order blocks on %s", len(order_blocks), timeframe)
        return order_blocks

    # ------------------------------------------------------------------
    # Fair Value Gap Detection
    # ------------------------------------------------------------------

    def detect_fair_value_gaps(
        self, candles: np.ndarray, atr: float
    ) -> List[FVG]:
        """Detect Fair Value Gaps (imbalances).

        Bullish FVG: candle[i-1].high < candle[i+1].low
        Bearish FVG: candle[i-1].low > candle[i+1].high

        Gaps are filtered by minimum size relative to ATR.

        Args:
            candles: Numpy structured array with OHLCV fields.
            atr: Current ATR value for filtering.

        Returns:
            List of FVG instances meeting the minimum gap threshold.
        """
        n = len(candles)
        if n < 3:
            return []

        highs = candles["high"]
        lows = candles["low"]
        times = candles["time"]

        min_gap = atr * self._alpha.fvg_min_gap_atr_ratio
        fvgs: List[FVG] = []

        for i in range(1, n - 1):
            # Bullish FVG: gap between candle[i-1].high and candle[i+1].low
            if lows[i + 1] > highs[i - 1]:
                gap_size = float(lows[i + 1] - highs[i - 1])
                if gap_size >= min_gap:
                    fvgs.append(
                        FVG(
                            high=float(lows[i + 1]),
                            low=float(highs[i - 1]),
                            direction=Direction.BUY,
                            gap_size=gap_size,
                            timestamp=datetime.fromtimestamp(
                                int(times[i]), tz=timezone.utc
                            ),
                        )
                    )

            # Bearish FVG: gap between candle[i+1].high and candle[i-1].low
            if lows[i - 1] > highs[i + 1]:
                gap_size = float(lows[i - 1] - highs[i + 1])
                if gap_size >= min_gap:
                    fvgs.append(
                        FVG(
                            high=float(lows[i - 1]),
                            low=float(highs[i + 1]),
                            direction=Direction.SELL,
                            gap_size=gap_size,
                            timestamp=datetime.fromtimestamp(
                                int(times[i]), tz=timezone.utc
                            ),
                        )
                    )

        logger.info("Detected %d fair value gaps", len(fvgs))
        return fvgs

    # ------------------------------------------------------------------
    # Asian Session Range
    # ------------------------------------------------------------------

    def get_asian_session_range(
        self, candles_m15: np.ndarray
    ) -> Optional[Tuple[float, float]]:
        """Calculate the Asian session high and low for the current day.

        Filters M15 candles between 00:00-08:00 UTC.

        Args:
            candles_m15: M15 numpy structured array.

        Returns:
            Tuple of (asian_high, asian_low) or None if insufficient data.
        """
        if candles_m15 is None or len(candles_m15) == 0:
            return None

        times = candles_m15["time"]
        highs = candles_m15["high"]
        lows = candles_m15["low"]

        asian_start_hour, asian_start_min = self._session.asian_start
        asian_end_hour, asian_end_min = self._session.asian_end

        # Get today's date from the latest candle
        latest_time = datetime.fromtimestamp(int(times[-1]), tz=timezone.utc)
        today = latest_time.date()

        asian_highs: List[float] = []
        asian_lows: List[float] = []

        for idx in range(len(candles_m15)):
            candle_time = datetime.fromtimestamp(int(times[idx]), tz=timezone.utc)
            if candle_time.date() != today:
                continue

            hour = candle_time.hour
            minute = candle_time.minute
            candle_minutes = hour * 60 + minute
            start_minutes = asian_start_hour * 60 + asian_start_min
            end_minutes = asian_end_hour * 60 + asian_end_min

            if start_minutes <= candle_minutes < end_minutes:
                asian_highs.append(float(highs[idx]))
                asian_lows.append(float(lows[idx]))

        if not asian_highs or not asian_lows:
            logger.debug("No Asian session candles found for %s", today)
            return None

        asian_high = max(asian_highs)
        asian_low = min(asian_lows)
        logger.info(
            "Asian session range: high=%.5f low=%.5f", asian_high, asian_low
        )
        return (asian_high, asian_low)

    # ------------------------------------------------------------------
    # Liquidity Sweep Detection
    # ------------------------------------------------------------------

    def detect_liquidity_sweep(
        self,
        candle: np.ndarray,
        asian_high: float,
        asian_low: float,
        atr: float,
    ) -> Optional[Direction]:
        """Detect a liquidity sweep of the Asian session range.

        A sweep occurs when a candle wicks outside the Asian range by at least
        atr * atr_sweep_multiplier, but the body closes back inside the range.

        Args:
            candle: Single candle (numpy structured array element or 1-element array).
            asian_high: Asian session high price.
            asian_low: Asian session low price.
            atr: Current ATR value for threshold calculation.

        Returns:
            Direction.SELL if swept above (sell setup), Direction.BUY if swept
            below (buy setup), or None if no sweep detected.
        """
        threshold = atr * self._alpha.atr_sweep_multiplier

        # Handle both single element and 1-element array
        if hasattr(candle, "__len__") and len(candle) == 1:
            candle = candle[0]

        high = float(candle["high"])
        low = float(candle["low"])
        open_price = float(candle["open"])
        close_price = float(candle["close"])

        body_high = max(open_price, close_price)
        body_low = min(open_price, close_price)

        # Sweep above Asian high: wick above, body closes back inside
        if high > asian_high + threshold and body_high <= asian_high:
            logger.info(
                "Liquidity sweep detected ABOVE Asian high (%.5f > %.5f + %.5f)",
                high,
                asian_high,
                threshold,
            )
            return Direction.SELL

        # Sweep below Asian low: wick below, body closes back inside
        if low < asian_low - threshold and body_low >= asian_low:
            logger.info(
                "Liquidity sweep detected BELOW Asian low (%.5f < %.5f - %.5f)",
                low,
                asian_low,
                threshold,
            )
            return Direction.BUY

        return None

    # ------------------------------------------------------------------
    # Displacement Detection
    # ------------------------------------------------------------------

    def detect_displacement(
        self, candles_m1: np.ndarray, lookback: int = 10
    ) -> bool:
        """Detect displacement on M1 candles.

        Displacement is defined as a candle with:
        - Volume > 2x the recent 20-bar average
        - Spread (high - low) > 1.5x ATR

        Checks the most recent `lookback` candles for any displacement.

        Args:
            candles_m1: M1 numpy structured array.
            lookback: Number of recent candles to check for displacement.

        Returns:
            True if displacement detected, False otherwise.
        """
        n = len(candles_m1)
        if n < 21:
            return False

        atr = self.calculate_atr(candles_m1, period=14)
        if atr <= 0:
            return False

        volumes = candles_m1["tick_volume"]
        highs = candles_m1["high"]
        lows = candles_m1["low"]

        start_idx = max(20, n - lookback)

        for i in range(start_idx, n):
            avg_vol = float(np.mean(volumes[i - 20 : i]))
            if avg_vol == 0:
                continue

            current_vol = float(volumes[i])
            spread = float(highs[i] - lows[i])

            if current_vol > 2.0 * avg_vol and spread > 1.5 * atr:
                logger.info(
                    "Displacement detected at index %d: vol_ratio=%.2f spread=%.5f atr=%.5f",
                    i,
                    current_vol / avg_vol,
                    spread,
                    atr,
                )
                return True

        return False

    # ------------------------------------------------------------------
    # ATR Calculation
    # ------------------------------------------------------------------

    def calculate_atr(self, candles: np.ndarray, period: int = 14) -> float:
        """Calculate Average True Range.

        Uses standard ATR formula: smoothed average of True Range over the
        given period.

        Args:
            candles: Numpy structured array with high, low, close fields.
            period: ATR lookback period (default 14).

        Returns:
            Current ATR value as float. Returns 0.0 if insufficient data.
        """
        n = len(candles)
        if n < period + 1:
            return 0.0

        highs = candles["high"].astype(float)
        lows = candles["low"].astype(float)
        closes = candles["close"].astype(float)

        # True Range: max(H-L, |H-Cprev|, |L-Cprev|)
        tr = np.zeros(n - 1)
        for i in range(1, n):
            hl = highs[i] - lows[i]
            hc = abs(highs[i] - closes[i - 1])
            lc = abs(lows[i] - closes[i - 1])
            tr[i - 1] = max(hl, hc, lc)

        if len(tr) < period:
            return 0.0

        # Simple moving average of TR for the ATR
        atr_value = float(np.mean(tr[-period:]))
        return atr_value
