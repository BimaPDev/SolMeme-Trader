"""Strategy features filled by the fomo.family adapter.

Wallet trades are stored on the token like any other tracked fill (``label == "fomo"``),
so ``kol_buys_600s`` counts them too. These features expose the fomo-only slice.
"""

from solmeme.features.registry import feature


def _rows(st, now: float, *, side: str, window: float | None):
    rows = st.wallet_buys if side == "buy" else st.wallet_sells
    out = []
    for ts, wallet, label in rows:
        if label != "fomo" or ts > now:
            continue
        if window is not None and ts < now - window:
            continue
        out.append(wallet)
    return out


def _seen(st, now: float) -> bool:
    if any(label == "fomo" and ts <= now for ts, _, label in (*st.wallet_buys, *st.wallet_sells)):
        return True
    if "fomo_trending" in st.signals and st.signals["fomo_trending"] <= now:
        return True
    return isinstance(st.ext.get("fomo"), dict)


@feature("fomo_trader_buys", "fomo", "count", "Distinct fomo traders that bought this token")
def _buys(st, now, m):
    if not _seen(st, now):
        return None
    return len(set(_rows(st, now, side="buy", window=None)))


@feature("fomo_trader_buys_600s", "fomo", "count", "Distinct fomo traders that bought in the last 10 min")
def _buys_600(st, now, m):
    if not _seen(st, now):
        return None
    return len(set(_rows(st, now, side="buy", window=600)))


@feature("fomo_trader_sells", "fomo", "count", "Distinct fomo traders that sold this token")
def _sells(st, now, m):
    if not _seen(st, now):
        return None
    return len(set(_rows(st, now, side="sell", window=None)))


@feature("fomo_trending", "fomo", "bool", "Token is (or was) on the fomo Solana trending list")
def _trending(st, now, m):
    ts = st.signals.get("fomo_trending")
    if ts is not None and ts <= now:
        return True
    if _seen(st, now):
        return False
    return None
