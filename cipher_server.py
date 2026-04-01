from flask import Flask, request, jsonify
from flask_cors import CORS
import requests
import os

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

    bybit_i  = {'1h':'60','4h':'240','1d':'D','1w':'W'}.get(interval,'60')
    okx_i    = {'1h':'1H','4h':'4H','1d':'1D','1w':'1W'}.get(interval,'1H')
    mexc_fi  = {'1h':'Min60','4h':'Hour4','1d':'Day1','1w':'Week1'}.get(interval,'Min60')

    sources = [
        ('BINANCE',    f'https://api.binance.com/api/v3/klines?symbol={symbol}USDT&interval={interval}&limit={limit}', 'binance'),
        ('BYBIT',      f'https://api.bybit.com/v5/market/kline?category=spot&symbol={symbol}USDT&interval={bybit_i}&limit={limit}', 'bybit'),
        ('OKX',        f'https://www.okx.com/api/v5/market/candles?instId={symbol}-USDT&bar={okx_i}&limit={limit}', 'okx'),
        ('MEXC_SPOT',  f'https://api.mexc.com/api/v3/klines?symbol={symbol}USDT&interval={interval}&limit={limit}', 'binance'),
        ('MEXC',       f'https://contract.mexc.com/api/v1/contract/kline/{symbol}_USDT?interval={mexc_fi}&limit={limit}', 'mexc'),
    ]

    for name, url, fmt in sources:
        try:
            r = requests.get(url, timeout=8)
            if not r.ok: continue
            data = r.json()
            out = []
            if fmt == 'binance' and isinstance(data, list) and len(data) > 5:
                out = [{'o':float(c[1]),'h':float(c[2]),'l':float(c[3]),'c':float(c[4]),'v':float(c[5])} for c in data]
            elif fmt == 'bybit':
                lst = data.get('result',{}).get('list',[])
                if lst: out = [{'o':float(c[1]),'h':float(c[2]),'l':float(c[3]),'c':float(c[4]),'v':float(c[5])} for c in reversed(lst)]
            elif fmt == 'okx':
                lst = data.get('data',[])
                if lst: out = [{'o':float(c[1]),'h':float(c[2]),'l':float(c[3]),'c':float(c[4]),'v':float(c[5])} for c in reversed(lst)]
            elif fmt == 'mexc':
                d = data.get('data',{})
                if d and d.get('time'):
                    out = [{'o':float(d['open'][i]),'h':float(d['high'][i]),'l':float(d['low'][i]),'c':float(d['close'][i]),'v':float(d['vol'][i])} for i in range(len(d['time']))]
            if out:
                return jsonify({'source': name, 'candles': out})
        except:
            continue

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
            r = requests.get(url, timeout=10)
            if not r.ok: continue
            data = r.json()
            if name in ('binance', 'mexc') and isinstance(data, list):
                for t in data:
                    if t.get('symbol','').endswith('USDT'):
                        sym = t['symbol'].replace('USDT','')
                        if not sym: continue
                        all_prices.setdefault(sym,[]).append({
                            'price':float(t.get('lastPrice',0)),
                            'change':float(t.get('priceChangePercent',0)),
                            'high':float(t.get('highPrice',0)),
                            'low':float(t.get('lowPrice',0)),
                            'source': name
                        })
            elif name == 'bybit':
                for t in data.get('result',{}).get('list',[]):
                    if t.get('symbol','').endswith('USDT'):
                        sym = t['symbol'].replace('USDT','')
                        all_prices.setdefault(sym,[]).append({'price':float(t['lastPrice']),'change':float(t.get('price24hPcnt',0))*100,'high':float(t['highPrice24h']),'low':float(t['lowPrice24h']),'source':'bybit'})
            elif name == 'okx':
                for t in data.get('data',[]):
                    if t.get('instId','').endswith('-USDT'):
                        sym = t['instId'].replace('-USDT','')
                        o = float(t.get('open24h',1) or 1); l = float(t.get('last',0))
                        all_prices.setdefault(sym,[]).append({'price':l,'change':((l-o)/o)*100,'high':float(t['high24h']),'low':float(t['low24h']),'source':'okx'})
        except: continue

    result = {
        sym: {
            'price':  sum(p['price']  for p in ps) / len(ps),
            'change': sum(p['change'] for p in ps) / len(ps),
            'high':   max(p['high']   for p in ps),
            'low':    min(p['low']    for p in ps),
            'sources': len(ps)
        }
        for sym, ps in all_prices.items() if ps and sum(p['price'] for p in ps)/len(ps) > 0
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

    # Try MEXC v3 API (more reliable than v2)
    try:
        r = requests.get(f"https://api.mexc.com/api/v3/ticker/24hr?symbol={symbol}USDT", timeout=8)
        d = r.json()
        if isinstance(d, dict) and float(d.get("lastPrice", 0)) > 0:
            price  = float(d["lastPrice"])
            change = float(d["priceChangePercent"])
            high   = float(d["highPrice"])
            low    = float(d["lowPrice"])
            vol    = float(d.get("quoteVolume", 0))
            source = 'MEXC'
    except Exception as e:
        log.warning(f"MEXC v3 error: {e}")

    # Try MEXC v2 API
    if not price:
        try:
            r = requests.get(f"https://www.mexc.com/open/api/v2/market/ticker?symbol={symbol}_USDT", timeout=8)
            d = r.json().get("data", [])
            if isinstance(d, list) and d: d = d[0]
            if isinstance(d, dict) and float(d.get("last", 0)) > 0:
                price  = float(d["last"])
                change = float(d.get("priceChangePercent", 0))
                high   = float(d.get("high", 0))
                low    = float(d.get("low", 0))
                vol    = float(d.get("volume", 0))
                source = 'MEXC'
        except Exception as e:
            log.warning(f"MEXC v2 error: {e}")

    # Fallback Binance
    if not price:
        try:
            r = requests.get(f"https://api.binance.com/api/v3/ticker/24hr?symbol={symbol}USDT", timeout=8)
            d = r.json()
            if isinstance(d, dict) and float(d.get("lastPrice", 0)) > 0:
                price  = float(d["lastPrice"])
                change = float(d["priceChangePercent"])
                high   = float(d["highPrice"])
                low    = float(d["lowPrice"])
                vol    = float(d.get("quoteVolume", 0))
                source = 'BINANCE'
        except Exception as e:
            log.warning(f"Binance error: {e}")

    # Fallback Bybit
    if not price:
        try:
            r = requests.get(f"https://api.bybit.com/v5/market/tickers?category=spot&symbol={symbol}USDT", timeout=8)
            d = r.json()["result"]["list"][0]
            if float(d.get("lastPrice", 0)) > 0:
                price  = float(d["lastPrice"])
                change = float(d.get("price24hPcnt", 0)) * 100
                high   = float(d["highPrice24h"])
                low    = float(d["lowPrice24h"])
                source = 'BYBIT'
        except Exception as e:
            log.warning(f"Bybit error: {e}")

    # Fallback OKX
    if not price:
        try:
            r = requests.get(f"https://www.okx.com/api/v5/market/ticker?instId={symbol}-USDT", timeout=8)
            d = r.json().get("data", [{}])[0]
            if float(d.get("last", 0)) > 0:
                price  = float(d["last"])
                open24 = float(d.get("open24h", 1) or 1)
                change = ((price - open24) / open24) * 100
                high   = float(d["high24h"])
                low    = float(d["low24h"])
                source = 'OKX'
        except Exception as e:
            log.warning(f"OKX error: {e}")

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

