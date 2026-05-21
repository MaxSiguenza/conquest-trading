# -*- coding: utf-8 -*-
"""
Alpaca Paper Trading Connector
===============================
Reads the paper account summary and optionally submits stock orders.
All activity goes to the paper account ($200k virtual money) — no real money.

Usage:
  python alpaca_connect.py          -- show account summary
  python alpaca_connect.py buy AAPL 10   -- paper buy 10 shares of AAPL
  python alpaca_connect.py sell AAPL 10  -- paper sell 10 shares of AAPL
"""
import os
import sys

_ENV_FILE = os.path.join(os.path.dirname(__file__), ".env")


def _get_client():
    """Return an authenticated Alpaca TradingClient (paper=True)."""
    from alpaca.trading.client import TradingClient
    from dotenv import dotenv_values
    vals   = dotenv_values(_ENV_FILE)
    key    = vals.get("ALPACA_API_KEY",    "") or os.getenv("ALPACA_API_KEY",    "")
    secret = vals.get("ALPACA_SECRET_KEY", "") or os.getenv("ALPACA_SECRET_KEY", "")
    if not key or not secret:
        raise RuntimeError(
            "ALPACA_API_KEY or ALPACA_SECRET_KEY not found in .env\n"
            "Add them: ALPACA_API_KEY=... and ALPACA_SECRET_KEY=..."
        )
    return TradingClient(key, secret, paper=True)


def get_account_summary() -> dict:
    """
    Return paper account summary for the web UI.
    Keys: equity, cash, buying_power, portfolio_value, pnl, pnl_pct, status, error
    """
    try:
        client  = _get_client()
        acct    = client.get_account()
        equity  = float(acct.equity)
        last_eq = float(acct.last_equity) if acct.last_equity else equity
        pnl     = equity - last_eq
        pnl_pct = pnl / last_eq if last_eq else 0
        return {
            "equity":          round(equity, 2),
            "cash":            round(float(acct.cash), 2),
            "buying_power":    round(float(acct.buying_power), 2),
            "portfolio_value": round(float(acct.portfolio_value), 2),
            "pnl":             round(pnl, 2),
            "pnl_pct":         round(pnl_pct, 4),
            "status":          str(acct.status.value) if acct.status else "active",
            "error":           None,
        }
    except Exception as e:
        return {"error": str(e)}


def get_positions() -> list:
    """
    Return list of current Alpaca paper positions.
    Each: symbol, qty, avg_entry, current_price, pnl, pnl_pct
    """
    try:
        client    = _get_client()
        positions = client.get_all_positions()
        result    = []
        for p in positions:
            try:
                avg_entry  = float(p.avg_entry_price)
                cur_price  = float(p.current_price)
                qty        = float(p.qty)
                pnl        = float(p.unrealized_pl)
                pnl_pct    = float(p.unrealized_plpc)
                result.append({
                    "symbol":        p.symbol,
                    "qty":           qty,
                    "avg_entry":     avg_entry,
                    "current_price": cur_price,
                    "pnl":           round(pnl, 2),
                    "pnl_pct":       round(pnl_pct, 4),
                    "side":          str(p.side.value) if p.side else "long",
                })
            except Exception:
                pass
        return result
    except Exception as e:
        return [{"error": str(e)}]


def submit_market_order(symbol: str, qty: int, side: str) -> dict:
    """
    Submit a paper market order.
    side = 'buy' or 'sell'
    Returns: {"order_id", "status", "symbol", "qty", "side", "error"}
    """
    try:
        from alpaca.trading.requests import MarketOrderRequest
        from alpaca.trading.enums    import OrderSide, TimeInForce
        client = _get_client()
        req    = MarketOrderRequest(
            symbol=symbol.upper(),
            qty=qty,
            side=OrderSide.BUY if side.lower() == "buy" else OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
        )
        order = client.submit_order(req)
        return {
            "order_id": str(order.id),
            "status":   str(order.status.value) if order.status else "submitted",
            "symbol":   symbol.upper(),
            "qty":      qty,
            "side":     side.lower(),
            "error":    None,
        }
    except Exception as e:
        return {"error": str(e)}


# ── CLI entry point ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    args = sys.argv[1:]

    if not args:
        print("\nAlpaca Paper Trading Account")
        print("=" * 40)
        s = get_account_summary()
        if s.get("error"):
            print(f"  ERROR: {s['error']}")
        else:
            print(f"  Status:        {s['status']}")
            print(f"  Portfolio:    ${s['portfolio_value']:>12,.2f}")
            print(f"  Equity:       ${s['equity']:>12,.2f}")
            print(f"  Cash:         ${s['cash']:>12,.2f}")
            print(f"  Buying Power: ${s['buying_power']:>12,.2f}")
            sign = "+" if s["pnl"] >= 0 else ""
            print(f"  Today P&L:    {sign}${s['pnl']:,.2f}  ({sign}{s['pnl_pct']*100:.2f}%)")

        positions = get_positions()
        if positions and not positions[0].get("error"):
            print(f"\n  Open Positions:")
            for p in positions:
                sign = "+" if p["pnl"] >= 0 else ""
                print(f"  {p['symbol']:<8} {p['qty']:.0f} shares  avg ${p['avg_entry']:.2f}"
                      f"  now ${p['current_price']:.2f}  P&L: {sign}${p['pnl']:.2f}")
        else:
            print("\n  No open stock positions.")
        print()

    elif len(args) == 3 and args[0].lower() in ("buy", "sell"):
        side, symbol, qty_str = args
        try:
            qty = int(qty_str)
        except ValueError:
            print(f"  ERROR: qty must be an integer, got '{qty_str}'")
            sys.exit(1)
        print(f"\n  Submitting paper {side.upper()} order: {qty} shares of {symbol.upper()}")
        result = submit_market_order(symbol, qty, side)
        if result.get("error"):
            print(f"  ERROR: {result['error']}")
        else:
            print(f"  Order submitted!")
            print(f"  ID: {result['order_id']}  Status: {result['status']}")
            print(f"  {result['side'].upper()} {result['qty']} {result['symbol']}")

    else:
        print("Usage:")
        print("  python alpaca_connect.py                 -- account summary")
        print("  python alpaca_connect.py buy AAPL 10     -- paper buy 10 shares")
        print("  python alpaca_connect.py sell AAPL 5     -- paper sell 5 shares")
