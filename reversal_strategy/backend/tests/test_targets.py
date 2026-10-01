"""Target / stop-loss derivation and the reward:risk it is gated on."""
from types import SimpleNamespace

import pytest

from app.services import targets

CFG = SimpleNamespace(atr_target_mult=2.0, atr_sl_mult=1.5,
                      default_target_pct=4.0, default_sl_pct=2.0)


class TestAutoTarget:
    def test_explicit_points_win_over_everything(self):
        assert targets.auto_target(1000, CFG, atr=10, resistance=1200, target_points=25) == 1025

    def test_resistance_above_entry_is_the_target(self):
        assert targets.auto_target(1000, CFG, atr=10, resistance=1080) == 1080

    def test_resistance_below_entry_is_ignored_for_atr(self):
        assert targets.auto_target(1000, CFG, atr=10, resistance=990) == 1020

    def test_percentage_is_last_resort(self):
        assert targets.auto_target(1000, CFG) == 1040

    def test_resistance_method_without_level_rounds_up(self):
        # No level from Amibroker: next round number above the entry.
        assert targets.auto_target(1234, CFG, method="resistance") == 1300

    def test_round_resistance_on_a_round_level_takes_the_next(self):
        assert targets._round_resistance(1300) == 1400


class TestAutoSL:
    def test_explicit_points(self):
        assert targets.auto_sl(1000, CFG, atr=10, support=950, sl_points=12) == 988

    def test_support_below_entry_is_the_stop(self):
        assert targets.auto_sl(1000, CFG, atr=10, support=960) == 960

    @pytest.mark.parametrize("support", [1000, 1050])
    def test_support_at_or_above_entry_is_never_used(self, support):
        # A stop above the market would fire on the next tick.
        assert targets.auto_sl(1000, CFG, atr=10, support=support) == 985

    def test_percentage_is_last_resort(self):
        assert targets.auto_sl(1000, CFG) == 980


class TestRewardRisk:
    def test_basic(self):
        assert targets.reward_risk(100, 110, 95) == 2.0

    @pytest.mark.parametrize("tgt,sl", [(100, 95), (90, 95), (110, 100), (110, 105.0001 + 5)])
    def test_unmeasurable_is_zero(self, tgt, sl):
        assert targets.reward_risk(100, tgt, sl) == 0.0

    def test_default_percentages_clear_min_rr(self):
        # 4% target vs 2% stop is a 2.0 R:R, so a signal with no S&R and no ATR
        # is still actionable at the default min_rr of 1.5.
        tgt, sl = targets.signal_levels(1000, CFG)
        assert targets.reward_risk(1000, tgt, sl) == 2.0


class TestResolve:
    def test_manual_value_is_kept(self):
        assert targets.resolve_target(1000, CFG, mode="manual", method="atr",
                                      manual_value=1111.111, atr=10, resistance=0) == 1111.11
        assert targets.resolve_sl(1000, CFG, mode="manual", method="atr",
                                  manual_value=944.444, atr=10) == 944.44

    def test_manual_without_value_falls_back_to_auto(self):
        assert targets.resolve_target(1000, CFG, mode="manual", method="atr",
                                      manual_value=0, atr=10, resistance=0) == 1020


class TestRecomputeForPosition:
    def test_manual_levels_survive_averaging(self):
        tgt, sl = targets.recompute_for_position(
            900, CFG, target_method="manual", sl_method="manual", atr=10,
            current_target=1111, current_sl=888)
        assert (tgt, sl) == (1111, 888)

    def test_auto_levels_use_the_entry_atr(self):
        # Without the captured ATR this degraded to the percentage fallback.
        tgt, sl = targets.recompute_for_position(
            900, CFG, target_method="atr", sl_method="atr", atr=10)
        assert (tgt, sl) == (920, 885)

    def test_support_is_kept_after_averaging(self):
        _, sl = targets.recompute_for_position(
            900, CFG, target_method="atr", sl_method="atr", atr=10, support=870)
        assert sl == 870
