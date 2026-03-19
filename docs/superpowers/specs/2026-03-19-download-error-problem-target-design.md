# Download Error Problem Target Design

## Summary

Standardize all upstream-facing failures from `POST /api/transfers/download` as Problem Details responses and add target metadata that identifies the target connector endpoint involved in the failure.

This change applies to both:

- upstream HTTP failures, where the provider returns a non-2xx response, and
- upstream transport failures, where the proxy cannot establish or complete the request to the provider.

The goal is to make error handling consistent for clients and to expose enough metadata to identify which connector endpoint failed without adding speculative or unstable fields.

## Current State

The current implementation in `app/main.py` has two different failure shapes:

- upstream HTTP failures return `application/problem+json` with `type`, `title`, `status`, `detail`, and `upstream_error`
- upstream transport failures raise `HTTPException(status_code=502, detail=str(exc))`, which produces FastAPI's default `{"detail": ...}` JSON body

This creates an inconsistent contract for frontend consumers and makes it harder to identify the exact target connector involved in a failed request.

## Requirements

### Functional

All upstream-facing failures from `POST /api/transfers/download` must return a standardized Problem Details response.

For this change, "upstream-facing failures" means:

- upstream HTTP failures, where the provider returns a non-2xx response
- upstream transport failures, where the proxy cannot establish or complete the request to the provider

FastAPI request-validation failures such as `422 Unprocessable Entity` are out of scope and keep their existing framework-managed behavior.

The standardized response must include:

- `type`
- `title`
- `status`
- `detail`
- `code`
- `target`

For upstream HTTP failures, the response should also preserve the parsed upstream body when available via `upstream_error`, keeping the current diagnostic value.

No standardized error response introduced by this change should include `trace_id`.

### Target Metadata

The `target` object must identify the connector target involved in the failed request.

The approved contract is:

- `target.operation`: fixed to `download`
- `target.resource`: the full `endpoint` value received in the request payload

Rationale:

- the proxy only supports one connector-facing operation, so operation inference adds no value
- the connector path alone is not sufficient to identify the real target, because multiple connectors may expose the same path shape
- the full endpoint string is the clearest identifier of the actual provider target the proxy attempted to contact

### Problem Classification

The error family should be represented with a stable application-specific problem type and code. The approved example is:

- `type`: `urn:problem:upstream-provider-failure`
- `title`: `External provider error`
- `code`: `UPSTREAM_PROVIDER_FAILURE`

The `status` and `detail` fields continue to reflect the concrete failure outcome.

## Proposed Design

### Centralized Problem Builder

Refactor error response creation so both failure branches in `app/main.py` flow through a shared builder function.

That builder is responsible for:

- constructing the common Problem Details envelope
- attaching `target`
- attaching `upstream_error` only when there is an upstream HTTP response body worth exposing
- setting `media_type="application/problem+json"`

This keeps the error contract consistent and avoids future drift between branches.

### Target Construction

Build `target` directly from the validated request payload before attempting the upstream request.

The builder input should include the original `payload.endpoint`, not a host-derived label and not a path-only derivative.

Example:

```json
{
  "target": {
    "operation": "download",
    "resource": "https://provider.example/edc/public"
  }
}
```

The contract should preserve the payload value as received unless the implementation already has a compelling consistency rule that must be shared across all responses. If normalization is applied, tests must lock that behavior explicitly.

### Upstream HTTP Failures

When the upstream responds with `status_code >= 400`:

- read the upstream body
- log the failure as today
- parse the body as JSON when possible
- build a standardized Problem response
- preserve `upstream_error` as:
  - the parsed object when the response body is a JSON object
  - a fallback wrapper such as `{"raw_body": ...}` when the body is valid JSON but not an object, using the decoded raw body string as the value
  - the same fallback wrapper when the body is non-JSON
  - omit `upstream_error` when the upstream error body is empty

Existing detail-generation behavior for known provider error bodies should be retained unless a small simplification is necessary during implementation.

### Upstream Transport Failures

When `open_upstream_stream(...)` raises `httpx.HTTPError`:

- log the exception
- return a standardized Problem response with HTTP status `502`
- use the same `type`, `title`, `code`, and `target` contract as other download failures

Because there is no upstream response body in this case, `upstream_error` is not required.

The default detail should communicate that the request could not be completed because the external provider failed. The implementation may append transport-specific context if that does not weaken the standardized contract.

## Response Contract

### Baseline Failure Shape

All download failures should serialize as `application/problem+json` and follow this shape:

```json
{
  "type": "urn:problem:upstream-provider-failure",
  "title": "External provider error",
  "status": 502,
  "detail": "The request could not be completed because an external provider failed.",
  "code": "UPSTREAM_PROVIDER_FAILURE",
  "target": {
    "operation": "download",
    "resource": "https://provider.example/edc/public"
  }
}
```

### Upstream HTTP Failure Extension

When the upstream returned a non-empty error response body, the Problem document may include:

```json
{
  "upstream_error": {
    "errors": ["NOT_FOUND"]
  }
}
```

This field remains diagnostic and should not replace the top-level standardized fields.

## Testing

Update the existing tests in `tests/test_download.py` to verify the new contract.

Required coverage:

- upstream HTTP failures return the standardized Problem fields and `target`
- upstream transport failures now return `application/problem+json` instead of `{"detail": ...}`
- the selected `target.resource` behavior is locked with a test so it remains stable over time

The tests should continue validating useful existing behavior where it still applies:

- known upstream provider payloads still drive meaningful `detail` text
- non-JSON upstream errors still surface a wrapped raw payload
- error logging still occurs for both failure paths

## Out of Scope

The following are explicitly out of scope for this change:

- adding `trace_id`
- introducing additional connector-operation inference
- changing successful download behavior
- changing framework-managed request-validation errors such as `422`
- broad refactoring beyond the download error-handling path

## Implementation Notes

This is a single-endpoint change and should stay local to the existing modules unless a small helper model improves readability.

A typed `Problem` model is optional. The preferred implementation bias is to keep the code small and centralized rather than introducing abstraction that only serves one endpoint today.
