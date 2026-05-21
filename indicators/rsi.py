import pandas as pd


def calculate_rsi(prices: pd.Series, period: int = 14) -> pd.Series:
    """
    Wilder's RSI using exponential smoothing (alpha=1/period).
    This matches Bloomberg/industry standard — NOT the naive rolling-mean version.
    """
    delta = prices.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()

    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))
