import sys
sys.path.insert(0, ".")

from config import Config, DataConfig
from data.fetcher import fetch_ohlcv, fetch_vix, get_earnings_dates
from signals.generator import generate_signals
from models.black_scholes import black_scholes_price, option_greeks

tickers = ["PLTR", "AAPL", "NVDA", "SPY", "TSLA", "MSFT"]

print(f"\n{'Ticker':<8} {'Price':>8} {'Regime':>8} {'Entry?':>8} {'SMA50':>8} {'SMA200':>8}  Signal")
print("-" * 70)

vix = None
for ticker in tickers:
    try:
        cfg = Config(data=DataConfig(ticker=ticker))
        df = fetch_ohlcv(ticker, cfg.data.start_date, cfg.data.end_date)
        if vix is None:
            from data.fetcher import fetch_vix
            vix = fetch_vix(cfg.data.start_date, cfg.data.end_date)
        earnings = get_earnings_dates(ticker)
        df = generate_signals(df, cfg.indicators, vix=vix, earnings_dates=earnings)

        last = df.iloc[-1]
        price   = last["Close"]
        regime  = int(last["Regime"])
        entry   = int(last["Entry_Signal"])
        sma50   = last["SMA_Short"]
        sma200  = last["SMA_Long"]

        if regime == 1 and entry == 1:
            signal = "BUY  <<<"
        elif regime == 1 and entry == 0:
            signal = "HOLD (already in trend, wait for pullback)"
        else:
            signal = "FLAT (death cross — stay out)"

        print(f"{ticker:<8} ${price:>7.2f} {regime:>8} {entry:>8} {sma50:>8.2f} {sma200:>8.2f}  {signal}")
    except Exception as e:
        print(f"{ticker:<8} ERROR: {e}")

print("-" * 70)
print(f"\nVIX right now: {vix.iloc[-1]:.1f}  ({'CALM - ok to trade' if vix.iloc[-1] < 20 else 'ELEVATED - be cautious' if vix.iloc[-1] < 30 else 'HIGH - stay out'})")
print("\nRegime: 1=golden cross (uptrend)  0=death cross (downtrend)")
print("Entry:  1=good entry point now    0=wait")
