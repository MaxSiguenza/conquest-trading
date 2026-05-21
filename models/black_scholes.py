from math import exp, log, sqrt

from scipy.stats import norm


def black_scholes_price(
    S: float,
    K: float,
    T: float,
    r: float,
    sigma: float,
    option_type: str = "call",
) -> float:
    """
    S     — current stock price
    K     — strike price
    T     — time to expiration in years (e.g. 30/365 for 30 days)
    r     — risk-free rate (e.g. 0.05 for 5%)
    sigma — implied volatility (e.g. 0.25 for 25%)
    """
    if T <= 0:
        # At expiration: intrinsic value only, no time premium left
        return max(S - K, 0.0) if option_type == "call" else max(K - S, 0.0)

    d1 = (log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * sqrt(T))
    d2 = d1 - sigma * sqrt(T)

    if option_type == "call":
        return S * norm.cdf(d1) - K * exp(-r * T) * norm.cdf(d2)
    return K * exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)


def option_greeks(S: float, K: float, T: float, r: float, sigma: float) -> dict:
    """
    The Greeks measure how sensitive the option price is to each input.

    Delta — how much the option moves per $1 move in the stock
    Gamma — how much Delta itself changes (convexity)
    Theta — daily time decay (options lose value every day)
    Vega  — how much the option moves per 1% change in implied vol
    """
    if T <= 0:
        return {"delta": 1.0 if S > K else 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}

    d1 = (log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * sqrt(T))
    d2 = d1 - sigma * sqrt(T)

    delta = norm.cdf(d1)
    gamma = norm.pdf(d1) / (S * sigma * sqrt(T))
    theta = (-(S * norm.pdf(d1) * sigma) / (2 * sqrt(T)) - r * K * exp(-r * T) * norm.cdf(d2)) / 365
    vega = S * norm.pdf(d1) * sqrt(T) / 100  # per 1% change in vol

    return {"delta": delta, "gamma": gamma, "theta": theta, "vega": vega}
