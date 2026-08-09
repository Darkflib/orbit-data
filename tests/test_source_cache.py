"""Conditional source-cache tests."""

# pylint: disable=missing-function-docstring

from pathlib import Path

import httpx
import pytest

from orbit_data.source_cache import ConditionalSourceCache, SourceFetchError


def test_conditional_get_reuses_cached_body(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(
                200,
                content=b"catalog",
                headers={"ETag": '"one"', "Last-Modified": "Sun, 09 Aug 2026 12:00:00 GMT"},
            )
        return httpx.Response(304)

    cache = ConditionalSourceCache(tmp_path, maximum_response_bytes=1024)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        first = cache.get(client, name="satcat", url="https://example.test/data")
        second = cache.get(client, name="satcat", url="https://example.test/data")

    assert not first.from_cache
    assert second.from_cache
    assert not second.stale
    assert second.body == b"catalog"
    assert requests[1].headers["if-none-match"] == '"one"'


def test_network_failure_uses_stale_last_good_body(tmp_path: Path) -> None:
    responses = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal responses
        responses += 1
        if responses == 1:
            return httpx.Response(200, content=b"good")
        raise httpx.ConnectError("offline", request=request)

    cache = ConditionalSourceCache(tmp_path, maximum_response_bytes=1024)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        cache.get(client, name="gcat", url="https://example.test/data")
        fallback = cache.get(client, name="gcat", url="https://example.test/data")

    assert fallback.body == b"good"
    assert fallback.from_cache
    assert fallback.stale
    assert "offline" in str(fallback.error)


def test_failure_without_cache_is_fatal(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    cache = ConditionalSourceCache(tmp_path, maximum_response_bytes=1024)
    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(SourceFetchError, match="no cached body"),
    ):
        cache.get(client, name="satcat", url="https://example.test/data")


def test_oversized_source_without_cache_is_fatal(tmp_path: Path) -> None:
    cache = ConditionalSourceCache(tmp_path, maximum_response_bytes=1024)
    with (
        httpx.Client(
            transport=httpx.MockTransport(lambda _request: httpx.Response(200, content=b"x" * 2048))
        ) as client,
        pytest.raises(SourceFetchError, match="no cached body"),
    ):
        cache.get(client, name="satcat", url="https://example.test/data")
