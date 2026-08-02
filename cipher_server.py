from flask import Flask, request, jsonify
from flask_cors import CORS
import requests
import os
import re
import json
import time
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger("CIPHER-SERVER")

app = Flask(__name__)
CORS(app, origins="*", allow_headers=["Content-Type", "Authorization"], methods=["GET", "POST", "OPTIONS"])

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
NOTIFY_BOT_TOKEN = os.environ.get("NOTIFY_BOT_TOKEN", "")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://zttdlnavawepvhbtldgq.supabase.co")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "sb_publishable_PiRo_l11XVyrqnhmn5NldQ_Ju0ItrBV")
CRYPTORANK_API_KEY = os.environ.get("CRYPTORANK_API_KEY", "")

# In-memory cache for the moving-scan (movement data changes fast but not per-request)
_scan_cache = {"ts": 0, "data": None}
SCAN_TTL = 300  # 5 minutes

# Token-unlock cache — schedules barely change intraday
_unlocks_cache = {"ts": 0, "data": None}
UNLOCKS_TTL = 21600  # 6 hours

@app.after_request
def add_cors(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    return response

@app.route('/analyze', methods=['POST'])
def analyze():
    if not ANTHROPIC_API_KEY:
        return jsonify({'error': 'API key not configured on server'}), 500
    try:
        data = request.get_json()
        prompt = data.get('prompt', '')
        if not prompt:
            return jsonify({'error': 'No prompt provided'}), 400
        response = requests.post(
            'https://api.anthropic.com/v1/messages',
            headers={'Content-Type':'application/json','x-api-key':ANTHROPIC_API_KEY,'anthropic-version':'2023-06-01'},
            json={'model':'claude-opus-5','max_tokens':4000,'messages':[{'role':'user','content':prompt}]},
            timeout=100
        )
        rj = response.json()
        if not response.ok or rj.get('type') == 'error':
            msg = rj.get('error', {}).get('message', f'Anthropic error {response.status_code}')
            return jsonify({'error': msg}), 502
        return jsonify(rj)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/notify', methods=['POST'])
def notify():
    if not NOTIFY_BOT_TOKEN:
        return jsonify({'error': 'Bot token not configured on server'}), 500
    try:
        jwt = request.headers.get('Authorization', '').removeprefix('Bearer ').strip()
        if not jwt:
            return jsonify({'error': 'Not authenticated'}), 401
        sb_headers = {'apikey': SUPABASE_KEY, 'Authorization': f'Bearer {jwt}'}
        user_res = requests.get(f'{SUPABASE_URL}/auth/v1/user', headers=sb_headers, timeout=10)
        if not user_res.ok:
            return jsonify({'error': 'Invalid session'}), 401
        user_id = user_res.json().get('id')
        prof_res = requests.get(
            f'{SUPABASE_URL}/rest/v1/profiles',
            params={'user_id': f'eq.{user_id}', 'select': 'telegram_chat_id,telegram_verified,notification_prefs'},
            headers=sb_headers, timeout=10
        )
        profiles = prof_res.json() if prof_res.ok else []
        prof = profiles[0] if profiles else None
        if not prof or not prof.get('telegram_verified') or not prof.get('telegram_chat_id'):
            return jsonify({'error': 'Telegram not linked'}), 403
        data = request.get_json() or {}
        text = data.get('text', '')
        if not text or len(text) > 4000:
            return jsonify({'error': 'Invalid text'}), 400
        prefs = prof.get('notification_prefs') or {}
        ntype = data.get('type', 'general')
        if prefs.get(ntype) is False:
            return jsonify({'ok': True, 'muted': True})
        tg = requests.post(
            f'https://api.telegram.org/bot{NOTIFY_BOT_TOKEN}/sendMessage',
            json={'chat_id': prof['telegram_chat_id'], 'text': text, 'parse_mode': 'HTML'},
            timeout=10
        )
        if not tg.ok:
            log.error(f"Telegram send failed: {tg.text[:200]}")
            return jsonify({'error': 'Telegram send failed'}), 502
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/candles', methods=['GET'])
def candles():
    symbol   = request.args.get('symbol', 'BTC').upper()
    interval = request.args.get('interval', '1h')
    limit    = int(request.args.get('limit', 80))
    MIN_CANDLES = 50  # need at least 50 for reliable EMA50/RSI/MACD

    bybit_i  = {'5m':'5','15m':'15','1h':'60','4h':'240','1d':'D','1w':'W'}.get(interval,'60')
    okx_i    = {'5m':'5m','15m':'15m','1h':'1H','4h':'4H','1d':'1D','1w':'1W'}.get(interval,'1H')
    mexc_fi  = {'5m':'Min5','15m':'Min15','1h':'Min60','4h':'Hour4','1d':'Day1','1w':'Week1'}.get(interval,'Min60')

    sources = [
        ('BINANCE',   f'https://api.binance.com/api/v3/klines?symbol={symbol}USDT&interval={interval}&limit={limit}', 'binance'),
        ('BYBIT',     f'https://api.bybit.com/v5/market/kline?category=spot&symbol={symbol}USDT&interval={bybit_i}&limit={limit}', 'bybit'),
        ('OKX',       f'https://www.okx.com/api/v5/market/candles?instId={symbol}-USDT&bar={okx_i}&limit={limit}', 'okx'),
        ('MEXC_SPOT', f'https://api.mexc.com/api/v3/klines?symbol={symbol}USDT&interval={interval}&limit={limit}', 'binance'),
        ('MEXC',      f'https://contract.mexc.com/api/v1/contract/kline/{symbol}_USDT?interval={mexc_fi}&limit={limit}', 'mexc'),
    ]

    best = None  # track best result in case none meet minimum

    for name, url, fmt in sources:
        try:
            r = requests.get(url, timeout=8)
            if not r.ok:
                log.warning(f"Candles {name} HTTP {r.status_code} for {symbol}")
                continue
            data = r.json()
            out = []
            if fmt == 'binance' and isinstance(data, list):
                out = [{'o':float(c[1]),'h':float(c[2]),'l':float(c[3]),'c':float(c[4]),'v':float(c[5])} for c in data if float(c[4]) > 0]
            elif fmt == 'bybit':
                lst = data.get('result',{}).get('list',[])
                if lst: out = [{'o':float(c[1]),'h':float(c[2]),'l':float(c[3]),'c':float(c[4]),'v':float(c[5])} for c in reversed(lst) if float(c[4]) > 0]
            elif fmt == 'okx':
                lst = data.get('data',[])
                if lst: out = [{'o':float(c[1]),'h':float(c[2]),'l':float(c[3]),'c':float(c[4]),'v':float(c[5])} for c in reversed(lst) if float(c[4]) > 0]
            elif fmt == 'mexc':
                d = data.get('data',{})
                if d and d.get('time'):
                    out = [{'o':float(d['open'][i]),'h':float(d['high'][i]),'l':float(d['low'][i]),'c':float(d['close'][i]),'v':float(d['vol'][i])} for i in range(len(d['time'])) if float(d['close'][i]) > 0]

            if not out:
                log.warning(f"Candles {name} returned empty for {symbol}")
                continue

            log.info(f"Candles {name} returned {len(out)} candles for {symbol}")

            # Keep best result so far
            if best is None or len(out) > len(best['candles']):
                best = {'source': name, 'candles': out}

            # Return immediately if we have enough candles
            if len(out) >= MIN_CANDLES:
                return jsonify({'source': name, 'candles': out})

        except Exception as e:
            log.warning(f"Candles {name} error for {symbol}: {e}")
            continue

    # Return best available even if below minimum — flag it
    if best:
        candle_count = len(best['candles'])
        log.warning(f"Best candles for {symbol}: {candle_count} from {best['source']} (below MIN_CANDLES={MIN_CANDLES})")
        return jsonify({
            'source': best['source'],
            'candles': best['candles'],
            'warning': f'Only {candle_count} candles available — analysis may be less accurate'
        })

    return jsonify({'error': 'All sources failed', 'source': 'NONE'}), 503

@app.route('/tickers', methods=['GET'])
def tickers():
    all_prices = {}
    sources = [
        ('binance', 'https://api.binance.com/api/v3/ticker/24hr'),
        ('bybit',   'https://api.bybit.com/v5/market/tickers?category=spot'),
        ('okx',     'https://www.okx.com/api/v5/market/tickers?instType=SPOT'),
        ('mexc',    'https://api.mexc.com/api/v3/ticker/24hr'),
    ]
    for name, url in sources:
        try:
            r = requests.get(url, timeout=6)
            if not r.ok: continue
            data = r.json()
            if name == 'binance' and isinstance(data, list):
                for t in data:
                    if t.get('symbol','').endswith('USDT'):
                        sym = t['symbol'].replace('USDT','')
                        if not sym: continue
                        price = float(t.get('lastPrice', 0) or 0)
                        if price <= 0: continue
                        all_prices.setdefault(sym,[]).append({
                            'price': price,
                            'change': float(t.get('priceChangePercent', 0) or 0),
                            'high': float(t.get('highPrice', 0) or 0),
                            'low': float(t.get('lowPrice', 0) or 0),
                        })
            elif name == 'mexc' and isinstance(data, list):
                for t in data:
                    if t.get('symbol','').endswith('USDT'):
                        sym = t['symbol'].replace('USDT','')
                        if not sym: continue
                        price = float(t.get('lastPrice', 0) or 0)
                        open_price = float(t.get('openPrice', 0) or 0)
                        if price <= 0: continue
                        # Calculate change from openPrice — more reliable than priceChangePercent
                        change = ((price - open_price) / open_price * 100) if open_price > 0 else 0
                        all_prices.setdefault(sym,[]).append({
                            'price': price,
                            'change': round(change, 2),
                            'high': float(t.get('highPrice', 0) or 0),
                            'low': float(t.get('lowPrice', 0) or 0),
                        })
            elif name == 'bybit':
                for t in data.get('result',{}).get('list',[]):
                    if t.get('symbol','').endswith('USDT'):
                        sym = t['symbol'].replace('USDT','')
                        price = float(t.get('lastPrice', 0) or 0)
                        if price <= 0: continue
                        all_prices.setdefault(sym,[]).append({
                            'price': price,
                            'change': float(t.get('price24hPcnt', 0) or 0) * 100,
                            'high': float(t.get('highPrice24h', 0) or 0),
                            'low': float(t.get('lowPrice24h', 0) or 0),
                        })
            elif name == 'okx':
                for t in data.get('data',[]):
                    if t.get('instId','').endswith('-USDT'):
                        sym = t['instId'].replace('-USDT','')
                        last = float(t.get('last', 0) or 0)
                        open24 = float(t.get('open24h', 0) or 0)
                        if last <= 0: continue
                        change = ((last - open24) / open24 * 100) if open24 > 0 else 0
                        all_prices.setdefault(sym,[]).append({
                            'price': last,
                            'change': round(change, 2),
                            'high': float(t.get('high24h', 0) or 0),
                            'low': float(t.get('low24h', 0) or 0),
                        })
        except Exception as e:
            log.warning(f"Tickers {name} error: {e}")
            continue

    result = {}
    for sym, ps in all_prices.items():
        if not ps: continue
        avg_price = sum(p['price'] for p in ps) / len(ps)
        if avg_price <= 0: continue
        result[sym] = {
            'price':   round(avg_price, 8),
            'change':  round(sum(p['change'] for p in ps) / len(ps), 2),
            'high':    max(p['high'] for p in ps),
            'low':     min(p['low']  for p in ps),
            'sources': len(ps),
        }
    return jsonify(result)

@app.route('/mexc-scan', methods=['GET'])
def mexc_scan():
    """Fetch all MEXC tickers for the scanner"""
    try:
        r = requests.get('https://api.mexc.com/api/v3/ticker/24hr', timeout=12)
        if r.ok:
            data = r.json()
            if isinstance(data, list) and len(data) > 0:
                result = {}
                for t in data:
                    sym = t.get('symbol','')
                    if not sym.endswith('USDT'): continue
                    sym = sym.replace('USDT','')
                    if not sym: continue
                    price      = float(t.get('lastPrice', 0) or 0)
                    open_price = float(t.get('openPrice', 0) or 0)
                    high       = float(t.get('highPrice', 0) or 0)
                    low        = float(t.get('lowPrice', 0) or 0)
                    vol        = float(t.get('quoteVolume', 0) or 0)
                    if price <= 0: continue

                    # Calculate change from open price (more reliable than priceChangePercent)
                    if open_price > 0:
                        change = ((price - open_price) / open_price) * 100
                    else:
                        change_raw = t.get('priceChangePercent', '0') or '0'
                        change = float(str(change_raw).strip() or 0)

                    result[sym] = {
                        'price': price,
                        'change': round(change, 2),
                        'high': high,
                        'low': low,
                        'volume': vol,
                        'source': 'MEXC'
                    }
                return jsonify(result)
    except Exception as e:
        log.warning(f"MEXC v3 scan error: {e}")

    # Fallback — MEXC v2
    try:
        r = requests.get('https://www.mexc.com/open/api/v2/market/ticker', timeout=12)
        if r.ok:
            data = r.json().get('data', [])
            result = {}
            for t in data:
                sym = t.get('symbol','')
                if not sym.endswith('_USDT'): continue
                sym = sym.replace('_USDT','')
                if not sym: continue
                price  = float(t.get('last', 0) or 0)
                high   = float(t.get('high', 0) or 0)
                low    = float(t.get('low', 0) or 0)
                vol    = float(t.get('volume', 0) or 0)
                if price <= 0: continue
                # Calculate change from high/low midpoint if no change field
                change_raw = t.get('priceChangePercent', '0') or '0'
                change = float(str(change_raw).strip() or 0)
                if change == 0 and low > 0:
                    open_est = (high + low) / 2
                    change = ((price - open_est) / open_est) * 100
                result[sym] = {
                    'price': price,
                    'change': round(change, 2),
                    'high': high,
                    'low': low,
                    'volume': vol,
                    'source': 'MEXC'
                }
            return jsonify(result)
    except Exception as e:
        log.warning(f"MEXC v2 scan error: {e}")

    return jsonify({'error': 'MEXC unavailable'}), 503

def _mexc_perp_bases():
    """Set of base coins that have a USDT-margined perpetual on MEXC."""
    r = requests.get('https://contract.mexc.com/api/v1/contract/detail', timeout=12)
    r.raise_for_status()
    bases = set()
    for c in r.json().get('data', []):
        if c.get('quoteCoin') == 'USDT' and c.get('baseCoin'):
            bases.add(c['baseCoin'].upper())
    return bases

def _grade_from_score(s):
    if s >= 8.5: return 'A+'
    if s >= 7.5: return 'A'
    if s >= 6.5: return 'B+'
    if s >= 5.5: return 'B'
    if s >= 4.5: return 'C+'
    if s >= 3.5: return 'C'
    if s >= 2.5: return 'D'
    return 'F'

def _score_mover(c):
    """Grade a token for MOVER QUALITY (alive + moving), not stability.
    Higher = better live setup. Returns (overall, scores) or None to exclude."""
    price = c['price']
    vol   = c['volume24h']
    ch24  = c['change24h']
    ch7d  = c['change7d']
    ch30d = c['change30d']
    rng   = ((c['high24h'] - c['low24h']) / c['low24h'] * 100) if c['low24h'] > 0 else 0

    # ── HARD FILTERS — kill the corpses ──
    if vol < 1_000_000:                       # untradeable / illiquid
        return None
    if abs(ch24) < 3 and abs(ch7d) < 6 and rng < 6:   # stagnant: flat day, flat week, tight range
        return None
    if price <= 0:
        return None

    # 1. MOMENTUM — magnitude of the 24h move (sweet spot 8-40%, spikes >70% capped)
    a = abs(ch24)
    if   a >= 70: momentum = 5.5
    elif a >= 40: momentum = 8
    elif a >= 20: momentum = 10
    elif a >= 10: momentum = 8.5
    elif a >= 5:  momentum = 6.5
    else:         momentum = 4

    # 2. SUSTAINED — moving on 7d in the SAME direction (not a one-candle spike)
    same_dir = (ch24 >= 0) == (ch7d >= 0)
    if same_dir and abs(ch7d) >= 15: sustained = 10
    elif same_dir and abs(ch7d) >= 6: sustained = 8
    elif same_dir:                    sustained = 6
    elif abs(ch7d) < 4:               sustained = 5   # flat week, today isolated
    else:                             sustained = 3   # 7d fighting today's move

    # 3. VOLUME — log-ish scale, $1M floor already applied
    if   vol >= 100_000_000: volume = 10
    elif vol >= 30_000_000:  volume = 9
    elif vol >= 10_000_000:  volume = 7.5
    elif vol >= 3_000_000:   volume = 6
    else:                    volume = 4.5

    # 4. TREND CONSISTENCY — how many of 24h/7d/30d agree on direction
    signs = [1 if x >= 0 else -1 for x in (ch24, ch7d, ch30d)]
    agree = abs(sum(signs))          # 3 = all agree, 1 = split
    trend = 10 if agree == 3 else 6 if agree == 1 else 3

    weights = {'momentum': 0.35, 'sustained': 0.25, 'volume': 0.25, 'trend': 0.15}
    scores = {'momentum': momentum, 'sustained': sustained, 'volume': volume, 'trend': trend}
    overall = sum(scores[k] * w for k, w in weights.items())
    return overall, scores

def _build_moving_scan():
    """Fetch CryptoRank movers, filter to MEXC perps, grade for mover-quality, sort."""
    if not CRYPTORANK_API_KEY:
        raise RuntimeError('CryptoRank API key not configured on server')

    perp_bases = _mexc_perp_bases()

    # CryptoRank v1 currencies — has the multi-window percentChange we need
    cr = requests.get(
        'https://api.cryptorank.io/v1/currencies',
        params={'api_key': CRYPTORANK_API_KEY, 'limit': 1000},
        timeout=20
    )
    cr.raise_for_status()
    coins = cr.json().get('data', [])

    STABLES = {'USDT','USDC','DAI','TUSD','BUSD','FDUSD','USDD','USDE','PYUSD'}
    out = []
    for coin in coins:
        sym = (coin.get('symbol') or '').upper()
        if not sym or sym in STABLES:
            continue
        if sym not in perp_bases:
            continue
        v = (coin.get('values') or {}).get('USD') or {}
        row = {
            'symbol': sym,
            'name': coin.get('name', sym),
            'price': float(v.get('price') or 0),
            'change24h': float(v.get('percentChange24h') or 0),
            'change7d': float(v.get('percentChange7d') or 0),
            'change30d': float(v.get('percentChange30d') or 0),
            'volume24h': float(v.get('volume24h') or 0),
            'high24h': float(v.get('high24h') or 0),
            'low24h': float(v.get('low24h') or 0),
            'marketCap': float(v.get('marketCap') or 0),
        }
        graded = _score_mover(row)
        if graded is None:
            continue
        overall, scores = graded
        row['gradeScore'] = round(overall, 2)
        row['grade'] = _grade_from_score(overall)
        row['scores'] = {k: round(x, 1) for k, x in scores.items()}
        out.append(row)

    out.sort(key=lambda r: r['gradeScore'], reverse=True)
    return {'updated': int(time.time() * 1000), 'count': len(out), 'tokens': out[:80]}

@app.route('/moving-scan', methods=['GET'])
def moving_scan():
    """Live movers from CryptoRank, filtered to MEXC perps, graded for mover-quality."""
    now = time.time()
    if _scan_cache['data'] and (now - _scan_cache['ts']) < SCAN_TTL:
        return jsonify(_scan_cache['data'])
    try:
        data = _build_moving_scan()
        _scan_cache['data'] = data
        _scan_cache['ts'] = now
        return jsonify(data)
    except Exception as e:
        log.error(f"moving-scan error: {e}")
        if _scan_cache['data']:   # serve stale rather than nothing
            return jsonify(_scan_cache['data'])
        return jsonify({'error': str(e)}), 502

def _build_unlocks():
    """Upcoming token unlocks from CryptoRank's public token-unlock page.
    Only the publicly-visible (non-gated) rows; flags MEXC-perp tradeability."""
    try:
        perp = _mexc_perp_bases()
    except Exception:
        perp = set()

    r = requests.get(
        'https://cryptorank.io/token-unlock',
        headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'},
        timeout=20
    )
    r.raise_for_status()
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.S)
    if not m:
        raise RuntimeError('unlock page structure changed')
    rows = json.loads(m.group(1))['props']['pageProps']['fallbackData']['data']

    out = []
    for row in rows:
        if row.get('isExclusive') or row.get('isHidden'):
            continue  # gated behind "sign up to view" — do not scrape
        sym = (row.get('symbol') or '').upper()
        if not sym:
            continue
        price = float(row.get('price') or 0)
        nu = row.get('nextUnlocks') or []
        tokens_unlocking = sum(float(a.get('tokens') or 0) for a in nu)
        out.append({
            'symbol': sym,
            'name': row.get('name', sym),
            'date': row.get('date'),
            'unlockPct': round(float(row.get('nextUnlockPercent') or 0), 2),
            'unlockUsd': round(tokens_unlocking * price, 2),
            'price': price,
            'change24h': round(float(row.get('chg24h') or 0), 2),
            'marketCap': float(row.get('marketCap') or 0),
            'lockedPct': round(float(row.get('lockedTokensPercent') or 0), 1),
            'image': row.get('image') or '',
            'perp': sym in perp,
        })
    out.sort(key=lambda x: x['date'] or '')  # soonest first
    return {'updated': int(time.time() * 1000), 'count': len(out), 'unlocks': out}

@app.route('/unlocks', methods=['GET'])
def unlocks():
    """Upcoming token unlocks (public CryptoRank data), cached 6h."""
    now = time.time()
    if _unlocks_cache['data'] and (now - _unlocks_cache['ts']) < UNLOCKS_TTL:
        return jsonify(_unlocks_cache['data'])
    try:
        data = _build_unlocks()
        _unlocks_cache['data'] = data
        _unlocks_cache['ts'] = now
        return jsonify(data)
    except Exception as e:
        log.error(f"unlocks error: {e}")
        if _unlocks_cache['data']:
            return jsonify(_unlocks_cache['data'])
        return jsonify({'error': str(e)}), 502

@app.route('/ticker', methods=['GET'])
def ticker():
    """Fetch price + 24H data for any token — tries multiple sources"""
    symbol = request.args.get('symbol', '').upper().replace('USDT','').replace('$','').replace('_','').strip()
    if not symbol:
        return jsonify({'error': 'symbol required'}), 400

    price = change = high = low = vol = 0
    source = ''

    # Try Binance first (most accurate for major tokens)
    try:
        r = requests.get(f"https://api.binance.com/api/v3/ticker/24hr?symbol={symbol}USDT", timeout=6)
        d = r.json()
        if isinstance(d, dict) and float(d.get("lastPrice", 0) or 0) > 0:
            price  = float(d["lastPrice"])
            change = float(d.get("priceChangePercent", 0) or 0)
            high   = float(d.get("highPrice", 0) or 0)
            low    = float(d.get("lowPrice", 0) or 0)
            vol    = float(d.get("quoteVolume", 0) or 0)
            source = 'BINANCE'
    except Exception as e:
        log.warning(f"Binance ticker error: {e}")

    # Try MEXC if Binance didn't have it (MEXC-only tokens)
    if not price:
        try:
            r = requests.get(f"https://api.mexc.com/api/v3/ticker/24hr?symbol={symbol}USDT", timeout=6)
            d = r.json()
            if isinstance(d, dict) and float(d.get("lastPrice", 0) or 0) > 0:
                price      = float(d["lastPrice"])
                high       = float(d.get("highPrice", 0) or 0)
                low        = float(d.get("lowPrice", 0) or 0)
                vol        = float(d.get("quoteVolume", 0) or 0)
                open_price = float(d.get("openPrice", 0) or 0)
                change     = ((price - open_price) / open_price * 100) if open_price > 0 else 0
                source     = 'MEXC'
        except Exception as e:
            log.warning(f"MEXC ticker error: {e}")

    # Bybit fallback
    if not price:
        try:
            r = requests.get(f"https://api.bybit.com/v5/market/tickers?category=spot&symbol={symbol}USDT", timeout=6)
            d = r.json()["result"]["list"][0]
            if float(d.get("lastPrice", 0) or 0) > 0:
                price  = float(d["lastPrice"])
                change = float(d.get("price24hPcnt", 0) or 0) * 100
                high   = float(d["highPrice24h"])
                low    = float(d["lowPrice24h"])
                source = 'BYBIT'
        except Exception as e:
            log.warning(f"Bybit ticker error: {e}")

    # OKX fallback
    if not price:
        try:
            r = requests.get(f"https://www.okx.com/api/v5/market/ticker?instId={symbol}-USDT", timeout=6)
            d = r.json().get("data", [{}])[0]
            if float(d.get("last", 0) or 0) > 0:
                price  = float(d["last"])
                open24 = float(d.get("open24h", 0) or 0)
                change = ((price - open24) / open24 * 100) if open24 > 0 else 0
                high   = float(d.get("high24h", 0) or 0)
                low    = float(d.get("low24h", 0) or 0)
                source = 'OKX'
        except Exception as e:
            log.warning(f"OKX ticker error: {e}")

    if not price:
        return jsonify({'error': f'{symbol} not found on any exchange', 'symbol': symbol}), 404

    return jsonify({
        'symbol': symbol,
        'price': price,
        'change': round(change, 4),
        'high': high,
        'low': low,
        'volume': vol,
        'source': source,
    })

@app.route('/ping', methods=['GET'])
def ping():
    return jsonify({'status': 'CIPHER server online'})

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
