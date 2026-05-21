import pandas as pd
from indicators.rsi import calculate_rsi
from indicators.macd import calculate_macd


def add_weekly_signals(df: pd.DataFrame) -> pd.DataFrame:
    """
    Resamples daily data to weekly candles, computes indicators on that
    timeframe, then forward-fills the values back to every trading day.

    Weekly SMA: 10-week / 20-week cross
      10 weeks = ~50 trading days  (medium-term momentum)
      20 weeks = ~100 trading days (intermediate trend)

    Forward-filling means Monday–Friday of a given week all carry the
    signal computed from last Friday's close — no look-ahead bias.
    """
    weekly = df["Close"].resample("W").last().dropna()

    w_sma10 = weekly.rolling(10).mean()
    w_sma20 = weekly.rolling(20).mean()
    w_rsi   = calculate_rsi(weekly, period=14)
    w_macd  = calculate_macd(weekly, short=6, long=13, signal=4)

    weekly_df = pd.DataFrame({
        "W_SMA10":      w_sma10,
        "W_SMA20":      w_sma20,
        "W_RSI":        w_rsi,
        "W_MACD":       w_macd["MACD_Line"],
        "W_MACD_Sig":   w_macd["MACD_Signal"],
        "W_Regime":     (w_sma10 > w_sma20).astype(int),
    }, index=weekly.index)

    return pd.concat([df, weekly_df.reindex(df.index, method="ffill")], axis=1)


def add_monthly_signals(df: pd.DataFrame) -> pd.DataFrame:
    """
    Resamples daily data to monthly candles.

    Monthly SMA: 6-month / 12-month cross
      6 months  = ~126 trading days (half-year momentum)
      12 months = ~252 trading days (full-year trend — the big picture)

    This is the slowest-moving filter. When the 6-month average crosses
    above the 12-month, a major multi-month uptrend is confirmed.
    This only changes a few times per year — when it's bullish, it means
    the macro backdrop is supportive.
    """
    try:
        monthly = df["Close"].resample("ME").last().dropna()
    except ValueError:
        monthly = df["Close"].resample("M").last().dropna()

    m_sma6  = monthly.rolling(6).mean()
    m_sma12 = monthly.rolling(12).mean()
    m_rsi   = calculate_rsi(monthly, period=10)
    m_macd  = calculate_macd(monthly, short=3, long=6, signal=2)

    monthly_df = pd.DataFrame({
        "M_SMA6":       m_sma6,
        "M_SMA12":      m_sma12,
        "M_RSI":        m_rsi,
        "M_MACD":       m_macd["MACD_Line"],
        "M_MACD_Sig":   m_macd["MACD_Signal"],
        "M_Regime":     (m_sma6 > m_sma12).astype(int),
    }, index=monthly.index)

    return pd.concat([df, monthly_df.reindex(df.index, method="ffill")], axis=1)
