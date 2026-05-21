"""
Configuration module for XAUUSD Institutional Trading System.

All system constants organized into frozen dataclasses for type safety
and immutability. These values define prop firm rules, session windows,
alpha model parameters, order management, risk limits, and symbol specifics.
"""

from dataclasses import dataclass
from typing import List, Tuple


@dataclass(frozen=True)
class PropFirmConfig:
    """Prop firm challenge/funded account risk parameters."""

    daily_max_loss_pct: float = 4.0
    total_max_drawdown_pct: float = 9.0
    max_risk_per_trade_pct: float = 0.5
    challenge_phase: bool = True


@dataclass(frozen=True)
class SessionConfig:
    """Trading session time windows in UTC (hour, minute) tuples."""

    asian_start: Tuple[int, int] = (0, 0)
    asian_end: Tuple[int, int] = (8, 0)
    london_start: Tuple[int, int] = (8, 0)
    london_end: Tuple[int, int] = (16, 0)
    ny_start: Tuple[int, int] = (13, 0)
    ny_end: Tuple[int, int] = (21, 0)
    broker_rollover_hour: int = 0


@dataclass(frozen=True)
class AlphaConfig:
    """Smart Money Confluence Alpha Model parameters."""

    htf_timeframes: Tuple[str, ...] = ("H1", "H4")
    execution_timeframes: Tuple[str, ...] = ("M1", "M5", "M15")
    atr_period: int = 14
    atr_sweep_multiplier: float = 0.5
    ob_lookback: int = 50
    fvg_min_gap_atr_ratio: float = 0.5


@dataclass(frozen=True)
class OrderConfig:
    """Order execution and trade management parameters."""

    partial_close_rr: float = 1.5
    partial_close_pct: float = 0.5
    final_target_rr_min: float = 4.0
    final_target_rr_max: float = 5.0
    max_deviation_points: int = 20
    retry_max_attempts: int = 5
    retry_base_delay_ms: int = 100


@dataclass(frozen=True)
class RiskConfig:
    """Risk guardian and position sizing parameters."""

    equity_poll_interval_ms: int = 500
    kelly_fraction: float = 0.25
    kelly_cap_pct: float = 0.5
    kelly_cold_start_fraction: float = 0.5
    """Fraction of max_risk_per_trade_pct to use when trade history has fewer
    than 10 samples and Kelly cannot produce a meaningful estimate.
    Default 0.5 means half of max risk (i.e. 0.25% with default prop config)."""


@dataclass(frozen=True)
class DatabaseConfig:
    """Database persistence configuration."""

    db_path: str = "data/trading_state.db"


@dataclass(frozen=True)
class SymbolConfig:
    """XAUUSD symbol-specific configuration."""

    symbol: str = "XAUUSD"
    magic_number: int = 202401
    point_value: float = 0.01
    pip_value: float = 0.10
