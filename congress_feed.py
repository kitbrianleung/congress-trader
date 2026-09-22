"""Congressional filing feed. Uses the huit.capitoltrades.com JSON API."""
import json, time, urllib.request, urllib.parse
from datetime import datetime, timezone

API_BASE = "https://huit.capitoltrades.com/api/trades"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json",
}
PAGE_SIZE = 100

def fetch_page(tx_type=None, page=1):
    """Fetch one page of trades from the JSON API."""
    params = {"pageSize": PAGE_SIZE, "page": page}
    if tx_type:
        params["txType"] = tx_type
    
    url = f"{API_BASE}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers=HEADERS)
    
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 429:
            wait = 2 ** (page % 3) * 5  # 5s, 10s, 20s backoff
            print(f"Rate limited, waiting {wait}s...")
            time.sleep(wait)
            return fetch_page(tx_type, page)  # retry
        raise

def fetch_signals(cfg):
    """Returns (buy_signals, sell_tickers). Filtered per strategy config."""
    win = cfg["signal"]["filing_window_days"]
    minv = cfg["signal"]["min_trade_value"]
    excl = set(cfg["universe"]["excluded_tickers"])
    
    signals = []
    sell_tickers = set()
    
    # Fetch buys
    for page in range(1, 4):  # Fetch 3 pages = ~300 recent trades
        try:
            data = fetch_page("buy", page)
            trades = data.get("data", [])
            if not trades:
                break
            
            for t in trades:
                # Parse the API response format
                ticker = t.get("ticker", "").split(":")[0]
                if not ticker or ticker in excl:
                    continue
                
                value = t.get("value", 0)
                if value < minv:
                    continue
                
                pub_date = t.get("pubDate", "")[:10]
                if not pub_date:
                    continue
                
                # Check if within filing window
                try:
                    pub_dt = datetime.fromisoformat(pub_date).replace(tzinfo=timezone.utc)
                    age_days = (datetime.now(timezone.utc) - pub_dt).total_seconds() / 86400
                    if age_days > win:
                        continue
                except:
                    continue
                
                politician = t.get("politician", {}).get("name", "Unknown")
                signals.append({
                    "ticker": ticker,
                    "politician": politician,
                    "value": value,
                    "pubDate": pub_date,
                    "txDate": t.get("txDate", "")[:10]
                })
            
            time.sleep(1.0)  # Be polite to the API
            
        except Exception as e:
            print(f"Error fetching buy page {page}: {e}")
            break
    
    # Fetch sells (for exit signals)
    for page in range(1, 3):  # Just 2 pages for sells
        try:
            data = fetch_page("sell", page)
            trades = data.get("data", [])
            if not trades:
                break
            
            for t in trades:
                ticker = t.get("ticker", "").split(":")[0]
                if ticker:
                    sell_tickers.add(ticker)
            
            time.sleep(1.0)
            
        except Exception as e:
            print(f"Error fetching sell page {page}: {e}")
            break
    
    # Deduplicate and sort
    seen = set()
    unique_signals = []
    for s in signals:
        key = (s["ticker"], s["politician"])
        if key not in seen:
            seen.add(key)
            unique_signals.append(s)
    
    unique_signals.sort(key=lambda x: (x["pubDate"], x["value"]), reverse=True)
    return unique_signals, sell_tickers
