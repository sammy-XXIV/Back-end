from flask import Flask, request, jsonify
from flask_cors import CORS
import requests
import os
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger("CIPHER-SERVER")

app = Flask(__name__)
CORS(app, origins="*", allow_headers=["Content-Type"], methods=["GET", "POST", "OPTIONS"])

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

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
            json={'model':'claude-sonnet-4-20250514','max_tokens':1000,'messages':[{'role':'user','content':prompt}]}
        )
        return jsonify(response.json())
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
