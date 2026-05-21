import pandas as pd


def calculate_macd(
    prices: pd.Series,
    short: int = 12,
    long: int = 26,
    signal: int = 9,
) -> pd.DataFrame:
    """Returns full MACD history as a DataFrame — not just the last value."""
    short_ema = prices.ewm(span=short, adjust=False).mean()
    long_ema = prices.ewm(span=long, adjust=False).mean()

    macd_line = short_ema - long_ema
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram = macd_line - signal_line

    return pd.DataFrame(
        {"MACD_Line": macd_line, "MACD_Signal": signal_line, "MACD_Hist": histogram},
        index=prices.index,
    )
