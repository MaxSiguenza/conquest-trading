from typing import Optional


class RiskManager:
    """
    Enforces three hard risk rules on every bar:
      1. Drawdown circuit breaker  — halt trading if portfolio falls too far from its peak
      2. Position size cap         — Kelly can suggest large bets; we cap them
      3. Per-trade stop-loss       — exit any position that moves against us by stop_loss_pct
    """

    def __init__(
        self,
        max_drawdown_pct: float,
        max_position_pct: float,
        stop_loss_pct: float,
    ):
        self.max_drawdown_pct = max_drawdown_pct
        self.max_position_pct = max_position_pct
        self.stop_loss_pct = stop_loss_pct
        self._peak: Optional[float] = None

    def check_drawdown(self, portfolio_value: float) -> bool:
        """Returns True if the drawdown limit has been breached — trading must halt."""
        if self._peak is None or portfolio_value > self._peak:
            self._peak = portfolio_value
        drawdown = (self._peak - portfolio_value) / self._peak
        return drawdown >= self.max_drawdown_pct

    def position_size(self, kelly_pct: float, portfolio_value: float, price: float) -> int:
        """
        Converts Kelly percentage into a share count.
        Caps the percentage at max_position_pct regardless of what Kelly says.
        """
        pct = min(kelly_pct, self.max_position_pct)
        capital_to_deploy = portfolio_value * pct
        return int(capital_to_deploy / price)

    def stop_loss_triggered(self, entry_price: float, current_price: float,
                            atr_stop_price: float = None) -> bool:
        """
        Returns True if stop is triggered.
        If atr_stop_price is provided, uses that level (2x ATR below entry).
        Falls back to fixed percentage stop if ATR wasn't available at entry.
        ATR stops are smarter — they give volatile stocks more room and tight
        stocks less, rather than treating FCX and KO identically.
        """
        if atr_stop_price is not None:
            return current_price < atr_stop_price
        return current_price < entry_price * (1 - self.stop_loss_pct)
