# Market Data Backend — Complete Reference

**Status:** Complete, tested, reviewed.  
**Location:** `backend/app/market/` (8 modules) + `backend/tests/market/` (6 test modules, 73 tests)

---

## Architecture

The market data subsystem uses the **Strategy pattern**: two data source implementations share a single abstract interface. All downstream code reads from a central `PriceCache` and never knows which source is active.

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

1. **Startup** — `create_market_data_source(cache)` reads `MASSIVE_API_KEY` and returns the right implementation.
2. **Background task** — the chosen source runs continuously: 500ms for the simulator, 15s for Massive; writes to `PriceCache` on every cycle.
3. **SSE endpoint** — polls `PriceCache` every 500ms using a version counter to detect changes; pushes all ticker prices to connected browsers when the version advances.
4. **API routes** — read `PriceCache` synchronously via dependency injection; no blocking I/O.
5. **Dynamic watchlist** — `add_ticker()` / `remove_ticker()` modify the active set at runtime; new tickers appear in the SSE stream on the next tick without client reconnection.

### Design Principles

| Principle | Implementation |
|-----------|----------------|
| Source-agnostic consumers | Downstream code depends on `PriceCache`, not on the source |
| Single point of truth | `PriceCache` is the only place prices are read from |
| Thread safety | `threading.Lock` in `PriceCache` — works from both asyncio and OS threads |
| Async-safe | Massive's sync SDK runs in `asyncio.to_thread()`; event loop is never blocked |
| Immediate data | Both sources seed the cache at startup so the first SSE event has data |

---

## Module Map

```
backend/app/market/
├── __init__.py          # Public re-exports
├── models.py            # PriceUpdate — immutable frozen dataclass
├── cache.py             # PriceCache — thread-safe in-memory store
├── interface.py         # MarketDataSource — abstract base class
├── seed_prices.py       # Seed prices, per-ticker GBM params, correlation groups
├── simulator.py         # GBMSimulator (math) + SimulatorDataSource (async wrapper)
├── massive_client.py    # MassiveDataSource — Polygon.io REST poller
├── factory.py           # create_market_data_source() — env-driven factory
└── stream.py            # create_stream_router() — FastAPI SSE endpoint
```

Public imports from `app/market/__init__.py`:

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

## Data Model — `models.py`

`PriceUpdate` is an **immutable frozen dataclass** — the only data structure crossing from the market data layer to the rest of the backend.

```python
@dataclass(frozen=True, slots=True)
class PriceUpdate:
    ticker: str
    price: float
    previous_price: float
    timestamp: float          # Unix seconds

    @property
    def change(self) -> float: ...          # price - previous_price
    @property
    def change_percent(self) -> float: ...  # % change (4 decimal places)
    @property
    def direction(self) -> str: ...         # "up" | "down" | "flat"

    def to_dict(self) -> dict: ...          # JSON-serializable dict for SSE/API
```

`to_dict()` output (used by SSE events and REST responses):

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

Key design decisions:
- `frozen=True` — immutable value objects, safe to share across async tasks.
- `slots=True` — memory optimization for high-frequency instantiation.
- First-update semantics: when a ticker is first seen, `previous_price == price` → `direction == "flat"`, preventing spurious flash animations.

---

## Price Cache — `cache.py`

Thread-safe in-memory store. One instance per app, shared between the data source (writer) and all consumers (readers).

```python
cache = PriceCache()

# Write (called by background task)
update: PriceUpdate = cache.update("AAPL", price=191.45)
cache.update("MSFT", price=420.10, timestamp=1705692894.63)

# Read (called by SSE endpoint, portfolio routes)
update: PriceUpdate | None      = cache.get("AAPL")
price: float | None             = cache.get_price("AAPL")
all_prices: dict[str, PriceUpdate] = cache.get_all()  # shallow copy

# Remove (called when ticker removed from watchlist)
cache.remove("AAPL")

# SSE change detection
version: int = cache.version   # monotonic counter, increments on every update
```

The `version` counter enables efficient SSE polling — the stream endpoint records the last version it sent and only pushes when `cache.version` changes. This is critical for Massive mode (15s poll interval) to avoid sending identical data every 500ms.

**Why `threading.Lock` instead of `asyncio.Lock`?** The Massive client runs its synchronous SDK inside `asyncio.to_thread()`, which runs in a real OS thread. `asyncio.Lock` can only be acquired from the event loop. `threading.Lock` works correctly from both.

---

## Abstract Interface — `interface.py`

All data sources implement this ABC. Downstream code depends only on this interface, never on `SimulatorDataSource` or `MassiveDataSource` directly.

```python
class MarketDataSource(ABC):

    @abstractmethod
    async def start(self, tickers: list[str]) -> None:
        """Start background task, seed cache with initial prices. Call exactly once."""

    @abstractmethod
    async def stop(self) -> None:
        """Cancel background task. Safe to call multiple times."""

    @abstractmethod
    async def add_ticker(self, ticker: str) -> None:
        """Add a ticker to the active set. No-op if already present.
        Takes effect on the next update cycle."""

    @abstractmethod
    async def remove_ticker(self, ticker: str) -> None:
        """Remove a ticker. Also removes it from the PriceCache."""

    @abstractmethod
    def get_tickers(self) -> list[str]:
        """Return list of currently tracked tickers."""
```

---

## Factory — `factory.py`

Selects the implementation based on environment variables. All startup code calls this — never instantiate `SimulatorDataSource` or `MassiveDataSource` directly.

```python
def create_market_data_source(price_cache: PriceCache) -> MarketDataSource:
    api_key = os.environ.get("MASSIVE_API_KEY", "").strip()
    if api_key:
        return MassiveDataSource(api_key=api_key, price_cache=price_cache)
    else:
        return SimulatorDataSource(price_cache=price_cache)
```

---

## GBM Simulator — `simulator.py`

The simulator generates realistic stock price movement without any external API calls. It is the default when `MASSIVE_API_KEY` is not set.

### Class Structure

```
GBMSimulator               — pure math, no I/O, easily unit-tested
    .step() → dict[str, float]
    .add_ticker(ticker)
    .remove_ticker(ticker)
    .get_price(ticker) → float | None
    .get_tickers() → list[str]

SimulatorDataSource(MarketDataSource)   — asyncio wrapper
    .start(tickers)        → seeds cache, starts background task
    .stop()                → cancels task
    .add_ticker(ticker)    → delegates to GBMSimulator, seeds cache
    .remove_ticker(ticker) → delegates to GBMSimulator, removes from cache
    .get_tickers()         → list[str]
```

### GBM Formula

Each tick advances every ticker by one step using the standard GBM discretization:

```
S(t + dt) = S(t) × exp( (μ - σ²/2) × dt  +  σ × √dt × Z )
```

Where:
- `S(t)` = current price
- `μ` (mu) = annualized drift (e.g. `0.05` = 5%/year)
- `σ` (sigma) = annualized volatility (e.g. `0.25` = 25%/year)
- `dt` = time step as fraction of a trading year (`0.5 / (252 × 6.5 × 3600) ≈ 8.48e-8`)
- `Z` = correlated standard normal random variable

The exponential form guarantees prices stay positive. At this `dt`, a single tick produces sub-cent moves that accumulate naturally over time.

### Correlated Moves

Without correlation, every ticker's price would be independent. The simulator uses **Cholesky decomposition** to produce sector-correlated moves:

1. Build an `n × n` correlation matrix `Σ` based on sector membership.
2. Compute Cholesky decomposition `L` so that `L @ Lᵀ = Σ`.
3. At each tick, generate `n` independent standard normal draws `z`.
4. Apply `z_correlated = L @ z` to get correlated draws.

Correlation values (from `seed_prices.py`):

```python
INTRA_TECH_CORR    = 0.6   # AAPL, GOOGL, MSFT, AMZN, META, NVDA, NFLX
INTRA_FINANCE_CORR = 0.5   # JPM, V
TSLA_CORR          = 0.3   # moves more independently
CROSS_GROUP_CORR   = 0.3   # between sectors / unknown tickers
```

The Cholesky matrix is rebuilt (O(n²)) when tickers are added or removed. Negligible at n < 50.

### Seed Prices and Parameters

```python
SEED_PRICES = {
    "AAPL": 190.00,  "GOOGL": 175.00,  "MSFT": 420.00,
    "AMZN": 185.00,  "TSLA": 250.00,   "NVDA": 800.00,
    "META": 500.00,  "JPM":  195.00,   "V":    280.00,
    "NFLX": 600.00,
}

TICKER_PARAMS = {
    "AAPL":  {"sigma": 0.22, "mu": 0.05},
    "GOOGL": {"sigma": 0.25, "mu": 0.05},
    "MSFT":  {"sigma": 0.20, "mu": 0.05},
    "AMZN":  {"sigma": 0.28, "mu": 0.05},
    "TSLA":  {"sigma": 0.50, "mu": 0.03},  # high vol, lower drift
    "NVDA":  {"sigma": 0.40, "mu": 0.08},  # high vol, strong drift
    "META":  {"sigma": 0.30, "mu": 0.05},
    "JPM":   {"sigma": 0.18, "mu": 0.04},  # low vol (bank)
    "V":     {"sigma": 0.17, "mu": 0.04},  # low vol (payments)
    "NFLX":  {"sigma": 0.35, "mu": 0.05},
}

DEFAULT_PARAMS = {"sigma": 0.25, "mu": 0.05}  # for dynamically added tickers
```

Tickers not in `SEED_PRICES` start at a random price between $50–$300.

### Random Shock Events

At each tick, every ticker has a 0.1% chance of a sudden 2–5% move:

```python
EVENT_PROBABILITY = 0.001  # per tick per ticker

if random.random() < self._event_prob:
    shock_magnitude = random.uniform(0.02, 0.05)
    shock_sign = random.choice([-1, 1])
    self._prices[ticker] *= 1 + shock_magnitude * shock_sign
```

With 10 tickers at 2 ticks/second, the expected interval between events is ~50 seconds.

### Background Loop

```python
async def _run_loop(self) -> None:
    while True:
        try:
            if self._sim:
                prices = self._sim.step()
                for ticker, price in prices.items():
                    self._cache.update(ticker=ticker, price=price)
        except Exception:
            logger.exception("Simulator step failed")
        await asyncio.sleep(self._interval)  # default: 0.5s
```

Exceptions are caught and logged — a single bad tick never kills the loop.

---

## Massive API Client — `massive_client.py`

Used when `MASSIVE_API_KEY` is set. Polls Polygon.io REST API for live prices.

### Rate Limits

| Plan | Requests/min | Recommended poll interval |
|------|-------------|--------------------------|
| Free | 5 | 15 seconds |
| Starter | 100 | 2–5 seconds |
| Developer | Unlimited | 500ms–2 seconds |

FinAlly defaults to 15s (free tier). Adjust via `MassiveDataSource(poll_interval=5.0)`.

### Primary Endpoint — Bulk Snapshots

```python
from massive import RESTClient
from massive.rest.models import SnapshotMarketType

client = RESTClient(api_key=api_key)

snapshots = client.get_snapshot_all(
    market_type=SnapshotMarketType.STOCKS,
    tickers=["AAPL", "MSFT", "TSLA"],
)

for snap in snapshots:
    price  = snap.last_trade.price               # float — last trade price
    ts_s   = snap.last_trade.timestamp / 1000.0  # Unix ms → seconds
```

> **Important:** `last_trade.timestamp` is in Unix **milliseconds** — divide by 1000 before storing in `PriceCache`.

Key fields on each snapshot object:

| Attribute | Description |
|-----------|-------------|
| `snap.ticker` | Ticker symbol |
| `snap.last_trade.price` | Last trade price |
| `snap.last_trade.timestamp` | Unix milliseconds |
| `snap.todays_change_perc` | % change from previous close |
| `snap.day.c` | Current day close / latest price |
| `snap.prevDay.c` | Previous day close |

### Thread Safety

The `RESTClient` is synchronous. Run in a thread to avoid blocking the event loop:

```python
snapshots = await asyncio.to_thread(
    client.get_snapshot_all,
    market_type=SnapshotMarketType.STOCKS,
    tickers=tickers,
)
```

### Error Handling

Exceptions are caught and logged — stale cache values remain available on poll failures:

```python
try:
    snapshots = await asyncio.to_thread(client.get_snapshot_all, ...)
except Exception as e:
    logger.error("Massive poll failed: %s", e)
    # Don't re-raise — stale prices remain in the cache
```

---

## SSE Streaming — `stream.py`

`create_stream_router(price_cache)` returns a FastAPI `APIRouter` with one endpoint:

```
GET /api/stream/prices   →   text/event-stream
```

Wire up in the FastAPI app:

```python
from app.market import create_stream_router

router = create_stream_router(price_cache)
app.include_router(router, prefix="/api")
```

The endpoint uses version-based change detection:

```python
last_version = -1
while True:
    current = price_cache.version
    if current != last_version:
        last_version = current
        yield f"data: {json.dumps(price_cache.get_all_dicts())}\n\n"
    await asyncio.sleep(0.5)
```

Each SSE event contains the full current price table. The frontend uses native `EventSource` with built-in reconnection.

---

## FastAPI Lifecycle Integration

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI
from app.market import PriceCache, create_market_data_source, create_stream_router

price_cache = PriceCache()

@asynccontextmanager
async def lifespan(app: FastAPI):
    source = create_market_data_source(price_cache)
    await source.start(watchlist_tickers_from_db())  # seeds cache, starts background task
    yield
    await source.stop()

app = FastAPI(lifespan=lifespan)
app.include_router(create_stream_router(price_cache))
```

In practice the initial ticker list is read from the `watchlist` database table.

### Dynamic Watchlist Integration

```python
# POST /api/watchlist
await market_source.add_ticker("PYPL")
# → appears in next SSE tick; no client reconnect needed

# DELETE /api/watchlist/PYPL
await market_source.remove_ticker("PYPL")
# → removed from active set and PriceCache
```

### Reading Prices in Route Handlers

```python
# Single ticker
update = price_cache.get("AAPL")
if update:
    current_price = update.price

# All tickers (portfolio valuation)
all_prices = price_cache.get_all()  # dict[str, PriceUpdate]
for ticker, update in all_prices.items():
    market_value = position.quantity * update.price
```

---

## Test Suite

**73 tests, all passing.** 6 test modules in `backend/tests/market/`.

| Module | Tests | Coverage |
|--------|-------|----------|
| `test_models.py` | 11 | `models.py`: 100% |
| `test_cache.py` | 13 | `cache.py`: 100% |
| `test_simulator.py` | 17 | `simulator.py`: 98% |
| `test_simulator_source.py` | 10 | integration tests |
| `test_factory.py` | 7 | `factory.py`: 100% |
| `test_massive.py` | 13 | `massive_client.py`: 94% |
| **Total** | **73** | **91%** |

Run tests:

```bash
cd backend
uv run --extra dev pytest -v              # all tests
uv run --extra dev pytest --cov=app       # with coverage report
uv run --extra dev ruff check app/ tests/ # lint (zero warnings)
```

---

## Open Issues from Code Review

Issues identified during the code review of 2026-04-08. All are currently unresolved and should be addressed as downstream development begins.

| # | Issue | Severity | File |
|---|-------|----------|------|
| 1 | SSE endpoint has zero functional test coverage | **High** | `stream.py` |
| 2 | `create_stream_router` mutates a module-level router (double-registration bug) | Medium | `stream.py:17` |
| 3 | `SimulatorDataSource.add_ticker` doesn't normalize ticker input (unlike `MassiveDataSource`) | Low | `simulator.py:242` |
| 4 | `test_exception_resilience` doesn't inject exceptions — uncovered path | Low | `test_simulator_source.py:96` |
| 5 | `_add_ticker_internal` duplicate guard is dead code | Very Low | `simulator.py:149` |
| 6 | `cache.version` reads without the lock (CPython-safe, but not portable) | Very Low | `cache.py:65` |
| 7 | Rounding assertion in test is weak (should use `round()` comparison) | Very Low | `test_simulator.py:124` |
| 8 | `event_loop_policy` fixture in `conftest.py` is unused | Very Low | `tests/conftest.py:6` |

**Priority:** Issue 1 (SSE test coverage) is the highest-priority item. Issue 2 should be fixed alongside it since tests would expose the multiple-registration bug. Issues 3–8 are minor.

**Issue 1 fix approach:** Add tests using FastAPI's `TestClient` with `httpx` in streaming mode, or mock the `Request` object to test `_generate_events` directly as an async generator.

**Issue 2 fix approach:** Make the router local to `create_stream_router`:

```python
def create_stream_router(price_cache: PriceCache) -> APIRouter:
    router = APIRouter(prefix="/api/stream", tags=["streaming"])
    @router.get("/prices")
    async def stream_prices(...): ...
    return router
```

---

## Demo

A Rich terminal dashboard is available at `backend/market_data_demo.py`:

```bash
cd backend
uv run market_data_demo.py
```

Displays for 90 seconds (or until Ctrl+C):
- Live price table with sparklines, session high/low, from-seed % change, tick count
- Market breadth bar (advancers vs decliners)
- Sector average change panel
- Realized volatility panel (annualized %, bar chart)
- Event log highlighting moves >1% (with ⚡ SHOCK marker for >3% moves)
- Session summary on exit

---

## Usage for Downstream Code

```python
from app.market import PriceCache, create_market_data_source

# Startup
cache = PriceCache()
source = create_market_data_source(cache)  # reads MASSIVE_API_KEY
await source.start(["AAPL", "GOOGL", "MSFT", ...])

# Read prices
update = cache.get("AAPL")          # PriceUpdate | None
price  = cache.get_price("AAPL")    # float | None
all_p  = cache.get_all()            # dict[str, PriceUpdate]

# Dynamic watchlist
await source.add_ticker("TSLA")
await source.remove_ticker("GOOGL")

# Shutdown
await source.stop()
```
