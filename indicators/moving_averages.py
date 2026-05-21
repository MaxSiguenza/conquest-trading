import pandas as pd


def add_moving_averages(df: pd.DataFrame, short: int = 20, long: int = 50) -> pd.DataFrame:
    df = df.copy()
    df["SMA_Short"] = df["Close"].rolling(short).mean()
    df["SMA_Long"] = df["Close"].rolling(long).mean()
    df["EMA_Short"] = df["Close"].ewm(span=short, adjust=False).mean()
    df["EMA_Long"] = df["Close"].ewm(span=long, adjust=False).mean()
    return df
