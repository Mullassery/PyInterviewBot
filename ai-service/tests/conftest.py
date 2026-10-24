"""Shared test configuration. All providers run deterministically, offline."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pyinterviewbot_ai import db, providers


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("PYINTERVIEWBOT_API_KEY", "test-key")
    monkeypatch.setenv("PYINTERVIEWBOT_LLM_PROVIDER", "mock")
    monkeypatch.setenv("PYINTERVIEWBOT_ASR_PROVIDER", "mock")
    monkeypatch.setenv("PYINTERVIEWBOT_TTS_PROVIDER", "mock")
    providers.reset_providers()
    yield
    providers.reset_providers()


@pytest.fixture
def client() -> Iterator[TestClient]:
    from pyinterviewbot_ai.main import app

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def auth_headers() -> dict[str, str]:
    return {"X-API-Key": "test-key"}
