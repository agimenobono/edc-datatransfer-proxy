# Product Requirements Document (PRD)
## EDC Consumer Backend Proxy (Python)

## Overview
Build a Python backend (BFF) that:
- Uses EDR to fetch data from provider dataplanes
- Avoids CORS issues
- Streams data to frontend

## Core Principle
EDR.endpoint + EDR.authorization = source of truth

## API
POST /api/transfers/download

Request:
{
  "endpoint": "https://.../edc/public",
  "authorization": "..."
}

## Key Rules
- Do NOT add 'Bearer' to Authorization
- Do NOT modify endpoint
- Always append '/' if missing
- Follow the general SKILS

## Implementation (FastAPI)
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
import httpx

app = FastAPI()

@app.post("/api/transfers/download")
async def download(edr: dict):
    url = edr["endpoint"].rstrip("/") + "/"
    async with httpx.AsyncClient(follow_redirects=True) as client:
        response = await client.get(url, headers={"Authorization": edr["authorization"]})
        return StreamingResponse(response.aiter_bytes())
