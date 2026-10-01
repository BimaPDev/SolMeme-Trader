"""Deterministic rug veto. Ported from PaperTrader swarm/orchestra._hard_rug:
reject obvious scams without any LLM call. Used as a locked rule (the AI
can never remove it)."""

from __future__ import annotations

from solmeme.sim import curve


def _f(x):
    try:
        return None if x is None else float(x)
    except (TypeError, ValueError):
        return None


def hard_rug_reasons(st) -> list[str]:
    reasons: list[str] = []
    g = st.gmgn
    sec = st.security
    if g.get("honeypot") in (True, 1, "1", "yes", "true") or sec.get("honeypot"):
        reasons.append("honeypot")
    if g.get("blacklist") in (True, 1, "1", "yes", "true"):
        reasons.append("blacklist")
    rug = _f(g.get("rug_ratio"))
    if rug is not None and rug > 0.5:
        reasons.append(f"rug_ratio={rug:.2f}>0.5")
    top10 = _f(g.get("top10_pct"))
    if top10 is not None and top10 > 80:
        reasons.append(f"gmgn_top10={top10:.0f}%>80")
    dev = _f(g.get("dev_hold_pct"))
    if dev is not None and dev > 30:
        reasons.append(f"dev_hold={dev:.0f}%>30")
    if st.has_trade_stream and st.balances:
        top1 = max(st.balances.values()) / curve.TOTAL_SUPPLY * 100
        if top1 > 70:
            reasons.append(f"top1={top1:.0f}%>70")
    score = _f(sec.get("rugcheck_score_normalised"))
    if score is not None and score > 80:
        reasons.append(f"rugcheck={score:.0f}")
    return reasons
