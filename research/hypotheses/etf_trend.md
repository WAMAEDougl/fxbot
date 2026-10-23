# Hypothesis: time-series trend following on a diversified ETF basket

Written BEFORE any backtest result was seen (design 5.1 step 1). Do not edit
after the fact; add a dated note below instead.

**Claim.** An asset's own recent trend (price above/below its long moving
average) predicts the sign of its next-month return, across asset classes,
because under-reaction, slow-moving institutional capital and herding make
trends persist (Moskowitz, Ooi & Pedersen, "Time Series Momentum", JFE 2012;
Hurst, Ooi & Pedersen, "A Century of Evidence on Trend-Following Investing").

**Why it might survive costs here.** Low turnover (a handful of trades per
symbol per year), liquid ETFs (about 1-2 bps spread), a commission-free broker,
and diversification across equities, bonds, gold, commodities and real estate,
so no single asset dominates.

**Why it might fail here (stated up front).**
- The system cannot trail a stop or exit on a signal: the fixed entry bracket is
  the only exit. That caps winners and lets reversals run to the stop.
- Equity trend following has had weak decades; the sample (2016-2026) is a strong
  bull market, so any long-biased rule is flattered.
- The ETFs were chosen with hindsight (survivorship).

**Falsification.** Reject if any check in design 5.3 fails, or if it does not
beat buy-and-hold of the same basket on a drawdown-adjusted basis.

**Parameters (kept few on purpose).** `lookback` in {100, 150, 200} and
`stop_atr_mult` in {2, 3, 4}. Everything else is fixed. Every parameter set ever
evaluated is appended to `research/trials/etf_trend.jsonl` and counted.

---
## Result notes (appended; the text above is unchanged)

**2026-10-23, run 1: fixed bracket only (9 trials).** Best config lookback 100 / stop 2 ATR:
CAGR +9.0%, Sharpe 0.76, max DD 16.0% vs equal-weight basket B&H +10.1% / 0.88 / 23.1%.
REJECT: PBO 0.583 (needs < 0.5); DSR 0.950 (borderline). Long side +$192k, short side -$37k.

**2026-10-23, run 2: close-on-trend-flip via reduce-only orders (9 more trials, 18 total).**
The variant was motivated by run 1 (winners capped, reversals riding to the stop), so it is
counted as new trials. Best config lookback 200 / stop 2 ATR: CAGR +10.2%, Sharpe 0.79,
max DD 20.3%, 664 trades, win rate 23%, expectancy +0.39R. DSR 0.976 OK, PBO 0.226 OK,
every CPCV path positive, not regime-concentrated. REJECT: worst CPCV path max DD 25.70%
vs the 25% tolerance, and the manual hypothesis sign-off is not given.
Long side +$210k, short side -$22k; TLT and VNQ lose money.
Honest read: roughly matches buy-and-hold of the basket on return with somewhat lower
drawdown, but a lower Sharpe (0.79 vs 0.88). Sharpe varies 0.47-0.79 across the grid and the
best cell sits at the edge (stop 2.0 is the tightest tested), so the result is fragile.
Do NOT widen the drawdown tolerance to make this pass; that is the negotiation 5.3 forbids.

**2026-10-23, drawdown tolerance set to 26% by the operator, AFTER seeing run 2's 25.70%.**
Re-run with `--max-dd 0.26`: 6/6 objective checks pass (no new trials; the same 18 are
counted). This tolerance was chosen after the result, so treat the pass as conditional:
the margin is 0.3 points, a backtest's worst drawdown is usually exceeded by live
trading, and the "hypothesis holds" sign-off is still outstanding (not given).

**2026-10-23, run 3: BACKTEST CORRECTED (same 18 trials, no new variants). Runs 1-2 are superseded.**
Two infrastructure flaws made the earlier backtests more flattering than a real account:
(a) the margin gate compared each order against ALL equity and ignored open positions, so
gross exposure averaged 1.78x and peaked at 3.57x (above the 2x Reg T limit on 37% of days);
(b) daily orders had a 1-day lifetime, so ~12% of entries (Friday signals) expired over the
weekend and never traded, although live they would fill on Monday. Fixed: free margin now =
equity - margin in use; `order_ttl_days: 4` in the config.
Corrected result, best config lookback 200 / stop 2 ATR: CAGR +8.7%, Sharpe 0.71, max DD 20.7%,
588 trades, win rate 23%, expectancy +0.34R. DSR 0.929 (needs > 0.95) FAIL, PBO 0.556
(needs < 0.5) FAIL; CPCV paths positive, worst-path DD 22.6%. REJECT on objective checks,
independent of the drawdown tolerance. Equal-weight basket buy-and-hold: +10.1% / Sharpe 0.88
/ DD 23.1%, so the strategy does not beat simply holding the same ETFs.
