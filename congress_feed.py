"""Congressional filing feed — capitoltrades.com HTML flight-stream scraper.
Proven approach, hardened: early-stop pagination, 429 backoff, stats, loud failure."""
import json, re, time, urllib.request, urllib.parse, urllib.error
from datetime import datetime, timezone

BASE = "https://www.capitoltrades.com/trades"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}
ASSETS = ["stock", "stock-options", "futures", "reit"]
MAX_PAGES = 15                              # hard cap; early-stop usually ends at 1-3
PUSH_RE = re.compile(r"self\.__next_f\.push\((\[.*?\])\)", re.S)

def build_url(tx_type, page=1):
    p = [("pageSize", "96")]                # no date param: site default window, we filter client-side
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

def http_get(url, max_retries=4):
    for attempt in range(max_retries):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < max_retries - 1:
                wait = 30 * (attempt + 1)     # 30s, 60s, 90s
                print(f"429 rate-limited; waiting {wait}s (retry {attempt+1}/{max_retries-1})")
                time.sleep(wait)
                continue
            raise

def days_old(date_str):
    try:
        d = datetime.fromisoformat(date_str).replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return (datetime.now(timezone.utc) - d).total_seconds() / 86400

def published_within(t, days):
    if not t["pubDate"]:
        return True                           # missing pubDate => fresh/provisional row; keep it
    age = days_old(t["pubDate"])
    return age is not None and age <= days

def scrape(tx_type, window_days, stats):
    """Fetch filings newest-first; stop once a whole page falls outside the window."""
    all_trades, seen = [], set()
    for page in range(1, MAX_PAGES + 1):
        flight = extract_flight(http_get(build_url(tx_type, page)))
        objs = json_objects_at(flight)
        if page == 1 and not objs:
            raise RuntimeError("SCRAPE FAILURE: 0 trade objects on page 1 (blocked or site redesign)")
        trades = []
        for o in objs:
            t = normalize(o)
            if t["txType"] != tx_type or t["txId"] in seen:
                continue
            seen.add(t["txId"])
            trades.append(t)
        all_trades.extend(trades)
        # early stop: entire page older than the window => deeper pages are older still
        dated = [t for t in trades if t["pubDate"]]
        if dated and all(not published_within(t, window_days) for t in dated):
            break
        if page < MAX_PAGES:
            time.sleep(2.0)
    stats[f"{tx_type}s_scanned"] = len(all_trades)
    return all_trades

def fetch_signals(cfg):
    """Returns (buy_signals, sell_tickers, stats)."""
    win = cfg["signal"]["filing_window_days"]
    minv = cfg["signal"]["min_trade_value"]
    excl = set(cfg["universe"]["excluded_tickers"])
    stats = {}
    buys = scrape("buy", win, stats)
    sells = scrape("sell", win, stats)
    seen, signals = set(), []
    reject = {"too_old": 0, "too_small": 0, "excluded": 0, "dupe": 0}
    for t in buys:
        if not published_within(t, win):        reject["too_old"] += 1;   continue
        if (t["value"] or 0) < minv:            reject["too_small"] += 1; continue
        if t["ticker"] in excl:                 reject["excluded"] += 1;  continue
        key = (t["ticker"], t["politician"])
        if key in seen:                         reject["dupe"] += 1;      continue
        seen.add(key)
        signals.append(t)
    signals.sort(key=lambda s: (s["pubDate"], s["value"] or 0), reverse=True)
    stats["signals"] = len(signals)
    stats["rejected"] = reject
    stats["examples"] = [f'{s["politician"]} {s["ticker"]} ~${s["value"]:,.0f} ({s["pubDate"]})'
                         for s in signals[:5]]
    sell_tickers = {t["ticker"] for t in sells if published_within(t, win)}
    return signals, sell_tickers, stats
