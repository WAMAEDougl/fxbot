"""Metrics collection (design section 7). SCAFFOLD - build order step 7.

Plain in-process counters/gauges with a Prometheus text-format exporter. Swap
for prometheus_client in production; the point of the design note is that
alerting matters more than dashboards, so keep this light.
"""

from __future__ import annotations

import math
from collections import defaultdict, deque
from dataclasses import dataclass, field

from core.bus import EventBus
from core.events import Fill, Heartbeat, Order, Rejection, Signal


@dataclass
class MetricStore:
    counters: dict[str, float] = field(default_factory=lambda: defaultdict(float))
    gauges: dict[str, float] = field(default_factory=dict)
    _slippage: deque[float] = field(default_factory=lambda: deque(maxlen=500))
    _fill_latency: deque[float] = field(default_factory=lambda: deque(maxlen=500))

    def inc(self, name: str, by: float = 1.0, **labels: str) -> None:
        self.counters[_key(name, labels)] += by

    def set(self, name: str, value: float, **labels: str) -> None:
        self.gauges[_key(name, labels)] = value

    def observe_slippage(self, bps: float) -> None:
        self._slippage.append(bps)
        self.set("slippage_bps_mean", _mean(self._slippage))

    def observe_fill_latency(self, seconds: float) -> None:
        self._fill_latency.append(seconds)
        self.set("fill_latency_s_mean", _mean(self._fill_latency))

    # -- Prometheus text exposition ------------------------------- #
    def render(self) -> str:
        lines = []
        for k, v in sorted(self.counters.items()):
            lines.append(f"{k} {v}")
        for k, v in sorted(self.gauges.items()):
            lines.append(f"{k} {v}")
        return "\n".join(lines) + "\n"


class MetricsCollector:
    """Subscribes to the bus and turns events into metrics."""

    def __init__(self, bus: EventBus, store: MetricStore | None = None) -> None:
        self.store = store or MetricStore()
        bus.subscribe(Signal, lambda e: self.store.inc("signals_total", strategy=e.strategy_id))
        bus.subscribe(Order, lambda e: self.store.inc("orders_total", strategy=e.strategy_id))
        bus.subscribe(Fill, self._on_fill)
        bus.subscribe(Rejection, self._on_rejection)
        bus.subscribe(Heartbeat, self._on_heartbeat)

    def _on_fill(self, e: Fill) -> None:
        self.store.inc("fills_total", strategy=e.strategy_id)
        self.store.inc("fees_paid_total", e.fees)
        latency = (e.received_ts - e.ts).total_seconds()
        if math.isfinite(latency) and latency >= 0:
            self.store.observe_fill_latency(latency)

    def _on_rejection(self, e: Rejection) -> None:
        self.store.inc("rejections_total", code=e.reason_code, origin=e.origin)

    def _on_heartbeat(self, e: Heartbeat) -> None:
        self.store.set("component_state", 1.0 if e.state == "ok" else 0.0, component=e.component_id)
        self.store.set("heartbeat_age_s", 0.0, component=e.component_id)


def _key(name: str, labels: dict[str, str]) -> str:
    if not labels:
        return name
    inner = ",".join(f'{k}="{v}"' for k, v in sorted(labels.items()))
    return f"{name}{{{inner}}}"


def _mean(d) -> float:
    return sum(d) / len(d) if d else 0.0
