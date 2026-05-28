#!/usr/bin/env python3
"""
Generate fresh paper trades via the Conquest system,
then place any options trades on Alpaca paper account.
"""
import os, sys, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, OptionLegRequest
from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass, PositionIntent, OrderType

ALPACA_KEY    = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET = os.getenv("ALPACA_SECRET_KEY", "")


def occ_symbol(ticker: str, expiry: str, opt_type: str, strike: float) -> str:
    """Build OCC option symbol. expiry = 'YYYY-MM-DD', opt_type = 'call'/'put'"""
    yy, mm, dd = expiry[2:4], expiry[5:7], expiry[8:10]
    cp = "C" if opt_type.lower().startswith("c") else "P"
    strike_int = int(round(strike * 1000))
    return f"{ticker}{yy}{mm}{dd}{cp}{strike_int:08d}"


def place_mleg(client, name, qty, legs_spec):
    legs = [
        OptionLegRequest(
            symbol=occ,
            ratio_qty=1,
            side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
            position_intent=PositionIntent.BUY_TO_OPEN if side == "buy" else PositionIntent.SELL_TO_OPEN,
        )
        for occ, side in legs_spec
    ]
    req = MarketOrderRequest(
        qty=qty,
        type=OrderType.MARKET,
        time_in_force=TimeInForce.DAY,
        order_class=OrderClass.MLEG,
        legs=legs,
    )
    order = client.submit_order(req)
    return str(order.id), str(order.status)


def place_single(client, name, occ, side, qty):
    req = MarketOrderRequest(
        symbol=occ,
        qty=qty,
        type=OrderType.MARKET,
        side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
        time_in_force=TimeInForce.DAY,
        position_intent=PositionIntent.BUY_TO_OPEN if side == "buy" else PositionIntent.SELL_TO_OPEN,
    )
    order = client.submit_order(req)
    return str(order.id), str(order.status)


def place_options_trade(client, trade: dict) -> dict:
    """Attempt to place an options trade on Alpaca. Returns result dict."""
    tt     = trade["trade_type"]
    ticker = trade["ticker"]
    expiry = trade.get("expiry_date") or trade.get("expiry", "")
    name   = f"{ticker} {tt}"

    try:
        if tt == "long_call":
            sym = occ_symbol(ticker, expiry, "call", trade["strike"])
            oid, status = place_single(client, name, sym, "buy", trade.get("contracts", 1))
            print(f"  OK  {name}: {sym}  order={oid}  status={status}")
            return {"name": name, "id": oid, "status": status, "symbols": [sym]}

        elif tt == "long_put":
            sym = occ_symbol(ticker, expiry, "put", trade["strike"])
            oid, status = place_single(client, name, sym, "buy", trade.get("contracts", 1))
            print(f"  OK  {name}: {sym}  order={oid}  status={status}")
            return {"name": name, "id": oid, "status": status, "symbols": [sym]}

        elif tt == "call_spread":
            long_sym  = occ_symbol(ticker, expiry, "call", trade["long_strike"])
            short_sym = occ_symbol(ticker, expiry, "call", trade["short_strike"])
            oid, status = place_mleg(client, name, trade.get("contracts", 1), [
                (long_sym,  "buy"),
                (short_sym, "sell"),
            ])
            print(f"  OK  {name}: BUY {long_sym} / SELL {short_sym}  order={oid}  status={status}")
            return {"name": name, "id": oid, "status": status, "symbols": [long_sym, short_sym]}

        elif tt == "put_spread":
            long_sym  = occ_symbol(ticker, expiry, "put", trade["long_strike"])
            short_sym = occ_symbol(ticker, expiry, "put", trade["short_strike"])
            oid, status = place_mleg(client, name, trade.get("contracts", 1), [
                (long_sym,  "buy"),
                (short_sym, "sell"),
            ])
            print(f"  OK  {name}: BUY {long_sym} / SELL {short_sym}  order={oid}  status={status}")
            return {"name": name, "id": oid, "status": status, "symbols": [long_sym, short_sym]}

        elif tt == "iron_condor":
            sc_sym = occ_symbol(ticker, expiry, "call", trade["short_call_k"])
            lc_sym = occ_symbol(ticker, expiry, "call", trade["long_call_k"])
            sp_sym = occ_symbol(ticker, expiry, "put",  trade["short_put_k"])
            lp_sym = occ_symbol(ticker, expiry, "put",  trade["long_put_k"])
            oid, status = place_mleg(client, name, trade.get("contracts", 1), [
                (sc_sym, "sell"),
                (lc_sym, "buy"),
                (sp_sym, "sell"),
                (lp_sym, "buy"),
            ])
            print(f"  OK  {name}: SELL {sc_sym}, BUY {lc_sym}, SELL {sp_sym}, BUY {lp_sym}  order={oid}  status={status}")
            return {"name": name, "id": oid, "status": status, "symbols": [sc_sym, lc_sym, sp_sym, lp_sym]}

        elif tt in ("bull_put_spread", "bear_call_spread"):
            opt_type  = "put" if tt == "bull_put_spread" else "call"
            long_sym  = occ_symbol(ticker, expiry, opt_type, trade["long_strike"])
            short_sym = occ_symbol(ticker, expiry, opt_type, trade["short_strike"])
            oid, status = place_mleg(client, name, trade.get("contracts", 1), [
                (short_sym, "sell"),
                (long_sym,  "buy"),
            ])
            print(f"  OK  {name}: SELL {short_sym} / BUY {long_sym}  order={oid}  status={status}")
            return {"name": name, "id": oid, "status": status, "symbols": [short_sym, long_sym]}

        else:
            return {"name": name, "skipped": True, "reason": f"unhandled type: {tt}"}

    except Exception as e:
        print(f"  ERR {name}: {e}")
        return {"name": name, "error": str(e)[:300]}


# ── Step 1: Generate trades ───────────────────────────────────────────────────

print("=" * 60)
print("Step 1: Running paper trader to generate today's trades...")
print("=" * 60)

from paper_trader import generate_daily_trades

new_trades = generate_daily_trades(n=10)

if not new_trades:
    print("\n[!] generate_daily_trades() returned no new trades.")
    print("    Likely already ran today. Loading current open trades to find options...\n")
    from paper_trader import load_trades
    from datetime import datetime
    import pytz
    ET = pytz.timezone("America/New_York")
    today_str = datetime.now(ET).strftime("%Y-%m-%d")
    all_trades = load_trades()
    new_trades = [t for t in all_trades
                  if t.get("date_entered", "").startswith(today_str)]
    print(f"    Found {len(new_trades)} trades opened today.")

OPTIONS_TYPES = ("long_call", "long_put", "call_spread", "put_spread",
                 "iron_condor", "bull_put_spread", "bear_call_spread")
options_trades = [t for t in new_trades if t.get("trade_type") in OPTIONS_TYPES]
stock_trades   = [t for t in new_trades if t.get("trade_type") not in OPTIONS_TYPES]

print(f"\nGenerated: {len(new_trades)} total — {len(options_trades)} options, {len(stock_trades)} stocks")
print("\nOptions trades to place:")
for t in options_trades:
    print(f"  {t['ticker']:6s} {t['trade_type']:18s}  expiry={t.get('expiry_date') or t.get('expiry', '?')}  "
          f"entry_price={t.get('entry_option_price') or t.get('entry_net_debit') or t.get('entry_net_credit', '?')}")

if not options_trades:
    print("\nNo options trades generated today. Done.")
    sys.exit(0)

# ── Step 2: Place options on Alpaca ──────────────────────────────────────────

print(f"\n{'=' * 60}")
print("Step 2: Placing options on Alpaca paper account...")
print("=" * 60)

client = TradingClient(api_key=ALPACA_KEY, secret_key=ALPACA_SECRET, paper=True)
acct   = client.get_account()
print(f"Account: {acct.id}  BP=${float(acct.buying_power):,.2f}\n")

results = []
for trade in options_trades:
    result = place_options_trade(client, trade)
    results.append(result)

print(f"\n{'=' * 60}")
print("Summary")
print("=" * 60)
ok      = [r for r in results if "id" in r]
failed  = [r for r in results if "error" in r]
skipped = [r for r in results if "skipped" in r]
print(f"  Placed:  {len(ok)}")
print(f"  Failed:  {len(failed)}")
print(f"  Skipped: {len(skipped)}")
for r in failed:
    print(f"  FAILED {r['name']}: {r['error']}")
