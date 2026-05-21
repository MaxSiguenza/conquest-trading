from dataclasses import dataclass, field
from datetime import date


@dataclass
class DataConfig:
    ticker: str = "AAPL"
    start_date: str = "2020-01-01"   # Extended back — SMA 200 needs ~10 months of warmup
    end_date: str = str(date.today())  # Always pulls up to today


@dataclass
class IndicatorConfig:
    # Golden cross / death cross — the most watched institutional signal on the planet
    sma_short: int = 50
    sma_long: int = 200

    # RSI at period 21 is smoother and more appropriate for multi-week holds
    rsi_period: int = 21
    rsi_oversold: float = 30.0
    rsi_overbought: float = 70.0

    # Standard MACD — works well on daily data regardless of hold duration
    macd_short: int = 12
    macd_long: int = 26
    macd_signal: int = 9

    # VIX above this level = market panic / macro risk event in progress → stay flat
    vix_threshold: float = 30.0

    # Don't open a NEW position this many days before a known earnings date
    earnings_buffer_days: int = 5


@dataclass
class RiskConfig:
    kelly_fraction: float = 0.25       # Quarter-Kelly — standard at most funds to cut variance
    max_position_pct: float = 0.20     # Never put more than 20% in one position
    max_drawdown_pct: float = 0.15     # Halt all trading if portfolio drops 15% from peak
    stop_loss_pct: float = 0.08        # Widened to 8% — longer holds need more room to breathe


@dataclass
class OptionsConfig:
    dte: int = 90                      # Days to expiration — 2-3x the expected hold time
    otm_pct: float = 0.05             # How far out-of-the-money (5% above current price)
    risk_free_rate: float = 0.05
    implied_vol: float = 0.25


@dataclass
class BacktestConfig:
    initial_capital: float = 100_000.0
    commission_pct: float = 0.001      # 0.1% per trade (realistic for retail/small fund)
    slippage_pct: float = 0.0005       # 0.05% slippage — market impact on fills


@dataclass
class Config:
    data: DataConfig = field(default_factory=DataConfig)
    indicators: IndicatorConfig = field(default_factory=IndicatorConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    options: OptionsConfig = field(default_factory=OptionsConfig)
    backtest: BacktestConfig = field(default_factory=BacktestConfig)
