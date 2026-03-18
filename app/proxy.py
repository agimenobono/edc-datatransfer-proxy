from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator

import httpx


@dataclass
class UpstreamStream:
    status_code: int
    body_stream: AsyncIterator[bytes]
    headers: dict[str, str]


def normalize_endpoint(endpoint: str) -> str:
    return endpoint if endpoint.endswith("/") else f"{endpoint}/"


def build_http_client(transport: httpx.AsyncBaseTransport | None = None) -> httpx.AsyncClient:
    return httpx.AsyncClient(follow_redirects=True, transport=transport)


def _filtered_headers(response: httpx.Response) -> dict[str, str]:
    content_type = response.headers.get("content-type")
    return {"content-type": content_type} if content_type else {}


@asynccontextmanager
async def open_upstream_stream(
    endpoint: str,
    authorization: str,
    client: httpx.AsyncClient | None = None,
):
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
            body_stream=response.aiter_bytes(),
            headers=_filtered_headers(response),
        )
    finally:
        await response_context.__aexit__(None, None, None)
        if owns_client:
            await upstream_client.aclose()
