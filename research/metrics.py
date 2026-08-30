"""Performance statistics on an equity curve / trade list."""

from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd

TRADING_DAYS = 252


@dataclass(frozen=True, slots=True)
class PerfStats:
    n_trades: int
    total_return: float
    cagr: float
    ann_vol: float
    sharpe: float
    sortino: float
    max_drawdown: float
    calmar: float
    win_rate: float
    profit_factor: float
    expectancy: float  # mean P/L per trade, account ccy
    expectancy_r: float  # mean P/L per trade in R multiples
    avg_win: float
    avg_loss: float
    exposure: float  # fraction of bars with a position

    def as_dict(self) -> dict:
        return asdict(self)


def _returns_from_equity(equity: pd.Series) -> pd.Series:
    return equity.pct_change().dropna()


def equity_stats(
    equity: pd.Series, *, periods_per_year: int = TRADING_DAYS, rf: float = 0.0
) -> dict:
    equity = equity.astype(float)
    if len(equity) < 3:
        return {"cagr": 0.0, "ann_vol": 0.0, "sharpe": 0.0, "sortino": 0.0,
                "max_drawdown": 0.0, "total_return": 0.0, "calmar": 0.0}
    r = _returns_from_equity(equity)
    total_return = equity.iloc[-1] / equity.iloc[0] - 1.0
    years = len(r) / periods_per_year
    cagr = (equity.iloc[-1] / equity.iloc[0]) ** (1 / years) - 1.0 if years > 0 else 0.0
    ann_vol = r.std(ddof=1) * np.sqrt(periods_per_year)
    excess = r - rf / periods_per_year
    sharpe = (excess.mean() / r.std(ddof=1) * np.sqrt(periods_per_year)) if r.std(ddof=1) > 0 else 0.0
    downside = r[r < 0].std(ddof=1)
    sortino = (excess.mean() / downside * np.sqrt(periods_per_year)) if downside and downside > 0 else 0.0

    running_max = equity.cummax()
    dd = equity / running_max - 1.0
    max_dd = dd.min()
    calmar = cagr / abs(max_dd) if max_dd < 0 else 0.0

    return {
        "total_return": float(total_return),
        "cagr": float(cagr),
        "ann_vol": float(ann_vol),
        "sharpe": float(sharpe),
        "sortino": float(sortino),
        "max_drawdown": float(max_dd),
        "calmar": float(calmar),
    }


def trade_stats(trades: pd.DataFrame) -> dict:
    """``trades`` needs columns: pnl (account ccy), r_multiple (optional)."""
    if trades.empty:
        return {"n_trades": 0, "win_rate": 0.0, "profit_factor": 0.0,
                "expectancy": 0.0, "expectancy_r": 0.0, "avg_win": 0.0, "avg_loss": 0.0}
    pnl = trades["pnl"].astype(float)
    wins = pnl[pnl > 0]
    losses = pnl[pnl < 0]
    gross_win = wins.sum()
    gross_loss = -losses.sum()
    return {
        "n_trades": int(len(trades)),
        "win_rate": float(len(wins) / len(trades)),
        "profit_factor": float(gross_win / gross_loss) if gross_loss > 0 else float("inf"),
        "expectancy": float(pnl.mean()),
        "expectancy_r": float(trades["r_multiple"].mean()) if "r_multiple" in trades else 0.0,
        "avg_win": float(wins.mean()) if len(wins) else 0.0,
        "avg_loss": float(losses.mean()) if len(losses) else 0.0,
    }


def summarise(
    equity: pd.Series,
    trades: pd.DataFrame,
    *,
    exposure: float = 0.0,
    periods_per_year: int = TRADING_DAYS,
) -> PerfStats:
    e = equity_stats(equity, periods_per_year=periods_per_year)
    t = trade_stats(trades)
    return PerfStats(
        n_trades=t["n_trades"],
        total_return=e["total_return"],
        cagr=e["cagr"],
        ann_vol=e["ann_vol"],
        sharpe=e["sharpe"],
        sortino=e["sortino"],
        max_drawdown=e["max_drawdown"],
        calmar=e["calmar"],
        win_rate=t["win_rate"],
        profit_factor=t["profit_factor"],
        expectancy=t["expectancy"],
        expectancy_r=t["expectancy_r"],
        avg_win=t["avg_win"],
        avg_loss=t["avg_loss"],
        exposure=exposure,
    )


def returns_series(equity: pd.Series) -> pd.Series:
    return _returns_from_equity(equity.astype(float))
