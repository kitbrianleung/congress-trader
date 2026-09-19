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

# --- token structure diagnostic (prints NO secret characters) ---
t = os.environ.get("DISCORD_BOT_TOKEN", "")
print(f"token len={len(t)} dots={t.count('.')} "
      f"alnum_ok={all(c.isalnum() or c in '._-' for c in t)} "
      f"starts_MT={t.startswith('MT')}")

def say(msg):
    for i in range(0, len(msg), 1900):
        payload = json.dumps({"content": msg[i:i+1900]}).encode()
        req = urllib.request.Request(
            f"https://discord.com/api/v10/channels/{CHANNEL}/messages",
            data=payload, method="POST",
            headers={"Authorization": f"Bot {TOKEN}", "Content-Type": "application/json",
                     "User-Agent": "DiscordBot (https://github.com/congress-trader, 1.0.0)"})
        try:
            urllib.request.urlopen(req, timeout=30).read()
        except Exception as e:
            print("discord send failed:", e, file=sys.stderr)
        time.sleep(0.4)

def load_state():
    if os.path.exists(STATE_FILE):
        return json.load(open(STATE_FILE))
    return {"positions": {}, "halted": False, "last_run": ""}

def save_state(st):
    json.dump(st, open(STATE_FILE, "w"), indent=1)

def poll_commands():
    req = urllib.request.Request(
        f"https://discord.com/api/v10/channels/{CHANNEL}/messages?limit=25",
        headers={"Authorization": f"Bot {TOKEN}",
                 "User-Agent": "DiscordBot (https://github.com/congress-trader, 1.0.0)"})
    try:
        msgs = json.loads(urllib.request.urlopen(req, timeout=30).read())
    except Exception:
        return None
    for m in msgs:
        if m.get("author", {}).get("bot"):
            continue
        c = (m.get("content") or "").strip().upper()
        if c in ("HALT", "RESUME", "STATUS"):
            return c
    return None

def latest_price(ticker):
    q = dc.get_stock_latest_quote(StockLatestQuoteRequest(symbol_or_symbols=ticker))[ticker]
    return float(q.ask_price or q.bid_price)

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

# --- temporary diagnostic: what can this bot actually see? ---
req = urllib.request.Request("https://discord.com/api/v10/users/@me",
    headers={"Authorization": f"Bot {TOKEN}"})
me = json.loads(urllib.request.urlopen(req, timeout=30).read())
print("bot identity:", me.get("username"), "#", me.get("discriminator"), "id:", me.get("id"))
req = urllib.request.Request("https://discord.com/api/v10/users/@me/guilds",
    headers={"Authorization": f"Bot {TOKEN}"})
guilds = json.loads(urllib.request.urlopen(req, timeout=30).read())
print("bot is in guilds:", [(g["name"], g["id"]) for g in guilds])
  
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
    if not clock.is_open and (clock.next_open - clock.timestamp).total_seconds() > 6 * 3600:
        say(f"ℹ️ **{today}** — market closed today; skipping.")
        return
    if st.get("last_run") == today:
        say(f"ℹ️ **{today}** — already ran today; skipping.")
        return

    # --- rebuild positions from broker if cache was lost ---
    broker_pos = {p.symbol: p for p in tc.get_all_positions()}
    for sym in broker_pos:
        st["positions"].setdefault(sym, {
            "politician": "?", "entry_date": today,
            "entry_price": float(broker_pos[sym].avg_entry_price),
            "peak": float(broker_pos[sym].current_price or broker_pos[sym].avg_entry_price)})

    equity = float(tc.get_account().equity)
    report = [f"📋 **Congressional Trader — {today}** (mode={CFG['mode']}, aggressive)",
              f"Equity ${equity:,.2f} | positions {len(broker_pos)}/{CFG['account']['max_positions']}"]

    # --- exits ---
    signals, sell_tickers = fetch_signals(CFG)
    for sym, info in list(st["positions"].items()):
        if sym not in broker_pos:            # position gone (manual close etc.)
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

    # --- entries (no market regime gate — aggressive profile) ---
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

    # --- reconciliation (the validator role) ---
    actual = set(tc.get_all_positions()) and {p.symbol for p in tc.get_all_positions()}
    drift = actual.symmetric_difference(st["positions"].keys())
    if drift:
        report.append(f"⚠️ Reconcile drift: {', '.join(sorted(drift))} — investigate.")

    st["last_run"] = today
    save_state(st)
    say("\n".join(report))

if __name__ == "__main__":
    run()
