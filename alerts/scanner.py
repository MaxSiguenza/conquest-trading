# -*- coding: utf-8 -*-
"""
Watchlist Scanner
=================
Scans a list of tickers and returns signal status for each.
"""
import sys
sys.path.insert(0, ".")

from config import Config, DataConfig
from data.fetcher import fetch_ohlcv, fetch_vix, get_earnings_dates
from signals.generator import generate_signals
from indicators.volatility import calculate_hv_rank, calculate_adx


def scan_ticker(ticker: str) -> dict:
    """
    Scan a single ticker for active signals.
    Signals are computed on end-of-day data; current price is fetched live
    so you can see if today's intraday move is validating or invalidating the signal.
    """
    import yfinance as yf

    try:
        cfg = Config(data=DataConfig(ticker=ticker))
        df  = fetch_ohlcv(ticker, cfg.data.start_date, cfg.data.end_date)
        vix = fetch_vix(cfg.data.start_date, cfg.data.end_date)
        ed  = get_earnings_dates(ticker)
        df  = generate_signals(df, cfg.indicators, vix=vix, earnings_dates=ed)

        hvr    = calculate_hv_rank(df)
        adx_df = calculate_adx(df)
        adx    = float(adx_df["ADX"].iloc[-1]) if not adx_df.empty else 0.0
        last   = df.iloc[-1]

        eod_price = float(last["Close"])  # last closing price used for signals

        # Live quote — shows what the stock is doing RIGHT NOW today
        try:
            fi            = yf.Ticker(ticker).fast_info
            live_price    = float(fi["lastPrice"])
            prev_close    = float(fi["previousClose"]) or eod_price
            today_chg_pct = (live_price / prev_close - 1) * 100
        except Exception:
            live_price    = eod_price
            prev_close    = eod_price
            today_chg_pct = 0.0

        entry_signal  = bool(last.get("Entry_Signal", 0))
        macd_cross_up = bool(last.get("MACD_Cross_Up", 0))

        # Warn if today's price is moving hard AGAINST a bullish signal
        # (e.g. entry signal but stock already down >2% today)
        signal_stale = (entry_signal or macd_cross_up) and today_chg_pct < -2.0

        # TTM Squeeze indicators (from pandas-ta, may be absent)
        sqz_on    = bool(last.get("SQZ_ON",    0))  # spring is loaded — low vol coiling
        sqz_fired = bool(last.get("SQZ_FIRED", 0))  # just broke out of squeeze
        sqz_mom   = float(last.get("SQZ_MOMENTUM", 0) or 0)

        result = {
            "ticker":        ticker,
            "price":         live_price,         # what it's trading at right now
            "eod_price":     eod_price,          # what signal was based on
            "today_chg_pct": today_chg_pct,
            "signal_stale":  signal_stale,
            "mtf_score":     int(last.get("MTF_Score", 0)),
            "entry_signal":  entry_signal,
            "macd_cross_up": macd_cross_up,
            "rsi":           float(last.get("RSI", 50)),
            "adx":           adx,
            "hv_rank":       float(hvr["hv_rank"]),
            "daily":         "BULL" if last.get("Regime",   0) == 1 else "BEAR",
            "weekly":        "BULL" if last.get("W_Regime", 0) == 1 else "BEAR",
            "monthly":       "BULL" if last.get("M_Regime", 0) == 1 else "BEAR",
            "sqz_on":        sqz_on,
            "sqz_fired":     sqz_fired,
            "sqz_momentum":  sqz_mom,
            "error":         None,
        }
        del df, adx_df, vix  # free DataFrames before returning
        return result
    except Exception as exc:
        return {"ticker": ticker, "error": str(exc)}


def scan_watchlist(tickers: list[str]) -> list[dict]:
    """
    Scan all tickers in parallel (up to 6 at once) for speed.
    Results are sorted: entry signals first, then MACD crosses, then MTF score.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    clean = [t.strip() for t in tickers if t.strip()]
    results = []

    with ThreadPoolExecutor(max_workers=6) as pool:
        future_to_ticker = {pool.submit(scan_ticker, t): t for t in clean}
        for future in as_completed(future_to_ticker):
            results.append(future.result())

    results.sort(key=lambda r: (
        0 if r.get("error") else -1,
        -int(r.get("entry_signal", False)),
        -int(r.get("macd_cross_up", False)),
        -int(r.get("mtf_score", 0)),
    ))

    return results
