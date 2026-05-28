#!/usr/bin/env python3
"""
Place real options orders on Alpaca paper account.
Mirrors the 8 open paper trading options positions using actual OCC symbols.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, OptionLegRequest
from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass, PositionIntent, OrderType

ALPACA_KEY    = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET = os.getenv("ALPACA_SECRET_KEY", "")
if not ALPACA_KEY or not ALPACA_SECRET:
    sys.exit("ERROR: ALPACA_API_KEY / ALPACA_SECRET_KEY not found in .env")

client = TradingClient(api_key=ALPACA_KEY, secret_key=ALPACA_SECRET, paper=True)
acct   = client.get_account()
print(f"Paper account: {acct.id}  BP=${float(acct.buying_power):,.2f}  PV=${float(acct.portfolio_value):,.2f}\n")

results = []

def mleg(name, qty, legs_spec):
    """Submit a multi-leg options order (spreads, condors)."""
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
    try:
        order = client.submit_order(req)
        print(f"  OK  {name}: order {order.id}  status={order.status}")
        results.append({"name": name, "id": str(order.id), "status": str(order.status)})
    except Exception as e:
        print(f"  ERR {name}: {e}")
        results.append({"name": name, "error": str(e)[:200]})

def single(name, occ, side, qty):
    """Submit a single-leg options order (long call / long put)."""
    req = MarketOrderRequest(
        symbol=occ,
        qty=qty,
        type=OrderType.MARKET,
        side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
        time_in_force=TimeInForce.DAY,
        position_intent=PositionIntent.BUY_TO_OPEN if side == "buy" else PositionIntent.SELL_TO_OPEN,
    )
    try:
        order = client.submit_order(req)
        print(f"  OK  {name}: order {order.id}  status={order.status}")
        results.append({"name": name, "id": str(order.id), "status": str(order.status)})
    except Exception as e:
        print(f"  ERR {name}: {e}")
        results.append({"name": name, "error": str(e)[:200]})


print("=== Placing 8 options orders ===\n")

# 1. WMT iron condor — sell $130/$135 call spread + sell $110/$105 put spread
mleg("WMT iron_condor", 1, [
    ("WMT260618C00130000", "sell"),   # short call at 130
    ("WMT260618C00135000", "buy"),    # long call wing at 135
    ("WMT260618P00110000", "sell"),   # short put at 110
    ("WMT260618P00105000", "buy"),    # long put wing at 105
])

# 2. AAPL put debit spread — buy $305p / sell $295p
mleg("AAPL put_spread", 1, [
    ("AAPL260618P00305000", "buy"),
    ("AAPL260618P00295000", "sell"),
])

# 3. AMZN put debit spread — buy $265p / sell $255p
mleg("AMZN put_spread", 1, [
    ("AMZN260618P00265000", "buy"),
    ("AMZN260618P00255000", "sell"),
])

# 4. GOOGL put debit spread — buy $380p / sell $370p
mleg("GOOGL put_spread", 1, [
    ("GOOGL260618P00380000", "buy"),
    ("GOOGL260618P00370000", "sell"),
])

# 5. SPY call debit spread — buy $755c / sell $765c
mleg("SPY call_spread", 1, [
    ("SPY260618C00755000", "buy"),
    ("SPY260618C00765000", "sell"),
])

# 6. QQQ long call — single leg, buy $730c
single("QQQ long_call", "QQQ260618C00730000", "buy", 1)

# 7. COP iron condor — sell $130/$135 call spread + sell $110/$105 put spread
mleg("COP iron_condor", 1, [
    ("COP260618C00130000", "sell"),
    ("COP260618C00135000", "buy"),
    ("COP260618P00110000", "sell"),
    ("COP260618P00105000", "buy"),
])

# 8. COST long put — single leg, buy $1015p
single("COST long_put", "COST260618P01015000", "buy", 1)


print("\n=== Summary ===")
ok  = [r for r in results if "error" not in r]
err = [r for r in results if "error" in r]
print(f"  Submitted: {len(ok)}/8   Failed: {len(err)}/8")
for r in err:
    print(f"  FAILED {r['name']}: {r['error']}")
