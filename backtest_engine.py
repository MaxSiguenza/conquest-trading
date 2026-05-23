from dataclasses import dataclass, field
from typing import List, Optional

import numpy as np
import pandas as pd

from config import BacktestConfig, RiskConfig
from risk.manager import RiskManager


@dataclass
class Trade:
    date: pd.Timestamp
    action: str   # "BUY" or "SELL"
    shares: int
    price: float
    value: float
    reason: str   # "signal", "stop_loss", "drawdown_halt"


class BacktestEngine:
    """
    Event-driven backtester that processes one bar at a time.
    Models realistic execution: fills include slippage and commission on both legs.
    """

    def __init__(self, bt_cfg: BacktestConfig, risk_cfg: RiskConfig):
        self.cfg = bt_cfg
        self.risk = RiskManager(
            risk_cfg.max_drawdown_pct,
            risk_cfg.max_position_pct,
            risk_cfg.stop_loss_pct,
        )
        self.cash: float = bt_cfg.initial_capital
        self.shares: int = 0
        self.entry_price: Optional[float] = None
        self.atr_stop_price: Optional[float] = None
        self.trades: List[Trade] = []
        self.equity_curve: List[float] = []

    # ------------------------------------------------------------------ #
    #  Execution helpers                                                   #
    # ------------------------------------------------------------------ #

    def _fill_price(self, price: float, action: str) -> float:
        """Buys fill slightly above market; sells slightly below (slippage)."""
        slip = self.cfg.slippage_pct
        return price * (1 + slip) if action == "BUY" else price * (1 - slip)

    def _commission(self, trade_value: float) -> float:
        return trade_value * self.cfg.commission_pct

    def _buy(self, date: pd.Timestamp, price: float, shares: int) -> None:
        if shares <= 0:
            return
        fill = self._fill_price(price, "BUY")
        total_cost = shares * fill * (1 + self.cfg.commission_pct)

        # If we can't afford the full lot, buy as many as cash allows
        if total_cost > self.cash:
            shares = int(self.cash / (fill * (1 + self.cfg.commission_pct)))
            total_cost = shares * fill * (1 + self.cfg.commission_pct)

        if shares <= 0:
            return

        self.cash -= total_cost
        self.shares += shares
        self.entry_price = fill
        self.trades.append(Trade(date, "BUY", shares, fill, total_cost, "signal"))
        self.atr_stop_price = None  # reset; set by caller after buy if ATR available

    def _sell(self, date: pd.Timestamp, price: float, reason: str = "signal") -> None:
        if self.shares <= 0:
            return
        fill = self._fill_price(price, "SELL")
        proceeds = self.shares * fill * (1 - self.cfg.commission_pct)

        self.cash += proceeds
        self.trades.append(Trade(date, "SELL", self.shares, fill, proceeds, reason))
        self.shares = 0
        self.entry_price = None
        self.atr_stop_price = None

    # ------------------------------------------------------------------ #
    #  Main loop                                                           #
    # ------------------------------------------------------------------ #

    def run(self, df: pd.DataFrame, kelly_pct: float) -> pd.DataFrame:
        """
        Position trading loop — entry and exit are driven by different signals:

        EXIT triggers (any one ends the trade):
          1. Portfolio drawdown exceeds circuit-breaker threshold
          2. Price falls stop_loss_pct below entry price
          3. Death cross: Regime flips to 0 (SMA 50 crosses below SMA 200)

        ENTRY trigger (all must be true):
          4. Regime == 1 (golden cross in effect)
          5. Entry_Signal == 1 (MACD + RSI + VIX + earnings conditions met)

        RSI and MACD do NOT cause exits — they only gate entries.
        This is what prevents the short whipsaw trades.
        """
        for date, row in df.iterrows():
            price        = float(row["Close"])
            regime       = int(row.get("Regime", row.get("Position", 0)))
            entry_signal = int(row.get("Entry_Signal", row.get("Position", 0)))
            atr          = float(row["ATR"]) if "ATR" in row and not pd.isna(row["ATR"]) else None
            portfolio_val = self.cash + self.shares * price

            # Rule 1: drawdown circuit breaker — flat immediately
            if self.risk.check_drawdown(portfolio_val):
                if self.shares > 0:
                    self._sell(date, price, reason="drawdown_halt")
                break

            # Rule 2: ATR-based stop-loss on open position
            if self.shares > 0 and self.risk.stop_loss_triggered(
                    self.entry_price, price, self.atr_stop_price):
                self._sell(date, price, reason="stop_loss")

            # Rule 3: death cross — trend has structurally reversed, exit
            elif self.shares > 0 and regime == 0:
                self._sell(date, price, reason="death_cross")

            # Rule 4: open a new position when regime and entry conditions align
            elif self.shares == 0 and regime == 1 and entry_signal == 1:
                shares = self.risk.position_size(kelly_pct, portfolio_val, price)
                self._buy(date, price, shares)
                # Set ATR stop: 2x ATR below entry — gives the trade room to breathe
                if atr is not None and self.entry_price is not None:
                    self.atr_stop_price = self.entry_price - (2 * atr)

            self.equity_curve.append(self.cash + self.shares * price)

        result = df.iloc[: len(self.equity_curve)].copy()
        result["Equity"] = self.equity_curve
        return result

    # ------------------------------------------------------------------ #
    #  Performance metrics                                                 #
    # ------------------------------------------------------------------ #

    def performance_metrics(self, equity: pd.Series, rf_annual: float = 0.05) -> dict:
        returns = equity.pct_change().dropna()
        if returns.empty:
            return {}

        trading_days = 252
        total_return = (equity.iloc[-1] / equity.iloc[0]) - 1
        ann_return = (1 + total_return) ** (trading_days / len(returns)) - 1
        ann_vol = returns.std() * np.sqrt(trading_days)

        rf_daily = (1 + rf_annual) ** (1 / trading_days) - 1
        excess = returns - rf_daily
        sharpe = excess.mean() / excess.std() * np.sqrt(trading_days) if excess.std() > 0 else 0.0

        downside = returns[returns < 0].std() * np.sqrt(trading_days)
        sortino = (ann_return - rf_annual) / downside if downside > 0 else 0.0

        rolling_max = equity.cummax()
        drawdown = (equity - rolling_max) / rolling_max
        max_dd = drawdown.min()

        calmar = ann_return / abs(max_dd) if max_dd != 0 else 0.0

        win_trades = [t for t in self.trades if t.action == "SELL"]
        # A rough win-rate: compare each sell value against the paired buy
        buy_values = [t.value for t in self.trades if t.action == "BUY"]
        sell_values = [t.value for t in self.trades if t.action == "SELL"]
        pairs = min(len(buy_values), len(sell_values))
        wins = sum(1 for i in range(pairs) if sell_values[i] > buy_values[i])
        win_rate = wins / pairs if pairs > 0 else 0.0

        return {
            "Total Return":    f"{total_return:.2%}",
            "Ann. Return":     f"{ann_return:.2%}",
            "Ann. Volatility": f"{ann_vol:.2%}",
            "Sharpe Ratio":    f"{sharpe:.2f}",
            "Sortino Ratio":   f"{sortino:.2f}",
            "Max Drawdown":    f"{max_dd:.2%}",
            "Calmar Ratio":    f"{calmar:.2f}",
            "Total Trades":    len([t for t in self.trades if t.action == "BUY"]),
            "Win Rate":        f"{win_rate:.2%}",
            "Final Equity":    f"${equity.iloc[-1]:,.2f}",
        }
