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

---
## PRE-REGISTRATION: out-of-sample replication (written 2026-10-23, BEFORE pulling or running any of this data)

**Why.** Run 1 failed the DSR/PBO bar only narrowly, and its real constraint is evidence (10 years,
5 ETFs). Alpaca's free history starts in 2016, so a longer history is not available. What IS
available is a cross-sectional replication on ETFs the rule was never tuned or examined on.

**Frozen configuration (no re-tuning on the new data, ever):** `rsi_entry` 15, `exit_window` 5,
everything else exactly as in configs/etf_reversion.yaml (long-only, trend_window 200, RSI(2),
max_hold 10, stop 3 ATR). This is ONE trial on the new universe.

**Universe B (all included, whatever the outcome):** DIA, MDY, XLB, XLE, XLF, XLI, XLK, XLP,
XLU, XLV, XLY, EWJ, EWG, EWU: US large/mid caps, the nine sector ETFs with full history,
and three developed-market country ETFs. None of these were used in run 1.

**Pass criteria, fixed now:** all of (1) the repo's objective checks (DSR > 0.95 with n_trials = 1,
every CPCV path positive, worst-path DD within 26%, not regime-concentrated, >= 100 trades;
PBO is not applicable to a single frozen configuration), AND (2) Sharpe >= 0.5, AND (3) profitable
on a majority (>= 60%) of the symbols. Anything else is a failed replication.

**Limits stated in advance.** Same 2016-2026 period and a strongly correlated equity market, so
this is NOT an independent sample in time; it tests breadth, not regime. A pass would raise
confidence but would not remove the bull-market caveat. The paper-trading stage is still required.

---
## RESULT of the pre-registered replication (2026-10-23)

Frozen config (rsi_entry 15, exit_window 5) run once on the 14 pre-registered ETFs, 1 trial.
- Sharpe **0.75**, CAGR +4.3%, max DD 11.1%, 1,561 trades, win rate 66%, expectancy +0.10R.
  Equal-weight B&H of the same 14: +12.1% / Sharpe 0.78 / DD 36.7%.
- DSR **0.993** (n_trials = 1); PBO n/a (single configuration); every CPCV path positive;
  worst-path DD 11.1% (tol 26%); >= 100 trades.
- **14 of 14 symbols profitable** (smallest XLE +$275, largest XLK +$8.2k).
- By year: +1.5, +15.9, -6.8, +6.2, +3.9, +9.7, -1.4, +0.4, +0.7, +12.0, +6.0 % (2016..2026);
  9 up, 2 down. Yearly win rate 57-76%.
- **FAIL: not_regime_concentrated.** max/median of yearly expectancy = 3.79 (limit 3). Two years
  (2017 and 2025) supply about 58% of the cumulative yearly return.

**Verdict under the pre-registered rule: FAILED REPLICATION** (criterion 1 requires every
objective check). Criteria 2 (Sharpe >= 0.5) and 3 (>= 60% of symbols profitable) were met.
This is not rescued by redefining the check afterwards. Any change to the regime check may only
apply to FUTURE runs and must re-score earlier results (etf_trend, etf_reversion run 1) the same way.

**Honest read.** The strongest evidence so far that the effect is real: it replicates, with a
similar Sharpe (0.75 vs 0.80), on 14 different ETFs, every one profitable, with a small
drawdown. What it does not show: independence in time (same 2016-2026 bull market), a
Sharpe above buy-and-hold (0.75 vs 0.78), or stability across regimes (profit clustered in
2017 and 2025; 2018 was -6.8%). Absolute return is small (+4.3%/yr at 0.5% risk per trade).

---
## FORWARD PAPER TEST (exploratory), plan written 2026-10-23 BEFORE it starts

**Status.** The strategy was NOT promoted (failed the pre-registered regime check). This forward
test is an owner-approved EXPLORATION on a paper account with no real money. It does not count as
rollout stage 2 and does not change the verdict above.

**What runs.** The frozen replication config (configs/etf_reversion_oos.yaml: 14 ETFs, rsi_entry 15,
exit_window 5, long-only), on Alpaca paper, fed SIP prices adjusted for splits and dividends (the
same data kind as the research). No parameter is changed during the test.

**What it can tell us.** (1) Does the plumbing work: queued after-hours orders, bracket entries, the
reduce-only close that cancels and restores protective legs, margin gating, fills and reconcile?
(2) Do live signals match the backtest on the same bars? (3) Is realised slippage close to the
modelled 1.5 bps spread plus impact? It cannot, in a few months, prove the strategy is profitable:
expect about 12 trades a month, so roughly 40 trades in 3 months.

**Review at** >= 3 months AND >= 100 closed trades (whichever is later). Consistent with the
backtest means: win rate within 55-75% (backtest 66%), mean trade expectancy > 0, realised slippage
no worse than 2x modelled, and no unreconciled position drift.

**Stop immediately if:** any position is ever left without protective orders (the adapter raises
"CRITICAL: close ... AND protection could not be restored"), the kill switch trips for a reason
other than market stress, or the account draws down more than 15%.
