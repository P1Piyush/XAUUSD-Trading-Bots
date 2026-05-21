"""Tests for src/config.py - verify config dataclass defaults and immutability."""

import dataclasses

import pytest

from src.config import (
    AlphaConfig,
    DatabaseConfig,
    OrderConfig,
    PropFirmConfig,
    RiskConfig,
    SessionConfig,
    SymbolConfig,
)


class TestPropFirmConfig:
    """Verify PropFirmConfig defaults match the spec."""

    def test_prop_firm_defaults(self):
        cfg = PropFirmConfig()
        assert cfg.daily_max_loss_pct == 4.0
        assert cfg.total_max_drawdown_pct == 9.0
        assert cfg.max_risk_per_trade_pct == 0.5

    def test_prop_firm_challenge_phase_default(self):
        cfg = PropFirmConfig()
        assert cfg.challenge_phase is True


class TestFrozenDataclasses:
    """Verify all config dataclasses are frozen (immutable)."""

    @pytest.mark.parametrize(
        "cls",
        [
            PropFirmConfig,
            SessionConfig,
            AlphaConfig,
            OrderConfig,
            RiskConfig,
            DatabaseConfig,
            SymbolConfig,
        ],
    )
    def test_config_is_frozen(self, cls):
        instance = cls()
        # Attempt to mutate any field should raise FrozenInstanceError
        fields = dataclasses.fields(instance)
        assert len(fields) > 0, f"{cls.__name__} has no fields"
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(instance, fields[0].name, "INVALID")


class TestSessionConfig:
    """Verify SessionConfig time tuples are valid."""

    def test_session_times_are_valid_tuples(self):
        cfg = SessionConfig()
        for attr_name in ("asian_start", "asian_end", "london_start", "london_end", "ny_start", "ny_end"):
            val = getattr(cfg, attr_name)
            assert isinstance(val, tuple), f"{attr_name} should be a tuple"
            assert len(val) == 2, f"{attr_name} should have 2 elements"
            hour, minute = val
            assert 0 <= hour <= 23, f"{attr_name} hour out of range"
            assert 0 <= minute <= 59, f"{attr_name} minute out of range"

    def test_asian_session_before_london(self):
        cfg = SessionConfig()
        asian_end_mins = cfg.asian_end[0] * 60 + cfg.asian_end[1]
        london_start_mins = cfg.london_start[0] * 60 + cfg.london_start[1]
        assert asian_end_mins <= london_start_mins

    def test_ny_overlap_with_london(self):
        cfg = SessionConfig()
        ny_start_mins = cfg.ny_start[0] * 60 + cfg.ny_start[1]
        london_end_mins = cfg.london_end[0] * 60 + cfg.london_end[1]
        # NY should start before London ends (overlap)
        assert ny_start_mins < london_end_mins
