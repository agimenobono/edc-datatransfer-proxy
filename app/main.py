import logging
from json import JSONDecodeError, loads

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.responses import RedirectResponse
from fastapi.responses import StreamingResponse

import httpx

from app.models import DownloadRequest
from app.proxy import open_upstream_stream, response_cache


logger = logging.getLogger(__name__)
app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/", include_in_schema=False)
async def root():
    return RedirectResponse(url="/docs")


@app.get("/api/cache/stats")
async def cache_stats():
    stats = response_cache.stats()
    return {
        "entries": stats.entries,
        "memory_entries": stats.memory_entries,
        "disk_entries": stats.disk_entries,
        "memory_bytes": stats.memory_bytes,
        "disk_bytes": stats.disk_bytes,
        "hits": {
            "memory": stats.hits_memory,
            "disk": stats.hits_disk,
        },
        "misses": stats.misses,
        "expired": stats.expired,
        "evictions": {
            "memory": stats.evictions_memory,
            "disk": stats.evictions_disk,
        },
    }


def _problem_detail_for_upstream_error(parsed_body: object | None) -> str:
    if isinstance(parsed_body, dict):
        errors = parsed_body.get("errors")
        if isinstance(errors, list) and "NOT_FOUND" in errors:
            return "The requested resource is no longer available from the EDC provider."

        root_cause = errors[0] if isinstance(errors, list) and errors and isinstance(errors[0], str) else None
        provider_message = parsed_body.get("message") if isinstance(parsed_body.get("message"), str) else None
        if root_cause or provider_message:
            parts = ["The EDC provider returned an error while processing the download request."]
            if root_cause:
                parts.append(f"Root cause: {root_cause}.")
            if provider_message:
                parts.append(provider_message)
            return " ".join(parts)

    return "The upstream EDC provider rejected the download request."


def _build_problem_content(status_code: int, detail: str, endpoint: str) -> dict[str, object]:
    return {
        "type": "urn:problem:upstream-provider-failure",
        "title": "External provider error",
        "status": status_code,
        "detail": detail,
        "code": "UPSTREAM_PROVIDER_FAILURE",
        "target": {
            "operation": "download",
            "resource": endpoint,
        },
    }


def _build_transport_problem_response(endpoint: str) -> JSONResponse:
    return JSONResponse(
        status_code=502,
        content=_build_problem_content(
            status_code=502,
            detail="The request could not be completed because an external provider failed.",
            endpoint=endpoint,
        ),
        media_type="application/problem+json",
    )


def _build_upstream_problem_response(status_code: int, body: bytes, endpoint: str) -> JSONResponse:
    raw_body = body.decode("utf-8", errors="replace")

    try:
        parsed_body = loads(raw_body)
    except JSONDecodeError:
        parsed_body = None

    content = _build_problem_content(
        status_code=status_code,
        detail=_problem_detail_for_upstream_error(parsed_body),
        endpoint=endpoint,
    )

    if raw_body:
        if isinstance(parsed_body, dict):
            content["upstream_error"] = parsed_body
        else:
            content["upstream_error"] = {"raw_body": raw_body}

    return JSONResponse(
        status_code=status_code,
        content=content,
        media_type="application/problem+json",
    )


@app.post("/api/transfers/download")
async def download(payload: DownloadRequest):
    upstream_context = open_upstream_stream(payload.endpoint, payload.authorization)

    try:
        upstream = await upstream_context.__aenter__()
    except httpx.HTTPError as exc:
        logger.exception("Upstream request failed: %s", exc)
        return _build_transport_problem_response(payload.endpoint)

    if upstream.status_code >= 400:
        try:
            body = b"".join([chunk async for chunk in upstream.body_stream])
        finally:
            await upstream_context.__aexit__(None, None, None)

        logger.error(
            "Upstream request failed with status %s: %s",
            upstream.status_code,
            body.decode("utf-8", errors="replace"),
        )
        return _build_upstream_problem_response(upstream.status_code, body, payload.endpoint)

    async def body_iterator():
        try:
            async for chunk in upstream.body_stream:
                yield chunk
        finally:
            await upstream_context.__aexit__(None, None, None)

    return StreamingResponse(
        body_iterator(),
        status_code=upstream.status_code,
        headers=upstream.headers,
    )
