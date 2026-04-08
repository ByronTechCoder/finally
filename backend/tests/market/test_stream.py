"""Tests for the SSE streaming endpoint."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from app.market.cache import PriceCache
from app.market.stream import _generate_events, create_stream_router


def _make_request(*, disconnected_after: int = 999) -> MagicMock:
    """Return a mock Request that reports disconnected after N is_disconnected() calls."""
    call_count = {"n": 0}

    async def is_disconnected() -> bool:
        call_count["n"] += 1
        return call_count["n"] > disconnected_after

    request = MagicMock()
    request.client = MagicMock()
    request.client.host = "127.0.0.1"
    request.is_disconnected = is_disconnected
    return request


class TestCreateStreamRouter:
    """Tests for the create_stream_router factory."""

    def test_returns_api_router(self):
        """create_stream_router returns a FastAPI APIRouter."""
        from fastapi import APIRouter

        cache = PriceCache()
        router = create_stream_router(cache)
        assert isinstance(router, APIRouter)

    def test_router_has_prices_route(self):
        """The router includes a /prices GET route (full path includes prefix)."""
        cache = PriceCache()
        router = create_stream_router(cache)
        paths = [route.path for route in router.routes]
        assert "/api/stream/prices" in paths

    def test_multiple_calls_return_independent_routers(self):
        """Each call to create_stream_router returns a fresh router."""
        cache = PriceCache()
        router1 = create_stream_router(cache)
        router2 = create_stream_router(cache)
        assert router1 is not router2

    def test_multiple_calls_do_not_accumulate_routes(self):
        """Calling create_stream_router twice does not double-register routes."""
        cache = PriceCache()
        router1 = create_stream_router(cache)
        router2 = create_stream_router(cache)
        assert len(router1.routes) == 1
        assert len(router2.routes) == 1


@pytest.mark.asyncio
class TestGenerateEvents:
    """Tests for the _generate_events async generator."""

    async def test_sends_retry_directive_first(self):
        """First yielded item is the SSE retry directive."""
        cache = PriceCache()
        request = _make_request(disconnected_after=1)

        events = []
        async for event in _generate_events(cache, request, interval=0.01):
            events.append(event)

        assert events[0] == "retry: 1000\n\n"

    async def test_sends_data_when_cache_has_prices(self):
        """Yields a data event when the cache contains prices."""
        cache = PriceCache()
        cache.update("AAPL", 190.50)

        request = _make_request(disconnected_after=2)

        events = []
        async for event in _generate_events(cache, request, interval=0.01):
            events.append(event)

        data_events = [e for e in events if e.startswith("data:")]
        assert len(data_events) >= 1

        # Validate JSON payload
        payload = json.loads(data_events[0].removeprefix("data: ").strip())
        assert "AAPL" in payload
        assert payload["AAPL"]["price"] == 190.50
        assert payload["AAPL"]["ticker"] == "AAPL"

    async def test_data_event_format(self):
        """SSE data events are correctly formatted."""
        cache = PriceCache()
        cache.update("MSFT", 420.00)

        request = _make_request(disconnected_after=2)

        events = []
        async for event in _generate_events(cache, request, interval=0.01):
            events.append(event)

        data_events = [e for e in events if e.startswith("data:")]
        assert len(data_events) >= 1
        # SSE format: "data: {...}\n\n"
        assert data_events[0].endswith("\n\n")

    async def test_stops_on_client_disconnect(self):
        """Generator stops when request.is_disconnected() returns True."""
        cache = PriceCache()
        cache.update("AAPL", 190.00)

        request = _make_request(disconnected_after=1)

        events = []
        async for event in _generate_events(cache, request, interval=0.01):
            events.append(event)

        # Generator should have stopped — not running forever
        assert len(events) >= 1  # At least the retry directive was sent

    async def test_no_data_sent_when_cache_empty(self):
        """No data event is sent if the cache is empty."""
        cache = PriceCache()
        request = _make_request(disconnected_after=2)

        events = []
        async for event in _generate_events(cache, request, interval=0.01):
            events.append(event)

        data_events = [e for e in events if e.startswith("data:")]
        assert data_events == []

    async def test_version_based_deduplication(self):
        """Data is only sent when the cache version changes."""
        cache = PriceCache()
        cache.update("AAPL", 190.00)

        # Disconnect after several polls but don't update the cache again
        request = _make_request(disconnected_after=5)

        events = []
        async for event in _generate_events(cache, request, interval=0.01):
            events.append(event)

        data_events = [e for e in events if e.startswith("data:")]
        # Even with 5 polls, only 1 data event should be sent (version didn't change)
        assert len(data_events) == 1

    async def test_multiple_tickers_in_payload(self):
        """All tickers appear in a single SSE payload."""
        cache = PriceCache()
        cache.update("AAPL", 190.00)
        cache.update("GOOGL", 175.00)
        cache.update("MSFT", 420.00)

        request = _make_request(disconnected_after=2)

        events = []
        async for event in _generate_events(cache, request, interval=0.01):
            events.append(event)

        data_events = [e for e in events if e.startswith("data:")]
        assert len(data_events) >= 1

        payload = json.loads(data_events[0].removeprefix("data: ").strip())
        assert set(payload.keys()) == {"AAPL", "GOOGL", "MSFT"}

    async def test_cancelled_error_handled_gracefully(self):
        """CancelledError stops the generator without propagating."""
        cache = PriceCache()
        cache.update("AAPL", 190.00)

        # Request that never disconnects — we'll cancel the task instead
        request = _make_request(disconnected_after=9999)

        async def collect_with_cancel():
            events = []
            gen = _generate_events(cache, request, interval=0.05)
            async for event in gen:
                events.append(event)
                if len(events) >= 2:
                    await gen.aclose()
                    break
            return events

        events = await collect_with_cancel()
        assert len(events) >= 1

    async def test_payload_contains_required_fields(self):
        """Each ticker entry in the SSE payload has all required fields."""
        cache = PriceCache()
        cache.update("AAPL", 190.00)

        request = _make_request(disconnected_after=2)

        events = []
        async for event in _generate_events(cache, request, interval=0.01):
            events.append(event)

        data_events = [e for e in events if e.startswith("data:")]
        payload = json.loads(data_events[0].removeprefix("data: ").strip())
        entry = payload["AAPL"]

        assert "ticker" in entry
        assert "price" in entry
        assert "previous_price" in entry
        assert "timestamp" in entry
        assert "change" in entry
        assert "change_percent" in entry
        assert "direction" in entry
