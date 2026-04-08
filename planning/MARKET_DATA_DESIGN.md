# Market Data Backend — Detailed Design

The complete implementation reference for the FinAlly market data subsystem.
Status: **Complete** — 73 tests passing, 84% coverage.

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [File Structure](#2-file-structure)
3. [Data Model — models.py](#3-data-model--modelspy)
4. [Price Cache — cache.py](#4-price-cache--cachepy)
5. [Abstract Interface — interface.py](#5-abstract-interface--interfacepy)
6. [Seed Prices — seed_prices.py](#6-seed-prices--seed_pricespy)
7. [GBM Simulator — simulator.py](#7-gbm-simulator--simulatorpy)
8. [Massive API Client — massive_client.py](#8-massive-api-client--massive_clientpy)
9. [Factory — factory.py](#9-factory--factorypy)
10. [SSE Streaming — stream.py](#10-sse-streaming--streampy)
11. [FastAPI Lifecycle Integration](#11-fastapi-lifecycle-integration)
12. [Watchlist Coordination](#12-watchlist-coordination)
13. [Reading Prices in Route Handlers](#13-reading-prices-in-route-handlers)
14. [Testing](#14-testing)
15. [Error Handling Reference](#15-error-handling-reference)

---

## 1. Architecture Overview

The market data subsystem uses the **Strategy pattern**: two data source implementations share a single abstract interface. All downstream code reads from a central **PriceCache** — it never knows or cares which data source is active.

```
MarketDataSource (ABC)
├── SimulatorDataSource  →  GBM simulator (default, no API key needed)
└── MassiveDataSource    →  Polygon.io REST poller (when MASSIVE_API_KEY set)
        │
        ▼
   PriceCache (thread-safe, in-memory)
        │
        ├──→ GET /api/stream/prices  (SSE stream to browser)
        ├──→ GET /api/portfolio      (P&L calculation)
        └──→ POST /api/portfolio/trade  (trade execution)
```

### Data Flow

1. **Startup**: `create_market_data_source(cache)` inspects `MASSIVE_API_KEY` and returns the right implementation.
2. **Background task**: The chosen source runs a continuous loop — 500ms for the simulator, 15s for Massive — writing to `PriceCache` on every cycle.
3. **SSE endpoint**: Polls `PriceCache` every 500ms using a version counter to detect changes; pushes all ticker prices to connected browsers when the version advances.
4. **API routes**: Read `PriceCache` synchronously via dependency injection — no blocking I/O.
5. **Dynamic watchlist**: `add_ticker()` / `remove_ticker()` modify the active set at runtime; new tickers appear in the SSE stream on the next tick without client reconnection.

### Design Principles

| Principle | Implementation |
|-----------|----------------|
| Source-agnostic consumers | All downstream code depends on `PriceCache`, not on the data source |
| Single point of truth | `PriceCache` is the only place prices are read from |
| Thread safety | `threading.Lock` in `PriceCache` (not `asyncio.Lock`) because Massive runs in a thread |
| Async-safe | Massive's sync SDK runs in `asyncio.to_thread()`; event loop is never blocked |
| Immediate data | Both sources seed the cache at startup so the first SSE event has data |

---

## 2. File Structure

```
backend/
  app/
    market/
      __init__.py          # Public re-exports
      models.py            # PriceUpdate dataclass
      cache.py             # PriceCache — thread-safe in-memory store
      interface.py         # MarketDataSource ABC
      seed_prices.py       # Seed prices and GBM parameters
      simulator.py         # GBMSimulator + SimulatorDataSource
      massive_client.py    # MassiveDataSource (Polygon.io REST)
      factory.py           # create_market_data_source() factory
      stream.py            # FastAPI SSE endpoint

  tests/
    market/
      test_models.py         # 11 tests — 100% coverage
      test_cache.py          # 13 tests — 100% coverage
      test_simulator.py      # 17 tests — 98% coverage
      test_simulator_source.py  # 10 integration tests
      test_factory.py        # 7 tests — 100% coverage
      test_massive.py        # 13 tests — 56% (API methods mocked)
```

The `__init__.py` re-exports all public symbols:

```python
from app.market import (
    PriceUpdate,
    PriceCache,
    MarketDataSource,
    create_market_data_source,
    create_stream_router,
)
```


---

## 3. Data Model — `models.py`

`PriceUpdate` is an **immutable frozen dataclass** and the only data structure that crosses from the market data layer to the rest of the backend.

```python
from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class PriceUpdate:
    ticker: str
    price: float
    previous_price: float
    timestamp: float = field(default_factory=time.time)  # Unix seconds

    @property
    def change(self) -> float:
        return round(self.price - self.previous_price, 4)

    @property
    def change_percent(self) -> float:
        if self.previous_price == 0:
            return 0.0
        return round((self.price - self.previous_price) / self.previous_price * 100, 4)

    @property
    def direction(self) -> str:
        if self.price > self.previous_price:
            return "up"
        elif self.price < self.previous_price:
            return "down"
        return "flat"

    def to_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "price": self.price,
            "previous_price": self.previous_price,
            "timestamp": self.timestamp,
            "change": self.change,
            "change_percent": self.change_percent,
            "direction": self.direction,
        }
```

### Example `to_dict()` output

```json
{
  "ticker": "AAPL",
  "price": 191.45,
  "previous_price": 190.22,
  "timestamp": 1705692894.63,
  "change": 1.23,
  "change_percent": 0.6466,
  "direction": "up"
}
```

### Design decisions

- **`frozen=True`**: Immutable value objects, safe to share across async tasks without copying.
- **`slots=True`**: Memory optimization for high-frequency instantiation.
- **Computed properties**: `change`, `direction`, `change_percent` derived from `price` and `previous_price` — never inconsistent.
- **`to_dict()`**: Single serialization point for both SSE events and REST responses.
- **First-update semantics**: When a ticker is first seen, `previous_price == price` → `direction == "flat"`. Prevents spurious flash animations on first render.


---

## 4. Price Cache — `cache.py`

Thread-safe in-memory store. One instance per app, shared between the data source (writer) and all consumers (readers).

```python
from __future__ import annotations

import time
from threading import Lock

from .models import PriceUpdate


class PriceCache:
    def __init__(self) -> None:
        self._prices: dict[str, PriceUpdate] = {}
        self._lock = Lock()
        self._version: int = 0

    def update(self, ticker: str, price: float, timestamp: float | None = None) -> PriceUpdate:
        with self._lock:
            ts = timestamp or time.time()
            prev = self._prices.get(ticker)
            previous_price = prev.price if prev else price
            update = PriceUpdate(
                ticker=ticker,
                price=round(price, 2),
                previous_price=round(previous_price, 2),
                timestamp=ts,
            )
            self._prices[ticker] = update
            self._version += 1
            return update

    def get(self, ticker: str) -> PriceUpdate | None:
        with self._lock:
            return self._prices.get(ticker)

    def get_all(self) -> dict[str, PriceUpdate]:
        """Returns a shallow copy — safe to iterate while background task writes."""
        with self._lock:
            return dict(self._prices)

    def get_price(self, ticker: str) -> float | None:
        update = self.get(ticker)
        return update.price if update else None

    def remove(self, ticker: str) -> None:
        with self._lock:
            self._prices.pop(ticker, None)

    @property
    def version(self) -> int:
        """Monotonic counter — increments on every update. Used by SSE for change detection."""
        return self._version

    def __len__(self) -> int:
        with self._lock:
            return len(self._prices)

    def __contains__(self, ticker: str) -> bool:
        with self._lock:
            return ticker in self._prices
```

### Why a version counter?

The SSE streaming loop polls every 500ms. Without a version counter it would send all prices every tick even when nothing changed (Massive API only updates every 15s). The version counter lets the loop skip sends:

```python
last_version = -1
while True:
    current = price_cache.version
    if current != last_version:
        last_version = current
        yield format_sse(price_cache.get_all())
    await asyncio.sleep(0.5)
```

### Why `threading.Lock` instead of `asyncio.Lock`?

The Massive client calls its synchronous SDK inside `asyncio.to_thread()`, which runs in a real OS thread. `asyncio.Lock` cannot be acquired from OS threads — only from the event loop. `threading.Lock` works correctly from both.

---

## 5. Abstract Interface — `interface.py`

All data sources implement this ABC. Downstream code depends only on this interface.

```python
from abc import ABC, abstractmethod


class MarketDataSource(ABC):
    """Contract for market data providers.

    Both implementations push price updates into a shared PriceCache.
    Downstream code reads from the cache; it never calls the source directly.

    Lifecycle:
        source = create_market_data_source(cache)
        await source.start(["AAPL", "GOOGL", ...])
        await source.add_ticker("TSLA")     # dynamic add
        await source.remove_ticker("GOOGL")  # dynamic remove
        await source.stop()                   # shutdown
    """

    @abstractmethod
    async def start(self, tickers: list[str]) -> None:
        """Start background task, seed cache. Call exactly once."""

    @abstractmethod
    async def stop(self) -> None:
        """Cancel background task. Safe to call multiple times."""

    @abstractmethod
    async def add_ticker(self, ticker: str) -> None:
        """Add a ticker. No-op if already present. Takes effect next cycle."""

    @abstractmethod
    async def remove_ticker(self, ticker: str) -> None:
        """Remove a ticker. Also removes from PriceCache."""

    @abstractmethod
    def get_tickers(self) -> list[str]:
        """Return list of currently tracked tickers."""
```

### Push vs pull

The source **pushes** to `PriceCache` rather than returning prices on demand. This decouples timing: the simulator ticks at 500ms, Massive polls at 15s, but SSE always reads from the cache at its own 500ms cadence. The SSE layer never needs to know which source is active.

---

## 6. Seed Prices — `seed_prices.py`

Constants only — no logic, no imports. Shared by the simulator (initial prices and GBM parameters).

```python
# Realistic starting prices for the default watchlist
SEED_PRICES: dict[str, float] = {
    "AAPL": 190.00,  "GOOGL": 175.00,  "MSFT": 420.00,
    "AMZN": 185.00,  "TSLA": 250.00,   "NVDA": 800.00,
    "META": 500.00,  "JPM":  195.00,   "V":    280.00,
    "NFLX": 600.00,
}

# Per-ticker GBM parameters
# sigma: annualized volatility  mu: annualized drift / expected return
TICKER_PARAMS: dict[str, dict[str, float]] = {
    "AAPL":  {"sigma": 0.22, "mu": 0.05},
    "GOOGL": {"sigma": 0.25, "mu": 0.05},
    "MSFT":  {"sigma": 0.20, "mu": 0.05},
    "AMZN":  {"sigma": 0.28, "mu": 0.05},
    "TSLA":  {"sigma": 0.50, "mu": 0.03},  # High volatility
    "NVDA":  {"sigma": 0.40, "mu": 0.08},  # High volatility, strong drift
    "META":  {"sigma": 0.30, "mu": 0.05},
    "JPM":   {"sigma": 0.18, "mu": 0.04},  # Low volatility (bank)
    "V":     {"sigma": 0.17, "mu": 0.04},  # Low volatility (payments)
    "NFLX":  {"sigma": 0.35, "mu": 0.05},
}

# Default parameters for dynamically added tickers
DEFAULT_PARAMS: dict[str, float] = {"sigma": 0.25, "mu": 0.05}

# Correlation groups for Cholesky decomposition
CORRELATION_GROUPS: dict[str, set[str]] = {
    "tech":    {"AAPL", "GOOGL", "MSFT", "AMZN", "META", "NVDA", "NFLX"},
    "finance": {"JPM", "V"},
}

INTRA_TECH_CORR    = 0.6   # Tech stocks move together
INTRA_FINANCE_CORR = 0.5   # Finance stocks move together
CROSS_GROUP_CORR   = 0.3   # Between sectors / unknown tickers
TSLA_CORR          = 0.3   # TSLA does its own thing
```

### Parameter tuning rationale

| Ticker | sigma | Note |
|--------|-------|------|
| TSLA | 0.50 | Highest volatility — dramatic moves |
| NVDA | 0.40 | High volatility, strong upward drift (0.08) |
| NFLX | 0.35 | Volatile growth stock |
| AAPL | 0.22 | Moderate — large cap, relatively stable |
| JPM  | 0.18 | Low volatility — financial institution |
| V    | 0.17 | Lowest — stable payments business |

Tickers not in `SEED_PRICES` start at a random price between $50–$300 and use `DEFAULT_PARAMS`.

---

## 7. GBM Simulator — `simulator.py`

Two classes: `GBMSimulator` (pure math engine, no I/O) and `SimulatorDataSource` (async wrapper that owns the background task).

### 7.1 The GBM Formula

Each tick advances every ticker using the standard GBM discretization:

```
S(t + dt) = S(t) x exp( (mu - sigma^2/2) * dt  +  sigma * sqrt(dt) * Z )
```

Where:
- `S(t)` = current price
- `mu` = annualized drift (expected return, e.g. 0.05 = 5%/year)
- `sigma` = annualized volatility (e.g. 0.25 = 25%/year)
- `dt` = time step as a fraction of a trading year
- `Z` = correlated standard normal random variable

```python
# 500ms as a fraction of a trading year
# 252 trading days * 6.5 hours/day * 3600 seconds/hour = 5,896,800 seconds
TRADING_SECONDS_PER_YEAR = 252 * 6.5 * 3600  # 5,896,800
DEFAULT_DT = 0.5 / TRADING_SECONDS_PER_YEAR   # ~8.48e-8
```

At this `dt`, a single tick produces sub-cent moves. The `exp()` form guarantees prices can never go negative.

### 7.2 Correlated Moves

Without correlation, every ticker moves independently. Real markets show sector correlation — when AAPL drops, GOOGL and MSFT tend to drop too.

**Approach:**
1. Build an `n x n` correlation matrix `Sigma` based on sector membership
2. Compute Cholesky decomposition `L` so that `L @ L^T = Sigma`
3. At each tick, generate `n` independent standard normal draws `z`
4. Apply `L @ z` to get correlated draws with the right covariance structure

```python
z_independent = np.random.standard_normal(n)  # shape: (n,)
z_correlated  = self._cholesky @ z_independent  # shape: (n,)
# Each z_correlated[i] feeds into GBM for ticker i
```

### 7.3 GBMSimulator — Full Code

```python
import math, random, logging
import numpy as np
from .seed_prices import (
    CORRELATION_GROUPS, CROSS_GROUP_CORR, DEFAULT_PARAMS,
    INTRA_FINANCE_CORR, INTRA_TECH_CORR, SEED_PRICES,
    TICKER_PARAMS, TSLA_CORR,
)

logger = logging.getLogger(__name__)


class GBMSimulator:
    TRADING_SECONDS_PER_YEAR = 252 * 6.5 * 3600
    DEFAULT_DT = 0.5 / TRADING_SECONDS_PER_YEAR  # ~8.48e-8

    def __init__(self, tickers: list[str], dt: float = DEFAULT_DT,
                 event_probability: float = 0.001) -> None:
        self._dt = dt
        self._event_prob = event_probability
        self._tickers: list[str] = []
        self._prices: dict[str, float] = {}
        self._params: dict[str, dict[str, float]] = {}
        self._cholesky: np.ndarray | None = None

        for ticker in tickers:
            self._add_ticker_internal(ticker)
        self._rebuild_cholesky()

    def step(self) -> dict[str, float]:
        """Advance all tickers one time step. Returns {ticker: new_price}.
        This is the hot path — called every 500ms."""
        n = len(self._tickers)
        if n == 0:
            return {}

        z_independent = np.random.standard_normal(n)
        z_correlated = self._cholesky @ z_independent if self._cholesky is not None else z_independent

        result: dict[str, float] = {}
        for i, ticker in enumerate(self._tickers):
            mu = self._params[ticker]["mu"]
            sigma = self._params[ticker]["sigma"]

            drift = (mu - 0.5 * sigma**2) * self._dt
            diffusion = sigma * math.sqrt(self._dt) * z_correlated[i]
            self._prices[ticker] *= math.exp(drift + diffusion)

            # Random shock: ~0.1% chance per tick per ticker
            # With 10 tickers at 2 ticks/sec, expect an event ~every 50 seconds
            if random.random() < self._event_prob:
                mag = random.uniform(0.02, 0.05)   # 2-5% shock
                sign = random.choice([-1, 1])
                self._prices[ticker] *= 1 + mag * sign
                logger.debug("Shock on %s: %.1f%% %s", ticker,
                             mag * 100, "up" if sign > 0 else "down")

            result[ticker] = round(self._prices[ticker], 2)

        return result

    def add_ticker(self, ticker: str) -> None:
        if ticker in self._prices:
            return
        self._add_ticker_internal(ticker)
        self._rebuild_cholesky()

    def remove_ticker(self, ticker: str) -> None:
        if ticker not in self._prices:
            return
        self._tickers.remove(ticker)
        del self._prices[ticker]
        del self._params[ticker]
        self._rebuild_cholesky()

    def get_price(self, ticker: str) -> float | None:
        return self._prices.get(ticker)

    def get_tickers(self) -> list[str]:
        return list(self._tickers)

    def _add_ticker_internal(self, ticker: str) -> None:
        """Add without rebuilding Cholesky (for batch initialization)."""
        if ticker in self._prices:
            return
        self._tickers.append(ticker)
        self._prices[ticker] = SEED_PRICES.get(ticker, random.uniform(50.0, 300.0))
        self._params[ticker] = TICKER_PARAMS.get(ticker, dict(DEFAULT_PARAMS))

    def _rebuild_cholesky(self) -> None:
        """Rebuild Cholesky decomposition. Called when tickers change. O(n^2), n<50."""
        n = len(self._tickers)
        if n <= 1:
            self._cholesky = None
            return
        corr = np.eye(n)
        for i in range(n):
            for j in range(i + 1, n):
                rho = self._pairwise_correlation(self._tickers[i], self._tickers[j])
                corr[i, j] = rho
                corr[j, i] = rho
        self._cholesky = np.linalg.cholesky(corr)

    @staticmethod
    def _pairwise_correlation(t1: str, t2: str) -> float:
        """Sector-based pairwise correlation."""
        tech = CORRELATION_GROUPS["tech"]
        finance = CORRELATION_GROUPS["finance"]
        if t1 == "TSLA" or t2 == "TSLA":
            return TSLA_CORR          # 0.3 — TSLA does its own thing
        if t1 in tech and t2 in tech:
            return INTRA_TECH_CORR    # 0.6
        if t1 in finance and t2 in finance:
            return INTRA_FINANCE_CORR  # 0.5
        return CROSS_GROUP_CORR        # 0.3
```

### 7.4 SimulatorDataSource — Async Wrapper

```python
import asyncio, logging
from .cache import PriceCache
from .interface import MarketDataSource


class SimulatorDataSource(MarketDataSource):
    def __init__(self, price_cache: PriceCache,
                 update_interval: float = 0.5,
                 event_probability: float = 0.001) -> None:
        self._cache = price_cache
        self._interval = update_interval
        self._event_prob = event_probability
        self._sim: GBMSimulator | None = None
        self._task: asyncio.Task | None = None

    async def start(self, tickers: list[str]) -> None:
        self._sim = GBMSimulator(tickers=tickers, event_probability=self._event_prob)
        # Seed cache with initial prices BEFORE the loop starts
        # so SSE has data to send on the very first tick
        for ticker in tickers:
            price = self._sim.get_price(ticker)
            if price is not None:
                self._cache.update(ticker=ticker, price=price)
        self._task = asyncio.create_task(self._run_loop(), name="simulator-loop")
        logger.info("Simulator started with %d tickers", len(tickers))

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

    async def add_ticker(self, ticker: str) -> None:
        if self._sim:
            self._sim.add_ticker(ticker)
            # Seed cache immediately — ticker has a price before next tick
            price = self._sim.get_price(ticker)
            if price is not None:
                self._cache.update(ticker=ticker, price=price)

    async def remove_ticker(self, ticker: str) -> None:
        if self._sim:
            self._sim.remove_ticker(ticker)
        self._cache.remove(ticker)

    def get_tickers(self) -> list[str]:
        return self._sim.get_tickers() if self._sim else []

    async def _run_loop(self) -> None:
        """Core loop: step simulation, write to cache, sleep."""
        while True:
            try:
                if self._sim:
                    prices = self._sim.step()
                    for ticker, price in prices.items():
                        self._cache.update(ticker=ticker, price=price)
            except Exception:
                logger.exception("Simulator step failed")
            await asyncio.sleep(self._interval)
```

### Key behaviors

- **Immediate seeding**: Cache populated with seed prices before the loop starts — no blank-screen on first SSE tick.
- **Graceful cancellation**: `stop()` awaits the cancelled task, catching `CancelledError` — clean shutdown during FastAPI lifespan teardown.
- **Exception resilience**: Exceptions are caught per-step; a single bad tick never kills the feed.
- **Dynamic add**: When `add_ticker()` is called, `GBMSimulator.add_ticker()` seeds the price and rebuilds the Cholesky matrix. The cache is immediately updated so the ticker has a price before the next tick.

---

## 8. Massive API Client — `massive_client.py`

Polls the Massive (Polygon.io) REST API snapshot endpoint on a configurable interval. The synchronous Massive SDK runs in `asyncio.to_thread()` to avoid blocking the event loop.

### Rate Limits

| Plan | Requests / min | Recommended poll interval |
|------|----------------|---------------------------|
| Free | 5 | 15 seconds (default) |
| Starter | 100 | 2-5 seconds |
| Developer | Unlimited | 500ms-2 seconds |

### Full Code

```python
from __future__ import annotations

import asyncio, logging

from massive import RESTClient
from massive.rest.models import SnapshotMarketType

from .cache import PriceCache
from .interface import MarketDataSource

logger = logging.getLogger(__name__)


class MassiveDataSource(MarketDataSource):
    """Polls Massive (Polygon.io) REST snapshot endpoint.

    Rate limits:
      Free tier: 5 req/min -> poll every 15s (default)
      Paid tiers: poll every 2-5s
    """

    def __init__(self, api_key: str, price_cache: PriceCache,
                 poll_interval: float = 15.0) -> None:
        self._api_key = api_key
        self._cache = price_cache
        self._interval = poll_interval
        self._tickers: list[str] = []
        self._task: asyncio.Task | None = None
        self._client: RESTClient | None = None

    async def start(self, tickers: list[str]) -> None:
        self._client = RESTClient(api_key=self._api_key)
        self._tickers = list(tickers)
        await self._poll_once()  # Immediate first poll
        self._task = asyncio.create_task(self._poll_loop(), name="massive-poller")
        logger.info("Massive poller started: %d tickers, %.1fs interval",
                    len(tickers), self._interval)

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        self._client = None

    async def add_ticker(self, ticker: str) -> None:
        ticker = ticker.upper().strip()
        if ticker not in self._tickers:
            self._tickers.append(ticker)
            logger.info("Massive: added %s (appears on next poll)", ticker)

    async def remove_ticker(self, ticker: str) -> None:
        ticker = ticker.upper().strip()
        self._tickers = [t for t in self._tickers if t != ticker]
        self._cache.remove(ticker)

    def get_tickers(self) -> list[str]:
        return list(self._tickers)

    async def _poll_loop(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            await self._poll_once()

    async def _poll_once(self) -> None:
        if not self._tickers or not self._client:
            return
        try:
            # RESTClient is synchronous — run in thread pool
            snapshots = await asyncio.to_thread(self._fetch_snapshots)
            processed = 0
            for snap in snapshots:
                try:
                    price = snap.last_trade.price
                    # Massive timestamps are Unix milliseconds — convert to seconds
                    timestamp = snap.last_trade.timestamp / 1000.0
                    self._cache.update(ticker=snap.ticker, price=price, timestamp=timestamp)
                    processed += 1
                except (AttributeError, TypeError) as e:
                    logger.warning("Skipping snapshot for %s: %s",
                                   getattr(snap, "ticker", "???"), e)
            logger.debug("Massive poll: updated %d/%d tickers",
                         processed, len(self._tickers))
        except Exception as e:
            logger.error("Massive poll failed: %s", e)
            # Don't re-raise — cache retains last-known prices

    def _fetch_snapshots(self) -> list:
        """Synchronous REST call. Runs in asyncio.to_thread()."""
        return self._client.get_snapshot_all(
            market_type=SnapshotMarketType.STOCKS,
            tickers=self._tickers,
        )
```

### Snapshot object field access

```python
# Key fields on each snapshot object:
snap.ticker                      # str — ticker symbol
snap.last_trade.price            # float — last trade price
snap.last_trade.timestamp        # int — Unix MILLISECONDS (divide by 1000)
snap.todays_change_perc          # float — % change from prev close
```

### Error handling

| Error | Behavior |
|-------|----------|
| 401 Unauthorized | Logged as error; poller keeps running |
| 429 Rate Limited | Logged as error; retries after `poll_interval` |
| Network timeout | Logged as error; retries automatically |
| Malformed snapshot | Individual ticker skipped with warning; others still processed |
| All tickers fail | Cache retains last-known prices; SSE streams stale data |

---

## 9. Factory — `factory.py`

Selects the implementation based on environment variables. All app startup code calls this — never instantiate `SimulatorDataSource` or `MassiveDataSource` directly.

```python
from __future__ import annotations

import logging, os

from .cache import PriceCache
from .interface import MarketDataSource
from .massive_client import MassiveDataSource
from .simulator import SimulatorDataSource

logger = logging.getLogger(__name__)


def create_market_data_source(price_cache: PriceCache) -> MarketDataSource:
    """Create the appropriate data source based on environment variables.

    - MASSIVE_API_KEY set and non-empty -> MassiveDataSource (real data)
    - Otherwise -> SimulatorDataSource (GBM simulation)

    Returns an unstarted source. Caller must await source.start(tickers).
    """
    api_key = os.environ.get("MASSIVE_API_KEY", "").strip()

    if api_key:
        logger.info("Market data source: Massive API (real data)")
        return MassiveDataSource(api_key=api_key, price_cache=price_cache)
    else:
        logger.info("Market data source: GBM Simulator")
        return SimulatorDataSource(price_cache=price_cache)
```

### Usage

```python
cache = PriceCache()
source = create_market_data_source(cache)  # reads MASSIVE_API_KEY
await source.start(["AAPL", "GOOGL", ...])  # seeds cache, starts background task
```

---

## 10. SSE Streaming — `stream.py`

The SSE endpoint holds open a long-lived HTTP connection and pushes price updates to the browser as `text/event-stream`.

```python
from __future__ import annotations

import asyncio, json, logging
from collections.abc import AsyncGenerator

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from .cache import PriceCache

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/stream", tags=["streaming"])


def create_stream_router(price_cache: PriceCache) -> APIRouter:
    """Factory: returns the SSE router with an injected PriceCache."""

    @router.get("/prices")
    async def stream_prices(request: Request) -> StreamingResponse:
        return StreamingResponse(
            _generate_events(price_cache, request),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",  # Disable nginx buffering
            },
        )

    return router


async def _generate_events(
    price_cache: PriceCache,
    request: Request,
    interval: float = 0.5,
) -> AsyncGenerator[str, None]:
    """Async generator yielding SSE-formatted price events."""
    yield "retry: 1000\n\n"  # Browser retries after 1s on disconnect

    last_version = -1
    client_ip = request.client.host if request.client else "unknown"
    logger.info("SSE client connected: %s", client_ip)

    try:
        while True:
            if await request.is_disconnected():
                logger.info("SSE client disconnected: %s", client_ip)
                break

            current_version = price_cache.version
            if current_version != last_version:
                last_version = current_version
                prices = price_cache.get_all()
                if prices:
                    data = {ticker: update.to_dict() for ticker, update in prices.items()}
                    payload = json.dumps(data)
                    yield f"data: {payload}\n\n"

            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        logger.info("SSE stream cancelled for: %s", client_ip)
```

### Wire format

Each event sent to the browser:

```
data: {"AAPL":{"ticker":"AAPL","price":190.50,"previous_price":190.42,"timestamp":1707580800.5,"change":0.08,"change_percent":0.042,"direction":"up"},"GOOGL":{...},...}

```

### Frontend consumption

```typescript
const es = new EventSource('/api/stream/prices');

es.onmessage = (event) => {
    const prices = JSON.parse(event.data) as Record<string, PriceUpdate>;
    // prices["AAPL"] = { ticker, price, previous_price, timestamp, change, change_percent, direction }
    // Flash green/red on direction change, accumulate into sparkline history
};

// EventSource reconnects automatically (built-in browser behavior)
// The retry: 1000 directive sets 1-second reconnect delay
```

### Why poll-and-push instead of event-driven?

The SSE endpoint polls the cache on a fixed 500ms interval rather than being notified by the data source. This produces regular, evenly-spaced updates for sparkline charts. Event-driven would create irregular update bursts and gaps.

---

## 11. FastAPI Lifecycle Integration

Wire up in `backend/app/main.py` using the `lifespan` context manager:

```python
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.market import PriceCache, MarketDataSource, create_market_data_source, create_stream_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- STARTUP ---

    # 1. Create shared price cache
    price_cache = PriceCache()
    app.state.price_cache = price_cache

    # 2. Create data source (simulator or Massive based on MASSIVE_API_KEY)
    source = create_market_data_source(price_cache)
    app.state.market_source = source

    # 3. Load initial tickers from the database watchlist
    initial_tickers = await load_watchlist_tickers()  # reads from SQLite
    await source.start(initial_tickers)

    # 4. Register SSE streaming router
    app.include_router(create_stream_router(price_cache))

    yield  # App is running

    # --- SHUTDOWN ---
    await source.stop()


app = FastAPI(title="FinAlly", lifespan=lifespan)


# FastAPI dependencies for injecting market state into routes
def get_price_cache() -> PriceCache:
    return app.state.price_cache


def get_market_source() -> MarketDataSource:
    return app.state.market_source
```

---

## 12. Watchlist Coordination

When the watchlist changes (via REST API or LLM chat), the data source must be notified so it tracks the right set of tickers.

### Adding a ticker

```
User/LLM -> POST /api/watchlist {ticker: "PYPL"}
  -> Insert into watchlist table (SQLite)
  -> await source.add_ticker("PYPL")
       Simulator: GBMSimulator.add_ticker(), rebuilds Cholesky, seeds cache immediately
       Massive: appends to ticker list, appears on next poll (~15s)
  -> Return {ticker: "PYPL", price: <current_price_if_available>}
```

### Removing a ticker

```
User/LLM -> DELETE /api/watchlist/PYPL
  -> Delete from watchlist table
  -> await source.remove_ticker("PYPL")
       Both: removes from active set and PriceCache
       SSE clients stop receiving PYPL on next tick (no reconnect needed)
  -> Return {status: "ok"}
```

### Edge case: ticker has an open position

If the user removes a ticker from the watchlist but still holds shares, the ticker must remain in the data source for portfolio valuation. The watchlist route should check:

```python
@router.delete("/watchlist/{ticker}")
async def remove_from_watchlist(
    ticker: str,
    source: MarketDataSource = Depends(get_market_source),
):
    await db.delete_watchlist_entry(ticker)

    # Only stop tracking if no open position
    position = await db.get_position(ticker)
    if position is None or position.quantity == 0:
        await source.remove_ticker(ticker)

    return {"status": "ok"}
```

---

## 13. Reading Prices in Route Handlers

All API routes use FastAPI dependency injection to access the price cache:

```python
from fastapi import APIRouter, Depends, HTTPException


router = APIRouter(prefix="/api")


# Trade execution — get current price before executing
@router.post("/portfolio/trade")
async def execute_trade(
    trade: TradeRequest,
    price_cache: PriceCache = Depends(get_price_cache),
):
    current_price = price_cache.get_price(trade.ticker)
    if current_price is None:
        raise HTTPException(404, f"No price available for {trade.ticker}")
    # execute trade at current_price ...


# Portfolio valuation — compute P&L for all positions
@router.get("/portfolio")
async def get_portfolio(
    price_cache: PriceCache = Depends(get_price_cache),
):
    all_prices = price_cache.get_all()  # dict[str, PriceUpdate]
    positions = await db.get_positions()
    result = []
    for pos in positions:
        update = all_prices.get(pos.ticker)
        current_price = update.price if update else pos.avg_cost
        unrealized_pnl = (current_price - pos.avg_cost) * pos.quantity
        result.append({...})
    return result


# Watchlist — get prices alongside ticker info
@router.get("/watchlist")
async def get_watchlist(
    price_cache: PriceCache = Depends(get_price_cache),
):
    tickers = await db.get_watchlist()
    all_prices = price_cache.get_all()
    return [
        {"ticker": t, "price": all_prices.get(t, {}).get("price")}
        for t in tickers
    ]
```

---

## 14. Testing

73 tests, all passing. 84% overall coverage. 6 test modules in `backend/tests/market/`.

| Module | Tests | Coverage |
|--------|-------|----------|
| test_models.py | 11 | 100% |
| test_cache.py | 13 | 100% |
| test_simulator.py | 17 | 98% |
| test_simulator_source.py | 10 | Integration |
| test_factory.py | 7 | 100% |
| test_massive.py | 13 | 56% (API mocked) |

### Running tests

```bash
cd backend
uv run --extra dev pytest -v              # All tests
uv run --extra dev pytest --cov=app       # With coverage
uv run --extra dev ruff check app/ tests/ # Lint
```

### Unit Tests — GBMSimulator

```python
# backend/tests/market/test_simulator.py
from app.market.seed_prices import SEED_PRICES
from app.market.simulator import GBMSimulator


class TestGBMSimulator:

    def test_prices_are_positive(self):
        """GBM prices can never go negative (exp() is always positive)."""
        sim = GBMSimulator(tickers=["AAPL"])
        for _ in range(10_000):
            assert sim.step()["AAPL"] > 0

    def test_initial_prices_match_seeds(self):
        sim = GBMSimulator(tickers=["AAPL"])
        assert sim.get_price("AAPL") == SEED_PRICES["AAPL"]

    def test_cholesky_rebuilds_on_add(self):
        sim = GBMSimulator(tickers=["AAPL"])
        assert sim._cholesky is None  # 1 ticker: no matrix
        sim.add_ticker("GOOGL")
        assert sim._cholesky is not None  # 2 tickers: matrix built

    def test_add_duplicate_is_noop(self):
        sim = GBMSimulator(tickers=["AAPL"])
        sim.add_ticker("AAPL")
        assert len(sim._tickers) == 1

    def test_unknown_ticker_gets_random_seed_price(self):
        sim = GBMSimulator(tickers=["ZZZZ"])
        price = sim.get_price("ZZZZ")
        assert price is not None
        assert 50.0 <= price <= 300.0

    def test_pairwise_correlation_tech_stocks(self):
        assert GBMSimulator._pairwise_correlation("AAPL", "GOOGL") == 0.6

    def test_pairwise_correlation_finance_stocks(self):
        assert GBMSimulator._pairwise_correlation("JPM", "V") == 0.5

    def test_pairwise_correlation_tsla(self):
        assert GBMSimulator._pairwise_correlation("TSLA", "AAPL") == 0.3

    def test_prices_rounded_to_two_decimals(self):
        sim = GBMSimulator(tickers=["AAPL"])
        price_str = str(sim.step()["AAPL"])
        if "." in price_str:
            assert len(price_str.split(".")[1]) <= 2
```

### Unit Tests — PriceCache

```python
# backend/tests/market/test_cache.py
from app.market.cache import PriceCache


class TestPriceCache:

    def test_first_update_is_flat(self):
        cache = PriceCache()
        update = cache.update("AAPL", 190.50)
        assert update.direction == "flat"
        assert update.previous_price == 190.50

    def test_direction_up(self):
        cache = PriceCache()
        cache.update("AAPL", 190.00)
        update = cache.update("AAPL", 191.00)
        assert update.direction == "up"
        assert update.change == 1.00

    def test_version_increments(self):
        cache = PriceCache()
        v0 = cache.version
        cache.update("AAPL", 190.00)
        assert cache.version == v0 + 1

    def test_remove(self):
        cache = PriceCache()
        cache.update("AAPL", 190.00)
        cache.remove("AAPL")
        assert cache.get("AAPL") is None
```

### Integration Tests — MassiveDataSource (mocked)

```python
# backend/tests/market/test_massive.py
from unittest.mock import MagicMock, patch
import pytest
from app.market.cache import PriceCache
from app.market.massive_client import MassiveDataSource


def _make_snapshot(ticker, price, ts_ms):
    snap = MagicMock()
    snap.ticker = ticker
    snap.last_trade = MagicMock()
    snap.last_trade.price = price
    snap.last_trade.timestamp = ts_ms
    return snap


@pytest.mark.asyncio
class TestMassiveDataSource:

    async def test_poll_updates_cache(self):
        cache = PriceCache()
        source = MassiveDataSource(api_key="test", price_cache=cache, poll_interval=60.0)
        source._tickers = ["AAPL"]
        source._client = MagicMock()

        mock_snaps = [_make_snapshot("AAPL", 190.50, 1707580800000)]
        with patch.object(source, "_fetch_snapshots", return_value=mock_snaps):
            await source._poll_once()

        assert cache.get_price("AAPL") == 190.50

    async def test_timestamp_conversion(self):
        """Massive timestamps are milliseconds; cache stores seconds."""
        cache = PriceCache()
        source = MassiveDataSource(api_key="test", price_cache=cache, poll_interval=60.0)
        source._tickers = ["AAPL"]
        source._client = MagicMock()

        with patch.object(source, "_fetch_snapshots",
                          return_value=[_make_snapshot("AAPL", 190.50, 1707580800000)]):
            await source._poll_once()

        assert cache.get("AAPL").timestamp == 1707580800.0

    async def test_malformed_snapshot_skipped(self):
        cache = PriceCache()
        source = MassiveDataSource(api_key="test", price_cache=cache, poll_interval=60.0)
        source._tickers = ["AAPL", "BAD"]
        source._client = MagicMock()

        bad_snap = MagicMock()
        bad_snap.ticker = "BAD"
        bad_snap.last_trade = None  # causes AttributeError

        with patch.object(source, "_fetch_snapshots",
                          return_value=[_make_snapshot("AAPL", 190.50, 1707580800000), bad_snap]):
            await source._poll_once()

        assert cache.get_price("AAPL") == 190.50
        assert cache.get_price("BAD") is None

    async def test_api_error_does_not_crash(self):
        cache = PriceCache()
        source = MassiveDataSource(api_key="test", price_cache=cache, poll_interval=60.0)
        source._tickers = ["AAPL"]
        source._client = MagicMock()

        with patch.object(source, "_fetch_snapshots", side_effect=Exception("network")):
            await source._poll_once()  # must not raise

        assert cache.get_price("AAPL") is None

    async def test_stop_cancels_task(self):
        cache = PriceCache()
        source = MassiveDataSource(api_key="test", price_cache=cache, poll_interval=10.0)

        with patch("app.market.massive_client.RESTClient"):
            with patch.object(source, "_fetch_snapshots", return_value=[]):
                await source.start(["AAPL"])

        assert source._task is not None and not source._task.done()
        await source.stop()
        assert source._task is None
```

---

## 15. Error Handling Reference

### Simulator errors

| Scenario | Behavior |
|----------|----------|
| Exception in `step()` | Caught and logged; loop continues on next tick |
| `add_ticker()` with no `_sim` | No-op (safe before `start()` is called) |
| `remove_ticker()` for unknown ticker | No-op (both `GBMSimulator.remove_ticker` and `cache.remove`) |
| `stop()` called twice | No-op (task already done check) |

### Massive API errors

| Error | Behavior |
|-------|----------|
| 401 Unauthorized | Logged as error; poller keeps running with stale cache |
| 429 Rate Limited | Logged as error; retries after `poll_interval` seconds |
| Network timeout | Logged as error; retries automatically on next cycle |
| Malformed snapshot (one ticker) | Skip with warning; all other tickers still processed |
| Empty ticker list | `_poll_once()` returns early without calling API |
| All tickers fail | Cache retains last-known prices; SSE streams stale but valid data |

### Cache access errors

| Scenario | Behavior |
|----------|----------|
| `get("UNKNOWN")` | Returns `None` — callers must check |
| `get_price("UNKNOWN")` | Returns `None` — callers must check |
| `remove("UNKNOWN")` | No-op (`dict.pop` with default) |
| Concurrent read while writing | Thread-safe via `threading.Lock` |

### SSE stream

| Scenario | Behavior |
|----------|----------|
| Client disconnects | Loop detects via `request.is_disconnected()`, exits cleanly |
| Task cancelled | `asyncio.CancelledError` caught, logged, stream ends |
| Empty cache | Skips sending event (`if prices:` guard) |
| Browser tab closed | `EventSource` will reconnect after 1 second (`retry: 1000`) |

---

## Quick Reference

```python
# All public symbols available from:
from app.market import (
    PriceUpdate,              # Immutable price snapshot dataclass
    PriceCache,               # Thread-safe in-memory store
    MarketDataSource,         # ABC (do not instantiate directly)
    create_market_data_source,  # Factory: reads MASSIVE_API_KEY
    create_stream_router,     # FastAPI router for GET /api/stream/prices
)

# Startup
cache = PriceCache()
source = create_market_data_source(cache)
await source.start(["AAPL", "GOOGL", ...])  # seeds cache + starts background task

# Read prices
update = cache.get("AAPL")          # PriceUpdate | None
price  = cache.get_price("AAPL")    # float | None
all_p  = cache.get_all()             # dict[str, PriceUpdate]

# Dynamic watchlist
await source.add_ticker("TSLA")      # appears in SSE immediately (sim) or next poll (massive)
await source.remove_ticker("GOOGL")  # removed from active set and cache

# Shutdown
await source.stop()
```
