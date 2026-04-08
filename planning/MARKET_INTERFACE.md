# Market Data Interface

This document defines the unified Python interface for market data in FinAlly: the abstract contract all data sources implement, the shared cache, the data model, and how they compose together. The code lives in `backend/app/market/`.

---

## Design Goals

- **Source-agnostic consumers** — SSE streaming, portfolio valuation, and trade execution never know whether prices come from the simulator or Massive.
- **Single point of truth** — the `PriceCache` is the only place prices are read from. Data sources write to it; everything else reads from it.
- **Dynamic watchlist** — tickers can be added/removed at runtime without restarting the background task.
- **Async-safe** — the FastAPI event loop is never blocked by synchronous I/O.

---

## Module Map

```
backend/app/market/
├── __init__.py          # Public re-exports
├── models.py            # PriceUpdate dataclass
├── cache.py             # PriceCache — thread-safe price store
├── interface.py         # MarketDataSource — abstract base class
├── simulator.py         # SimulatorDataSource (GBM-based, default)
├── massive_client.py    # MassiveDataSource (Polygon.io REST poller)
├── factory.py           # create_market_data_source() — env-driven factory
├── seed_prices.py       # Seed prices and per-ticker GBM parameters
└── stream.py            # create_stream_router() — FastAPI SSE endpoint
```

---

## Data Model — `PriceUpdate`

`PriceUpdate` is an **immutable frozen dataclass** representing a single price observation.

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
    def change_percent(self) -> float: ...  # % change
    @property
    def direction(self) -> str: ...         # "up" | "down" | "flat"

    def to_dict(self) -> dict: ...          # JSON-serializable dict for SSE/API
```

`to_dict()` output:

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

---

## Price Cache — `PriceCache`

Thread-safe in-memory store. One instance per application, shared between the data source (writer) and all consumers (readers).

```python
cache = PriceCache()

# Write (called by data source background task)
update: PriceUpdate = cache.update("AAPL", price=191.45)
cache.update("MSFT", price=420.10, timestamp=1705692894.63)

# Read (called by SSE endpoint, portfolio routes)
update: PriceUpdate | None = cache.get("AAPL")
price: float | None        = cache.get_price("AAPL")
all_prices: dict[str, PriceUpdate] = cache.get_all()

# Remove (called when ticker removed from watchlist)
cache.remove("AAPL")

# SSE change detection
version: int = cache.version   # increments on every update
```

The `version` counter enables efficient SSE polling: the stream endpoint records the last version it sent and only pushes when `cache.version` changes.

Key behaviors:
- First update for a ticker: `previous_price == price` (direction = "flat")
- Subsequent updates: `previous_price` = the previous `price`
- `get_all()` returns a shallow copy — safe to iterate while the background task continues writing

---

## Abstract Interface — `MarketDataSource`

All data sources implement this ABC. Downstream code depends only on this interface, never on `SimulatorDataSource` or `MassiveDataSource` directly.

```python
class MarketDataSource(ABC):

    @abstractmethod
    async def start(self, tickers: list[str]) -> None:
        """Start background task, seed cache with initial prices.
        Call exactly once at app startup."""

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

## Factory — `create_market_data_source()`

Selects the implementation based on environment variables. All app startup code calls this — never instantiate `SimulatorDataSource` or `MassiveDataSource` directly.

```python
from app.market import PriceCache, create_market_data_source

cache = PriceCache()
source = create_market_data_source(cache)
# Returns MassiveDataSource if MASSIVE_API_KEY is set and non-empty,
# otherwise SimulatorDataSource.
```

Logic in `factory.py`:

```python
def create_market_data_source(price_cache: PriceCache) -> MarketDataSource:
    api_key = os.environ.get("MASSIVE_API_KEY", "").strip()
    if api_key:
        return MassiveDataSource(api_key=api_key, price_cache=price_cache)
    else:
        return SimulatorDataSource(price_cache=price_cache)
```

---

## Lifecycle — App Startup / Shutdown

Wire up in FastAPI lifespan:

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI
from app.market import PriceCache, create_market_data_source

price_cache = PriceCache()
market_source: MarketDataSource | None = None

INITIAL_TICKERS = ["AAPL", "GOOGL", "MSFT", "AMZN", "TSLA",
                   "NVDA", "META", "JPM", "V", "NFLX"]

@asynccontextmanager
async def lifespan(app: FastAPI):
    global market_source
    market_source = create_market_data_source(price_cache)
    await market_source.start(INITIAL_TICKERS)  # seeds cache, starts background task
    yield
    await market_source.stop()

app = FastAPI(lifespan=lifespan)
```

In practice, the initial ticker list is read from the database watchlist table.

---

## Dynamic Watchlist Integration

When the user adds or removes a ticker via the watchlist API:

```python
# POST /api/watchlist  — add a ticker
await market_source.add_ticker("PYPL")
# The ticker appears in the next SSE tick automatically.
# SimulatorDataSource seeds the cache immediately with an initial price.
# MassiveDataSource includes it in the next poll cycle.

# DELETE /api/watchlist/PYPL  — remove a ticker
await market_source.remove_ticker("PYPL")
# Removed from both the active set and the PriceCache.
# SSE clients stop receiving updates for this ticker on the next tick.
```

---

## SSE Streaming

`create_stream_router()` returns a FastAPI `APIRouter` that mounts a single SSE endpoint:

```
GET /api/stream/prices
```

```python
from app.market import create_stream_router

router = create_stream_router(price_cache)
app.include_router(router, prefix="/api")
```

The stream endpoint uses version-based change detection: it records `last_version = cache.version`, sleeps briefly, and only sends a new SSE event when the version has advanced. Each event is a JSON object produced by `PriceUpdate.to_dict()`.

---

## Reading Prices in Route Handlers

```python
# Single ticker
update = price_cache.get("AAPL")
if update:
    current_price = update.price

# All tickers (e.g., portfolio valuation)
all_prices = price_cache.get_all()  # dict[str, PriceUpdate]
for ticker, update in all_prices.items():
    market_value = position.quantity * update.price
```

---

## Public Imports

All public symbols are re-exported from `app/market/__init__.py`:

```python
from app.market import (
    PriceUpdate,
    PriceCache,
    MarketDataSource,
    create_market_data_source,
    create_stream_router,
)
```
