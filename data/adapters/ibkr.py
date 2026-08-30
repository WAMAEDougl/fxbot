"""Interactive Brokers feed adapter (equities + spot FX) via ``ib_async``.

Requires a running TWS or IB Gateway. ``pip install "trading-system[ibkr]"``.

Contract resolution:
* ``"AAPL"``            -> Stock(AAPL, SMART, USD)
* ``"EUR.USD"`` / ``"EURUSD"`` -> Forex pair
* ``("MSFT", "NASDAQ")`` tuple -> Stock on a specific primary exchange

Design notes honoured here:
* WebSocket-equivalent (``reqRealTimeBars``) for live, REST-equivalent
  (``reqHistoricalData``) for backfill and gap repair - no REST polling for
  live prices.
* Every event carries venue ts and local receipt ts.
* FX has no "volume" from IB; it is reported as 0 and must not be used as a
  liquidity proxy.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

from core.events import MarketData

try:  # pragma: no cover - optional dependency
    from ib_async import IB, Contract, Forex, Stock

    _HAVE_IB = True
except ImportError:  # pragma: no cover
    _HAVE_IB = False
    IB = Contract = Forex = Stock = object  # type: ignore


_BAR_SIZE = {
    "1d": "1 day",
    "1h": "1 hour",
    "30m": "30 mins",
    "15m": "15 mins",
    "5m": "5 mins",
    "1m": "1 min",
}


def _resolve_contract(symbol: str | tuple[str, str]) -> Contract:
    if isinstance(symbol, tuple):
        ticker, exchange = symbol
        return Stock(ticker, "SMART", "USD", primaryExchange=exchange)
    s = symbol.replace(".", "").upper()
    # 6-letter all-alpha with a known quote currency -> treat as FX
    if len(s) == 6 and s.isalpha() and s[3:] in {"USD", "EUR", "JPY", "GBP", "CHF", "AUD", "CAD", "NZD"}:
        return Forex(s)
    return Stock(symbol.upper(), "SMART", "USD")


class IbkrFeed:
    venue = "ibkr"

    def __init__(self, host: str = "127.0.0.1", port: int = 7497, client_id: int = 11):
        if not _HAVE_IB:
            raise ImportError("ib_async not installed. `pip install \"trading-system[ibkr]\"`")
        self.host, self.port, self.client_id = host, port, client_id
        self._ib: IB | None = None

    async def connect(self) -> None:
        if self._ib is not None and self._ib.isConnected():
            return
        self._ib = IB()
        await self._ib.connectAsync(self.host, self.port, clientId=self.client_id)

    async def disconnect(self) -> None:
        if self._ib is not None:
            self._ib.disconnect()
            self._ib = None

    async def backfill(
        self, symbol: str, start: datetime, end: datetime, interval: str = "1d"
    ) -> list[MarketData]:
        await self.connect()
        assert self._ib is not None
        contract = _resolve_contract(symbol)
        await self._ib.qualifyContractsAsync(contract)

        span_days = max(1, (end - start).days + 1)
        duration = f"{span_days} D" if span_days <= 365 else f"{(span_days // 365) + 1} Y"
        bars = await self._ib.reqHistoricalDataAsync(
            contract,
            endDateTime=end,
            durationStr=duration,
            barSizeSetting=_BAR_SIZE[interval],
            whatToShow="MIDPOINT" if isinstance(contract, Forex) else "TRADES",
            useRTH=True,
            formatDate=2,  # UTC epoch
        )
        now = datetime.now(UTC)
        out: list[MarketData] = []
        for b in bars:
            ts = b.date if isinstance(b.date, datetime) else datetime.combine(
                b.date, datetime.min.time(), tzinfo=UTC
            )
            ts = ts.astimezone(UTC)
            if ts < start or ts > end:
                continue
            out.append(
                MarketData(
                    ts=ts,
                    received_ts=now,
                    symbol=symbol,
                    source=self.venue,
                    open=float(b.open),
                    high=float(b.high),
                    low=float(b.low),
                    close=float(b.close),
                    volume=float(b.volume) if b.volume and b.volume > 0 else 0.0,
                    bar_interval=interval,
                )
            )
        return out

    async def stream(
        self, symbols: list[str], interval: str = "5m"
    ) -> AsyncIterator[MarketData]:  # pragma: no cover - needs live gateway
        await self.connect()
        assert self._ib is not None
        import asyncio

        queue: asyncio.Queue[MarketData] = asyncio.Queue()
        contracts = {}
        for sym in symbols:
            c = _resolve_contract(sym)
            await self._ib.qualifyContractsAsync(c)
            contracts[c.conId] = sym
            self._ib.reqRealTimeBars(c, 5, "MIDPOINT", False)

        def _on_bar(bars, has_new_bar):  # noqa: ANN001
            if not has_new_bar:
                return
            b = bars[-1]
            sym = contracts.get(bars.contract.conId, bars.contract.symbol)
            queue.put_nowait(
                MarketData(
                    ts=b.time.astimezone(UTC),
                    received_ts=datetime.now(UTC),
                    symbol=sym,
                    source=self.venue,
                    open=float(b.open_),
                    high=float(b.high),
                    low=float(b.low),
                    close=float(b.close),
                    volume=float(b.volume) if b.volume > 0 else 0.0,
                    bar_interval="5s",
                )
            )

        self._ib.barUpdateEvent += _on_bar
        try:
            while True:
                yield await queue.get()
        finally:
            self._ib.barUpdateEvent -= _on_bar
