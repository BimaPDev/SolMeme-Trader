"""Strategy features filled by the Axiom adapter (``state.ext['axiom']`` / ``state.signals``)."""

from solmeme.features.registry import feature

_SIGNALS = ("axiom_new_pair", "axiom_final_stretch", "axiom_migrated", "axiom_pulse")


def _seen(st, now: float) -> bool:
    return any(name in st.signals and st.signals[name] <= now for name in _SIGNALS)


def _num(st, key: str):
    data = st.ext.get("axiom")
    if not isinstance(data, dict) or data.get(key) is None:
        return None
    try:
        return float(data[key])
    except (TypeError, ValueError):
        return None


@feature("axiom_listed", "axiom", "bool", "Token has been seen on Axiom Pulse")
def _listed(st, now, m):
    if _seen(st, now) or isinstance(st.ext.get("axiom"), dict):
        return True
    return None


@feature("axiom_final_stretch", "axiom", "bool", "Listed in Axiom Pulse 'Final Stretch'")
def _stretch(st, now, m):
    if not _seen(st, now) and not isinstance(st.ext.get("axiom"), dict):
        return None
    ts = st.signals.get("axiom_final_stretch")
    return bool(ts is not None and ts <= now)


@feature("axiom_top10_pct", "axiom", "%", "Axiom top-10 holder share (0-100)")
def _top10(st, now, m):
    return _num(st, "top10_pct")


@feature("axiom_dev_hold_pct", "axiom", "%", "Axiom dev holding share (0-100)")
def _dev(st, now, m):
    return _num(st, "dev_hold_pct")


@feature("axiom_sniper_pct", "axiom", "%", "Axiom sniper holding share (0-100)")
def _sniper(st, now, m):
    return _num(st, "sniper_pct")


@feature("axiom_insider_pct", "axiom", "%", "Axiom insider holding share (0-100)")
def _insider(st, now, m):
    return _num(st, "insider_pct")


@feature("axiom_bundle_pct", "axiom", "%", "Axiom bundled-supply share (0-100)")
def _bundle(st, now, m):
    return _num(st, "bundle_pct")


@feature("axiom_holders", "axiom", "count", "Axiom holder count")
def _holders(st, now, m):
    return _num(st, "holders")


@feature("axiom_pro_traders", "axiom", "count", "Axiom pro-trader / trading-bot user count")
def _pros(st, now, m):
    return _num(st, "pro_traders")
