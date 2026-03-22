# edc-datatransfer-proxy

Minimal FastAPI backend proxy for EDC data transfers driven by EDR data.

The project supports two workflows:

- local development with `uvicorn`, and
- containerized execution with Docker and Docker Compose, so you can run it without installing Python dependencies on the host.

## Project goal

The main goal of this project is to provide a small backend-for-frontend proxy that:

- uses the `endpoint` and `authorization` values from an EDR as the source of truth,
- fetches data from provider dataplanes on behalf of the frontend,
- avoids browser CORS limitations, and
- streams the upstream response back to the client with minimal transformation.

## Run with Docker

Build and start the service with:

```bash
docker compose up --build
```

The API will be available at:

```text
http://localhost:8010/
```

## Local development

For direct local development, install the project dependencies in a Python 3.13 environment and start the API with:

```bash
uvicorn app.main:app --reload --port 8010
```

Open the service in a browser at:

```text
http://localhost:8010/
```

Swagger UI is available at:

```text
http://localhost:8010/docs
```

The proxy endpoint is:

```text
POST /api/transfers/download
```

Example request body:

```json
{
  "endpoint": "https://provider.example/edc/public",
  "authorization": "raw-token"
}
```

Behavior notes:

- the proxy performs a `GET` request against the provided endpoint,
- a trailing `/` is added when missing,
- the `Authorization` header is forwarded exactly as received, and
- redirects are followed automatically,
- successful upstream responses are cached with a bounded two-tier policy,
- small hot responses stay in memory,
- larger responses spill to disk so they do not accumulate in RAM, and
- TTL is the freshness boundary for cached content.

## Test

Run the test suite with:

```bash
python3 -m pytest -v
```
