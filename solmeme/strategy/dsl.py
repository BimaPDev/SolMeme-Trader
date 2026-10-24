"""Rule evaluation over a FeatureView.

entry passes  <=>  every `all` rule true  AND  (no `any` rules OR one is true)
                   AND  no `none` rule true  AND  (no score OR score >= min_score).
score = the sum of the weights of the `score` terms that hold; a term on missing data adds 0.
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
        if not explain:
            return False, failed
    if e.score and e.min_score is not None:
        s, need = score(spec, fv), float(spec.p(e.min_score))
        if s < need:
            failed.append("score")
    return (not failed), failed


def score(spec: StrategySpec, fv) -> float:
    """Points from the score terms that hold right now."""
    return sum(float(spec.p(t.w)) for t in spec.entry.score if eval_cond(t, fv.get(t.f), spec.p(t.v)))


def score_terms(spec: StrategySpec, fv) -> list[str]:
    """The terms that scored, for the trade's 'why'."""
    return [t.id for t in spec.entry.score if eval_cond(t, fv.get(t.f), spec.p(t.v)) and float(spec.p(t.w)) > 0]


def max_score(spec: StrategySpec) -> float:
    return sum(max(0.0, float(spec.p(t.w))) for t in spec.entry.score)


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
