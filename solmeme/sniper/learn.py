"""Online logistic learner for the launch sniper.

Starts from the domain prior in solmeme.sniper.model so a fresh session can
score immediately, then nudges weights as labeled outcomes arrive.
"""

from __future__ import annotations

import math

from solmeme.sniper.model import (
    LEARN_FEATURES,
    PRIOR_BIAS,
    PRIOR_MOMENTS,
    PRIOR_STRENGTH,
    PRIOR_WEIGHTS,
    bin_label,
)


def _sigmoid(x: float) -> float:
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


def _append_cap(buf: list, value, cap: int) -> None:
    buf.append(value)
    if len(buf) > cap:
        del buf[:-cap]


class SniperLearner:
    def __init__(self, features: tuple[str, ...] = LEARN_FEATURES) -> None:
        self.features = tuple(features)
        self.weights = {f: float(PRIOR_WEIGHTS.get(f, 0.0)) for f in self.features}
        self.bias = float(PRIOR_BIAS)
        self.stats = {f: {"n": 0, "mean": 0.0, "m2": 0.0} for f in self.features}
        self.bins: dict[tuple[str, str], dict] = {}
        self.n_labels = 0
        self.n_wins = 0
        self.buys = 0
        self.skips = 0
        self.buy_wins = 0
        self.skip_wins = 0
        self.recent_buy_pnl: list[float] = []
        self.recent_buy_wins: list[bool] = []
        self.recent_skip_wins: list[bool] = []

    def _moments(self, feature: str) -> tuple[float, float]:
        """Blended mean, std. Prior has PRIOR_STRENGTH pseudo-observations."""
        st = self.stats[feature]
        p_mean, p_std = PRIOR_MOMENTS.get(feature, (0.0, 1.0))
        n = st["n"]
        if n <= 0:
            return p_mean, max(p_std, 1e-3)
        mean = (n * st["mean"] + PRIOR_STRENGTH * p_mean) / (n + PRIOR_STRENGTH)
        var = st["m2"] / n if n else 0.0
        std = (n * (var ** 0.5) + PRIOR_STRENGTH * p_std) / (n + PRIOR_STRENGTH)
        return mean, max(std, 1e-3)

    def _z(self, feature: str, value: float) -> float:
        mean, std = self._moments(feature)
        z = (value - mean) / std
        if z > 4:
            return 4.0
        if z < -4:
            return -4.0
        return z

    def _welford(self, feature: str, x: float) -> None:
        st = self.stats[feature]
        st["n"] += 1
        delta = x - st["mean"]
        st["mean"] += delta / st["n"]
        st["m2"] += delta * (x - st["mean"])

    def score(self, vector: dict[str, float]) -> float:
        """Probability in (0, 1) that this launch is a runner."""
        logit = self.bias
        for f in self.features:
            if f not in vector or vector[f] is None:
                continue
            try:
                v = float(vector[f])
            except (TypeError, ValueError):
                continue
            logit += self.weights[f] * self._z(f, v)
        return _sigmoid(logit)

    def observe(
        self,
        vector: dict[str, float],
        win: bool,
        *,
        action: str,
        pnl: float | None = None,
    ) -> None:
        """One labeled decision. Updates weights, bin counts, and threshold stats."""
        present: list[tuple[str, float, float]] = []
        for f in self.features:
            if f not in vector or vector[f] is None:
                continue
            try:
                v = float(vector[f])
            except (TypeError, ValueError):
                continue
            z = self._z(f, v)
            self._welford(f, v)
            present.append((f, v, z))

        logit = self.bias
        for f, _v, z in present:
            logit += self.weights[f] * z
        p = _sigmoid(logit)
        g = p - (1.0 if win else 0.0)
        lr = 0.15 / (1.0 + self.n_labels / 200.0)
        l2 = 0.001
        for f, _v, z in present:
            w = self.weights[f]
            self.weights[f] = w - lr * (g * z + l2 * w)
        self.bias -= lr * g

        self.n_labels += 1
        if win:
            self.n_wins += 1

        for f, v, _z in present:
            label = bin_label(f, v)
            if not label:
                continue
            key = (f, label)
            slot = self.bins.get(key)
            if slot is None:
                slot = {"n": 0, "wins": 0}
                self.bins[key] = slot
            slot["n"] += 1
            if win:
                slot["wins"] += 1

        if action in ("buy", "explore"):
            self.buys += 1
            if win:
                self.buy_wins += 1
            _append_cap(self.recent_buy_wins, bool(win), 40)
            if pnl is not None:
                _append_cap(self.recent_buy_pnl, float(pnl), 20)
        elif action == "skip":
            self.skips += 1
            if win:
                self.skip_wins += 1
            _append_cap(self.recent_skip_wins, bool(win), 40)

    def note_pnl(self, action: str, pnl: float) -> None:
        """Record realized PnL that arrived after the path was already labeled."""
        if action not in ("buy", "explore"):
            return
        _append_cap(self.recent_buy_pnl, float(pnl), 20)

    def threshold(self) -> float:
        """Buy probability cutoff in [0.45, 0.85]. Starts at 0.62."""
        base = 0.62
        if len(self.recent_buy_pnl) >= 12:
            m = sum(self.recent_buy_pnl) / len(self.recent_buy_pnl)
            if m < 0:
                base += 0.08
            elif m > 0.02:
                base -= 0.04
        if len(self.recent_skip_wins) >= 20 and len(self.recent_buy_wins) >= 8:
            sw = sum(1.0 if w else 0.0 for w in self.recent_skip_wins) / len(self.recent_skip_wins)
            bw = sum(1.0 if w else 0.0 for w in self.recent_buy_wins) / len(self.recent_buy_wins)
            if sw > bw + 0.10:
                base -= 0.05
        if base < 0.45:
            return 0.45
        if base > 0.85:
            return 0.85
        return base

    def explore_rate(self) -> float:
        return max(0.04, 0.22 * (0.5 ** (self.n_labels / 80.0)))

    def _base_rate(self) -> float:
        if not self.n_labels:
            return 0.0
        return self.n_wins / self.n_labels

    def _bin_rows(self, min_n: int, *, working: bool) -> list[dict]:
        base_rate = self._base_rate()
        rows: list[dict] = []
        for (feature, label), slot in self.bins.items():
            n = int(slot["n"])
            if n < min_n:
                continue
            wins = int(slot["wins"])
            win_rate = wins / n
            lift = win_rate - base_rate
            if working:
                if win_rate < base_rate + 0.05:
                    continue
            elif win_rate > base_rate - 0.05:
                continue
            rows.append(
                {
                    "feature": feature,
                    "bin": label,
                    "n": n,
                    "wins": wins,
                    "win_rate": win_rate,
                    "lift": lift,
                    "base_rate": base_rate,
                }
            )
        if working:
            rows.sort(key=lambda r: (-r["lift"], r["feature"], r["bin"]))
        else:
            rows.sort(key=lambda r: (r["lift"], r["feature"], r["bin"]))
        return rows

    def working(self, min_n: int = 8) -> list[dict]:
        """Bins whose win rate beats the base rate by at least 0.05."""
        return self._bin_rows(min_n, working=True)

    def failing(self, min_n: int = 8) -> list[dict]:
        """Bins whose win rate trails the base rate by at least 0.05."""
        return self._bin_rows(min_n, working=False)

    def summary(self) -> dict:
        return {
            "labels": self.n_labels,
            "base_rate": self._base_rate(),
            "threshold": self.threshold(),
            "explore_rate": self.explore_rate(),
            "buys": self.buys,
            "skips": self.skips,
            "buy_win_rate": (self.buy_wins / self.buys) if self.buys else None,
            "skip_win_rate": (self.skip_wins / self.skips) if self.skips else None,
            "recent_buy_pnl": (
                sum(self.recent_buy_pnl) / len(self.recent_buy_pnl)
                if self.recent_buy_pnl
                else None
            ),
            "weights": {f: self.weights[f] for f in self.features},
            "bias": self.bias,
        }

    def to_dict(self) -> dict:
        return {
            "features": list(self.features),
            "weights": {f: self.weights[f] for f in self.features},
            "bias": self.bias,
            "stats": {
                f: {
                    "n": self.stats[f]["n"],
                    "mean": self.stats[f]["mean"],
                    "m2": self.stats[f]["m2"],
                }
                for f in self.features
            },
            "bins": [
                {"feature": f, "bin": label, "n": slot["n"], "wins": slot["wins"]}
                for (f, label), slot in self.bins.items()
            ],
            "n_labels": self.n_labels,
            "n_wins": self.n_wins,
            "buys": self.buys,
            "skips": self.skips,
            "buy_wins": self.buy_wins,
            "skip_wins": self.skip_wins,
            "recent_buy_pnl": list(self.recent_buy_pnl),
            "recent_buy_wins": list(self.recent_buy_wins),
            "recent_skip_wins": list(self.recent_skip_wins),
        }

    @classmethod
    def from_dict(cls, d: dict) -> SniperLearner:
        raw_features = d.get("features")
        features = tuple(raw_features) if raw_features is not None else LEARN_FEATURES
        obj = cls(features)

        weights = d.get("weights")
        if isinstance(weights, dict):
            for f in obj.features:
                if f in weights and weights[f] is not None:
                    obj.weights[f] = float(weights[f])
        if "bias" in d and d["bias"] is not None:
            obj.bias = float(d["bias"])

        stats = d.get("stats")
        if isinstance(stats, dict):
            for f in obj.features:
                st = stats.get(f)
                if not isinstance(st, dict):
                    continue
                cur = obj.stats[f]
                if "n" in st and st["n"] is not None:
                    cur["n"] = int(st["n"])
                if "mean" in st and st["mean"] is not None:
                    cur["mean"] = float(st["mean"])
                if "m2" in st and st["m2"] is not None:
                    cur["m2"] = float(st["m2"])

        if "bins" in d and d["bins"] is not None:
            obj.bins = {}
            for item in d["bins"]:
                obj.bins[(item["feature"], item["bin"])] = {
                    "n": int(item["n"]),
                    "wins": int(item["wins"]),
                }

        for key in ("n_labels", "n_wins", "buys", "skips", "buy_wins", "skip_wins"):
            if key in d and d[key] is not None:
                setattr(obj, key, int(d[key]))
        if "recent_buy_pnl" in d and d["recent_buy_pnl"] is not None:
            obj.recent_buy_pnl = [float(x) for x in d["recent_buy_pnl"]]
        if "recent_buy_wins" in d and d["recent_buy_wins"] is not None:
            obj.recent_buy_wins = [bool(x) for x in d["recent_buy_wins"]]
        if "recent_skip_wins" in d and d["recent_skip_wins"] is not None:
            obj.recent_skip_wins = [bool(x) for x in d["recent_skip_wins"]]
        return obj
