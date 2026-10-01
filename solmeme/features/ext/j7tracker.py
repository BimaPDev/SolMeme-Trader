"""Strategy features filled by the j7tracker adapter.

``state.ext['j7tracker']`` only keeps the latest signal data, so ``tweet_count`` and
``max_followers`` are whatever the source last emitted (it accumulates them).
``j7_secs_since_first_tweet`` comes from the first time a j7 signal was seen.
"""

from solmeme.features.registry import feature

_SIGNALS = ("j7_tweet_ca", "j7_launch")


def _first(st, now: float):
    times = [st.signals[name] for name in _SIGNALS if name in st.signals and st.signals[name] <= now]
    return min(times) if times else None


def _ext(st) -> dict:
    data = st.ext.get("j7tracker")
    return data if isinstance(data, dict) else {}


@feature("j7_tweeted", "social", "bool", "A tracked j7tracker account mentioned this mint, or a launch referenced it")
def _tweeted(st, now, m):
    if _first(st, now) is not None:
        return True
    return None


@feature("j7_tweet_count", "social", "count", "Distinct tweets that mentioned this mint")
def _count(st, now, m):
    if _first(st, now) is None:
        return None
    value = _ext(st).get("tweet_count")
    try:
        return None if value is None else int(value)
    except (TypeError, ValueError):
        return None


@feature("j7_max_followers", "social", "count", "Largest follower count among authors who mentioned this mint")
def _followers(st, now, m):
    if _first(st, now) is None:
        return None
    value = _ext(st).get("max_followers")
    if value is None:
        value = _ext(st).get("followers")
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


@feature("j7_secs_since_first_tweet", "social", "s", "Seconds since the first j7tracker signal for this mint")
def _since(st, now, m):
    first = _first(st, now)
    return None if first is None else now - first
