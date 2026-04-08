# Market Simulator

The simulator generates realistic-looking stock price movement without any external API calls. It is the default data source when `MASSIVE_API_KEY` is not set. The implementation lives in `backend/app/market/simulator.py` and `backend/app/market/seed_prices.py`.

---

## Overview

The simulator uses **Geometric Brownian Motion (GBM)** with **Cholesky-decomposed correlated noise** to produce price paths that:

- Follow the standard financial model for equity prices
- Exhibit sector-based correlation (tech stocks move together, finance stocks move together)
- Include occasional random "shock" events for visual drama
- Tick every 500 ms, producing smooth real-time updates

---

## Class Structure

```
GBMSimulator               — pure math, no I/O
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

`GBMSimulator` is deliberately kept pure: no asyncio, no I/O, easily unit-tested. `SimulatorDataSource` is the thin async shell that owns the background task and talks to `PriceCache`.

---

## The GBM Formula

Each tick advances every ticker by one step using the standard GBM discretization:

```
S(t + dt) = S(t) × exp( (μ - σ²/2) × dt  +  σ × √dt × Z )
```

Where:
- `S(t)` = current price
- `μ` (mu) = annualized drift (expected return, e.g. 0.05 = 5%/year)
- `σ` (sigma) = annualized volatility (e.g. 0.25 = 25%/year)
- `dt` = time step as a fraction of a trading year
- `Z` = correlated standard normal random variable

### Time step

```python
# 500ms expressed as a fraction of a trading year
TRADING_SECONDS_PER_YEAR = 252 * 6.5 * 3600  # 5,896,800
DEFAULT_DT = 0.5 / TRADING_SECONDS_PER_YEAR   # ≈ 8.48e-8
```

At this `dt`, a single tick produces sub-cent moves that accumulate naturally over hours and days. The exponential form guarantees prices stay positive (no negative prices).

---

## Correlated Moves

Without correlation, every ticker's price would be independent. Real markets show sector correlation — when AAPL drops, GOOGL and MSFT tend to drop too.

### Approach

1. Build an `n × n` correlation matrix `Σ` based on sector membership
2. Compute its Cholesky decomposition `L` so that `L @ Lᵀ = Σ`
3. At each tick, generate `n` independent standard normal draws `z`
4. Apply `L @ z` to get correlated draws with the right covariance structure

```python
z_independent = np.random.standard_normal(n)  # shape: (n,)
z_correlated  = self._cholesky @ z_independent # shape: (n,)
```

Each `z_correlated[i]` is then used in the GBM formula for ticker `i`.

### Correlation values (from `seed_prices.py`)

```python
INTRA_TECH_CORR    = 0.6   # AAPL, GOOGL, MSFT, AMZN, META, NVDA, NFLX
INTRA_FINANCE_CORR = 0.5   # JPM, V
TSLA_CORR          = 0.3   # TSLA is in tech sector but moves independently
CROSS_GROUP_CORR   = 0.3   # Between sectors, or unknown tickers
```

The correlation matrix is rebuilt (O(n²)) whenever tickers are added or removed. This is negligible at n < 50.

---

## Seed Prices and Per-Ticker Parameters

`seed_prices.py` holds the starting prices and GBM parameters for the default watchlist:

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

Tickers not in `SEED_PRICES` start at a random price between $50–$300 and use `DEFAULT_PARAMS`.

---

## Random Shock Events

At each tick, every ticker has a small independent probability of a sudden large move:

```python
EVENT_PROBABILITY = 0.001  # 0.1% per tick per ticker

if random.random() < self._event_prob:
    shock_magnitude = random.uniform(0.02, 0.05)  # 2–5%
    shock_sign = random.choice([-1, 1])
    self._prices[ticker] *= 1 + shock_magnitude * shock_sign
```

With 10 tickers ticking twice per second, the expected interval between events is:

```
1 / (10 tickers × 2 ticks/s × 0.001) = 50 seconds
```

This creates visible drama without overwhelming the chart with noise.

---

## The Step Function (hot path)

Called every 500 ms. Must be fast.

```python
def step(self) -> dict[str, float]:
    n = len(self._tickers)
    z_independent = np.random.standard_normal(n)
    z_correlated  = self._cholesky @ z_independent if self._cholesky else z_independent

    result = {}
    for i, ticker in enumerate(self._tickers):
        mu, sigma = self._params[ticker]["mu"], self._params[ticker]["sigma"]
        drift     = (mu - 0.5 * sigma**2) * self._dt
        diffusion = sigma * math.sqrt(self._dt) * z_correlated[i]
        self._prices[ticker] *= math.exp(drift + diffusion)

        # Optional shock event
        if random.random() < self._event_prob:
            mag  = random.uniform(0.02, 0.05)
            sign = random.choice([-1, 1])
            self._prices[ticker] *= 1 + mag * sign

        result[ticker] = round(self._prices[ticker], 2)

    return result
```

---

## Background Task Loop

`SimulatorDataSource._run_loop()` runs as an `asyncio.Task`:

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

Exceptions are caught and logged so a single bad tick never kills the loop.

---

## Adding Tickers Dynamically

When a new ticker is added at runtime (e.g., user adds "PYPL" via chat):

1. `SimulatorDataSource.add_ticker("PYPL")` calls `GBMSimulator.add_ticker("PYPL")`
2. `GBMSimulator` looks up seed price (or picks random $50–$300) and default params
3. Cholesky matrix is rebuilt to include the new ticker
4. The cache is immediately seeded with the initial price (so the ticker isn't "unknown" until the next tick)

The reverse happens on `remove_ticker()`: ticker is removed from the simulation state, Cholesky is rebuilt, and the price is removed from the cache.

---

## Test Coverage

Tests in `backend/tests/market/test_simulator.py` and `test_simulator_source.py` cover:

| Scenario | Test |
|----------|------|
| GBM price always positive | `test_prices_always_positive` |
| Correct direction of drift | `test_high_drift_tends_up` |
| Correlated moves | `test_correlation_positive` |
| Add / remove ticker rebuilds Cholesky | `test_add_ticker`, `test_remove_ticker` |
| Unknown ticker uses defaults | `test_unknown_ticker_defaults` |
| SimulatorDataSource lifecycle | `test_start_stop`, `test_add_remove` |
| Cache seeded on start | `test_cache_seeded_on_start` |
| Cache seeded immediately on add_ticker | `test_add_ticker_seeds_cache` |
