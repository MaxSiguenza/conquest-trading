# -*- coding: utf-8 -*-
"""
Conquest Trading — Robinhood Options Layer
==========================================
Handles real options positions and order execution on Robinhood
via the robin_stocks library. Stock trades stay on Alpaca.

Supported operations:
  - get_options_positions()           → all open options with P&L
  - buy_option(...)                   → buy to open (long call / long put)
  - sell_option(...)                  → sell to close an existing position
  - buy_spread(...)                   → multi-leg debit spread
  - sell_spread(...)                  → multi-leg credit spread
  - get_options_orders()              → recent options orders
  - cancel_options_order(order_id)    → cancel a pending order

Environment variables (set in .env / Railway):
  ROBINHOOD_EMAIL     — Robinhood account email
  ROBINHOOD_PASSWORD  — Robinhood account password
"""

import os
import sys

APP_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, APP_DIR)

from dotenv import load_dotenv
load_dotenv(os.path.join(APP_DIR, ".env"))

RH_EMAIL    = os.getenv("ROBINHOOD_EMAIL", "")
RH_PASSWORD = os.getenv("ROBINHOOD_PASSWORD", "")

_logged_in = False


# ── Authentication ─────────────────────────────────────────────────────────────

def rh_login() -> bool:
    """
    Log in to Robinhood. Caches session token to disk so subsequent calls
    in the same day don't require re-authentication.
    Returns True on success.
    """
    global _logged_in
    if _logged_in:
        return True

    if not RH_EMAIL or not RH_PASSWORD:
        raise RuntimeError(
            "ROBINHOOD_EMAIL and ROBINHOOD_PASSWORD must be set in .env"
        )

    try:
        import robin_stocks.robinhood as rh
        rh.login(
            username=RH_EMAIL,
            password=RH_PASSWORD,
            store_session=True,
            mfa_code=None,
        )
        _logged_in = True
        print("[RH] Logged in to Robinhood")
        return True
    except Exception as e:
        print(f"[RH] Login failed: {e}")
        raise


def _rh():
    """Return the robin_stocks.robinhood module, ensuring we're logged in."""
    import robin_stocks.robinhood as rh
    if not _logged_in:
        rh_login()
    return rh


# ── Positions ──────────────────────────────────────────────────────────────────

def get_options_positions() -> list[dict]:
    """
    Fetch all open options positions from Robinhood with full details.

    Returns list of dicts:
      ticker, expiry, strike, option_type, quantity, avg_price,
      current_price, market_value, unrealized_pnl, unrealized_pnl_pct,
      delta, gamma, theta, vega, iv, dte, contract_id, tradability
    """
    rh = _rh()
    try:
        raw_positions = rh.options.get_open_option_positions()
        if not raw_positions:
            return []

        positions = []
        for pos in raw_positions:
            qty = float(pos.get("quantity", 0))
            if qty == 0:
                continue

            # Fetch instrument details (strike, expiry, type)
            instrument_url = pos.get("option")
            instrument = {}
            if instrument_url:
                try:
                    instrument = rh.helper.request_get(instrument_url) or {}
                except Exception:
                    pass

            ticker      = pos.get("chain_symbol", "")
            expiry      = instrument.get("expiration_date", "")
            strike      = float(instrument.get("strike_price") or 0)
            option_type = instrument.get("type", "")  # 'call' or 'put'
            contract_id = instrument.get("id", "")
            tradability = instrument.get("tradability", "")

            avg_price     = float(pos.get("average_price") or 0) / 100  # Robinhood stores in cents
            current_price = 0.0
            market_value  = 0.0
            delta = gamma = theta = vega = iv = 0.0
            dte = 0

            # Fetch current market data for the contract
            try:
                mkt = rh.options.get_option_market_data_by_id(contract_id)
                if mkt and len(mkt) > 0:
                    m = mkt[0] if isinstance(mkt, list) else mkt
                    current_price = float(m.get("adjusted_mark_price") or m.get("mark_price") or 0)
                    delta = float(m.get("delta") or 0)
                    gamma = float(m.get("gamma") or 0)
                    theta = float(m.get("theta") or 0)
                    vega  = float(m.get("vega")  or 0)
                    iv    = float(m.get("implied_volatility") or 0)
            except Exception:
                pass

            market_value     = round(current_price * qty * 100, 2)
            cost_basis       = round(avg_price * qty * 100, 2)
            unrealized_pnl   = round(market_value - cost_basis, 2)
            unrealized_pnl_pct = round((unrealized_pnl / cost_basis * 100) if cost_basis else 0, 2)

            # Days to expiry
            if expiry:
                from datetime import date
                try:
                    dte = (date.fromisoformat(expiry) - date.today()).days
                except Exception:
                    dte = 0

            positions.append({
                "ticker":              ticker,
                "expiry":              expiry,
                "strike":              strike,
                "option_type":         option_type,
                "quantity":            qty,
                "avg_price":           round(avg_price, 4),
                "current_price":       round(current_price, 4),
                "market_value":        market_value,
                "cost_basis":          cost_basis,
                "unrealized_pnl":      unrealized_pnl,
                "unrealized_pnl_pct":  unrealized_pnl_pct,
                "delta":               round(delta, 4),
                "gamma":               round(gamma, 4),
                "theta":               round(theta, 4),
                "vega":                round(vega,  4),
                "iv":                  round(iv,    4),
                "dte":                 dte,
                "contract_id":         contract_id,
                "tradability":         tradability,
            })

        return positions

    except Exception as e:
        print(f"[RH] get_options_positions error: {e}")
        raise


# ── Order placement ────────────────────────────────────────────────────────────

def buy_option(
    ticker: str,
    expiry: str,
    strike: float,
    option_type: str,
    quantity: int = 1,
    limit_price: float | None = None,
    time_in_force: str = "gtc",
) -> dict:
    """
    Buy to open a single-leg option on Robinhood.

    Args:
        ticker:       Stock symbol (e.g. "AAPL")
        expiry:       Expiration date "YYYY-MM-DD"
        strike:       Strike price
        option_type:  "call" or "put"
        quantity:     Number of contracts (default 1)
        limit_price:  Limit price per contract. If None, uses current mid.
        time_in_force: "gtc" (default) or "gfd"

    Returns dict with order_id, status, price, symbol.
    """
    rh = _rh()
    try:
        # Resolve current mid if no limit price given
        if limit_price is None:
            market_data = rh.options.get_option_market_data(
                ticker, expiry, str(strike), option_type
            )
            if market_data and market_data[0]:
                m = market_data[0][0] if isinstance(market_data[0], list) else market_data[0]
                bid = float(m.get("bid_price") or 0)
                ask = float(m.get("ask_price") or 0)
                limit_price = round((bid + ask) / 2, 2) if bid and ask else float(m.get("mark_price") or 1.0)
            else:
                raise ValueError(f"Could not fetch market data for {ticker} {expiry} ${strike} {option_type}")

        order = rh.orders.order_buy_option_limit(
            positionEffect="open",
            creditOrDebit="debit",
            price=limit_price,
            symbol=ticker,
            quantity=quantity,
            expirationDate=expiry,
            strike=str(strike),
            optionType=option_type,
            timeInForce=time_in_force,
        )

        result = {
            "action":     "buy_to_open",
            "ticker":     ticker,
            "expiry":     expiry,
            "strike":     strike,
            "option_type": option_type,
            "quantity":   quantity,
            "limit_price": limit_price,
            "order_id":   order.get("id", ""),
            "status":     order.get("state", ""),
            "legs":       order.get("legs", []),
        }
        print(f"[RH] BUY {quantity}x {ticker} {expiry} ${strike} {option_type} @ ${limit_price} — {result['status']}")
        return result

    except Exception as e:
        print(f"[RH] buy_option error: {e}")
        return {"error": str(e), "action": "buy_to_open", "ticker": ticker}


def sell_option(
    ticker: str,
    expiry: str,
    strike: float,
    option_type: str,
    quantity: int = 1,
    limit_price: float | None = None,
    time_in_force: str = "gtc",
) -> dict:
    """
    Sell to close an existing single-leg options position on Robinhood.

    Args:
        ticker:       Stock symbol
        expiry:       Expiration date "YYYY-MM-DD"
        strike:       Strike price
        option_type:  "call" or "put"
        quantity:     Number of contracts to close
        limit_price:  Limit price per contract. If None, uses current mid.
        time_in_force: "gtc" (default) or "gfd"

    Returns dict with order_id, status, price.
    """
    rh = _rh()
    try:
        if limit_price is None:
            market_data = rh.options.get_option_market_data(
                ticker, expiry, str(strike), option_type
            )
            if market_data and market_data[0]:
                m = market_data[0][0] if isinstance(market_data[0], list) else market_data[0]
                bid = float(m.get("bid_price") or 0)
                ask = float(m.get("ask_price") or 0)
                limit_price = round((bid + ask) / 2, 2) if bid and ask else float(m.get("mark_price") or 0.01)
            else:
                raise ValueError(f"Could not fetch market data for {ticker} {expiry} ${strike} {option_type}")

        order = rh.orders.order_sell_option_limit(
            positionEffect="close",
            creditOrDebit="credit",
            price=limit_price,
            symbol=ticker,
            quantity=quantity,
            expirationDate=expiry,
            strike=str(strike),
            optionType=option_type,
            timeInForce=time_in_force,
        )

        result = {
            "action":      "sell_to_close",
            "ticker":      ticker,
            "expiry":      expiry,
            "strike":      strike,
            "option_type": option_type,
            "quantity":    quantity,
            "limit_price": limit_price,
            "order_id":    order.get("id", ""),
            "status":      order.get("state", ""),
        }
        print(f"[RH] SELL {quantity}x {ticker} {expiry} ${strike} {option_type} @ ${limit_price} — {result['status']}")
        return result

    except Exception as e:
        print(f"[RH] sell_option error: {e}")
        return {"error": str(e), "action": "sell_to_close", "ticker": ticker}


def buy_spread(
    ticker: str,
    spread_type: str,
    legs: list[dict],
    quantity: int = 1,
    limit_price: float | None = None,
    time_in_force: str = "gtc",
) -> dict:
    """
    Place a multi-leg debit spread on Robinhood (buy spread / open position).

    Args:
        ticker:      Stock symbol
        spread_type: "call_spread", "put_spread" (used for labeling)
        legs:        List of leg dicts:
                       [{"expiry": "YYYY-MM-DD", "strike": float,
                         "option_type": "call"/"put", "action": "buy"/"sell"}]
        quantity:    Number of spreads
        limit_price: Net debit limit price. If None, uses sum of leg mids.
        time_in_force: "gtc" or "gfd"

    Returns dict with order_id, status.
    """
    rh = _rh()
    try:
        spread_legs = []
        mid_total   = 0.0

        for leg in legs:
            action    = "buy"  if leg["action"] == "buy"  else "sell"
            effect    = "open" if leg["action"] == "buy"  else "open"
            spread_legs.append({
                "expirationDate": leg["expiry"],
                "strike":         str(leg["strike"]),
                "optionType":     leg["option_type"],
                "effect":         effect,
                "action":         action,
            })

            if limit_price is None:
                try:
                    mkt = rh.options.get_option_market_data(
                        ticker, leg["expiry"], str(leg["strike"]), leg["option_type"]
                    )
                    if mkt and mkt[0]:
                        m   = mkt[0][0] if isinstance(mkt[0], list) else mkt[0]
                        bid = float(m.get("bid_price") or 0)
                        ask = float(m.get("ask_price") or 0)
                        mid = (bid + ask) / 2 if bid and ask else float(m.get("mark_price") or 0)
                        mid_total += mid if action == "buy" else -mid
                except Exception:
                    pass

        if limit_price is None:
            limit_price = round(max(mid_total, 0.01), 2)

        order = rh.orders.order_option_spread(
            direction="debit",
            price=limit_price,
            symbol=ticker,
            quantity=quantity,
            spread=spread_legs,
            timeInForce=time_in_force,
        )

        result = {
            "action":      "buy_spread",
            "spread_type": spread_type,
            "ticker":      ticker,
            "legs":        legs,
            "quantity":    quantity,
            "limit_price": limit_price,
            "order_id":    order.get("id", ""),
            "status":      order.get("state", ""),
        }
        print(f"[RH] SPREAD {spread_type} {ticker} x{quantity} @ ${limit_price} net debit — {result['status']}")
        return result

    except Exception as e:
        print(f"[RH] buy_spread error: {e}")
        return {"error": str(e), "action": "buy_spread", "ticker": ticker}


def sell_spread(
    ticker: str,
    spread_type: str,
    legs: list[dict],
    quantity: int = 1,
    limit_price: float | None = None,
    time_in_force: str = "gtc",
) -> dict:
    """
    Close a multi-leg spread (sell to close) or open a credit spread.

    Args:
        ticker:      Stock symbol
        spread_type: "call_spread", "put_spread", "iron_condor", etc.
        legs:        List of leg dicts with action "buy" (buy-to-close) or "sell" (sell-to-close)
        quantity:    Number of spreads
        limit_price: Net credit to receive. If None, uses sum of leg mids.
        time_in_force: "gtc" or "gfd"
    """
    rh = _rh()
    try:
        spread_legs = []
        mid_total   = 0.0

        for leg in legs:
            action = leg["action"]
            effect = "close"
            spread_legs.append({
                "expirationDate": leg["expiry"],
                "strike":         str(leg["strike"]),
                "optionType":     leg["option_type"],
                "effect":         effect,
                "action":         action,
            })

            if limit_price is None:
                try:
                    mkt = rh.options.get_option_market_data(
                        ticker, leg["expiry"], str(leg["strike"]), leg["option_type"]
                    )
                    if mkt and mkt[0]:
                        m   = mkt[0][0] if isinstance(mkt[0], list) else mkt[0]
                        bid = float(m.get("bid_price") or 0)
                        ask = float(m.get("ask_price") or 0)
                        mid = (bid + ask) / 2 if bid and ask else float(m.get("mark_price") or 0)
                        mid_total += mid if action == "sell" else -mid
                except Exception:
                    pass

        if limit_price is None:
            limit_price = round(max(mid_total, 0.01), 2)

        order = rh.orders.order_option_spread(
            direction="credit",
            price=limit_price,
            symbol=ticker,
            quantity=quantity,
            spread=spread_legs,
            timeInForce=time_in_force,
        )

        result = {
            "action":      "sell_spread",
            "spread_type": spread_type,
            "ticker":      ticker,
            "legs":        legs,
            "quantity":    quantity,
            "limit_price": limit_price,
            "order_id":    order.get("id", ""),
            "status":      order.get("state", ""),
        }
        print(f"[RH] CLOSE SPREAD {spread_type} {ticker} x{quantity} @ ${limit_price} net credit — {result['status']}")
        return result

    except Exception as e:
        print(f"[RH] sell_spread error: {e}")
        return {"error": str(e), "action": "sell_spread", "ticker": ticker}


# ── Order management ───────────────────────────────────────────────────────────

def get_options_orders(limit: int = 20) -> list[dict]:
    """
    Return recent options orders (open + filled + cancelled).
    """
    rh = _rh()
    try:
        orders = rh.orders.get_all_option_orders()
        if not orders:
            return []

        results = []
        for o in orders[:limit]:
            legs = o.get("legs", [])
            results.append({
                "order_id":     o.get("id", ""),
                "status":       o.get("state", ""),
                "direction":    o.get("direction", ""),
                "quantity":     float(o.get("quantity") or 0),
                "price":        float(o.get("price") or 0),
                "created_at":   o.get("created_at", ""),
                "updated_at":   o.get("updated_at", ""),
                "legs":         [
                    {
                        "option":   leg.get("option", ""),
                        "side":     leg.get("side", ""),
                        "effect":   leg.get("position_effect", ""),
                        "ratio":    leg.get("ratio_quantity", 1),
                    }
                    for leg in legs
                ],
            })
        return results

    except Exception as e:
        print(f"[RH] get_options_orders error: {e}")
        return []


def cancel_options_order(order_id: str) -> dict:
    """
    Cancel a pending options order by order ID.
    """
    rh = _rh()
    try:
        result = rh.orders.cancel_option_order(order_id)
        print(f"[RH] Cancelled order {order_id}")
        return {"cancelled": True, "order_id": order_id, "result": result}
    except Exception as e:
        print(f"[RH] cancel_options_order error: {e}")
        return {"cancelled": False, "order_id": order_id, "error": str(e)}


# ── Status / availability ──────────────────────────────────────────────────────

def rh_available() -> bool:
    """True if Robinhood credentials are configured."""
    return bool(RH_EMAIL and RH_PASSWORD)


def rh_status_embed() -> dict:
    """Discord embed dict showing Robinhood connection status."""
    if not rh_available():
        return {
            "title": "Robinhood Status",
            "description": (
                "No Robinhood credentials configured.\n"
                "Add `ROBINHOOD_EMAIL` and `ROBINHOOD_PASSWORD` to Railway env vars."
            ),
            "color": 0xfb923c,
        }
    status = "Connected" if _logged_in else "Credentials set (not yet logged in)"
    return {
        "title": "Robinhood Status — Real Account",
        "description": (
            f"**Status:** {status}\n"
            f"**Mode:** LIVE (real money)\n"
            f"**Capabilities:** Options positions, buy/sell single-leg & spreads\n\n"
            f"*Use `!rh_positions` to see open options*"
        ),
        "color": 0x22c55e,
    }


# ── CLI quick-test ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("Logging in to Robinhood...")
    rh_login()

    print("\nFetching open options positions...\n")
    positions = get_options_positions()

    if not positions:
        print("No open options positions found.")
    else:
        print(f"{'Ticker':<8} {'Expiry':<12} {'Strike':>8} {'Type':<5} {'Qty':>4} "
              f"{'AvgCost':>8} {'Current':>8} {'MktVal':>10} {'P&L':>10} {'P&L%':>7} "
              f"{'Delta':>7} {'IV':>6} {'DTE':>4}")
        print("-" * 105)
        for p in positions:
            print(
                f"{p['ticker']:<8} {p['expiry']:<12} {p['strike']:>8.2f} {p['option_type']:<5} "
                f"{p['quantity']:>4.0f} ${p['avg_price']:>7.4f} ${p['current_price']:>7.4f} "
                f"${p['market_value']:>9.2f} ${p['unrealized_pnl']:>+9.2f} {p['unrealized_pnl_pct']:>+6.1f}% "
                f"{p['delta']:>+6.3f} {p['iv']:>5.0%} {p['dte']:>4}d"
            )
