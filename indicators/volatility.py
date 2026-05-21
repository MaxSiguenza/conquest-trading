# -*- coding: utf-8 -*-
import numpy as np
import pandas as pd


def calculate_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """
    Average True Range (Wilder's method).
    Measures how much a stock moves on a typical day — used to set
    stops that respect each stock's actual volatility instead of a
    fixed percentage.

    A volatile stock like FCX ($3/day swings) needs a wider stop than
    KO ($0.50/day). ATR captures that automatically.
    """
    high       = df["High"]
    low        = df["Low"]
    close_prev = df["Close"].shift(1)

    tr = pd.concat([
        high - low,
        (high - close_prev).abs(),
        (low  - close_prev).abs(),
    ], axis=1).max(axis=1)

    return tr.ewm(span=period, adjust=False).mean().rename("ATR")


def calculate_hv_rank(df: pd.DataFrame, window: int = 30, lookback: int = 252) -> dict:
    """
    Historical Volatility Rank (HVR) — the closest free proxy for IV Rank.

    yfinance doesn't give us historical implied volatility, so we use
    historical volatility (HV) instead. HV and IV are highly correlated —
    when HV spikes, IV almost always spikes with it, and vice versa.

    HVR = where current HV sits within its 1-year high/low range (0-100).

    HVR > 50  vol is elevated   options are expensive  SELL premium  Put Credit Spread
    HVR 30-50 vol is moderate   either type works      check signal strength
    HVR < 30  vol is compressed options are cheap      BUY options   Call Debit Spread

    This tells you which spread type gives better value for money right now.
    """
    log_ret   = np.log(df["Close"] / df["Close"].shift(1)).dropna()
    hv_series = log_ret.rolling(window).std() * np.sqrt(252)
    recent_hv = hv_series.dropna().tail(lookback)

    if len(recent_hv) < 30:
        return {"current_hv": 0.0, "hv_rank": 50.0, "hv_low": 0.0, "hv_high": 0.0}

    current_hv = float(recent_hv.iloc[-1])
    hv_low     = float(recent_hv.min())
    hv_high    = float(recent_hv.max())
    hv_rank    = (current_hv - hv_low) / (hv_high - hv_low) * 100 if hv_high > hv_low else 50.0

    return {
        "current_hv": round(current_hv, 4),
        "hv_rank":    round(hv_rank, 1),
        "hv_low":     round(hv_low, 4),
        "hv_high":    round(hv_high, 4),
    }


def calculate_adx(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """
    Average Directional Index — measures TREND STRENGTH, not direction.
    The golden cross tells you which way. ADX tells you how hard it's moving.

    ADX > 25  strong trend  signal is reliable, ride it
    ADX 20-25 trend forming watch for confirmation
    ADX < 20  weak/choppy   golden cross may be noise, skip or reduce size

    Also returns +DI and -DI (directional indicators).
    +DI > -DI confirms bullish pressure. When that gap widens, momentum is building.
    """
    high  = df["High"]
    low   = df["Low"]
    close = df["Close"]
    prev_high  = high.shift(1)
    prev_low   = low.shift(1)
    prev_close = close.shift(1)

    plus_dm  = (high - prev_high).clip(lower=0)
    minus_dm = (prev_low - low).clip(lower=0)
    # Only keep the larger move; zero out the smaller
    mask = plus_dm >= minus_dm
    plus_dm  = plus_dm.where(mask, 0)
    minus_dm = minus_dm.where(~mask, 0)

    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low  - prev_close).abs(),
    ], axis=1).max(axis=1)

    atr_s     = tr.ewm(span=period, adjust=False).mean()
    plus_di   = 100 * plus_dm.ewm(span=period, adjust=False).mean() / atr_s
    minus_di  = 100 * minus_dm.ewm(span=period, adjust=False).mean() / atr_s
    dx        = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx       = dx.ewm(span=period, adjust=False).mean()

    return pd.DataFrame({"ADX": adx, "Plus_DI": plus_di, "Minus_DI": minus_di})


def calculate_bollinger_bands(df: pd.DataFrame, period: int = 20, std_dev: float = 2.0) -> pd.DataFrame:
    """
    Bollinger Bands + Squeeze detector.

    The SQUEEZE is the key signal here:
    When the bands compress to a 6-month low width, the stock is coiling —
    energy is building for a big move. Combined with a golden cross and high
    ADX, a squeeze breakout is one of the highest conviction setups in technical analysis.

    BB_Width  = how wide the bands are relative to price (lower = more compressed)
    BB_Squeeze = True when width is at or near a 6-month low (breakout is coming)
    BB_Position = where price sits within the bands (0 = lower band, 1 = upper band)
    """
    middle = df["Close"].rolling(period).mean()
    std    = df["Close"].rolling(period).std()
    upper  = middle + std_dev * std
    lower  = middle - std_dev * std
    width  = (upper - lower) / middle

    # Squeeze = width is below its 6-month (126 trading day) 20th percentile
    squeeze = width < width.rolling(126).quantile(0.20)

    # Where is price within the bands? 0 = at lower band, 1 = at upper band
    band_range = (upper - lower).replace(0, np.nan)
    position   = (df["Close"] - lower) / band_range

    return pd.DataFrame({
        "BB_Upper":    upper,
        "BB_Middle":   middle,
        "BB_Lower":    lower,
        "BB_Width":    width,
        "BB_Squeeze":  squeeze,
        "BB_Position": position,
    })


def calculate_obv(df: pd.DataFrame) -> pd.Series:
    """
    On-Balance Volume — tracks where institutional money is actually flowing.

    Raw volume tells you HOW MUCH traded. OBV tells you WHETHER buyers or
    sellers are winning. It adds volume on up days and subtracts on down days.

    Key signal: OBV DIVERGENCE
    - Price rising + OBV falling = distribution (institutions selling into strength)
    - Price rising + OBV rising  = accumulation (institutions buying — trust the move)

    OBV_Trend = OBV above its own 20-day MA = net accumulation phase
    """
    direction = np.sign(df["Close"].diff()).fillna(0)
    obv       = (direction * df["Volume"]).cumsum()
    return obv.rename("OBV")


def spread_recommendation(hv_rank: float, signal_score: int) -> tuple[str, str]:
    """
    Returns (spread_type, reason) based on HVR and signal strength.
    Combines vol regime (HVR) with signal conviction (MTF score).
    """
    if hv_rank >= 50:
        return (
            "Put Credit Spread",
            f"HVR {hv_rank:.0f} -- vol elevated, options expensive, collect premium"
        )
    elif hv_rank < 30:
        return (
            "Call Debit Spread",
            f"HVR {hv_rank:.0f} -- vol compressed, options cheap, buy the move"
        )
    else:
        # Moderate vol -- let signal strength decide
        if signal_score == 3:
            return (
                "Call Debit Spread",
                f"HVR {hv_rank:.0f} -- moderate vol, but 3/3 signal warrants buying upside"
            )
        else:
            return (
                "Put Credit Spread",
                f"HVR {hv_rank:.0f} -- moderate vol + partial signal, collect premium"
            )
