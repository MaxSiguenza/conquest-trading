import pandas as pd
import yfinance as yf


def fetch_ohlcv(ticker: str, start: str, end: str) -> pd.DataFrame:
    raw = yf.download(ticker, start=start, end=end, auto_adjust=True, progress=False)
    if raw.empty:
        raise ValueError(f"No data returned for ticker '{ticker}'. Check the symbol and date range.")

    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = [col[0] for col in raw.columns]

    df = raw[["Open", "High", "Low", "Close", "Volume"]].copy()
    df.dropna(inplace=True)
    return df


def fetch_vix(start: str, end: str) -> pd.Series:
    """
    The VIX (CBOE Volatility Index) is Wall Street's fear gauge.
    It measures how much volatility the options market is pricing in for the next 30 days.
    VIX < 20  → calm market, normal conditions
    VIX 20–30 → elevated uncertainty
    VIX > 30  → fear/panic mode — major news events, crashes, crises
    We use it as a macro filter: don't open new positions during panic regimes.
    """
    raw = yf.download("^VIX", start=start, end=end, auto_adjust=True, progress=False)
    if raw.empty:
        return pd.Series(dtype=float, name="VIX")

    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = [col[0] for col in raw.columns]

    return raw["Close"].rename("VIX")


def fetch_options_chain(ticker: str, target_dte: int = 90) -> dict:
    """
    Pulls the live options chain for a ticker from Yahoo Finance.
    Returns the expiration date closest to target_dte with full calls + puts data.

    Each row in calls/puts contains:
      strike, bid, ask, lastPrice, impliedVolatility, volume, openInterest, inTheMoney
    """
    from datetime import date, datetime

    t = yf.Ticker(ticker)

    try:
        expirations = t.options
    except Exception:
        return {}

    if not expirations:
        return {}

    today = date.today()

    def dte(exp: str) -> int:
        return (datetime.strptime(exp, "%Y-%m-%d").date() - today).days

    valid = [e for e in expirations if dte(e) > 0]
    if not valid:
        return {}

    # Find the expiry nearest to requested DTE
    best = min(valid, key=lambda e: abs(dte(e) - target_dte))

    try:
        chain = t.option_chain(best)
    except Exception:
        return {}

    return {
        "ticker":       ticker.upper(),
        "expiry":       best,
        "dte":          dte(best),
        "calls":        chain.calls.reset_index(drop=True),
        "puts":         chain.puts.reset_index(drop=True),
        "all_expiries": [(e, dte(e)) for e in valid],
    }


def get_earnings_dates(ticker: str) -> pd.DatetimeIndex:
    """
    Returns historical and upcoming earnings dates for a ticker.
    Earnings are the single biggest source of overnight gap risk for individual stocks.
    Options traders call the post-earnings volatility collapse 'IV crush' —
    implied vol spikes into the print, then collapses after regardless of the result.
    We avoid opening new positions in the days leading up to earnings.
    """
    try:
        t = yf.Ticker(ticker)
        earnings = t.get_earnings_dates(limit=40)
        if earnings is None or earnings.empty:
            return pd.DatetimeIndex([])
        return pd.DatetimeIndex(earnings.index.normalize())
    except Exception:
        return pd.DatetimeIndex([])
