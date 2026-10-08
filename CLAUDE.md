# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Single-node, multi-strategy systematic trading system (IBKR, equities + spot FX, daily/swing horizon, asyncio). It implements `trading-system-design.md` — consult that doc (section numbers are cited throughout the code) for rationale. `README.md` has the build-order status table: steps 1–5 are done; OMS/broker/reconcile (step 6) and ops metrics/alerting (step 7) are scaffolds.

## Commands

```bash
python -m venv .venv
.venv/Scripts/pip install -e ".[dev]"      # add ",ibkr" for the live adapter, ",scipy" optionally
.venv/Scripts/pytest                        # full suite (asyncio_mode=auto, testpaths=tests)
.venv/Scripts/pytest tests/unit/test_gates.py::test_name   # single test
.venv/Scripts/ruff check .                  # lint (line-length 100, py312)
.venv/Scripts/mypy .

# Paper/live runner (needs IB Gateway; TS_ENV must match IB_PORT; live needs --confirm-live)
python -m scripts.run_live --config configs/ma_crossover.example.yaml

# End-to-end with synthetic data (no broker)
python -m scripts.backfill     --venue synthetic --symbols SYNTH --start 2015-01-01
python -m scripts.run_backtest --config configs/ma_crossover.example.yaml
python -m scripts.validate     --config configs/ma_crossover.example.yaml --trials 30 --hypothesis-holds
```

New top-level packages must be added to `[tool.setuptools] packages` in `pyproject.toml` (the list is explicit, not auto-discovered; e.g. `ops.reports` and `strategies.*` subpackages are not currently listed).

## Architecture

Two planes, deliberately separated:

- **Research plane** (`research/`, `data/`): backtester, cost model, validation (`validation/`: CPCV, walk-forward, purged k-fold, DSR, PBO), promotion criteria. Holds no broker credentials. The only artifact that crosses to execution is a signed config in the strategy registry (`research/registry.py`).
- **Execution plane** (`execution/`, `risk/`, `ops/`): strategy engine → risk service → OMS → broker adapter, plus portfolio state, reconciliation, kill switches.

Everything in the execution plane is an event on an async bus (`core/bus.py`). **Backtest and live share `core/`** — same events (`core/events.py`, six frozen dataclasses with venue-time UTC `ts`), same bus, same clock interface (`core/clock.py`, sim + live). The backtester drives the loop by feeding events in venue-time order; `research/broker_sim.py` stands in for the broker. Keep any new logic clock- and bus-agnostic so it runs identically in both.

Flow: `Strategy` (protocol in `core/protocols.py`) emits `Signal` (intent only) → `risk/manager.py` runs the 11 pre-trade gates (`risk/gates.py`, returning `core/gate_result.py`), sizes the position (`risk/sizing.py`), and is the *only* constructor of `Order` → OMS → broker. All events are written to the audit log (`core/audit.py`, a wildcard bus subscriber).

Data: `data/lake.py` is a parquet lake with adjustments applied at read time and gap detection; adapters in `data/adapters/` (synthetic, csv, ibkr); `data/features/` is a causal feature store (registry + library) — features must not look ahead.

Live: `execution/live.py` (`LiveRunner`) wires the same core to `IbkrFeed`/`IbkrBroker`. It polls *completed* bars, journals a per-symbol cursor (`execution/persist.py`) so restarts neither replay nor skip bars, warms strategies up with signals discarded, adopts broker positions at startup, and trips the kill switch on reconcile drift / daily loss / repeated rejections / handler errors. `scripts/run_live.py` refuses to start if `TS_ENV` disagrees with `IB_PORT`.

Strategies live one package each under `strategies/<name>/` (strategy + pydantic config schema), configured via YAML in `configs/`. `ma_crossover` is a pipeline test harness and is expected to lose money after costs.

## Invariants (enforced in code and tests — don't weaken)

- `Signal` has no qty/side; only the risk manager builds an `Order`.
- `Order` without both stop-loss and take-profit raises at construction (`Order.__post_init__`).
- Position size is derived from `risk_amount / (stop_distance × value_per_point)`, never configured; hard cap 2% of equity.
- Timestamps must be tz-aware UTC (events reject naive/non-UTC).
- Bus dispatch is deterministic (handlers fire in subscription order; `publish` drains cascades). A raising handler is captured and routed to the error hook (used to disable a crashing strategy), not propagated.
- Replaying the same tape twice yields identical orders (`tests/property/test_invariants.py`).

Tests are split `tests/unit`, `tests/integration` (end-to-end backtest), `tests/property` (hypothesis: never exceed risk, never unbracketed, replay determinism).
