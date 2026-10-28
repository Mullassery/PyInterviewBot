import json
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from pyinterviewbot_ai import db, main, providers
from pyinterviewbot_ai.rate_limit import SlidingWindowLimiter


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path, monkeypatch) -> Iterator[None]:
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(main, "limiter", SlidingWindowLimiter(0))
    monkeypatch.setenv("PYINTERVIEWBOT_API_KEY", "test-key")
    monkeypatch.setenv(
        "PYINTERVIEWBOT_PRINCIPALS",
        json.dumps(
            {
                "test-key": {
                    "subject": "test-tenant",
                    "roles": ["owner", "author", "deployer", "approver"],
                }
            }
        ),
    )
    monkeypatch.setenv("PYINTERVIEWBOT_LLM_PROVIDER", "mock")
    monkeypatch.setenv("PYINTERVIEWBOT_ASR_PROVIDER", "mock")
    monkeypatch.setenv("PYINTERVIEWBOT_TTS_PROVIDER", "mock")
    providers.reset_providers()
    yield
    providers.reset_providers()


@pytest.fixture
def client() -> Iterator[TestClient]:
    from fastapi.testclient import TestClient
    from pyinterviewbot_ai.main import app

    with TestClient(app, base_url="http://testserver") as test_client:
        yield test_client


@pytest.fixture
def auth_headers() -> dict[str, str]:
    return {"X-API-Key": "test-key"}


@pytest.fixture
def mock_llm_provider(monkeypatch):
    monkeypatch.setenv("PYINTERVIEWBOT_LLM_PROVIDER", "mock")
    providers.reset_providers()
    yield
    providers.reset_providers()


@pytest.fixture
def openrouter_env(monkeypatch):
    monkeypatch.setenv("PYINTERVIEWBOT_LLM_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("OPENROUTER_MODEL", "anthropic/claude-3.5-sonnet")
    providers.reset_providers()
    yield
    providers.reset_providers()
