import sys
sys.path.insert(0, ".")

from config import Config
from data.fetcher import fetch_ohlcv, fetch_vix, get_earnings_dates
from signals.generator import generate_signals
from models.kelly_criterion import kelly_criterion
from backtest.engine import BacktestEngine

cfg = Config()
df = fetch_ohlcv(cfg.data.ticker, cfg.data.start_date, cfg.data.end_date)
vix = fetch_vix(cfg.data.start_date, cfg.data.end_date)
earnings_dates = get_earnings_dates(cfg.data.ticker)

df = generate_signals(df, cfg.indicators, vix=vix, earnings_dates=earnings_dates)
kelly_pct = kelly_criterion(0.55, 1.5, cfg.risk.kelly_fraction)

engine = BacktestEngine(cfg.backtest, cfg.risk)
engine.run(df.copy(), kelly_pct)

buys  = [t for t in engine.trades if t.action == "BUY"]
sells = [t for t in engine.trades if t.action == "SELL"]

print(f"{'#':<4} {'Entry':>12} {'Exit':>12} {'Days':>6} {'P&L':>10}  Exit reason")
print("-" * 60)

hold_days = []
for i, (b, s) in enumerate(zip(buys, sells)):
    days = (s.date - b.date).days
    pnl = s.value - b.value
    hold_days.append(days)
    flag = " *** STOP LOSS" if s.reason == "stop_loss" else (
           " *** DRAWDOWN HALT" if s.reason == "drawdown_halt" else
           f" [{s.reason}]")
    print(f"{i+1:<4} {str(b.date.date()):>12} {str(s.date.date()):>12} {days:>6} {pnl:>+10.2f}{flag}")

print("-" * 60)
if hold_days:
    print(f"\nTotal trades    : {len(hold_days)}")
    print(f"Average hold    : {sum(hold_days)/len(hold_days):.0f} days  ({sum(hold_days)/len(hold_days)/21:.1f} months)")
    print(f"Median hold     : {sorted(hold_days)[len(hold_days)//2]} days")
    print(f"Shortest hold   : {min(hold_days)} days")
    print(f"Longest hold    : {max(hold_days)} days  ({max(hold_days)/21:.1f} months)")
else:
    print("No completed trades.")
