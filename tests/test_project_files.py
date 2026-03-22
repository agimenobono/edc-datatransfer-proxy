from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_project_defines_container_workflow_and_local_uvicorn_docs():
    dockerfile = (ROOT / "Dockerfile").read_text()
    compose = (ROOT / "docker-compose.yml").read_text()
    readme = (ROOT / "README.md").read_text()
    pyproject = (ROOT / "pyproject.toml").read_text()

    assert "FROM python:3.13-slim" in dockerfile
    assert 'CMD ["uvicorn", "app.main:app"' in dockerfile
    assert "8010:8010" in compose
    assert "PROXY_CACHE_DIR: /var/cache/edc-proxy" in compose
    assert "edc-proxy-cache:/var/cache/edc-proxy" in compose
    assert "docker compose up --build" in readme
    assert "uvicorn app.main:app --reload --port 8010" in readme
    assert 'requires-python = ">=3.13"' in pyproject
