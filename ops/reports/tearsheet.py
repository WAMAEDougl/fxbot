"""Plain-text tearsheet for a backtest or a live period. No plotting deps -
the design note says alerting matters more than dashboards.
"""

from __future__ import annotations

import pandas as pd

from research.metrics import PerfStats


def render(stats: PerfStats, equity: pd.Series, trades: pd.DataFrame, *, title: str = "Tearsheet") -> str:
    lines = [f"{'='*60}", f" {title}", f"{'='*60}"]
    rows = [
        ("Trades", f"{stats.n_trades}"),
        ("Total return", f"{stats.total_return:+.2%}"),
        ("CAGR", f"{stats.cagr:+.2%}"),
        ("Ann. vol", f"{stats.ann_vol:.2%}"),
        ("Sharpe", f"{stats.sharpe:.2f}"),
        ("Sortino", f"{stats.sortino:.2f}"),
        ("Max drawdown", f"{stats.max_drawdown:.2%}"),
        ("Calmar", f"{stats.calmar:.2f}"),
        ("Win rate", f"{stats.win_rate:.1%}"),
        ("Profit factor", f"{stats.profit_factor:.2f}"),
        ("Expectancy / trade", f"{stats.expectancy:+.2f}"),
        ("Expectancy (R)", f"{stats.expectancy_r:+.2f}"),
        ("Avg win / loss", f"{stats.avg_win:+.2f} / {stats.avg_loss:+.2f}"),
        ("Exposure", f"{stats.exposure:.1%}"),
    ]
    for k, v in rows:
        lines.append(f"  {k:<22} {v:>16}")

    if not trades.empty and "exit_reason" in trades:
        lines.append("-" * 60)
        lines.append("  Exits by reason:")
        for reason, n in trades["exit_reason"].value_counts().items():
            lines.append(f"    {reason:<20} {n:>4}")

    if not equity.empty:
        lines.append("-" * 60)
        lines.append(f"  Period: {equity.index[0]}  ->  {equity.index[-1]}")
        lines.append(f"  Equity: {equity.iloc[0]:,.0f}  ->  {equity.iloc[-1]:,.0f}")
    lines.append("=" * 60)
    return "\n".join(lines)
