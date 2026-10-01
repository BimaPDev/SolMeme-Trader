"""Deterministic launch analyzer for the paper sniper.

A feature snapshot becomes a LaunchCard: hard vetoes, component scores in
roughly [-1, 1], a numeric vector limited to LEARN_FEATURES, and a one-line note.
"""

from __future__ import annotations

from solmeme.sniper.model import LEARN_FEATURES, LaunchCard


def analyze(
    snapshot: dict,
    *,
    mint: str,
    ts: float,
    symbol: str = "",
    price: float | None = None,
) -> LaunchCard:
    vetoes = _vetoes(snapshot, price)
    parts = _parts(snapshot)
    return LaunchCard(
        mint=mint,
        ts=ts,
        symbol=symbol,
        price=price,
        vetoes=vetoes,
        parts=parts,
        vector=_vector(snapshot),
        note=_note(vetoes, symbol, mint, parts["flow"], snapshot),
        snapshot=dict(snapshot),
    )


def _vetoes(snapshot: dict, price: float | None) -> list[str]:
    vetoes: list[str] = []
    if price is None:
        vetoes.append("no_price")
    if snapshot.get("hard_rug") is True:
        vetoes.append("hard_rug")
    if snapshot.get("dev_sold") is True:
        vetoes.append("dev_sold")
    top1 = _as_float(snapshot.get("top1_pct"))
    if top1 is not None and top1 >= 45:
        vetoes.append("top1")
    sniper = _as_float(snapshot.get("sniper_hold_pct"))
    if sniper is not None and sniper >= 40:
        vetoes.append("sniper_bundle")
    bonding = _as_float(snapshot.get("bonding_pct"))
    if bonding is not None and bonding >= 85:
        vetoes.append("too_late")
    if snapshot.get("unique_buyers_30s") is None and snapshot.get("buys_30s") is None:
        vetoes.append("no_flow")
    return vetoes


def _vector(snapshot: dict) -> dict[str, float]:
    vector: dict[str, float] = {}
    for name in LEARN_FEATURES:
        value = snapshot.get(name)
        if isinstance(value, bool) or isinstance(value, (int, float)):
            number = float(value)
            if number == number:
                vector[name] = number
    return vector


def _parts(snapshot: dict) -> dict[str, float]:
    return {
        "flow": _flow(snapshot),
        "pressure": _pressure(snapshot),
        "concentration": _concentration(snapshot),
        "whale": _whale(snapshot),
    }


def _flow(snapshot: dict) -> float:
    buyers = _as_float(snapshot.get("unique_buyers_30s"))
    if buyers is None:
        return 0.0
    score = _clamp((buyers - 4.0) / 20.0, -1.0, 1.0)
    ratio = _as_float(snapshot.get("buy_sell_ratio_30s"))
    if ratio is not None and ratio >= 1.5:
        score += 0.15
    if ratio is not None and ratio < 1.0:
        score -= 0.25
    return _clamp(score, -1.0, 1.0)


def _pressure(snapshot: dict) -> float:
    terms: list[float] = []
    net = _as_float(snapshot.get("net_flow_30s"))
    if net is not None:
        terms.append(_clamp(net / 8.0, -1.0, 1.0))
    chg = _as_float(snapshot.get("price_chg_30s_pct"))
    if chg is not None:
        terms.append(_clamp(chg / 80.0, -1.0, 1.0))
    if not terms:
        return 0.0
    return sum(terms) / len(terms)


def _concentration(snapshot: dict) -> float:
    top10 = _as_float(snapshot.get("top10_pct"))
    sniper = _as_float(snapshot.get("sniper_hold_pct"))
    if top10 is None and sniper is None:
        return 0.0
    score = 0.0
    if top10 is not None:
        score -= _clamp(top10 / 80.0, 0.0, 1.0)
    if sniper is not None:
        score -= _clamp(sniper / 60.0, 0.0, 1.0)
    return _clamp(score, -1.0, 1.0)


def _whale(snapshot: dict) -> float:
    max_buy = _as_float(snapshot.get("max_buy_30s"))
    if max_buy is None:
        return 0.0
    return -_clamp((max_buy - 1.5) / 6.0, 0.0, 1.0)


def _note(
    vetoes: list[str],
    symbol: str,
    mint: str,
    flow: float,
    snapshot: dict,
) -> str:
    if vetoes:
        text = "skip: " + ", ".join(vetoes)
    else:
        label = symbol or mint[:6]
        text = f"{label} flow {flow:+.2f}"
        buyers = _as_float(snapshot.get("unique_buyers_30s"))
        if buyers is not None:
            shown = str(int(buyers)) if buyers.is_integer() else f"{buyers:g}"
            text += f", {shown} buyers in 30s"
    return text[:119]


def _as_float(value: object) -> float | None:
    if isinstance(value, bool) or isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value)
        except ValueError:
            return None
    else:
        return None
    if number != number:
        return None
    return number


def _clamp(value: float, lo: float, hi: float) -> float:
    return float(min(hi, max(lo, value)))
