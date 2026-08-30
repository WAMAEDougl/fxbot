# Automated Trading System — System Design

**Version:** 1.0
**Status:** Design for review
**Scope:** A single-node, multi-strategy systematic trading system covering research, validation, paper trading, and supervised live execution.

---

## 1. Design principles

These are load-bearing. Every decision below traces back to one of them.

1. **Edge is assumed absent until proven.** No signal reaches live capital until it survives out-of-sample validation net of spread, commission, and slippage. The system is built to *disprove* strategies, not to run them.
2. **Risk is the only thing fully under your control.** You cannot control whether a trade wins. You control what it costs when it loses. The risk layer is a mandatory gate, not a post-trade report.
3. **Research and production share one code path.** If the backtester and the live engine run different code, your backtest is fiction. Same strategy classes, same event types, same fill semantics.
4. **The human is in the loop for live capital.** The system proposes; a person confirms. Full autonomy is a later, earned stage — not the starting point.
5. **Everything is reproducible.** Every decision, order, fill, and rejection is logged with the inputs that produced it. If you can't replay a bad day, you can't fix it.

### Non-goals

- Low-latency / HFT. Sub-millisecond execution requires colocation and a completely different architecture. This design targets seconds-to-minutes decision horizons.
- Market prediction. No component here claims to know what price will do.
- Scaling to a team of quants on day one. This is a single-node system for one operator.

---

## 2. High-level architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                        RESEARCH PLANE                            │
│  (offline, batch, no connection to live capital)                 │
│                                                                  │
│  Data Lake ──> Feature Store ──> Strategy Lab ──> Validation     │
│  (parquet)     (versioned)       (hypotheses)     (CPCV/WFO)     │
│                                        │                         │
│                                        v                         │
│                              Strategy Registry                   │
│                        (approved configs + provenance)           │
└────────────────────────────────────────┬─────────────────────────┘
                                         │ promotes signed config
                                         v
┌──────────────────────────────────────────────────────────────────┐
│                       EXECUTION PLANE                            │
│                                                                  │
│  ┌──────────┐   MarketData    ┌──────────┐   Signal              │
│  │ Feed     │ ──────────────> │ Strategy │ ──────────┐           │
│  │ Adapters │     events      │ Engine   │           │           │
│  └──────────┘                 └──────────┘           v           │
│       │                                        ┌───────────┐     │
│       │                                        │   RISK    │     │
│       │                                        │  MANAGER  │     │
│       │                                        │ (gate)    │     │
│       │                                        └─────┬─────┘     │
│       │                                              │ Order     │
│       │                                              v           │
│       │           ┌──────────────┐          ┌──────────────┐     │
│       └─────────> │  Portfolio / │ <──────  │     OMS      │     │
│         Fills     │  State Store │  Fills   │  + Broker    │     │
│                   └──────────────┘          │   Adapter    │     │
│                                             └──────────────┘     │
│                                                                  │
│  Cross-cutting: Event Bus · Clock · Audit Log · Kill Switch      │
└──────────────────────────────────────┬───────────────────────────┘
                                       │
                         ┌─────────────┴─────────────┐
                         │  Observability & Control  │
                         │  metrics · alerts · TUI   │
                         └───────────────────────────┘
```

The two planes are deliberately separated. The research plane has **no credentials** for the live broker. The only artifact that crosses the boundary is an approved, versioned strategy config.

---

## 3. Core abstractions

Everything in the execution plane is an event on a bus. Six event types cover the whole system:

| Event | Emitted by | Consumed by | Payload |
|---|---|---|---|
| `MarketData` | Feed adapter | Strategy engine, Portfolio | symbol, ts, bid/ask/bar, source |
| `Signal` | Strategy | Risk manager | symbol, direction, conviction, strategy_id, features_hash |
| `Order` | Risk manager | OMS | client_order_id, symbol, side, qty, type, sl, tp, ttl |
| `Fill` | Broker adapter | Portfolio, Audit | order_id, price, qty, fees, ts |
| `Rejection` | OMS or Risk | Audit, Alerting | reason_code, originating event |
| `Heartbeat` | Clock | Watchdog | component_id, ts, state |

**Critical invariant:** a `Signal` is not an `Order`. Strategies emit intent; only the risk manager can create an executable order. A strategy has no method that can place a trade. This is enforced by interface, not by convention.

```python
class Strategy(Protocol):
    strategy_id: str
    def on_market_data(self, event: MarketData, ctx: ReadOnlyContext) -> list[Signal]: ...
    def on_fill(self, event: Fill) -> None: ...   # notification only
```

`ReadOnlyContext` exposes positions and equity but has no mutating methods and no broker handle.

---

## 4. Component specifications

### 4.1 Data layer

**Feed adapters** normalize every venue into the same `MarketData` event. One adapter per venue; the rest of the system never sees venue-specific formats.

- **Transport:** WebSocket for live, REST for backfill and gap repair. Polling REST for live prices is a design smell — it costs latency and rate limit budget for no benefit.
- **Gap detection:** every adapter tracks sequence numbers or bar timestamps. A gap emits a `DataGap` condition that the risk manager treats as a trading halt for that symbol until backfilled.
- **Storage:** raw ticks/bars append to a Parquet lake partitioned by `symbol/date`. This is your backtest corpus; never mutate it in place.
- **Clock discipline:** every event carries both venue timestamp and local receipt timestamp. Backtests replay on venue time; latency modelling uses the delta.

**Corporate actions / contract rolls** (equities and futures) need a separate adjustment table applied at read time, not baked into stored data. Baking splits into stored prices is a classic irreversible mistake.

### 4.2 Feature store

Sits between raw data and strategies. Its one job is preventing lookahead bias.

- Every feature is a pure function of data available **at or before** timestamp `t`.
- Features are computed once, versioned, and cached. A feature's version hash goes into every `Signal` it produces — so you can trace any trade back to exactly which code computed its inputs.
- Rolling windows must be causal. `df.rolling(20).mean()` is fine; anything using `shift(-1)`, full-sample normalisation, or `fillna(method='bfill')` is a leak.

**Rule:** if a feature can't be computed in a streaming loop with only past data, it doesn't go in the store.

### 4.3 Strategy engine

Hosts N strategies, each implementing the `Strategy` protocol. Strategies are pure-ish: same inputs produce same outputs, deterministic given a seed.

- Each strategy is independently enabled/disabled via config — no redeploy needed to kill one.
- Strategies are sandboxed by exception boundary: a crashing strategy is disabled and alerts; it does not take down the engine.
- Conviction is a float in `[0, 1]`, not a position size. Sizing belongs to risk.

### 4.4 Risk manager — the mandatory gate

Every `Signal` passes through here. It can **clip, block, or annotate** — never pass through blindly. This is the most important component in the system and should be the most heavily tested.

**Pre-trade checks, in order (fail-fast):**

| # | Gate | Default | Rejection code |
|---|---|---|---|
| 1 | Kill switch engaged? | — | `KILL_SWITCH` |
| 2 | Data freshness (no stale/gapped feed) | < 3 bar intervals | `STALE_DATA` |
| 3 | Daily loss limit hit? | 3% of start-of-day equity | `DAILY_LOSS_LIMIT` |
| 4 | Max trades today? | 4 | `TRADE_CAP` |
| 5 | Symbol cooldown after stop-out? | 30 min | `COOLDOWN` |
| 6 | Direction flip on same symbol? | blocked | `REVERSAL_BLOCK` |
| 7 | Correlated exposure cap | 1.5× single-trade risk | `CORRELATION_CAP` |
| 8 | Stop distance ≥ 1× ATR and ≫ spread | — | `NOISE_TIGHT_STOP` |
| 9 | Reward:risk ≥ 1.5 (target 2.0) | 1.5 | `INSUFFICIENT_RR` |
| 10 | Margin available with headroom | — | `INSUFFICIENT_MARGIN` |
| 11 | High-impact event window | ±30 min | size ×0.5, stop ×1.5 |

**Position sizing** is derived, never configured directly:

```
risk_amount   = equity × risk_fraction          # 0.5% default, 1.0% max standard, never > 2%
stop_distance = |entry - stop|                  # in price terms
size          = risk_amount / (stop_distance × value_per_point)
size          = floor_to_lot_step(size)
```

Size is **always** expressed in account currency of risk, never in lots. "0.1 lots" is meaningless across instruments — 0.1 lot EURUSD and 0.1 lot XAUUSD differ by roughly 30–40× in risk per point.

**Bracket enforcement:** an `Order` with no stop-loss and take-profit attached is rejected at construction. Brackets go to the venue *with* the entry, server-side. A plan to "close it manually later" is not a stop.

### 4.5 Order Management System (OMS)

Owns order lifecycle and is the only component holding broker credentials.

- **Idempotency:** every order carries a `client_order_id` derived from `(strategy_id, symbol, signal_ts, nonce)`. Retries reuse the same ID. This is what stops a network timeout from becoming a double position.
- **State machine:** `PENDING → SUBMITTED → {PARTIAL → FILLED | REJECTED | CANCELLED | EXPIRED}`. Transitions are persisted before the next action is taken.
- **Reconciliation loop:** on startup and every N minutes, fetch the broker's authoritative positions and compare to local state. Divergence triggers an alert and halts new orders. Your local state is a cache; the broker is the source of truth.
- **Retry policy:** exponential backoff with jitter, capped. Distinguish retryable (timeout, 5xx, rate limit) from terminal (insufficient funds, invalid symbol, market closed). Never blind-retry a terminal error.
- **Order TTL:** unfilled limit orders expire rather than lingering. A stale order that fills three hours later on a signal that has decayed is a real and expensive failure mode.

### 4.6 Portfolio / state store

Single source of local truth: positions, equity curve, open orders, realised and unrealised P/L, per-strategy attribution.

- Persist to an embedded DB (SQLite/DuckDB) or Postgres. **Not** in-memory only — a process restart must not lose position state.
- Write-ahead: state changes are journalled before being acted on, so a crash mid-order is recoverable.
- Every position row links back to the `Signal` and feature version that created it.

### 4.7 Audit log

Append-only, immutable. Every event on the bus, in order, with timestamps. Sized to hold at least a year.

This is not optional and not "nice for debugging." When a strategy loses money, the only way to distinguish *bad edge* from *bad execution* from *a bug* is to replay the tape. Without it you will guess, and you will guess wrong.

---

## 5. Validation pipeline — the part that decides if you have anything

Most of your engineering effort should land here, not on the execution engine. A perfectly-built system running a curve-fit strategy loses money efficiently.

### 5.1 Stages

1. **Hypothesis first.** Write down the economic reason the edge should exist *before* testing. "Momentum persists because institutional order flow is split across sessions" is a hypothesis. "RSI < 30 worked in the backtest" is a data artifact.
2. **In-sample exploration** on the earliest slice of data. Free-form. Expect most ideas to die here.
3. **Purged, embargoed cross-validation.** Standard k-fold leaks in time series. Purging removes training samples whose label windows overlap the test set; embargoing drops a buffer after each test fold to kill serial correlation. A 21-day (≈1 month) purge/embargo is a reasonable default for daily-bar strategies.
4. **Combinatorial Purged Cross-Validation (CPCV).** Generates many train/test path combinations rather than one, giving a *distribution* of Sharpe ratios instead of a single number. It measurably outperforms plain walk-forward on both Probability of Backtest Overfitting and Deflated Sharpe Ratio, at meaningfully higher compute cost.
5. **Walk-forward analysis** as the realism check. Re-optimise on a rolling window, trade forward on unseen data, repeat. This is the closest simulation of how you'd actually operate, and it's still the industry standard for that reason.
6. **Multiple-testing correction.** This is the step everyone skips. If you tried 200 parameter combinations, the best one looks great by construction. The **Deflated Sharpe Ratio** corrects the observed Sharpe for the number of configurations tried and for non-normal returns. **Track your trial count honestly** — including the ideas you abandoned. Self-reported trial counts are where people lie to themselves.
7. **Probability of Backtest Overfitting (PBO).** Compute it. If PBO > 0.5, your strategy selection process is worse than random.

### 5.2 Execution realism in the backtester

A backtest that doesn't model these is not evidence:

- **Spread** at the actual time of day (spreads widen at rollover and in thin sessions).
- **Commission and financing/swap** costs.
- **Slippage**, modelled as a function of order size versus available liquidity — not a flat constant.
- **Queue position** for limit orders. Assuming your limit fills whenever price touches it is the single most common source of fake backtest profit.
- **Partial fills and rejections.**
- **Latency** between signal generation and order arrival.

Strategies that look strong on clean OHLC bars routinely collapse once tick-level fills, realistic slippage, queue probability, and rejections are added. Budget for this outcome; it is the normal result, not a setback.

### 5.3 Promotion criteria

A strategy moves from research to the registry only if **all** of:

- Deflated Sharpe Ratio > 0 at 95% confidence, after honest trial-count correction
- PBO < 0.5
- Positive expectancy across **every** CPCV path, not just on average
- Maximum drawdown within your tolerance on the worst path, not the mean path
- Performance is not concentrated in one regime, one month, or one symbol
- ≥ 100 trades in the test sample (below that, results are noise)
- The stated economic hypothesis still holds after inspection

Falling short on any one is a rejection, not a negotiation.

---

## 6. Rollout stages

Never go from backtest to live capital. Four gates:

| Stage | Duration | Capital | Exit criteria |
|---|---|---|---|
| **1. Replay** | days | none | Live engine replays historical tape and reproduces backtest P/L within tolerance. Any divergence is a bug in one of the two — find it. |
| **2. Paper** | ≥ 3 months | none | Live data, simulated fills. Compare realised paper P/L to backtest expectation. Divergence here is your slippage/latency model being wrong. |
| **3. Canary** | ≥ 2 months | minimum size | Real money, smallest size the broker permits. You are testing the *plumbing*: fills, reconciliation, restarts, weekend gaps, broker quirks. P/L is irrelevant at this size; correctness is everything. |
| **4. Scaled** | ongoing | risk-budgeted | Increase only after a clean canary period, and increase gradually. |

**Stage 3 is the one people skip and shouldn't.** The bugs that cost real money — duplicate orders on reconnect, timezone errors around DST, brackets not attaching, position drift after a restart — appear only against a live broker.

---

## 7. Observability and control

### Metrics (per strategy and aggregate)

- **Health:** heartbeat age, feed staleness, event queue depth, reconciliation drift
- **Execution:** fill ratio, realised vs. expected slippage, implementation shortfall, order rejection rate, mean time-to-fill
- **Risk:** current drawdown vs. limit, exposure by symbol and by correlation cluster, margin utilisation
- **Edge:** rolling expectancy per trade, rolling Sharpe versus the backtest's confidence band, **alpha decay** — expected return per trade measured over successive live weeks

### Alpha decay monitoring

Track expectancy over a rolling live window and compare against the backtest distribution. Set an automatic threshold: if live expectancy falls below the backtest's 5th-percentile path for N consecutive periods, the strategy is auto-demoted to paper. Edges do expire — momentum systems commonly last 3–6 months, swing systems 6–18 — so treat decay as expected behaviour to be detected, not a surprise to be argued with.

### Kill switches (three layers)

1. **Manual** — one command, or a physical file on disk the engine polls. Halts all new orders immediately.
2. **Automatic** — triggered by daily loss limit, reconciliation drift, feed outage, or repeated order rejections.
3. **Broker-side** — a hard account-level limit set with the broker, outside your code. Your code is the thing most likely to have the bug; the outermost limit must not depend on it.

Kill switch semantics: **stop opening, keep managing.** Halting must never orphan open positions with unattached brackets. Since brackets are server-side at entry, existing positions remain protected even if your process is dead — which is precisely why bracket-at-entry is a hard rule.

---

## 8. Deployment

- **Runtime:** Docker container, single node. A VPS geographically near the broker's servers (latency here is about avoiding stale quotes, not about competing on speed).
- **Process supervision:** systemd or Docker restart policy. On restart the engine must reconcile before doing anything else.
- **Secrets:** broker credentials in environment or a secrets manager, never in the repo, never in the research plane. Use API keys scoped to trading only — no withdrawal permission, ever.
- **Config as code:** strategy configs are versioned files, deployed by tag. You must be able to answer "what exactly was running at 14:32 last Tuesday" from git alone.
- **Backups:** state DB and audit log backed up off-host, daily.
- **Two environments** minimum: paper and live, identical code, different credentials, visually distinct in every log line and dashboard so you can never confuse them.

---

## 9. Suggested tech stack

| Layer | Choice | Why |
|---|---|---|
| Language | Python 3.12 | Ecosystem. Rust/C++ only if you later need sub-millisecond. |
| Engine | NautilusTrader, or custom on asyncio | Nautilus gives event-driven backtest and live from identical strategy code — the hardest thing to get right yourself. Custom is fine if you want full control of the abstractions above. |
| Crypto venues | CCXT | ~100 exchanges behind one interface. |
| Data (research) | Parquet + DuckDB | Columnar, fast, no server to run. |
| Data (state) | SQLite → Postgres | Start embedded; migrate if you outgrow it. |
| Vendor data | Alpha Vantage (learning) → Polygon/Massive or Databento (production) | Free tiers have history limits that make real backtesting impossible. |
| Validation | `mlfinlab`-style CPCV, custom DSR/PBO | Implement DSR and PBO yourself; you need to understand them. |
| Monitoring | Prometheus + Grafana, or plain logs + a Telegram bot | Alerting matters more than dashboards. |
| Alerting | Telegram or email, with severity tiers | You need to be woken up for a kill-switch trip, not for a fill. |

---

## 10. Repository layout

```
trading-system/
├── core/
│   ├── events.py            # the six event types, frozen dataclasses
│   ├── bus.py               # async event bus
│   ├── clock.py             # sim clock + live clock, same interface
│   └── protocols.py         # Strategy, Adapter, RiskGate interfaces
├── data/
│   ├── adapters/            # one module per venue
│   ├── lake.py              # parquet read/write, gap repair
│   └── features/            # causal feature functions, versioned
├── strategies/
│   └── <name>/              # strategy + its config schema + its tests
├── risk/
│   ├── gates.py             # the 11 pre-trade checks, individually testable
│   ├── sizing.py            # position sizing math
│   └── limits.py            # loss limits, cooldowns, correlation clusters
├── execution/
│   ├── oms.py               # order state machine, idempotency, retries
│   ├── reconcile.py
│   └── brokers/             # one adapter per broker
├── research/
│   ├── backtest.py          # uses core/ — same code as live
│   ├── validation/          # cpcv.py, walkforward.py, dsr.py, pbo.py
│   └── notebooks/
├── ops/
│   ├── killswitch.py
│   ├── metrics.py
│   └── reports/
└── tests/
    ├── unit/
    ├── integration/          # against broker sandbox
    └── property/             # invariants: never exceed risk, never unbracketed
```

**Property tests worth writing:** no order ever exceeds max risk fraction; no order is ever emitted without brackets; total exposure never exceeds correlation cap; replaying the same tape twice produces identical orders.

---

## 11. Failure modes to design against

| Failure | Symptom | Mitigation |
|---|---|---|
| Lookahead bias | Backtest Sharpe implausibly high | Causal-only feature store; replay-stage reconciliation |
| Overfitting | Great backtest, flat live | CPCV, DSR, PBO, honest trial counting |
| Survivorship bias | Equity backtests too rosy | Point-in-time universe including delisted names |
| Duplicate orders | Double position after reconnect | Idempotent `client_order_id` |
| State drift | Local ≠ broker positions | Periodic reconciliation, halt on divergence |
| Orphaned positions | Process dies with unprotected position | Server-side brackets at entry |
| Silent feed death | Stale prices, trading on old data | Freshness gate + heartbeat watchdog |
| Timezone / DST | Trades at wrong session boundaries | All internal time UTC; venue calendars explicit |
| Alpha decay | Slow bleed after profitable start | Rolling expectancy monitor, auto-demote |
| Revenge logic | One bad session becomes four | Cooldown, trade cap, reversal block — enforced in code |

---

## 12. Build order

Do not build this in the order it's documented. Build it in the order that fails fastest:

1. **Data lake + one feed adapter.** Get clean historical data for one instrument. (~1 week)
2. **Backtester with realistic costs.** Spread, commission, slippage, queue. (~2 weeks)
3. **One trivial strategy** (moving-average crossover). Expect it to lose money — that's the point. It's your test harness, not your edge. (~2 days)
4. **The validation pipeline.** CPCV, walk-forward, DSR, PBO. (~2–3 weeks) *If you stop building here, you've still gained the ability to tell good ideas from bad ones — which is most of the value.*
5. **Risk manager with all gates, fully unit-tested.** (~1 week)
6. **OMS + one broker adapter against a sandbox.** (~2 weeks)
7. **Observability, kill switches, reconciliation.** (~1 week)
8. **Paper trade for three months.** Build nothing new. Watch.
9. **Canary.**

Steps 1–4 are roughly 60% of the total effort and produce zero trades. That ratio is correct.

---

## 13. Open decisions

Answer these before implementation begins — they change the architecture:

1. **Asset class and venue?** Crypto (24/7, CCXT, no market hours logic, but thinner regulation and worse data), FX/CFD via MT5 (broker-dependent execution quality), or equities via Alpaca/IBKR (market hours, corporate actions, PDT rules)?
2. **Decision horizon?** Minutes, hours, or days. This determines data granularity, cost sensitivity, and infrastructure requirements more than any other choice.
3. **Capital and risk budget?** Sets the sizing floor and determines whether costs are survivable at all.
4. **Autonomy level?** Human-confirms-every-trade, human-confirms-daily, or fully autonomous with limits. Start at the first.
5. **Regulatory position?** Broker availability, tax treatment, and reporting obligations in your jurisdiction.

---

## Appendix: reading

- López de Prado, *Advances in Financial Machine Learning* — purging, embargoing, CPCV, PBO. The foundational text for the validation plane.
- Bailey & López de Prado (2014), *The Deflated Sharpe Ratio* — the multiple-testing correction.
- Pardo, *The Evaluation and Optimization of Trading Strategies* — walk-forward methodology.
- NautilusTrader documentation — a reference implementation of the execution plane described here.
