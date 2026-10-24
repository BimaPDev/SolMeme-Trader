

def test_every_gmgn_client_shares_one_rate_limit(settings):
    from solmeme.sources.gmgn import Gmgn

    class Ctx:
        pass
    a, b = Ctx(), Ctx()
    a.settings = b.settings = settings
    assert Gmgn(a).bucket is Gmgn(b).bucket            # the boards scan's client waits behind the feeds


def test_the_shared_rate_backs_off_on_429_and_recovers(settings):
    from solmeme.sources.gmgn import Gmgn

    class Ctx:
        pass
    c = Ctx(); c.settings = settings
    g = Gmgn(c)
    full = settings.sources.gmgn.rate_per_s
    g.bucket.rate = full
    g._adapt(429); g._adapt(429)
    assert g.bucket.rate == full * 0.7 * 0.7
    for _ in range(200):
        g._adapt(200)
    assert g.bucket.rate == full                         # never above the configured rate
