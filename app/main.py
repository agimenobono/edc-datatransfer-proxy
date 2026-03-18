from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.responses import StreamingResponse

import httpx

from app.models import DownloadRequest
from app.proxy import open_upstream_stream


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


@app.post("/api/transfers/download")
async def download(payload: DownloadRequest):
    upstream_context = open_upstream_stream(payload.endpoint, payload.authorization)

    try:
        upstream = await upstream_context.__aenter__()
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="Failed to fetch upstream response") from exc

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
