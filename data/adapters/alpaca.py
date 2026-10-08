"""Alpaca feed adapter (US equities) over the REST market-data API.

No third-party SDK: plain HTTPS via the stdlib, run in a worker thread so the
event loop never blocks. Credentials come from the environment
(``ALPACA_API_KEY`` / ``ALPACA_API_SECRET``), never the repo.

* Equities only. Alpaca has no spot FX - an FX-looking symbol (``EUR.USD``) is
  rejected rather than silently mis-routed to a stock ticker.
* ``feed=iex`` is the default because it is the only feed free accounts can
  query without the 15-minute SIP delay. IEX volume is a fraction of
  consolidated volume - do not use it as a liquidity proxy. Set
  ``ALPACA_DATA_FEED=sip`` on a paid plan.
* Bars are split-adjusted (``adjustment=split``) so warm-up history has no
  cliffs at split dates.
"""

from __future__ import annotations

import asyncio
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from core.events import MarketData

DATA_URL = "https://data.alpaca.markets"
PAPER_URL = "https://paper-api.alpaca.markets"
LIVE_URL = "https://api.alpaca.markets"

_TIMEFRAME = {
    "1d": "1Day",
    "1h": "1Hour",
    "30m": "30Min",
    "15m": "15Min",
    "5m": "5Min",
    "1m": "1Min",
}
_FX_QUOTES = {"USD", "EUR", "JPY", "GBP", "CHF", "AUD", "CAD", "NZD"}


class AlpacaError(RuntimeError):
    """HTTP failure. The message embeds a keyword the OMS error classifier
    understands (``rate_limit``, ``5xx``, ``insufficient_funds`` ...)."""

    def __init__(self, status: int, body: str) -> None:
        self.status, self.body = status, body
        super().__init__(f"{_kind(status, body)}: HTTP {status} {body[:300]}")


def _kind(status: int, body: str) -> str:
    low = body.lower()
    if status == 429:
        return "rate_limit"
    if status >= 500:
        return "5xx"
    if "insufficient" in low:
        return "insufficient_funds"
    if ("not found" in low and "asset" in low) or "invalid symbol" in low:
        return "invalid_symbol"
    if "market is closed" in low or "market closed" in low:
        return "market_closed"
    return "http_error"


# transport(method, url, headers, body) -> (status, text)
Transport = Callable[[str, str, dict[str, str], bytes | None], tuple[int, str]]


def _urllib_transport(method: str, url: str, headers: dict[str, str],
                      body: bytes | None) -> tuple[int, str]:
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode(errors="replace")


class AlpacaHttp:
    """Authenticated JSON client shared by the feed and the broker."""

    def __init__(self, key: str | None = None, secret: str | None = None,
                 transport: Transport | None = None) -> None:
        self.key = key or os.environ.get("ALPACA_API_KEY", "")
        self.secret = secret or os.environ.get("ALPACA_API_SECRET", "")
        if not (self.key and self.secret) and transport is None:
            raise RuntimeError("ALPACA_API_KEY / ALPACA_API_SECRET are not set")
        self._transport = transport or _urllib_transport

    async def request(self, method: str, url: str, *, params: dict[str, Any] | None = None,
                      body: dict[str, Any] | None = None) -> Any:
        if params:
            url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        headers = {
            "APCA-API-KEY-ID": self.key,
            "APCA-API-SECRET-KEY": self.secret,
            "Accept": "application/json",
        }
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        status, text = await asyncio.to_thread(self._transport, method, url, headers, data)
        if not 200 <= status < 300:
            raise AlpacaError(status, text)
        return json.loads(text) if text.strip() else None


def _check_symbol(symbol: str) -> str:
    s = symbol.replace(".", "").upper()
    if len(s) == 6 and s.isalpha() and s[:3] in _FX_QUOTES and s[3:] in _FX_QUOTES:
        raise ValueError(f"{symbol!r} looks like spot FX; Alpaca trades US equities only")
    return symbol.upper()


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class AlpacaFeed:
    venue = "alpaca"

    def __init__(self, http: AlpacaHttp | None = None, data_feed: str | None = None) -> None:
        self.http = http or AlpacaHttp()
        self.data_feed = data_feed or os.environ.get("ALPACA_DATA_FEED", "iex")

    async def connect(self) -> None:  # symmetry with IbkrFeed; REST needs no session
        return None

    async def disconnect(self) -> None:
        return None

    async def backfill(
        self, symbol: str, start: datetime, end: datetime, interval: str = "1d"
    ) -> list[MarketData]:
        sym = _check_symbol(symbol)
        out: list[MarketData] = []
        token: str | None = None
        now = datetime.now(UTC)
        while True:
            page = await self.http.request(
                "GET", f"{DATA_URL}/v2/stocks/{sym}/bars",
                params={
                    "timeframe": _TIMEFRAME[interval],
                    "start": _iso(start), "end": _iso(end),
                    "adjustment": "split", "feed": self.data_feed,
                    "limit": 10000, "sort": "asc", "page_token": token,
                },
            )
            for b in page.get("bars") or []:
                ts = datetime.fromisoformat(b["t"].replace("Z", "+00:00")).astimezone(UTC)
                if ts < start or ts > end:
                    continue
                out.append(MarketData(
                    ts=ts, received_ts=now, symbol=symbol, source=self.venue,
                    open=float(b["o"]), high=float(b["h"]), low=float(b["l"]),
                    close=float(b["c"]), volume=float(b.get("v") or 0.0),
                    bar_interval=interval,
                ))
            token = page.get("next_page_token")
            if not token:
                return out

    async def stream(self, symbols: list[str], interval: str = "5m"):  # pragma: no cover
        raise NotImplementedError(
            "AlpacaFeed is REST-polled (daily/swing horizon); use backfill()")
