import sys
sys.path.insert(0, ".")

from config import Config
from data.fetcher import fetch_ohlcv
from signals.generator import generate_signals
from models.black_scholes import black_scholes_price, option_greeks
from models.kelly_criterion import kelly_criterion
from backtest.engine import BacktestEngine

cfg = Config()
print("Config OK")

df = fetch_ohlcv(cfg.data.ticker, cfg.data.start_date, cfg.data.end_date)
print(f"Data OK: {len(df)} rows")

df = generate_signals(df, cfg.indicators)
long_days = (df["Position"] == 1).sum()
print(f"Signals OK: {long_days} long days of {len(df)}")

kelly_pct = kelly_criterion(0.55, 1.5, cfg.risk.kelly_fraction)
print(f"Kelly OK: {kelly_pct:.2%} per trade")

engine = BacktestEngine(cfg.backtest, cfg.risk)
result = engine.run(df.copy(), kelly_pct)
metrics = engine.performance_metrics(result["Equity"])
print("\n--- Performance ---")
for k, v in metrics.items():
    print(f"  {k}: {v}")

S = float(df["Close"].iloc[-1])
p = black_scholes_price(S, S * 1.05, 30 / 365, 0.05, 0.25)
g = option_greeks(S, S * 1.05, 30 / 365, 0.05, 0.25)
print(f"\nOptions OK — price=${p:.4f}, delta={g['delta']:.4f}")
print("\nALL SYSTEMS GO")
