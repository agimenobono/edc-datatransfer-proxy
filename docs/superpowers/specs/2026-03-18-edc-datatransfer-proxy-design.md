# EDC Data Transfer Proxy Design

**Date:** 2026-03-18

## Goal

Build a minimal Python backend proxy that accepts an EDR-derived payload, fetches data from a provider dataplane using the provided endpoint and authorization, and streams the upstream response back to the frontend to avoid CORS issues.

## Scope

Included in this first version:
- One FastAPI endpoint: `POST /api/transfers/download`
- Strict request validation for exactly `endpoint` and `authorization`
- Endpoint normalization by appending a trailing `/` if missing
- Upstream `GET` request using the raw authorization value exactly as received
- Streaming the upstream response body back to the client
- Basic error handling for invalid input and upstream request failures

Explicitly out of scope:
- Authentication or authorization added by the proxy
- Retries, circuit breakers, or rate limiting
- Configurable transport policies beyond minimal defaults
- Additional routes, methods, or generalized proxy features
- Logging, metrics, tracing, or deployment hardening

## API Contract

### Route

`POST /api/transfers/download`

### Request Body

The API accepts a strict JSON object with exactly these fields:

```json
{
  "endpoint": "https://.../edc/public",
  "authorization": "raw token or header value"
}
```

Rules:
- `endpoint` is required
- `authorization` is required
- Extra fields are rejected
- The proxy must not prepend `Bearer`
- The proxy must not rewrite the endpoint beyond ensuring a trailing `/`
- Endpoints containing `?` or `#` are invalid for this version and should be rejected during validation so trailing-slash normalization stays unambiguous

### Response

On success, the API returns a streamed HTTP response whose body is sourced from the upstream dataplane response.

Response expectations for this version:
- Stream bytes without buffering the full payload in memory
- Follow upstream redirects
- Preserve the upstream status code for both success and non-success upstream responses
- Forward upstream `content-type` when present
- Return `502 Bad Gateway` for upstream transport failures

## Architecture

The service is composed of two small units:

1. API layer
   Responsible for FastAPI app setup, request validation, dependency wiring, and conversion of service output into `StreamingResponse`.

2. Proxy service
   Responsible for endpoint normalization, upstream request execution with `httpx`, and exposing the upstream byte stream and response metadata back to the API layer.

This boundary keeps HTTP validation concerns separate from network behavior while remaining minimal enough for a small codebase.

## Component Design

### FastAPI App

Responsibilities:
- Initialize the FastAPI application
- Register `POST /api/transfers/download`
- Accept the strict request model
- Translate service results into a streamed response
- Map known upstream transport failures to `502`

Non-responsibilities:
- Building authorization headers dynamically
- Applying business rules beyond the PRD constraints

### Request Model

Responsibilities:
- Represent the incoming request body with `endpoint` and `authorization`
- Reject extra fields
- Provide a clear schema contract at the API boundary

Notes:
- Validation should be strict enough to reject unrelated keys
- Any further semantic URL checks should stay minimal to avoid deviating from the PRD

### Proxy Service

Responsibilities:
- Normalize the endpoint by appending `/` if missing
- Send an upstream `GET` to the normalized endpoint
- Forward the raw `Authorization` header value exactly as provided
- Return a small response contract containing:
  - `status_code`
  - `body_stream`
  - `headers`, limited to upstream `content-type` when present

Non-responsibilities:
- Retrying failed requests
- Mutating headers beyond setting `Authorization`
- Supporting arbitrary methods or a generic reverse proxy feature set

### Service Response Contract

The proxy service should expose a focused result object or equivalent structure with:
- `status_code`: upstream HTTP status code
- `body_stream`: async byte iterator from the upstream response
- `headers`: a minimal mapping containing `content-type` when the upstream response provides it

No broader header passthrough is required in this version.

Streaming ownership rule:
- The layer that opens the upstream streamed response must keep it open until the downstream `StreamingResponse` finishes consuming `body_stream`
- The implementation must close the upstream response only after streaming completes or aborts
- The service/API boundary must make this ownership explicit, for example with an async context-managed service call or an equivalent pattern that prevents premature response closure

## Data Flow

1. Frontend sends `POST /api/transfers/download`.
2. FastAPI validates the request body against the strict schema.
3. The proxy service receives the validated payload.
4. The service normalizes `endpoint` so it ends with `/`.
5. The service performs an upstream `GET` to that URL with:
   - `Authorization: <authorization>`
   - redirect following enabled
6. The upstream response body is exposed as an async byte stream.
7. FastAPI returns a `StreamingResponse` backed by that stream.

## Error Handling

Minimal error handling for this version:

- Validation errors:
  - Cause: missing required fields or extra fields
  - Behavior: FastAPI validation response

- Upstream transport errors:
  - Cause: DNS failure, connection failure, timeout, or similar `httpx` request failure
  - Behavior: return `502 Bad Gateway`

- Upstream non-2xx responses:
  - Behavior: pass through the upstream status code
  - Behavior: continue streaming the upstream response body as received
  - Behavior: forward upstream `content-type` when present
  - These responses are not treated as transport failures

- Mid-stream upstream read failure after response headers are already sent:
  - Behavior: terminate the downstream stream and close the upstream response
  - No status remapping is attempted after streaming has started

## Testing Strategy

The test suite should stay focused on the PRD rules and proxy behavior.

Required coverage:
- Request validation accepts a payload with exactly `endpoint` and `authorization`
- Request validation rejects extra fields
- Request validation rejects endpoints containing `?` or `#`
- Endpoint normalization adds a trailing `/` when missing
- Authorization header is forwarded unchanged
- Successful upstream responses are streamed back through the API
- Upstream non-2xx responses preserve status code and streamed body
- Redirecting upstream responses are followed
- Upstream transport failures return `502`

Test levels:
- Unit tests for endpoint normalization and service behavior where helpful
- API tests for request validation and response handling

## Implementation Tasks

### Task 1: Scaffold the backend structure

Create the initial application layout for FastAPI, the request model, and the proxy service module.

### Task 2: Implement strict request validation

Define the request schema so only `endpoint` and `authorization` are accepted, with extra fields rejected and endpoints containing `?` or `#` rejected.

### Task 3: Implement the proxy service

Add endpoint normalization and the upstream `GET` request using `httpx`, forwarding the raw authorization value unchanged and enabling redirect following.

### Task 4: Implement streamed API response handling

Wire the route to the proxy service and return a `StreamingResponse` without buffering the whole upstream payload.

### Task 5: Add minimal error mapping

Translate upstream transport failures into `502 Bad Gateway` and preserve upstream response status codes unconditionally.

### Task 6: Add focused automated tests

Cover schema strictness, URL normalization, authorization forwarding, stream passthrough, and upstream failure handling.

## Acceptance Criteria

- `POST /api/transfers/download` exists and is reachable
- The request body accepts exactly `endpoint` and `authorization`
- Endpoints containing `?` or `#` are rejected
- The proxy appends a trailing `/` when missing and makes no other endpoint changes
- The proxy forwards the authorization value exactly as received, without prepending `Bearer`
- The API streams the upstream response body to the client
- Redirecting upstream responses are followed
- Upstream non-2xx responses preserve status code and body stream
- Upstream transport failures produce `502 Bad Gateway`

## Open Questions

None for this version.
