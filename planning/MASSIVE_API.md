# Massive API Reference

Massive (formerly Polygon.io) is the financial data provider used when `MASSIVE_API_KEY` is set. It exposes a REST API for US stocks. This document covers the two key patterns used in FinAlly: bulk snapshots for live polling and end-of-day data.

---

## Authentication & Base URL

All requests require an API key. The Python client handles this automatically:

```python
from massive import RESTClient

client = RESTClient(api_key="YOUR_MASSIVE_API_KEY")
```

The base URL is `https://api.massive.com` (accessed transparently through the SDK).

---

## Rate Limits

| Plan | Requests / minute | Recommended poll interval |
|------|-------------------|--------------------------|
| Free | 5 | 15 seconds |
| Starter | 100 | 2–5 seconds |
| Developer | Unlimited | 500 ms–2 seconds |
| Business | Unlimited + WebSocket | Real-time |

FinAlly defaults to a 15-second poll interval (free tier). Adjust via `MassiveDataSource(poll_interval=5.0)` for paid tiers.

---

## Endpoint 1 — Bulk Snapshots (primary usage)

Fetches the latest state for multiple tickers in a **single API call**. This is the workhorse endpoint for FinAlly's live price feed.

### Request

```
GET /v2/snapshot/locale/us/markets/stocks/tickers
```

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `tickers` | string | No | Comma-separated ticker list, e.g. `AAPL,MSFT,TSLA`. Omit for all tickers (expensive). |
| `include_otc` | boolean | No | Include OTC securities. Default: `false`. |

### Python SDK

```python
from massive.rest.models import SnapshotMarketType

snapshots = client.get_snapshot_all(
    market_type=SnapshotMarketType.STOCKS,
    tickers=["AAPL", "MSFT", "TSLA", "NVDA"],
)

for snap in snapshots:
    price = snap.last_trade.price          # float — last trade price
    ts_ms = snap.last_trade.timestamp      # int — Unix milliseconds
    ts_s  = snap.last_trade.timestamp / 1000.0  # convert to seconds
    change_pct = snap.todays_change_perc   # float — % change from prev close
    print(f"{snap.ticker}: ${price:.2f}  ({change_pct:+.2f}%)")
```

### Response structure (raw JSON)

```json
{
  "status": "OK",
  "count": 1,
  "tickers": [
    {
      "ticker": "AAPL",
      "todaysChange": 1.23,
      "todaysChangePerc": 0.65,
      "updated": 1605192894630916600,
      "day": {
        "o": 189.50,
        "h": 192.30,
        "l": 188.10,
        "c": 191.45,
        "v": 54312000,
        "vw": 190.87
      },
      "prevDay": {
        "o": 187.20,
        "h": 190.10,
        "l": 186.55,
        "c": 190.22,
        "v": 61200000,
        "vw": 188.94
      },
      "min": {
        "t": 1605192600000,
        "o": 191.10,
        "h": 191.80,
        "l": 190.95,
        "c": 191.45,
        "v": 120000,
        "vw": 191.32,
        "n": 84
      },
      "lastTrade": {
        "p": 191.45,
        "s": 200,
        "x": 4,
        "t": 1605192894630916600
      },
      "lastQuote": {
        "p": 191.40,
        "s": 100,
        "P": 191.50,
        "S": 200,
        "t": 1605192894000000000
      }
    }
  ]
}
```

### Key field reference

| Field path | Description |
|------------|-------------|
| `lastTrade.p` | Last trade price (float) |
| `lastTrade.s` | Last trade size (shares) |
| `lastTrade.t` | Last trade timestamp (Unix **milliseconds**) |
| `lastTrade.x` | Exchange ID |
| `lastQuote.p` | Bid price |
| `lastQuote.P` | Ask price |
| `day.c` | Current day close / latest price during session |
| `day.o` | Today's open |
| `day.h` / `day.l` | Today's high / low |
| `day.v` | Today's volume |
| `prevDay.c` | Previous day close |
| `todaysChange` | Dollar change vs. previous close |
| `todaysChangePerc` | Percentage change vs. previous close |
| `min.c` | Close of most recent minute bar |
| `min.t` | Minute bar timestamp (Unix milliseconds) |

> **Important:** `lastTrade.timestamp` is in **Unix milliseconds**. Divide by 1000 before storing in `PriceCache` (which uses Unix seconds).

---

## Endpoint 2 — Previous Day Close (end-of-day)

Fetches OHLCV data for the most recent complete trading day. Useful for seeding the cache before market open or for daily P&L baselines.

### Request

```
GET /v2/aggs/ticker/{stocksTicker}/prev
```

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `stocksTicker` | string | Yes | Ticker symbol, e.g. `AAPL` |
| `adjusted` | boolean | No | Adjust for splits/dividends. Default: `true` |

### Python SDK

```python
prev = client.get_previous_close(ticker="AAPL")

# prev is a list; take the first result
bar = prev[0] if prev else None
if bar:
    print(f"AAPL prev close: ${bar.close:.2f}")
    print(f"  Open: ${bar.open:.2f}, High: ${bar.high:.2f}, Low: ${bar.low:.2f}")
    print(f"  Volume: {bar.volume:,.0f}")
    print(f"  VWAP: ${bar.vwap:.2f}")
```

### Response structure (raw JSON)

```json
{
  "ticker": "AAPL",
  "queryCount": 1,
  "resultsCount": 1,
  "adjusted": true,
  "status": "OK",
  "results": [
    {
      "T": "AAPL",
      "o": 115.55,
      "h": 117.59,
      "l": 114.13,
      "c": 115.97,
      "v": 131704427,
      "vw": 116.3058,
      "t": 1605042000000,
      "n": 964462
    }
  ]
}
```

| Field | Description |
|-------|-------------|
| `c` / `bar.close` | Closing price |
| `o` / `bar.open` | Opening price |
| `h` / `bar.high` | Session high |
| `l` / `bar.low` | Session low |
| `v` / `bar.volume` | Share volume |
| `vw` / `bar.vwap` | Volume-weighted average price |
| `t` / `bar.timestamp` | Session start timestamp (Unix milliseconds) |
| `n` / `bar.transactions` | Number of transactions |

---

## Endpoint 3 — Last Trade (single ticker)

Returns the most recent trade for one ticker. Less efficient than the bulk snapshot for polling multiple tickers but useful for on-demand price lookups.

### Request

```
GET /v2/last/trade/{stocksTicker}
```

### Python SDK

```python
trade = client.get_last_trade(ticker="AAPL")
print(f"Last trade: ${trade.price:.2f} × {trade.size} shares at {trade.timestamp}")
```

---

## Error Handling

The SDK raises exceptions on HTTP errors. Common cases to handle:

| Status | Cause | Action |
|--------|-------|--------|
| 401 | Invalid API key | Log error, continue using cached prices |
| 403 | Plan does not include this endpoint | Fall back to simulator |
| 429 | Rate limit exceeded | Increase `poll_interval`, log warning |
| 503 | API unavailable | Log error, retry on next cycle |

```python
try:
    snapshots = client.get_snapshot_all(
        market_type=SnapshotMarketType.STOCKS,
        tickers=tickers,
    )
except Exception as e:
    logger.error("Massive poll failed: %s", e)
    # Don't re-raise — stale cache values remain available
```

---

## Thread Safety

The `RESTClient` is **synchronous**. In an async FastAPI app, run calls in a thread to avoid blocking the event loop:

```python
import asyncio

snapshots = await asyncio.to_thread(
    client.get_snapshot_all,
    market_type=SnapshotMarketType.STOCKS,
    tickers=tickers,
)
```

---

## Useful Imports

```python
from massive import RESTClient
from massive.rest.models import SnapshotMarketType

client = RESTClient(api_key=api_key)
```

The `massive` package is installed as a project dependency in `backend/pyproject.toml`.
