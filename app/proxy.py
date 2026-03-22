from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import dataclass
from hashlib import sha256
import os
from pathlib import Path
from tempfile import mkdtemp
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


@dataclass
class CacheEntry:
    status_code: int
    headers: dict[str, str]
    expires_at: float
    size_bytes: int
    tier: str
    body: bytes | None = None
    file_path: Path | None = None


@dataclass(frozen=True)
class CacheStats:
    entries: int
    memory_entries: int
    disk_entries: int
    memory_bytes: int
    disk_bytes: int
    hits_memory: int
    hits_disk: int
    misses: int
    expired: int
    evictions_memory: int
    evictions_disk: int


@dataclass(frozen=True)
class CacheConfiguration:
    enabled: bool
    cache_dir: str
    max_entries: int
    ttl_seconds: int
    max_memory_bytes: int
    max_disk_bytes: int
    backend: str


def normalize_endpoint(endpoint: str) -> str:
    return endpoint if endpoint.endswith("/") else f"{endpoint}/"


def cache_key(endpoint: str, authorization: str) -> str:
    material = f"{normalize_endpoint(endpoint)}\n{authorization}".encode("utf-8")
    return sha256(material).hexdigest()


def build_http_client(transport: httpx.AsyncBaseTransport | None = None) -> httpx.AsyncClient:
    return httpx.AsyncClient(follow_redirects=True, transport=transport)


def _filtered_headers(response: httpx.Response) -> dict[str, str]:
    content_type = response.headers.get("content-type")
    return {"content-type": content_type} if content_type else {}


def _cached_stream(body: bytes) -> AsyncIterator[bytes]:
    async def iterator():
        yield body

    return iterator()


def _disk_stream(path: Path, chunk_size: int = 64 * 1024) -> AsyncIterator[bytes]:
    async def iterator():
        with path.open("rb") as file:
            while True:
                chunk = file.read(chunk_size)
                if not chunk:
                    break
                yield chunk

    return iterator()


class UpstreamResponseCache:
    def __init__(
        self,
        max_entries: int = 128,
        ttl_seconds: int = 300,
        max_memory_bytes: int = 1_000_000,
        max_disk_bytes: int = 100_000_000,
        cache_dir: str | None = None,
    ):
        self.max_entries = max_entries
        self.ttl_seconds = ttl_seconds
        self.max_memory_bytes = max_memory_bytes
        self.max_disk_bytes = max_disk_bytes
        self.cache_dir = Path(cache_dir or mkdtemp(prefix="edc-proxy-cache-"))
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._entries: OrderedDict[str, CacheEntry] = OrderedDict()
        self._memory_bytes = 0
        self._disk_bytes = 0
        self._hits_memory = 0
        self._hits_disk = 0
        self._misses = 0
        self._expired = 0
        self._evictions_memory = 0
        self._evictions_disk = 0

    def configuration(self) -> CacheConfiguration:
        return CacheConfiguration(
            enabled=True,
            cache_dir=str(self.cache_dir),
            max_entries=self.max_entries,
            ttl_seconds=self.ttl_seconds,
            max_memory_bytes=self.max_memory_bytes,
            max_disk_bytes=self.max_disk_bytes,
            backend="enabled",
        )

    def get(self, endpoint: str, authorization: str) -> CacheEntry | None:
        key = cache_key(endpoint, authorization)
        entry = self._entries.get(key)
        if entry is None:
            self._misses += 1
            return None

        if entry.expires_at <= monotonic():
            self._remove_entry(key)
            self._expired += 1
            self._misses += 1
            return None

        self._entries.move_to_end(key)
        if entry.tier == "memory":
            self._hits_memory += 1
        else:
            self._hits_disk += 1
        return entry

    def set(self, endpoint: str, authorization: str, response: CachedUpstreamResponse) -> None:
        if response.status_code >= 400:
            return

        key = cache_key(endpoint, authorization)
        expires_at = monotonic() + self.ttl_seconds
        size_bytes = len(response.body)

        if size_bytes <= self.max_memory_bytes:
            self._store_memory_entry(key, response, expires_at, size_bytes)
            return

        if self._disk_bytes + size_bytes > self.max_disk_bytes:
            return

        file_path = self.cache_dir / key
        file_path.write_bytes(response.body)
        self._disk_bytes += size_bytes
        self._entries[key] = CacheEntry(
            status_code=response.status_code,
            headers=response.headers,
            expires_at=expires_at,
            size_bytes=size_bytes,
            tier="disk",
            file_path=file_path,
        )
        self._entries.move_to_end(key)
        self._enforce_limits()

    def _store_memory_entry(
        self,
        key: str,
        response: CachedUpstreamResponse,
        expires_at: float,
        size_bytes: int,
    ) -> None:
        previous = self._entries.get(key)
        if previous is not None:
            self._remove_entry(key)

        self._entries[key] = CacheEntry(
            status_code=response.status_code,
            headers=response.headers,
            expires_at=expires_at,
            size_bytes=size_bytes,
            tier="memory",
            body=response.body,
        )
        self._entries.move_to_end(key)
        self._memory_bytes += size_bytes
        self._enforce_limits()

    def _remove_entry(self, key: str) -> None:
        entry = self._entries.pop(key, None)
        if entry is None:
            return

        if entry.tier == "memory":
            self._memory_bytes -= entry.size_bytes
        else:
            self._disk_bytes -= entry.size_bytes
            if entry.file_path is not None and entry.file_path.exists():
                entry.file_path.unlink()

    def _enforce_limits(self) -> None:
        while len(self._entries) > self.max_entries or self._memory_bytes > self.max_memory_bytes:
            key, entry = self._entries.popitem(last=False)
            if entry.tier == "memory":
                self._memory_bytes -= entry.size_bytes
                self._evictions_memory += 1
            else:
                self._disk_bytes -= entry.size_bytes
                self._evictions_disk += 1
                if entry.file_path is not None and entry.file_path.exists():
                    entry.file_path.unlink()

    def stats(self) -> CacheStats:
        memory_entries = sum(1 for entry in self._entries.values() if entry.tier == "memory")
        disk_entries = len(self._entries) - memory_entries
        return CacheStats(
            entries=len(self._entries),
            memory_entries=memory_entries,
            disk_entries=disk_entries,
            memory_bytes=self._memory_bytes,
            disk_bytes=self._disk_bytes,
            hits_memory=self._hits_memory,
            hits_disk=self._hits_disk,
            misses=self._misses,
            expired=self._expired,
            evictions_memory=self._evictions_memory,
            evictions_disk=self._evictions_disk,
        )


class DisabledUpstreamResponseCache:
    def __init__(self, cache_dir: str | None = None, max_entries: int = 128, ttl_seconds: int = 300, max_memory_bytes: int = 1_000_000, max_disk_bytes: int = 100_000_000):
        self.cache_dir = str(Path(cache_dir or mkdtemp(prefix="edc-proxy-cache-")))
        self.max_entries = max_entries
        self.ttl_seconds = ttl_seconds
        self.max_memory_bytes = max_memory_bytes
        self.max_disk_bytes = max_disk_bytes

    def get(self, endpoint: str, authorization: str) -> CacheEntry | None:
        return None

    def set(self, endpoint: str, authorization: str, response: CachedUpstreamResponse) -> None:
        return None

    def stats(self) -> CacheStats:
        return CacheStats(
            entries=0,
            memory_entries=0,
            disk_entries=0,
            memory_bytes=0,
            disk_bytes=0,
            hits_memory=0,
            hits_disk=0,
            misses=0,
            expired=0,
            evictions_memory=0,
            evictions_disk=0,
        )

    def configuration(self) -> CacheConfiguration:
        return CacheConfiguration(
            enabled=False,
            cache_dir=self.cache_dir,
            max_entries=self.max_entries,
            ttl_seconds=self.ttl_seconds,
            max_memory_bytes=self.max_memory_bytes,
            max_disk_bytes=self.max_disk_bytes,
            backend="disabled",
        )


def build_response_cache_from_env() -> UpstreamResponseCache | DisabledUpstreamResponseCache:
    enabled = os.getenv("PROXY_CACHE_ENABLED", "true").strip().lower()
    cache_dir = os.getenv("PROXY_CACHE_DIR")
    if enabled in {"0", "false", "no", "off"}:
        return DisabledUpstreamResponseCache(cache_dir=cache_dir)
    return UpstreamResponseCache(cache_dir=cache_dir)


response_cache = build_response_cache_from_env()


def _cache_body_as_stream(response: httpx.Response, endpoint: str, authorization: str):
    async def iterator():
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

    return iterator()


@asynccontextmanager
async def open_upstream_stream(
    endpoint: str,
    authorization: str,
    client: httpx.AsyncClient | None = None,
):
    entry = response_cache.get(endpoint, authorization)
    if entry is not None:
        body_stream = _cached_stream(entry.body) if entry.tier == "memory" else _disk_stream(entry.file_path)
        yield UpstreamStream(status_code=entry.status_code, body_stream=body_stream, headers=entry.headers)
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
        yield UpstreamStream(
            status_code=response.status_code,
            body_stream=_cache_body_as_stream(response, endpoint, authorization),
            headers=_filtered_headers(response),
        )
    finally:
        await response_context.__aexit__(None, None, None)
        if owns_client:
            await upstream_client.aclose()
