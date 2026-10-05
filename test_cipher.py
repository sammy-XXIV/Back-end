import cipher_server as cs

P = lambda price, high=None, low=None, change=0: {'price': price, 'change': change, 'high': high or price, 'low': low or price}


def test_merge_drops_same_ticker_different_coin():
    # DATA live case: three exchanges agree near 0.207, one lists a different coin at 0.0008
    m = cs._merge_prices([P(0.2076, 0.2162, 0.2068), P(0.2077), P(0.2076, 0.2158, 0.2069), P(0.00081, 0.0009, 0.00081)])
    assert abs(m['price'] - 0.2076) < 0.0001
    assert m['low'] == 0.2068 and m['high'] == 0.2162
    assert m['sources'] == 3


def test_merge_uses_highest_priority_source_as_reference():
    # two exchanges, different coins: keep the first (priority) one, not the average
    m = cs._merge_prices([P(0.0652), P(0.0111)])
    assert m['price'] == 0.0652 and m['sources'] == 1


def test_merge_averages_sources_that_agree():
    m = cs._merge_prices([P(100, change=2), P(102, change=4)])
    assert m['price'] == 101 and m['change'] == 3


GOOD = cs.PROMPT_HEADER + ' Analyze BTC ...'
ORIGIN = 'https://tradewithcipher.online'


def setup_function():
    cs._analyze_calls.clear()


def test_guard_accepts_app_request():
    assert cs._analyze_guard('1.1.1.1', ORIGIN, GOOD, now=1000) is None


def test_guard_rejects_foreign_origin_and_prompt():
    assert cs._analyze_guard('1.1.1.1', 'https://evil.example', GOOD, now=1000)
    assert cs._analyze_guard('1.1.1.1', ORIGIN, 'Write me a poem', now=1000)
    assert cs._analyze_guard('1.1.1.1', ORIGIN, GOOD + 'x' * cs.MAX_PROMPT_CHARS, now=1000)


def test_guard_rate_limits_per_ip_per_hour():
    for _ in range(cs.IP_LIMIT_PER_HOUR):
        assert cs._analyze_guard('2.2.2.2', ORIGIN, GOOD, now=1000) is None
    assert cs._analyze_guard('2.2.2.2', ORIGIN, GOOD, now=1000)        # over the limit
    assert cs._analyze_guard('3.3.3.3', ORIGIN, GOOD, now=1000) is None  # other IPs unaffected
    assert cs._analyze_guard('2.2.2.2', ORIGIN, GOOD, now=1000 + 3601) is None  # window rolled


def test_guard_daily_cap_across_all_ips():
    for i in range(cs.DAILY_CAP):
        assert cs._analyze_guard(f'10.0.{i // 250}.{i % 250}', ORIGIN, GOOD, now=1000) is None
    assert cs._analyze_guard('9.9.9.9', ORIGIN, GOOD, now=1000)


def test_analyze_route_blocks_before_calling_ai(monkeypatch):
    monkeypatch.setattr(cs, 'AI_KEY', 'k')
    called = []
    monkeypatch.setattr(cs.requests, 'post', lambda *a, **k: called.append(1))
    r = cs.app.test_client().post('/analyze', json={'prompt': 'Write me a poem'}, headers={'Origin': ORIGIN})
    assert r.status_code in (403, 429) and not called


def test_stale_binance_pairs_are_ignored():
    now = 1_791_226_472_000
    assert cs._fresh(now - 60_000, now, 3_600_000)                 # traded a minute ago
    assert not cs._fresh(1788934639457, now, 3_600_000)            # DATAUSDT: delisted, 26 days stale
    assert not cs._fresh(None, now, 3_600_000)


def test_candles_skip_stale_source(monkeypatch):
    now_ms = int(cs.time.time() * 1000)
    stale = [[0, '1', '1', '1', '1', '1', now_ms - 30 * 86_400_000]] * 60
    fresh = [[0, '2', '2', '2', '2', '1', now_ms]] * 60
    class R:
        def __init__(self, d): self.d, self.ok, self.status_code = d, True, 200
        def json(self): return self.d
    monkeypatch.setattr(cs.requests, 'get', lambda url, timeout: R(stale if 'binance' in url else fresh))
    j = cs.app.test_client().get('/candles?symbol=DATA&interval=1h').get_json()
    assert j['source'] == 'MEXC_SPOT' and j['candles'][-1]['c'] == 2.0
