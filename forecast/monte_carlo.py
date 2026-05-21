import numpy as np
import pandas as pd


def run_monte_carlo(
    prices: pd.Series,
    days: int = 120,
    simulations: int = 1000,
) -> dict:
    """
    Simulates future price paths using Geometric Brownian Motion (GBM).

    GBM is the same math Black-Scholes uses — it says tomorrow's price equals
    today's price multiplied by a random return drawn from a normal distribution.
    The mean and volatility of that distribution come from the stock's own history.

    Running 1,000 of these paths forward gives you a probability cloud:
    some paths go up, some go down, and the spread between them tells you
    how uncertain the future is. Wide cone = volatile stock. Narrow cone = stable.

    Important limitation: GBM does not know about earnings, Fed decisions,
    or any future news. It only knows historical volatility. Use it as a
    probability framework, not a price prediction.
    """
    log_returns = np.log(prices / prices.shift(1)).dropna()
    mu    = log_returns.mean()
    sigma = log_returns.std()

    last_price = float(prices.iloc[-1])

    # Daily random shocks for all simulations at once — vectorized for speed
    shocks = np.random.normal(
        loc   = (mu - 0.5 * sigma ** 2),   # drift adjusted for GBM
        scale = sigma,
        size  = (simulations, days),
    )

    # Each path: cumulative product of daily returns starting from last price
    price_paths = last_price * np.exp(np.cumsum(shocks, axis=1))

    # Future business-day date index
    future_dates = pd.bdate_range(start=prices.index[-1], periods=days + 1)[1:]

    # Probability the stock is above the current price at each future day
    prob_above = (price_paths > last_price).mean(axis=0)

    return {
        "future_dates":   future_dates,
        "last_price":     last_price,
        "sigma_annual":   sigma * np.sqrt(252),
        "mu_annual":      mu    * 252,
        "price_paths":    price_paths,          # full (simulations × days) array
        "p10":  np.percentile(price_paths, 10,  axis=0),
        "p25":  np.percentile(price_paths, 25,  axis=0),
        "p50":  np.percentile(price_paths, 50,  axis=0),   # median path
        "p75":  np.percentile(price_paths, 75,  axis=0),
        "p90":  np.percentile(price_paths, 90,  axis=0),
        "prob_above_today": prob_above,
    }


def forecast_summary(mc: dict, days_out: int = 90) -> dict:
    """
    Pulls key stats at a specific day horizon from the simulation results.
    Used to populate the annotation box on the chart.
    """
    idx = min(days_out - 1, len(mc["p50"]) - 1)
    return {
        "horizon_days": days_out,
        "current":      mc["last_price"],
        "bear":         mc["p10"][idx],
        "low":          mc["p25"][idx],
        "median":       mc["p50"][idx],
        "high":         mc["p75"][idx],
        "bull":         mc["p90"][idx],
        "prob_up":      mc["prob_above_today"][idx],
        "implied_vol":  mc["sigma_annual"],
    }
