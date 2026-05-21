def kelly_criterion(win_prob: float, win_loss_ratio: float, fraction: float = 0.25) -> float:
    """
    Fractional Kelly position sizing.

    win_prob       — probability of a winning trade (e.g. 0.55 = 55% win rate)
    win_loss_ratio — average win / average loss (e.g. 1.5 means you win $1.50 per $1 lost)
    fraction       — Kelly multiplier. Full Kelly (1.0) maximizes growth but drawdowns
                     are brutal. Quarter-Kelly (0.25) is standard at most hedge funds —
                     you give up some compounding in exchange for much smoother equity curve.

    Returns the fraction of portfolio to risk. Returns 0.0 when edge is negative
    (never bet when the math says you have no edge).
    """
    if win_loss_ratio <= 0:
        raise ValueError("win_loss_ratio must be positive")

    full_kelly = (win_prob * (win_loss_ratio + 1) - 1) / win_loss_ratio
    return max(full_kelly * fraction, 0.0)
