import pytest
import httpx

from app.config import settings
from app.main import app



@pytest.mark.anyio
async def test_root_exposes_version_and_build_metadata():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/")

    assert response.status_code == 200
    assert response.json() == {
        "message": "Investment Tracker API",
        "api_version": settings.app_version,
        "build": settings.build_sha,
        "docs": "/docs" if settings.enable_docs else None,
    }


@pytest.mark.anyio
async def test_health_check_verifies_database_connectivity():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "healthy"
    assert response.json()["database"] == "reachable"
    assert response.json()["api_version"] == settings.app_version
    assert response.json()["build"] == settings.build_sha
