"""Rule evaluation over a FeatureView.

entry passes  <=>  every `all` rule true  AND  (no `any` rules OR one is true)
                   AND  no `none` rule true.
Unknown feature values (None) make a rule false — so `all`/`any` rules fail
closed and `none` rules don't veto on missing data (except locked safety
rules, whose features are designed to be always defined).
"""

from __future__ import annotations

from solmeme.strategy.spec import Cond, StrategySpec


def eval_cond(c: Cond, value, target) -> bool:
    if value is None:
        return False
    op = c.op
    try:
        if op == ">":
            return value > target
        if op == ">=":
            return value >= target
        if op == "<":
            return value < target
        if op == "<=":
            return value <= target
        if op == "==":
            return value == target
        if op == "!=":
            return value != target
        if op == "between":
            return target[0] <= value <= target[1]
        if op == "in":
            return value in target
    except TypeError:
        return False
    return False


def evaluate(spec: StrategySpec, fv, explain: bool = False) -> tuple[bool, list[str]]:
    """Returns (passed, failed_rule_ids). With explain=False, stops at the first failure."""
    failed: list[str] = []
    e = spec.entry
    for c in e.all:
        if not eval_cond(c, fv.get(c.f), spec.p(c.v)):
            failed.append(c.id)
            if not explain:
                return False, failed
    for c in e.none:
        if eval_cond(c, fv.get(c.f), spec.p(c.v)):
            failed.append(c.id)
            if not explain:
                return False, failed
    if e.any and not any(eval_cond(c, fv.get(c.f), spec.p(c.v)) for c in e.any):
        failed.append("any:" + "|".join(c.id for c in e.any))
    return (not failed), failed


def in_universe(spec: StrategySpec, st, now: float) -> bool:
    u = spec.universe
    if u.pools is not None and st.pool not in u.pools:
        return False
    if u.stage == "curve" and st.migrated:
        return False
    if u.stage == "migrated" and not st.migrated:
        return False
    age = now - st.age_ref_ts
    if age < u.min_age_s:
        return False
    if u.max_age_s is not None and age > u.max_age_s:
        return False
    return st.price_sol is not None
