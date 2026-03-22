from contextlib import asynccontextmanager
from dataclasses import dataclass
from collections import OrderedDict
from time import monotonic
from typing import AsyncIterator

import httpx


@dataclass
class UpstreamStream:
    status_code: int
    body_stream: AsyncIterator[bytes]
    headers: dict[str, str]


@dataclass(frozen=True)
class CachedUpstreamResponse:
    status_code: int
    body: bytes
    headers: dict[str, str]


class UpstreamResponseCache:
    def __init__(self, max_entries: int = 128, ttl_seconds: int = 300, max_body_bytes: int = 5_000_000):
        self.max_entries = max_entries
        self.ttl_seconds = ttl_seconds
        self.max_body_bytes = max_body_bytes
        self._entries: OrderedDict[tuple[str, str], tuple[float, CachedUpstreamResponse]] = OrderedDict()

    def get(self, endpoint: str, authorization: str) -> CachedUpstreamResponse | None:
        key = (normalize_endpoint(endpoint), authorization)
        entry = self._entries.get(key)
        if entry is None:
            return None

        expires_at, cached = entry
        if expires_at <= monotonic():
            self._entries.pop(key, None)
            return None

        self._entries.move_to_end(key)
        return cached

    def set(self, endpoint: str, authorization: str, response: CachedUpstreamResponse) -> None:
        if len(response.body) > self.max_body_bytes:
            return

        key = (normalize_endpoint(endpoint), authorization)
        self._entries[key] = (monotonic() + self.ttl_seconds, response)
        self._entries.move_to_end(key)

        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)


response_cache = UpstreamResponseCache()


def normalize_endpoint(endpoint: str) -> str:
    return endpoint if endpoint.endswith("/") else f"{endpoint}/"


def build_http_client(transport: httpx.AsyncBaseTransport | None = None) -> httpx.AsyncClient:
    return httpx.AsyncClient(follow_redirects=True, transport=transport)


def _filtered_headers(response: httpx.Response) -> dict[str, str]:
    content_type = response.headers.get("content-type")
    return {"content-type": content_type} if content_type else {}


def _cached_stream(body: bytes) -> AsyncIterator[bytes]:
    async def iterator():
        yield body

    return iterator()


@asynccontextmanager
async def open_upstream_stream(
    endpoint: str,
    authorization: str,
    client: httpx.AsyncClient | None = None,
):
    cached = response_cache.get(endpoint, authorization)
    if cached is not None:
        yield UpstreamStream(
            status_code=cached.status_code,
            body_stream=_cached_stream(cached.body),
            headers=cached.headers,
        )
        return

    owns_client = client is None
    upstream_client = client or build_http_client()
    response_context = upstream_client.stream(
        "GET",
        normalize_endpoint(endpoint),
        headers={"Authorization": authorization},
    )

    try:
        response = await response_context.__aenter__()

        async def caching_body_stream():
            chunks: list[bytes] = []
            complete = False
            try:
                async for chunk in response.aiter_bytes():
                    chunks.append(chunk)
                    yield chunk
                complete = True
            finally:
                if complete and response.status_code < 400:
                    response_cache.set(
                        endpoint,
                        authorization,
                        CachedUpstreamResponse(
                            status_code=response.status_code,
                            body=b"".join(chunks),
                            headers=_filtered_headers(response),
                        ),
                    )

        yield UpstreamStream(
            status_code=response.status_code,
            body_stream=caching_body_stream(),
            headers=_filtered_headers(response),
        )
    finally:
        await response_context.__aexit__(None, None, None)
        if owns_client:
            await upstream_client.aclose()
