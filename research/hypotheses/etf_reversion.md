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

---
## Forward-test log

**2026-10-27: first live trade, and a bug found before the first close.** The strategy bought
144 EWJ (about $94.41, filled in 7 pieces) as a bracket (stop 90.66, take-profit 128.52). Inspecting
the real position showed Alpaca lists only the take-profit leg under `status=open`; the stop leg is
`held` and appears only in the nested listing. The close code (written against a simplified fake)
would have cancelled one leg, never captured the stop, and on a failed close could have restored
only half the protection, which breaks the stop-immediately rule above. Fixed BEFORE any close:
legs are now found through the nested listing (new/held, exit side only; finished legs ignored),
with tests built from the real structure. Verified read-only on the live position (finds both legs),
engine restarted on the fix, position adopted and still protected.
Note: the strategy's time-stop clock (10 bars) restarts when the engine restarts, so EWJ's
time-stop is counted from 2026-10-27 at the latest; the recovery exit is unaffected.
One transient warning so far: a DNS failure in the fill poller (2026-10-27 08:40 UTC), retried.

**2026-10-29: STOP CONDITION TRIGGERED; position left naked for ~26 hours; root cause fixed.**
The first real close (EWJ, 28 Oct 04:00 UTC, market closed) failed. The old close cancelled the
stop and target first. Alpaca cancelled the held stop at once but deferred cancelling the live
take-profit until the 08:00 UTC pre-market, so the shares stayed locked, the sell could not be
placed, and the restore attempt failed for the same reason. The error text contained
"insufficient_funds", which the OMS treated as final (no retry). At 08:00 the deferred cancel
completed and EWJ had no orders at all until 29 Oct 06:50 UTC, when the original stop (90.66) and
target (128.52) were re-placed as an OCO. Paper money, price roughly flat, but it is precisely the
condition the stop rule above exists for, and the earlier warning that "the first real close is
untested against Alpaca" was borne out within two days.
Fixes (all with tests, 162 passing): (1) a close never removes protection first: it sells THROUGH
the take-profit leg by moving that leg to a marketable limit (stop stays live; OCO cancels it on
the fill); no protective orders -> plain market sell; only a stop -> refuse and leave untouched;
(2) if the broker cannot amend the order yet (an after-hours order is "accepted" and not
amendable) the close waits, retried in the background for ~10 hours without blocking market data;
(3) an order that runs out of retries now raises a visible rejection (was silent); (4) the hourly
watchdog alerts on any open position with no stop, and on any live exit order with no position
(orphan); (5) scripts/close_position.py closes one position safely by hand.
Kill switch engaged 29 Oct 05:40 UTC while this was fixed. The real close of EWJ is being run
through the new path (replace on the first amendable moment, fill at the 13:30 UTC open).

**2026-11-05: false kill-switch trip, one buy signal missed.** On 4 Nov 02:28 UTC the reconciler
halted the engine on "equity drift" (-500) with every position matching. Cause: local equity is
re-marked once per daily bar while the broker marks live, so a normal 1-3% day exceeded the 0.5%
tolerance. The trip latched, and the XLE buy at 5 Nov 05:00 (RSI 5) was rejected; exits kept working
and no position was ever unprotected. This counts as a kill-switch trip NOT caused by market stress.
Fixes (170 tests): the money check compares broker cash with cash implied by local fills (equity is
informational), needs two consecutive mismatches; self-clearing trips (stale feed, daily loss, cash
drift) now release by themselves while position divergence/handler errors/manual stay latched; the
engine publishes its kill-switch state so the status and watchdog messages show it.
