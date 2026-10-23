# Hypothesis: short-term mean reversion in broad equity-index ETFs

Written BEFORE any backtest result was seen (design 5.1 step 1). Do not edit
after the fact; add a dated note below instead.

**Claim.** After a sharp multi-day sell-off inside a long-term uptrend, broad
equity indices tend to rebound over the following days. Short-term reversal is
documented for stocks (Jegadeesh 1990, "Evidence of Predictable Behavior of
Security Returns", JF; Lehmann 1990, "Fads, Martingales, and Market Efficiency",
QJE), and Nagel (2012, "Evaporating Liquidity", RFS) argues reversal returns
compensate liquidity providers who absorb forced selling, with larger payoffs
when volatility is high. The index-ETF version is mostly practitioner evidence
(for example Connors and Alvarez's RSI(2) work), which is a weaker foundation
than the academic work above; that is stated up front.

**Rule (fixed before testing).** Long only: buy when the 2-period RSI is below
`rsi_entry` while the close is above its 200-day average. Exit when the close
recovers above its `exit_window`-day average, or after 10 bars, whichever is
first. Long-only is chosen a priori: the documented effect is buying panic
selling, and shorting overbought indices fights equity drift. Universe: the five
broad equity ETFs in the lake (SPY, QQQ, IWM, EFA, EEM); bonds, gold, commodities
and real estate are excluded because the mechanism is about equity liquidity.

**Why it might survive costs here.** Liquid ETFs (about 1-2 bps spread), a
commission-free broker, few days held per trade, and a diversified set of five
indices that sell off at partly different times.

**Why it might fail here (stated up front).**
- The effect is widely known and may have decayed or been arbitraged.
- 2016-2026 is a strong bull market with sharp V-shaped dips, which flatters
  buying dips; a slow grinding bear market (2000-2002, 2008) is not in the sample.
- A time stop and a wide disaster stop may cut off the rebound; the mandatory
  stop-loss conflicts with a strategy that profits from averaging into weakness.
- Few positions open at once, so returns are small in absolute terms.

**Falsification.** Reject if any check in design 5.3 fails. Benchmark: buy-and-hold
of the same five ETFs. Because this strategy is invested only a small fraction of
the time, compare Sharpe and drawdown, not raw return.

**Parameters (kept few on purpose).** `rsi_entry` in {5, 10, 15} and `exit_window`
in {5, 10}: six cells. Everything else is fixed (`trend_window` 200, RSI period 2,
`max_hold_bars` 10, stop 3 ATR, long-only). Every parameter set is appended to
`research/trials/etf_reversion.jsonl`. This work is a SECOND hypothesis on the same
data after etf_trend (18 trials), so the validation is run with `--extra-trials 18`:
selection across hypotheses is itself a multiple-testing problem.

---
## Result notes (appended; the text above is unchanged)

**2026-10-23, run 1 (6 trials; validated with `--extra-trials 18` = 24 counted, because etf_trend's
18 trials were run on the same data). Corrected backtester (margin counts open positions, 4-day
order TTL).** Best config rsi_entry 15 / exit_window 5: CAGR +2.4%, Sharpe 0.80, max DD 5.4%,
581 trades, win rate 67%, expectancy +0.17R (avg win $194, avg loss -$240), invested about 43% of
the time. Benchmark equal-weight B&H of the same five ETFs: +13.0% / Sharpe 0.76 / DD 33.3%.
REJECT: DSR 0.921 (needs > 0.95), PBO 0.508 (needs < 0.5); worst-path DD 6.75% and every CPCV
path positive. P&L is long-only by construction: SPY +$10.1k, QQQ +$9.6k, EFA +$6.2k, IWM +$3.1k,
EEM about $0.
Honest read: a real but small effect with very low drawdown and a Sharpe in line with
buy-and-hold, but too little evidence to rule out luck once all 24 trials are counted.
Scaling position size would not help: Sharpe, DSR and PBO are scale-invariant.
Not counted as a pass by leaving out the 18 earlier trials; that would be the cheat 5.1 warns about.
