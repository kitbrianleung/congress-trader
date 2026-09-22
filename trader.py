"""Aggressive congressional-copy trader. GitHub Actions edition.
State: state.json via Actions cache; degrades to broker-rebuild on cache miss."""
import json, os, sys, time, urllib.request
from datetime import datetime, timezone
import yaml
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestQuoteRequest
from alpaca.data.timeframe import TimeFrame
from congress_feed import fetch_signals

CFG = yaml.safe_load(open("strategy.yaml"))
tc = TradingClient(os.environ["ALPACA_KEY_ID"], os.environ["ALPACA_SECRET_KEY"],
                   paper=(CFG["mode"] == "paper"))
dc = StockHistoricalDataClient(os.environ["ALPACA_KEY_ID"], os.environ["ALPACA_SECRET_KEY"])
TOKEN, CHANNEL = os.environ["DISCORD_BOT_TOKEN"], os.environ["DISCORD_CHANNEL_ID"]
STATE_FILE = "state.json"

# ============ TEMPORARY DIAGNOSTICS (delete after tests pass) ============
# A: token structure check — prints NO secret characters, only shape
_t = os.environ.get("DISCORD_BOT_TOKEN", "")
print(f"token len={len(_t)} dots={_t.count('.')} "
      f"alnum_ok={all(c.isalnum() or c in '._-' for c in _t)} "
      f"starts_MT={_t.startswith('MT')}")

# B: does Discord accept this token? (with proper User-Agent header)
_req = urllib.request.Request(
    "https://discord.com/api/v10/users/@me",
    headers={"Authorization": f"Bot {_t}",
             "User-Agent": "DiscordBot (https://github.com/congress-trader, 1.0.0)"})
try:
    _me = json.loads(urllib.request.urlopen(_req, timeout=30).read())
    print("bot identity OK:", _me.get("username"), "id:", _me.get("id"))
except urllib.error.HTTPError as e:
    print("discord @me failed:", e.code, e.read().decode()[:200])
    raise SystemExit(1)
# ====================== END DIAGNOSTICS ===================================

def say(msg):
    for i in range(0, len(msg), 1900):
        payload = json.dumps({"content": msg[i:i+1900]}).encode()
        req = urllib.request.Request(
            f"https://discord.com/api/v10/channels/{CHANNEL}/messages",
            data=payload, method="POST",
            headers={"Authorization": f"Bot {TOKEN}", "Content-Type": "application/json",
                     "User-Agent": "DiscordBot (https://github.com/congress-trader, 1.0.0)"})
        try:
            resp = urllib.request.urlopen(req, timeout=30)
            print(f"discord posted: {resp.status}")  # NEW: log success
        except urllib.error.HTTPError as e:
            body = e.read().decode()[:500]
            print(f"discord send failed: HTTP {e.code} {body}", file=sys.stderr)
            print(f"  channel={CHANNEL} token_prefix={TOKEN[:10]}...", file=sys.stderr)
            raise  # NEW: crash so you see the error instead of silent skip
        except Exception as e:
            print(f"discord send failed: {e}", file=sys.stderr)
            raise
        time.sleep(0.4)

def load_state():
    if os.path.exists(STATE_FILE):
        return json.load(open(STATE_FILE))
    return {"positions": {}, "halted": False, "last_run": ""}

def save_state(st):
    json.dump(st, open(STATE_FILE, "w"), indent=1)

def poll_commands():
    """Only responds to messages that are EXACTLY 'HALT', 'RESUME', or 'STATUS'
    (nothing else in the message). Ignores all other channel chatter."""
    req = urllib.request.Request(
        f"https://discord.com/api/v10/channels/{CHANNEL}/messages?limit=50",
        headers={"Authorization": f"Bot {TOKEN}",
                 "User-Agent": "DiscordBot (https://github.com/congress-trader, 1.0.0)"})
    try:
        msgs = json.loads(urllib.request.urlopen(req, timeout=30).read())
    except Exception:
        return None
    for m in msgs:  # newest first
        if m.get("author", {}).get("bot"):
            continue                       # ignore bot messages (e.g., its own STATUS posts)
        content = (m.get("content") or "").strip()
        if content in ("HALT", "RESUME", "STATUS"):   # exact match only, not substring
            return content
    return None

def latest_price(ticker):
    try:
        q = dc.get_stock_latest_quote(StockLatestQuoteRequest(symbol_or_symbols=ticker))[ticker]
        px = float(q.ask_price or q.bid_price or 0)
        if px > 0:
            return px
    except Exception:
        pass
    # pre-market fallback: yesterday's close
    bar = dc.get_stock_bars(StockBarsRequest(
        symbol_or_symbols=[ticker], timeframe=TimeFrame.Day, limit=2))[ticker][-1]
    return float(bar.close)

def passes_liquidity(ticker):
    try:
        bar = dc.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=[ticker], timeframe=TimeFrame.Day, limit=2))[ticker][-1]
        return (bar.close >= CFG["universe"]["min_price"]
                and bar.close * bar.volume >= CFG["universe"]["min_dollar_volume"]), bar.close
    except Exception:
        return False, None

def submit(side, ticker, qty, tag):
    order = MarketOrderRequest(
        symbol=ticker, qty=round(qty, 4),
        side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
        time_in_force=TimeInForce.DAY,
        client_order_id=f"{tag}-{ticker}-{datetime.now(timezone.utc):%Y%m%d}")
    tc.submit_order(order)

def run():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    st = load_state()

    cmd = poll_commands()
    if cmd == "HALT":
        st["halted"] = True
    elif cmd == "RESUME":
        st["halted"] = False
    save_state(st)
    if st["halted"]:
        say(f"⛔ **{today}** — HALTED. No orders. Send `RESUME` to re-enable.")
        return
    if cmd == "STATUS":
        a = tc.get_account()
        say(f"ℹ️ **{today}** — mode={CFG['mode']} equity=${float(a.equity):,.2f} "
            f"positions={len(tc.get_all_positions())}")
        return

    clock = tc.get_clock()
    if not clock.is_open and (clock.next_open - clock.timestamp).total_seconds() > 26 * 3600:
        # true skip (weekend/holiday): do NOT stamp last_run, so a later
        # same-day manual run can still execute fully
        say(f"ℹ️ **{today}** — market closed today; skipping.")
        return
    if st.get("last_run") == today:
        say(f"ℹ️ **{today}** — already ran today; skipping.")
        return
    st["last_run"] = today          # stamp BEFORE trading block, only on real runs
    save_state(st)

    broker_pos = {p.symbol: p for p in tc.get_all_positions()}
    for sym in broker_pos:
        st["positions"].setdefault(sym, {
            "politician": "?", "entry_date": today,
            "entry_price": float(broker_pos[sym].avg_entry_price),
            "peak": float(broker_pos[sym].current_price or broker_pos[sym].avg_entry_price)})

    equity = float(tc.get_account().equity)
    report = [f"📋 **Congressional Trader — {today}** (mode={CFG['mode']}, aggressive)",
              f"Equity ${equity:,.2f} | positions {len(broker_pos)}/{CFG['account']['max_positions']}"]

    signals, sell_tickers = fetch_signals(CFG)
    for sym, info in list(st["positions"].items()):
        if sym not in broker_pos:
            del st["positions"][sym]
            continue
        try:
            cur = latest_price(sym)
        except Exception:
            report.append(f"⚠️ {sym}: quote failed — holding, retry tomorrow")
            continue
        info["peak"] = max(info["peak"], cur)
        held = (datetime.now(timezone.utc).date() -
                datetime.fromisoformat(info["entry_date"]).date()).days
        reason = None
        if sym in sell_tickers and CFG["exit"]["exit_on_sell_filing"]:
            reason = "congressional SELL filing"
        elif held >= CFG["exit"]["max_holding_calendar_days"]:
            reason = f"time stop ({held}d)"
        elif cur <= info["peak"] * (1 - CFG["exit"]["trailing_stop_pct"]):
            reason = f"trailing stop (peak ${info['peak']:.2f} → ${cur:.2f})"
        if reason:
            try:
                submit("sell", sym, float(broker_pos[sym].qty), "exit")
                del st["positions"][sym]
                report.append(f"🔴 SELL {sym} — {reason} (entry {info['entry_date']} @ "
                              f"${info['entry_price']:.2f}, politician: {info['politician']})")
            except Exception as e:
                report.append(f"⚠️ SELL {sym} failed: {e}")

    slots = CFG["account"]["max_positions"] - len(tc.get_all_positions())
    per_trade = equity * CFG["account"]["position_pct"]
    entered = 0
    for s in signals:
        if entered >= slots:
            break
        if s["ticker"] in broker_pos or s["ticker"] in st["positions"]:
            continue
        ok, px = passes_liquidity(s["ticker"])
        if not ok:
            continue
        try:
            submit("buy", s["ticker"], per_trade / px, "entry")
            st["positions"][s["ticker"]] = {"politician": s["politician"], "entry_date": today,
                                            "entry_price": px, "peak": px}
            report.append(f"🟢 BUY {s['ticker']} ~${per_trade:,.0f} @ ~${px:.2f} — "
                          f"{s['politician']}, filed {s['pubDate']}, est ${s['value']:,.0f}")
            entered += 1
        except Exception as e:
            report.append(f"⚠️ BUY {s['ticker']} failed: {e}")
    if entered == 0:
        report.append("No new entries today.")

    actual = {p.symbol for p in tc.get_all_positions()}
    drift = actual.symmetric_difference(st["positions"].keys())
    if drift:
        report.append(f"⚠️ Reconcile drift: {', '.join(sorted(drift))} — investigate.")

    st["last_run"] = today
    save_state(st)
    say("\n".join(report))

if __name__ == "__main__":
    run()
