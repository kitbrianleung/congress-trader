"""Congressional filing feed. Vendored from the proven openclaw scraper."""
import json, re, time, urllib.request, urllib.parse
from datetime import datetime, timezone

BASE = "https://www.capitoltrades.com/trades"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}
ASSETS = ["stock", "stock-options", "futures", "reit"]
PAGE_CAP = 60
PUSH_RE = re.compile(r"self\.__next_f\.push\((\[.*?\])\)", re.S)

def build_url(tx_type, page=1):
    p = [("pageSize", "96")]
    p += [("assetType", a) for a in ASSETS]
    if tx_type:
        p.append(("txType", tx_type))
    if page > 1:
        p.append(("page", str(page)))
    return BASE + "?" + urllib.parse.urlencode(p)

def extract_flight(html):
    parts = []
    for m in PUSH_RE.finditer(html):
        try:
            c = json.loads(m.group(1))
            if isinstance(c, list) and len(c) > 1 and isinstance(c[1], str):
                parts.append(c[1])
        except Exception:
            pass
    return "".join(parts)

def total_pages(flight):
    m = re.search(r'"totalPages":(\d+)', flight)
    return int(m.group(1)) if m else None

def json_objects_at(buf, needle='{"_issuerId":'):
    out, i = [], 0
    while True:
        j = buf.find(needle, i)
        if j == -1:
            break
        depth, in_str, esc, end = 0, False, False, -1
        for k in range(j, len(buf)):
            c = buf[k]
            if in_str:
                if esc: esc = False
                elif c == "\\": esc = True
                elif c == '"': in_str = False
                continue
            if c == '"': in_str = True
            elif c == "{": depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    end = k + 1
                    break
        if end == -1:
            break
        try:
            obj = json.loads(buf[j:end])
            if isinstance(obj, dict) and obj.get("_txId"):
                out.append(obj)
            i = end
        except Exception:
            i = j + len(needle)
    return out

def normalize(o):
    pol, iss = o.get("politician") or {}, o.get("issuer") or {}
    name = ((pol.get("nickname") or pol.get("firstName") or "") + " " + (pol.get("lastName") or "")).strip() or "Unknown"
    v = o.get("value")
    return {
        "politician": name,
        "ticker": (iss.get("issuerTicker") or "N/A").split(":")[0],
        "txDate": (o.get("txDate") or "")[:10],
        "pubDate": (o.get("pubDate") or "")[:10],
        "txType": (o.get("txType") or "").lower(),
        "value": v if isinstance(v, (int, float)) else None,
        "txId": o.get("_txId"),
    }

def http_get(url, max_retries=3):
    """Fetch with retry and backoff for rate limits."""
    for attempt in range(max_retries):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = 2 ** attempt * 5  # 5s, 10s, 20s
                print(f"Rate limited (429), waiting {wait}s before retry {attempt+1}/{max_retries}")
                time.sleep(wait)
                continue
            raise
    raise Exception(f"Failed to fetch {url} after {max_retries} attempts")

def scrape(tx_type):
    page, total, out, seen = 1, None, [], set()
    while True:
        flight = extract_flight(http_get(build_url(tx_type, page)))
        if total is None:
            total = total_pages(flight)
        trades = [t for t in (normalize(o) for o in json_objects_at(flight))
                  if t["txType"] == tx_type and t["txId"] not in seen and not seen.add(t["txId"])]
        if not trades:
            break
        out.extend(trades)
        if (total and page >= total) or page >= PAGE_CAP:
            break
        page += 1
        time.sleep(2.0)
    return out

def days_old(date_str):
    try:
        d = datetime.fromisoformat(date_str).replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return (datetime.now(timezone.utc) - d).total_seconds() / 86400

def published_within(t, days):
    if not t["pubDate"]:
        return True
    age = days_old(t["pubDate"])
    return age is not None and age <= days

def fetch_signals(cfg):
    """Returns (buy_signals, sell_tickers). Filtered per strategy config."""
    buys, sells = scrape("buy"), scrape("sell")
    win = cfg["signal"]["filing_window_days"]
    minv = cfg["signal"]["min_trade_value"]
    excl = set(cfg["universe"]["excluded_tickers"])
    seen, signals = set(), []
    for t in buys:
        if not published_within(t, win) or (t["value"] or 0) < minv or t["ticker"] in excl:
            continue
        key = (t["ticker"], t["politician"])
        if key in seen:
            continue
        seen.add(key)
        signals.append(t)
    signals.sort(key=lambda s: (s["pubDate"], s["value"] or 0), reverse=True)
    sell_tickers = {t["ticker"] for t in sells if published_within(t, win)}
    return signals, sell_tickers
