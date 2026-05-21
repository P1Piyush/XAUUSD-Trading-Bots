"""Tests for src/market_intelligence.py - market analysis primitives."""

from datetime import datetime, timezone

import numpy as np
import pytest

from src.market_intelligence import (
    Direction,
    FVG,
    MarketIntelligence,
    MarketStructure,
    OrderBlock,
)


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


def _make_candles(opens, highs, lows, closes, volumes=None, start_ts=None, interval_s=3600):
    """Helper to build numpy structured array."""
    n = len(opens)
    if volumes is None:
        volumes = [200] * n
    if start_ts is None:
        start_ts = int(datetime(2024, 1, 15, 0, 0, tzinfo=timezone.utc).timestamp())
    candles = np.zeros(n, dtype=_CANDLE_DTYPE)
    for i in range(n):
        candles[i]["time"] = start_ts + i * interval_s
        candles[i]["open"] = opens[i]
        candles[i]["high"] = highs[i]
        candles[i]["low"] = lows[i]
        candles[i]["close"] = closes[i]
        candles[i]["tick_volume"] = volumes[i]
        candles[i]["spread"] = 20
        candles[i]["real_volume"] = 0
    return candles


class TestCalculateATR:
    def test_calculate_atr_known_data(self):
        """Known data produces correct ATR value."""
        mi = MarketIntelligence()
        # Create 20 bars with known TR values
        # TR = max(H-L, |H-Cprev|, |L-Cprev|)
        n = 20
        opens = [100.0] * n
        highs = [105.0] * n  # H-L = 5.0 always
        lows = [100.0] * n
        closes = [102.0] * n  # close in middle
        candles = _make_candles(opens, highs, lows, closes)

        atr = mi.calculate_atr(candles, period=14)
        # With constant H-L=5.0 and constant close, TR should be ~5.0
        # (since |H-Cprev|=3, |L-Cprev|=2, H-L=5 -> TR=5)
        assert atr == pytest.approx(5.0, abs=0.01)

    def test_calculate_atr_insufficient_data(self):
        """Returns 0.0 with fewer bars than period+1."""
        mi = MarketIntelligence()
        # Only 10 bars, period=14 needs at least 15
        candles = _make_candles([100]*10, [105]*10, [100]*10, [102]*10)
        atr = mi.calculate_atr(candles, period=14)
        assert atr == 0.0


class TestDetectMarketStructure:
    def test_detect_market_structure_bullish(self, sample_candles_bullish):
        """HH/HL data returns BULLISH."""
        mi = MarketIntelligence()
        structure = mi.detect_market_structure(sample_candles_bullish, lookback=5)
        assert structure == MarketStructure.BULLISH

    def test_detect_market_structure_bearish(self, sample_candles_bearish):
        """LH/LL data returns BEARISH."""
        mi = MarketIntelligence()
        structure = mi.detect_market_structure(sample_candles_bearish, lookback=5)
        assert structure == MarketStructure.BEARISH

    def test_detect_market_structure_ranging(self):
        """Mixed data with no clear trend returns RANGING."""
        mi = MarketIntelligence()
        # Create data with alternating swing highs and lows that don't form a trend
        # Need HH but LL (or LH but HL) to produce RANGING
        n = 50
        opens = np.zeros(n)
        highs = np.zeros(n)
        lows = np.zeros(n)
        closes = np.zeros(n)

        base = 2050.0
        for i in range(n):
            mid = base
            opens[i] = mid
            closes[i] = mid + 0.5
            highs[i] = mid + 2.0
            lows[i] = mid - 2.0

        # Create swing highs at 10 and 25: second LOWER than first (LH)
        highs[10] = base + 20.0
        highs[25] = base + 15.0  # Lower high

        # Create swing lows at 17 and 32: second HIGHER than first (HL)
        lows[17] = base - 20.0
        lows[32] = base - 15.0  # Higher low

        # LH + HL = RANGING (not both LH+LL or HH+HL)
        candles = _make_candles(opens, highs, lows, closes)
        structure = mi.detect_market_structure(candles, lookback=5)
        assert structure == MarketStructure.RANGING


class TestDetectOrderBlocks:
    def test_detect_order_blocks(self):
        """Sample with high-volume displacement finds OB."""
        mi = MarketIntelligence()
        n = 30
        opens = [2050.0] * n
        highs = [2055.0] * n
        lows = [2045.0] * n
        closes = [2052.0] * n  # Bullish candles
        volumes = [100] * n

        # Create a bearish candle at index 25 (last opposing candle)
        opens[25] = 2055.0
        closes[25] = 2048.0
        highs[25] = 2056.0
        lows[25] = 2047.0
        volumes[25] = 100

        # Create a bullish displacement candle at index 26 (high volume)
        opens[26] = 2048.0
        closes[26] = 2065.0  # Strong bullish move
        highs[26] = 2066.0
        lows[26] = 2047.5
        volumes[26] = 500  # 5x average - definitely > 1.5x

        candles = _make_candles(opens, highs, lows, closes, volumes)
        obs = mi.detect_order_blocks(candles, MarketStructure.BULLISH, "H1")
        assert len(obs) >= 1
        # The OB should be a BUY direction (bullish OB)
        buy_obs = [ob for ob in obs if ob.direction == Direction.BUY]
        assert len(buy_obs) >= 1


class TestDetectFairValueGaps:
    def test_detect_fair_value_gaps(self):
        """Sample with price gap finds FVG."""
        mi = MarketIntelligence()
        # Bullish FVG: candle[0].high < candle[2].low
        # candle[0]: high=2050, candle[1]: big move, candle[2]: low=2060
        opens = [2045.0, 2050.0, 2060.0]
        highs = [2050.0, 2065.0, 2068.0]
        lows = [2043.0, 2049.0, 2058.0]
        closes = [2048.0, 2063.0, 2065.0]

        candles = _make_candles(opens, highs, lows, closes)
        # ATR-like value; gap = 2058 - 2050 = 8.0, need min_gap = atr * 0.5
        # Set atr to 10 -> min_gap = 5.0, gap=8.0 passes
        fvgs = mi.detect_fair_value_gaps(candles, atr=10.0)
        assert len(fvgs) >= 1
        assert fvgs[0].direction == Direction.BUY
        assert fvgs[0].gap_size == pytest.approx(8.0, abs=0.01)


class TestGetAsianSessionRange:
    def test_get_asian_session_range(self):
        """Filters correctly by time (00:00-08:00 UTC)."""
        mi = MarketIntelligence()
        # Create M15 candles spanning a full day
        n = 96  # 24 hours * 4 bars per hour
        start_ts = int(datetime(2024, 1, 15, 0, 0, tzinfo=timezone.utc).timestamp())
        opens = np.zeros(n)
        highs = np.zeros(n)
        lows = np.zeros(n)
        closes = np.zeros(n)

        for i in range(n):
            base = 2050.0 + i * 0.1
            opens[i] = base
            closes[i] = base + 0.5
            highs[i] = base + 2.0
            lows[i] = base - 1.0

        # Make Asian session (first 32 bars = 8 hours) have distinct range
        # Set a clear high and low in Asian hours
        highs[10] = 2080.0  # Clear Asian high
        lows[5] = 2020.0   # Clear Asian low

        candles = _make_candles(opens, highs, lows, closes, interval_s=900, start_ts=start_ts)
        result = mi.get_asian_session_range(candles)

        assert result is not None
        asian_high, asian_low = result
        assert asian_high == 2080.0
        assert asian_low == 2020.0


class TestDetectLiquiditySweep:
    def test_detect_liquidity_sweep_above(self):
        """Wick above Asian high + body inside returns SELL direction."""
        mi = MarketIntelligence()
        asian_high = 2060.0
        asian_low = 2040.0
        atr = 10.0  # threshold = 10 * 0.5 = 5.0

        # Candle wicks above asian_high + threshold, body closes inside
        candle = np.zeros(1, dtype=_CANDLE_DTYPE)
        candle[0]["open"] = 2058.0
        candle[0]["close"] = 2055.0  # body_high = 2058 <= asian_high=2060? No.
        # body_high = max(open, close) = 2058, needs to be <= asian_high (2060)
        # high needs to be > asian_high + threshold (2065)
        candle[0]["high"] = 2066.0
        candle[0]["low"] = 2054.0
        candle[0]["time"] = 1705305600

        result = mi.detect_liquidity_sweep(candle, asian_high, asian_low, atr)
        assert result == Direction.SELL

    def test_detect_liquidity_sweep_below(self):
        """Wick below Asian low + body inside returns BUY direction."""
        mi = MarketIntelligence()
        asian_high = 2060.0
        asian_low = 2040.0
        atr = 10.0  # threshold = 10 * 0.5 = 5.0

        # Candle wicks below asian_low - threshold, body closes inside
        candle = np.zeros(1, dtype=_CANDLE_DTYPE)
        candle[0]["open"] = 2042.0
        candle[0]["close"] = 2044.0  # body_low = min(open, close) = 2042 >= asian_low (2040)
        candle[0]["high"] = 2046.0
        candle[0]["low"] = 2033.0  # < asian_low - threshold (2035)
        candle[0]["time"] = 1705305600

        result = mi.detect_liquidity_sweep(candle, asian_high, asian_low, atr)
        assert result == Direction.BUY

    def test_no_sweep_when_body_outside(self):
        """No sweep when body closes outside the range."""
        mi = MarketIntelligence()
        asian_high = 2060.0
        asian_low = 2040.0
        atr = 10.0

        # Body high > asian_high (not a sweep - body breaks above)
        candle = np.zeros(1, dtype=_CANDLE_DTYPE)
        candle[0]["open"] = 2062.0
        candle[0]["close"] = 2068.0  # body_high = 2068 > asian_high
        candle[0]["high"] = 2070.0
        candle[0]["low"] = 2061.0
        candle[0]["time"] = 1705305600

        result = mi.detect_liquidity_sweep(candle, asian_high, asian_low, atr)
        assert result is None


class TestDetectDisplacement:
    def test_detect_displacement(self):
        """High-volume expansive candle is detected."""
        mi = MarketIntelligence()
        # Need 21+ candles. Make normal candles then one displacement candle.
        n = 30
        opens = [2050.0] * n
        highs = [2053.0] * n  # normal spread = 5.0
        lows = [2048.0] * n
        closes = [2051.0] * n
        volumes = [100] * n

        # Last candle: displacement (vol > 2x avg, spread > 1.5x ATR)
        # ATR approx = 5.0 (since H-L=5 normally), need spread > 7.5
        opens[-1] = 2050.0
        highs[-1] = 2062.0  # spread = 14.0 > 7.5
        lows[-1] = 2048.0
        closes[-1] = 2061.0
        volumes[-1] = 500  # 5x avg of 100 > 2x

        candles = _make_candles(opens, highs, lows, closes, volumes)
        result = mi.detect_displacement(candles, lookback=10)
        assert result is True

    def test_no_displacement_low_volume(self):
        """Normal volume does not trigger displacement."""
        mi = MarketIntelligence()
        n = 30
        opens = [2050.0] * n
        highs = [2053.0] * n
        lows = [2048.0] * n
        closes = [2051.0] * n
        volumes = [100] * n  # All normal volume

        candles = _make_candles(opens, highs, lows, closes, volumes)
        result = mi.detect_displacement(candles, lookback=10)
        assert result is False
