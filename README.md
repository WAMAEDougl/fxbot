# Automated Trading System

Implementation of `trading-system-design.md`. Single-node, multi-strategy
systematic trading: research → validation → paper → supervised live.

**Locked decisions** (design §13): IBKR (equities + spot FX) · daily / swing
horizon · custom Python on asyncio · build order steps 1–4 first.

## Status against the build order (design §12)

| Step | Item | State |
|---|---|---|
| 1 | Data lake + one feed adapter | **done** — `data/lake.py`, `data/adapters/` (synthetic, csv, ibkr) |
| 2 | Backtester with realistic costs | **done** — `research/backtest.py`, `research/costs.py` (spread, commission, swap, size-vs-liquidity slippage, limit-queue, partials, rejections, latency) |
| 3 | One trivial strategy (MA crossover) | **done** — `strategies/ma_crossover/` |
| 4 | Validation pipeline (CPCV, WFO, DSR, PBO) | **done** — `research/validation/`, `research/promotion.py` |
| 5 | Risk manager, all 11 gates, unit-tested | **done** — `risk/` (gates, sizing, limits, manager) |
| 6 | OMS + one broker adapter (sandbox) | **scaffold** — `execution/oms.py`, `execution/brokers/ibkr.py`, `execution/reconcile.py`, `execution/persist.py` |
| 7 | Observability, kill switches, reconciliation | **scaffold** — `ops/killswitch.py` (complete), `ops/metrics.py`, `ops/alerting.py` |
| 8–9 | Paper / canary | not started (operational, not code) |

The two planes are separated: `research/` holds no broker credentials; the only
artifact that crosses to execution is a signed config in the strategy registry
(`research/registry.py`).

## Layout

```
core/        events (the 6 types) · bus · clock (sim+live) · protocols · audit log · gate result
data/        lake (parquet, adjustments at read time, gap detection) · adapters · causal feature store
strategies/  one package per strategy (+ its config schema + its tests)
risk/        the 11 pre-trade gates · position sizing math · limit bookkeeping · the manager
execution/   portfolio/state · strategy engine · risk service · OMS · broker adapters · reconcile
research/    backtester (same core as live) · cost model · metrics · validation/ · promotion · registry
ops/         kill switch (3 layers) · metrics · alerting · tearsheet
tests/       unit · integration · property (never exceed risk, never unbracketed, replay determinism)
scripts/     backfill · run_backtest · validate
```

## Setup

```bash
python -m venv .venv
.venv/Scripts/pip install -e ".[dev]"        # add ",ibkr" for the live adapter
.venv/Scripts/pytest                          # run the suite
```

SciPy is optional — `research/validation` uses a pure-Python normal distribution
when it is absent (`.[scipy]` to add it back).

## Try it (synthetic data, no broker)

```bash
python -m scripts.backfill    --venue synthetic --symbols SYNTH --start 2015-01-01
python -m scripts.run_backtest --config configs/ma_crossover.example.yaml
python -m scripts.validate     --config configs/ma_crossover.example.yaml --trials 30 --hypothesis-holds
```

The MA-crossover strategy is expected to **lose money after costs** — it is the
test harness for the pipeline, not an edge (design §12 step 3).

## Key invariants (enforced, not by convention)

- A `Signal` has no `qty`/`side`; only the risk manager constructs an `Order`.
- An `Order` with no stop-loss **and** take-profit is rejected at construction.
- Position size is derived from `risk_amount / (stop_distance × value_per_point)`,
  never configured; hard-capped at 2% of equity.
- Backtest and live share `core/` — same events, same bus, same clock interface.
- Replaying the same tape twice produces identical orders (property test).

## Next

- Wire OMS against the IBKR paper sandbox (step 6), then reconciliation loop (step 7).
- Replace HMAC registry signing with asymmetric signing before any live use.
- Real trading calendars for gap detection (currently `bdate_range`).
- Then: 3 months paper, build nothing new (design §12 step 8).
