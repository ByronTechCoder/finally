# Market Data Backend — Code Review

**Date:** 2026-04-08  
**Reviewer:** Claude Sonnet 4.6  
**Scope:** `backend/app/market/` (8 modules, ~500 lines) + `backend/tests/market/` (6 test modules, 73 tests)

---

## Test Run Results

```
73 passed in 3.84s
```

All 73 tests pass. Ruff linter reports zero warnings (`All checks passed!`).

### Coverage by Module

| Module | Coverage | Uncovered Lines |
|---|---|---|
| `models.py` | 100% | — |
| `cache.py` | 100% | — |
| `interface.py` | 100% | — |
| `seed_prices.py` | 100% | — |
| `factory.py` | 100% | — |
| `simulator.py` | 98% | 149, 268–269 |
| `massive_client.py` | 94% | 85–87, 125 |
| `stream.py` | 33% | 26–48, 62–87 |
| **TOTAL** | **91%** | |

---

## Issues Found

### 1. SSE Endpoint Has Zero Functional Test Coverage — `stream.py`

**Severity: High**

`stream.py` reports 33% coverage, but the covered lines are only the module-level imports and the `router = APIRouter(...)` declaration. The actual endpoint handler (`stream_prices`, lines 26–48) and the async generator (`_generate_events`, lines 62–87) are completely untested.

This is the most consumer-facing component — it is what every browser client connects to — yet the following paths are never exercised:

- The `StreamingResponse` is constructed correctly
- The `retry: 1000\n\n` directive is sent on connection
- Version-based change detection (only push when `cache.version` changes)
- Client disconnect detection via `request.is_disconnected()`
- `asyncio.CancelledError` handling in the generator
- The SSE event format (`data: {...}\n\n`)

**Recommendation:** Add tests using FastAPI's `TestClient` with `httpx` in streaming mode, or mock the `Request` object to test `_generate_events` directly as an async generator.

---

### 2. `create_stream_router` Registers on a Module-Level Router — `stream.py:17,26`

**Severity: Medium**

```python
# Line 17 — module-level singleton
router = APIRouter(prefix="/api/stream", tags=["streaming"])

def create_stream_router(price_cache: PriceCache) -> APIRouter:
    @router.get("/prices")           # decorates the module-level router
    async def stream_prices(...):
        ...
    return router                    # returns the same module-level router
```

The function name implies it creates a new router each time, but it decorates and returns the same module-level `router` object. Calling `create_stream_router` a second time (e.g., in tests or during app reload) would register a second `/prices` route on the same router. FastAPI would use the first registered handler, silently ignoring the second closure — meaning the second call's `price_cache` would be wired to a dead route.

In production this is called once and works correctly. But the pattern is fragile and misleading.

**Recommendation:** Either make the router local to the function:

```python
def create_stream_router(price_cache: PriceCache) -> APIRouter:
    router = APIRouter(prefix="/api/stream", tags=["streaming"])
    @router.get("/prices")
    async def stream_prices(...): ...
    return router
```

Or add a guard to raise if called more than once.

---

### 3. `SimulatorDataSource` Does Not Normalize Ticker Input — `simulator.py:242`

**Severity: Low**

`MassiveDataSource.add_ticker` normalizes the input:

```python
async def add_ticker(self, ticker: str) -> None:
    ticker = ticker.upper().strip()   # normalized
    ...
```

`SimulatorDataSource.add_ticker` does not:

```python
async def add_ticker(self, ticker: str) -> None:
    if self._sim:
        self._sim.add_ticker(ticker)  # raw input passed through
```

Adding `"aapl"` via the simulator would track it as `"aapl"`, but the same call via Massive would track it as `"AAPL"`. This means the two implementations diverge in behavior when handling lowercase or padded ticker input. Any upstream code that normalizes before calling is fine, but the inconsistency violates the principle that both implementations are interchangeable.

**Recommendation:** Add the same `.upper().strip()` normalization to `SimulatorDataSource.add_ticker` (and `remove_ticker`).

---

### 4. `test_exception_resilience` Does Not Test Exception Resilience — `test_simulator_source.py:96`

**Severity: Low**

The test named `test_exception_resilience` simply starts the simulator, waits 150ms, and asserts the task is still running. It does not inject a failing `step()`. The exception-catching path in `_run_loop` (lines 268–269 in `simulator.py`) is never reached:

```python
except Exception:
    logger.exception("Simulator step failed")   # never exercised
```

This is the uncovered line at 268–269 in coverage.

**Recommendation:** Patch `GBMSimulator.step` to raise, confirm the loop continues, and the exception is logged:

```python
with patch.object(source._sim, "step", side_effect=ValueError("boom")):
    await asyncio.sleep(0.15)
assert not source._task.done()  # loop survived
```

---

### 5. `_add_ticker_internal` Guard Is Dead Code — `simulator.py:149`

**Severity: Low (cosmetic)**

`_add_ticker_internal` has a duplicate-entry guard:

```python
def _add_ticker_internal(self, ticker: str) -> None:
    if ticker in self._prices:   # line 149 — never reached
        return
```

But every caller of `_add_ticker_internal` already guards against duplicates before calling it: `add_ticker` checks `if ticker in self._prices` first, and `__init__` iterates a caller-supplied list where duplicates would be a caller error. This line cannot be reached through the public API.

**Recommendation:** Remove the guard from `_add_ticker_internal` (it's unnecessary), or keep it and add a test that calls `_add_ticker_internal` directly with a duplicate to cover the line explicitly. The former is cleaner.

---

### 6. `PriceCache.version` Reads Without the Lock — `cache.py:65`

**Severity: Very Low (CPython-safe)**

```python
@property
def version(self) -> int:
    return self._version   # no lock acquired
```

All write operations acquire `self._lock` before modifying `_version`, but the read does not. In CPython this is safe because integer attribute reads are atomic under the GIL. However, strictly speaking it is not thread-safe and would be unsafe on non-CPython runtimes (PyPy with JIT, Jython, etc.).

**Recommendation:** For correctness across runtimes, acquire the lock:

```python
@property
def version(self) -> int:
    with self._lock:
        return self._version
```

The performance cost is negligible; this property is read twice per SSE tick.

---

### 7. `test_prices_rounded_to_two_decimals` Is a Weak Assertion — `test_simulator.py:124`

**Severity: Very Low**

```python
if '.' in price_str:
    decimal_part = price_str.split('.')[1]
    assert len(decimal_part) <= 2
```

This test passes for prices like `190.1` (1 decimal place), `190.0`, or even `190` (no decimal). It does not verify that prices are rounded — it only verifies they don't have *more than* 2 decimal places. A price of `190` (no rounding applied at all) would pass.

**Recommendation:** Use the direct mathematical assertion:

```python
assert result["AAPL"] == round(result["AAPL"], 2)
```

---

### 8. `conftest.py` `event_loop_policy` Fixture Is Unused — `tests/conftest.py:6`

**Severity: Very Low (cosmetic)**

```python
@pytest.fixture
def event_loop_policy():
    return asyncio.DefaultEventLoopPolicy()
```

With `asyncio_mode = "auto"` set in `pyproject.toml`, `pytest-asyncio` manages the event loop automatically. This fixture is never requested by any test and has no effect. It appears to be a leftover from an earlier manual configuration approach.

**Recommendation:** Remove it.

---

## Strengths

The implementation is well-structured and production-quality in most respects:

- **Architecture**: Clean strategy pattern. Both data sources implement the same ABC; all downstream code is source-agnostic. The `PriceCache` as a single point of truth is the right call.
- **GBM math**: Correct implementation. The exponential form guarantees positive prices. Cholesky decomposition for correlated moves is the standard approach and correctly applied. The tiny `dt ≈ 8.5e-8` produces realistic sub-cent tick moves.
- **Async safety**: The synchronous Massive `RESTClient` is correctly wrapped in `asyncio.to_thread`, preventing event loop blocking.
- **Error resilience**: Both `_run_loop` (simulator) and `_poll_loop` (Massive) catch all exceptions and log them, ensuring a single bad tick or network hiccup never kills the background task.
- **Thread safety**: `PriceCache` uses a `threading.Lock` for all writes and reads, and `get_all()` returns a copy — safe to iterate while the background task continues writing.
- **Lint**: Zero ruff warnings.
- **Test quality**: 73 tests, clean unit/integration separation, meaningful test names, proper async test handling. Factory, cache, models, and simulator unit tests are thorough.

---

## Summary

| # | Issue | Severity | File |
|---|---|---|---|
| 1 | SSE endpoint has zero test coverage | High | `stream.py` |
| 2 | `create_stream_router` mutates a module-level router | Medium | `stream.py:17` |
| 3 | `SimulatorDataSource` doesn't normalize ticker input | Low | `simulator.py:242` |
| 4 | Exception resilience test doesn't inject exceptions | Low | `test_simulator_source.py:96` |
| 5 | `_add_ticker_internal` duplicate guard is dead code | Very Low | `simulator.py:149` |
| 6 | `cache.version` reads without lock | Very Low | `cache.py:65` |
| 7 | Rounding assertion is weak | Very Low | `test_simulator.py:124` |
| 8 | Unused fixture in `conftest.py` | Very Low | `tests/conftest.py:6` |

The highest-priority item before moving to the next phase is **Issue 1** (SSE test coverage) — the streaming endpoint is the real-time backbone of the whole application and currently has no tests. Issue 2 should be fixed alongside it since adding tests would expose the multiple-registration bug. Issues 3–8 are minor and can be addressed opportunistically.
