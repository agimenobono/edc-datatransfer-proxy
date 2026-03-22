import asyncio
import logging
from contextlib import asynccontextmanager

import httpx
from fastapi.testclient import TestClient

from app.main import app
from app.proxy import (
    CachedUpstreamResponse,
    UpstreamResponseCache,
    UpstreamStream,
    build_http_client,
    normalize_endpoint,
    open_upstream_stream,
)


client = TestClient(app)


async def iter_bytes(chunks):
    for chunk in chunks:
        yield chunk


def test_docs_ui_is_available():
    response = client.get("/docs")

    assert response.status_code == 200
    assert "Swagger UI" in response.text


def test_root_redirects_to_docs():
    response = client.get("/", allow_redirects=False)

    assert response.status_code in (307, 308)
    assert response.headers["location"] == "/docs"


def test_cors_preflight_is_allowed():
    response = client.options(
        "/api/transfers/download",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "POST",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "*"


def test_accepts_exact_required_fields(monkeypatch):
    @asynccontextmanager
    async def fake_open_upstream_stream(endpoint, authorization):
        assert endpoint == "https://provider.example/edc/public"
        assert authorization == "token"
        yield UpstreamStream(
            status_code=200,
            body_stream=iter_bytes([b"ok"]),
            headers={"content-type": "text/plain"},
        )

    monkeypatch.setattr("app.main.open_upstream_stream", fake_open_upstream_stream)

    response = client.post(
        "/api/transfers/download",
        json={
            "endpoint": "https://provider.example/edc/public",
            "authorization": "token",
        },
    )

    assert response.status_code == 200
    assert response.content == b"ok"


def test_cors_header_is_present_on_download_response(monkeypatch):
    @asynccontextmanager
    async def fake_open_upstream_stream(_endpoint, _authorization):
        yield UpstreamStream(
            status_code=200,
            body_stream=iter_bytes([b"ok"]),
            headers={"content-type": "text/plain"},
        )

    monkeypatch.setattr("app.main.open_upstream_stream", fake_open_upstream_stream)

    response = client.post(
        "/api/transfers/download",
        headers={"Origin": "http://localhost:3000"},
        json={
            "endpoint": "https://provider.example/edc/public",
            "authorization": "token",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "*"


def test_rejects_extra_fields():
    response = client.post(
        "/api/transfers/download",
        json={
            "endpoint": "https://provider.example/edc/public",
            "authorization": "token",
            "extra": "nope",
        },
    )

    assert response.status_code == 422


def test_rejects_endpoint_with_query_string():
    response = client.post(
        "/api/transfers/download",
        json={
            "endpoint": "https://provider.example/edc/public?x=1",
            "authorization": "token",
        },
    )

    assert response.status_code == 422


def test_rejects_endpoint_with_fragment():
    response = client.post(
        "/api/transfers/download",
        json={
            "endpoint": "https://provider.example/edc/public#part",
            "authorization": "token",
        },
    )

    assert response.status_code == 422


def test_normalize_endpoint_adds_trailing_slash():
    assert normalize_endpoint("https://provider.example/edc/public") == "https://provider.example/edc/public/"
    assert normalize_endpoint("https://provider.example/edc/public/") == "https://provider.example/edc/public/"


def test_proxy_forwards_authorization_and_filters_headers():
    captured = {}

    def handler(request):
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers["Authorization"]
        return httpx.Response(
            200,
            headers={"content-type": "application/json", "cache-control": "no-cache"},
            content=b'{"ok":true}',
            request=request,
        )

    async def run():
        async with build_http_client(transport=httpx.MockTransport(handler)) as upstream_client:
            async with open_upstream_stream(
                "https://provider.example/edc/public",
                "raw-token",
                client=upstream_client,
            ) as upstream:
                body = b"".join([chunk async for chunk in upstream.body_stream])

        assert captured == {
            "method": "GET",
            "url": "https://provider.example/edc/public/",
            "authorization": "raw-token",
        }
        assert upstream.status_code == 200
        assert upstream.headers == {"content-type": "application/json"}
        assert body == b'{"ok":true}'

    asyncio.run(run())


def test_proxy_follows_redirects():
    calls = []

    def handler(request):
        calls.append(str(request.url))
        if len(calls) == 1:
            return httpx.Response(
                307,
                headers={"location": "https://provider.example/final/"},
                request=request,
            )
        return httpx.Response(200, content=b"redirected", request=request)

    async def run():
        async with build_http_client(transport=httpx.MockTransport(handler)) as upstream_client:
            async with open_upstream_stream(
                "https://provider.example/edc/public",
                "token",
                client=upstream_client,
            ) as upstream:
                body = b"".join([chunk async for chunk in upstream.body_stream])

        assert calls == [
            "https://provider.example/edc/public/",
            "https://provider.example/final/",
        ]
        assert upstream.status_code == 200
        assert body == b"redirected"

    asyncio.run(run())


def test_proxy_caches_successful_responses(monkeypatch):
    cache = UpstreamResponseCache(max_entries=8, ttl_seconds=60, max_body_bytes=1024)
    monkeypatch.setattr("app.proxy.response_cache", cache)

    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=b'{"cached":true}',
            request=request,
        )

    async def fetch():
        async with build_http_client(transport=httpx.MockTransport(handler)) as upstream_client:
            async with open_upstream_stream(
                "https://provider.example/edc/public",
                "token",
                client=upstream_client,
            ) as upstream:
                body = b"".join([chunk async for chunk in upstream.body_stream])
                return upstream.status_code, upstream.headers, body

    first = asyncio.run(fetch())
    second = asyncio.run(fetch())

    assert calls == ["https://provider.example/edc/public/"]
    assert first == (200, {"content-type": "application/json"}, b'{"cached":true}')
    assert second == (200, {"content-type": "application/json"}, b'{"cached":true}')


def test_proxy_uses_lru_eviction(monkeypatch):
    cache = UpstreamResponseCache(max_entries=1, ttl_seconds=60, max_body_bytes=1024)
    monkeypatch.setattr("app.proxy.response_cache", cache)

    cache.set(
        "https://provider.example/edc/public",
        "token-a",
        CachedUpstreamResponse(status_code=200, body=b"a", headers={"content-type": "text/plain"}),
    )
    cache.set(
        "https://provider.example/edc/other",
        "token-b",
        CachedUpstreamResponse(status_code=200, body=b"b", headers={"content-type": "text/plain"}),
    )

    assert cache.get("https://provider.example/edc/public", "token-a") is None
    cached = cache.get("https://provider.example/edc/other", "token-b")
    assert cached is not None
    assert cached.body == b"b"


def test_proxy_expires_cached_responses(monkeypatch):
    cache = UpstreamResponseCache(max_entries=8, ttl_seconds=0, max_body_bytes=1024)
    monkeypatch.setattr("app.proxy.response_cache", cache)

    cache.set(
        "https://provider.example/edc/public",
        "token",
        CachedUpstreamResponse(status_code=200, body=b"a", headers={"content-type": "text/plain"}),
    )

    assert cache.get("https://provider.example/edc/public", "token") is None


def test_streams_successful_upstream_response(monkeypatch):
    @asynccontextmanager
    async def fake_open_upstream_stream(_endpoint, _authorization):
        yield UpstreamStream(
            status_code=200,
            body_stream=iter_bytes([b"hello", b" ", b"world"]),
            headers={"content-type": "text/plain"},
        )

    monkeypatch.setattr("app.main.open_upstream_stream", fake_open_upstream_stream)

    response = client.post(
        "/api/transfers/download",
        json={"endpoint": "https://provider.example/edc/public", "authorization": "token"},
    )

    assert response.status_code == 200
    assert response.content == b"hello world"
    assert response.headers["content-type"].startswith("text/plain")


def test_returns_problem_details_for_non_json_upstream_error(monkeypatch, caplog):
    @asynccontextmanager
    async def fake_open_upstream_stream(_endpoint, _authorization):
        yield UpstreamStream(
            status_code=404,
            body_stream=iter_bytes([b"missing"]),
            headers={"content-type": "text/plain"},
        )

    monkeypatch.setattr("app.main.open_upstream_stream", fake_open_upstream_stream)

    with caplog.at_level(logging.ERROR):
        response = client.post(
            "/api/transfers/download",
            json={"endpoint": "https://provider.example/edc/public", "authorization": "token"},
        )

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json() == {
        "type": "urn:problem:upstream-provider-failure",
        "title": "External provider error",
        "status": 404,
        "detail": "The upstream EDC provider rejected the download request.",
        "code": "UPSTREAM_PROVIDER_FAILURE",
        "target": {
            "operation": "download",
            "resource": "https://provider.example/edc/public",
        },
        "upstream_error": {"raw_body": "missing"},
    }
    assert "Upstream request failed with status 404: missing" in caplog.text


def test_logs_and_returns_upstream_500_details(monkeypatch, caplog):
    @asynccontextmanager
    async def fake_open_upstream_stream(_endpoint, _authorization):
        yield UpstreamStream(
            status_code=500,
            body_stream=iter_bytes([b'{"errors":["NOT_FOUND"]}']),
            headers={"content-type": "application/json"},
        )

    monkeypatch.setattr("app.main.open_upstream_stream", fake_open_upstream_stream)

    with caplog.at_level(logging.ERROR):
        response = client.post(
            "/api/transfers/download",
            json={"endpoint": "https://provider.example/edc/public", "authorization": "token"},
        )

    assert response.status_code == 500
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json() == {
        "type": "urn:problem:upstream-provider-failure",
        "title": "External provider error",
        "status": 500,
        "detail": "The requested resource is no longer available from the EDC provider.",
        "code": "UPSTREAM_PROVIDER_FAILURE",
        "target": {
            "operation": "download",
            "resource": "https://provider.example/edc/public",
        },
        "upstream_error": {"errors": ["NOT_FOUND"]},
    }
    assert 'Upstream request failed with status 500: {"errors":["NOT_FOUND"]}' in caplog.text


def test_returns_problem_details_for_unknown_provider_error_code(monkeypatch, caplog):
    @asynccontextmanager
    async def fake_open_upstream_stream(_endpoint, _authorization):
        yield UpstreamStream(
            status_code=409,
            body_stream=iter_bytes([b'{"errors":["CONFLICT"],"message":"Asset is locked"}']),
            headers={"content-type": "application/json"},
        )

    monkeypatch.setattr("app.main.open_upstream_stream", fake_open_upstream_stream)

    with caplog.at_level(logging.ERROR):
        response = client.post(
            "/api/transfers/download",
            json={"endpoint": "https://provider.example/edc/public", "authorization": "token"},
        )

    assert response.status_code == 409
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json() == {
        "type": "urn:problem:upstream-provider-failure",
        "title": "External provider error",
        "status": 409,
        "detail": "The EDC provider returned an error while processing the download request. Root cause: CONFLICT. Asset is locked",
        "code": "UPSTREAM_PROVIDER_FAILURE",
        "target": {
            "operation": "download",
            "resource": "https://provider.example/edc/public",
        },
        "upstream_error": {"errors": ["CONFLICT"], "message": "Asset is locked"},
    }
    assert 'Upstream request failed with status 409: {"errors":["CONFLICT"],"message":"Asset is locked"}' in caplog.text


def test_problem_target_uses_original_payload_endpoint(monkeypatch):
    @asynccontextmanager
    async def fake_open_upstream_stream(_endpoint, _authorization):
        yield UpstreamStream(
            status_code=404,
            body_stream=iter_bytes([b"missing"]),
            headers={"content-type": "text/plain"},
        )

    monkeypatch.setattr("app.main.open_upstream_stream", fake_open_upstream_stream)

    response = client.post(
        "/api/transfers/download",
        json={"endpoint": "https://provider.example/edc/public", "authorization": "token"},
    )

    assert response.status_code == 404
    assert response.json()["target"] == {
        "operation": "download",
        "resource": "https://provider.example/edc/public",
    }


def test_returns_502_for_upstream_transport_failure(monkeypatch, caplog):
    @asynccontextmanager
    async def fake_open_upstream_stream(_endpoint, _authorization):
        raise httpx.ConnectError("boom")
        yield

    monkeypatch.setattr("app.main.open_upstream_stream", fake_open_upstream_stream)

    with caplog.at_level(logging.ERROR):
        response = client.post(
            "/api/transfers/download",
            json={"endpoint": "https://provider.example/edc/public", "authorization": "token"},
        )

    assert response.status_code == 502
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json() == {
        "type": "urn:problem:upstream-provider-failure",
        "title": "External provider error",
        "status": 502,
        "detail": "The request could not be completed because an external provider failed.",
        "code": "UPSTREAM_PROVIDER_FAILURE",
        "target": {
            "operation": "download",
            "resource": "https://provider.example/edc/public",
        },
    }
    assert "Upstream request failed: boom" in caplog.text
