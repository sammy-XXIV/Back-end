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


class FakeResp:
    def __init__(self, data): self.data, self.ok, self.status_code = data, True, 200
    def json(self): return self.data


def _oi_hist(values, price=0.25):
    # Binance openInterestHist rows, oldest first; value / amount = price, used for the same-coin check
    return [{'sumOpenInterestValue': str(v), 'sumOpenInterest': str(v / price)} for v in values]


def test_oi_binance_change_over_24h(monkeypatch):
    rows = _oi_hist([100e6] + [101e6] * 20 + [104e6, 105e6, 106e6, 110e6])  # 25 hourly points
    monkeypatch.setattr(cs.requests, 'get', lambda url, timeout: FakeResp(rows))
    j = cs.app.test_client().get('/oi?symbol=ENA&price=0.25').get_json()
    assert j['source'] == 'BINANCE' and j['oi_usd'] == 110e6
    assert j['change_24h'] == 10.0 and j['change_4h'] == round((110 / 101 - 1) * 100, 2)


def test_oi_rejects_different_coin_and_falls_back_to_mexc(monkeypatch):
    def get(url, timeout):
        if 'binance' in url: return FakeResp(_oi_hist([1e6] * 25, price=0.065))   # Binance "BEAM" is another coin
        if 'detail' in url: return FakeResp({'data': {'contractSize': 10}})
        return FakeResp({'data': {'holdVol': 1_000_000, 'fairPrice': 0.0087}})
    monkeypatch.setattr(cs.requests, 'get', get)
    j = cs.app.test_client().get('/oi?symbol=BEAM&price=0.0087').get_json()
    assert j['source'] == 'MEXC' and j['oi_usd'] == 87000.0 and j['change_24h'] is None


def test_oi_none_found(monkeypatch):
    monkeypatch.setattr(cs.requests, 'get', lambda url, timeout: FakeResp({'code': 400}))
    assert cs.app.test_client().get('/oi?symbol=ZZZQQ').status_code == 404


def test_candles_include_open_time_ms(monkeypatch):
    now_ms = int(cs.time.time() * 1000)
    rows = [[now_ms - (60 - i) * 3_600_000, '1', '2', '0.5', '1.5', '10', now_ms] for i in range(60)]
    monkeypatch.setattr(cs.requests, 'get', lambda url, timeout: FakeResp(rows))
    c = cs.app.test_client().get('/candles?symbol=BTC&interval=1h').get_json()['candles']
    assert c[0]['t'] == rows[0][0] and c[-1]['t'] == rows[-1][0]


def _ratio_rows(key, first, last, n=25):
    rows = [{key: str(first)} for _ in range(n - 1)] + [{key: str(last)}]
    return rows


def test_positioning_binance_ratios(monkeypatch):
    def get(url, timeout):
        if 'premiumIndex' in url: return FakeResp({'markPrice': '0.1627'})
        if 'globalLongShortAccountRatio' in url: return FakeResp([{'longAccount': '0.712'}] * 24 + [{'longAccount': '0.730'}])
        if 'topLongShortPositionRatio' in url: return FakeResp([{'longAccount': '0.592'}] * 24 + [{'longAccount': '0.595'}])
        if 'takerlongshortRatio' in url: return FakeResp(_ratio_rows('buySellRatio', 2.0, 1.0)[:21] + [{'buySellRatio': '1.2'}, {'buySellRatio': '1.4'}, {'buySellRatio': '1.6'}, {'buySellRatio': '1.8'}])
        return FakeResp({})
    monkeypatch.setattr(cs.requests, 'get', get)
    j = cs.app.test_client().get('/positioning?symbol=CYS&price=0.1625').get_json()
    assert j == {'accounts_long_pct': 73.0, 'accounts_long_pct_24h': 71.2, 'top_long_pct': 59.5, 'top_long_pct_24h': 59.2,
                 'taker_ratio_4h': 1.5, 'source': 'BINANCE'}  # mean of the last 4 hourly ratios; one hour alone is noise


def test_positioning_rejects_different_coin(monkeypatch):
    monkeypatch.setattr(cs.requests, 'get', lambda url, timeout: FakeResp({'markPrice': '0.065'} if 'premiumIndex' in url else [{'longAccount': '0.5', 'buySellRatio': '1'}] * 25))
    assert cs.app.test_client().get('/positioning?symbol=BEAM&price=0.0087').status_code == 404


def test_positioning_not_listed(monkeypatch):
    monkeypatch.setattr(cs.requests, 'get', lambda url, timeout: FakeResp({'code': -1121, 'msg': 'Invalid symbol.'}))
    assert cs.app.test_client().get('/positioning?symbol=ZZZQQ').status_code == 404
