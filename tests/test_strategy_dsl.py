import pytest

from solmeme.strategy.diff import Change, InvalidChange, apply_changes, change_signature
from solmeme.strategy.spec import StrategySpec


def test_specs_load_and_hash_stably(specs):
    s = specs["curve_momentum"]
    assert s.version == StrategySpec.model_validate_json(s.canonical_json()).version


def test_unknown_feature_rejected(specs):
    with pytest.raises(InvalidChange, match="unknown feature"):
        apply_changes(specs["curve_momentum"], [Change(op="add_rule", section="all", rule_id="x",
                                                       feature="moon_vibes", operator=">", value=1)])


def test_param_bounds_enforced(specs):
    with pytest.raises(InvalidChange, match="outside bounds"):
        apply_changes(specs["curve_momentum"], [Change(op="set_param", param="sl", value=99)])


def test_parameterized_rule_routes_through_bounds(specs):
    with pytest.raises(InvalidChange, match="outside bounds"):
        apply_changes(specs["curve_momentum"], [Change(op="set_rule_value", rule_id="uniq", value=500)])
    new = apply_changes(specs["curve_momentum"], [Change(op="set_rule_value", rule_id="uniq", value=20)])
    assert new.params["min_uniq"].value == 20


def test_locked_rule_cannot_be_removed(specs):
    with pytest.raises(InvalidChange, match="locked"):
        apply_changes(specs["curve_momentum"], [Change(op="remove_rule", rule_id="rug")])


def test_exit_sanity_bounds(specs):
    with pytest.raises(InvalidChange):
        apply_changes(specs["kol_follow"], [Change(op="set_exit", exit_field="max_hold_s", value=5)])
    with pytest.raises(InvalidChange, match="cannot be changed"):
        apply_changes(specs["kol_follow"], [Change(op="set_exit", exit_field="sizing", value=5)])


def test_valid_change_makes_new_version(specs):
    base = specs["curve_momentum"]
    new = apply_changes(base, [Change(op="add_rule", section="none", rule_id="whale", feature="max_buy_60s",
                                      operator=">", value=3.0)])
    assert new.version != base.version
    assert any(c.id == "whale" for c in new.entry.none)


def test_signature_ignores_values():
    a = [Change(op="set_param", param="sl", value=20)]
    b = [Change(op="set_param", param="sl", value=25)]
    assert change_signature(a) == change_signature(b)


def test_too_many_changes(specs):
    ch = [Change(op="set_param", param="sl", value=v) for v in (20, 21, 22, 23)]
    with pytest.raises(InvalidChange, match="too many"):
        apply_changes(specs["curve_momentum"], ch)
