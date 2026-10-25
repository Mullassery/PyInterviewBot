from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pyinterviewbot_ai import db, main, providers
from pyinterviewbot_ai.rate_limit import SlidingWindowLimiter


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path, monkeypatch) -> Iterator[None]:
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
    from fastapi.testclient import TestClient

    with TestClient(app, base_url="http://testserver") as test_client:
        yield test_client


@pytest.fixture
def auth_headers() -> dict[str, str]:
    return {"X-API-Key": "test-key"}


@pytest.fixture
def mock_llm_provider(monkeypatch):
    """Set up mock LLM provider for tests that need it."""
    import os
    os.environ["PYINTERVIEWBOT_LLM_PROVIDER"] = "mock"
    import pyinterviewbot_ai.providers as providers
    providers.reset_providers()
    yield
    providers.reset_providers()


@pytest.fixture
def openrouter_env(monkeypatch):
    """Set up environment for OpenRouter tests."""
    import os
    os.environ["PYINTERVIEWBOT_LLM_PROVIDER"] = "openrouter"
    os.environ["OPENROUTER_API_KEY"] = "test-key"
    os.environ["OPENROUTER_MODEL"] = "anthropic/claude-3.5-sonnet"
    import pyinterviewbot_ai.providers as providers
    providers.reset_providers()
    yield
    providers.reset_providers()


@pytest.fixture
def client() -> Iterator[TestClient]:
    from pyinterviewbot_ai.main import app
    from fastapi.testclient import TestClient

    with TestClient(app, base_url="http://testserver") as test_client:
        yield test_client


@pytest.fixture
def auth_headers() -> dict[str, str]:
    return {"X-API-Key": "test-key"}


@pytest.fixture
def mock_llm_provider(monkeypatch):
    """Set up mock LLM provider for tests that need it."""
    import os
    os.environ["PYINTERVIEWBOT_LLM_PROVIDER"] = "mock"
    import pyinterviewbot_ai.providers as providers
    providers.reset_providers()
    yield
    providers.reset_providers()


@pytest.fixture
def openrouter_env(monkeypatch):
    """Set up environment for OpenRouter tests."""
    import os
    os.environ["PYINTERVIEWBOT_LLM_PROVIDER"] = "openrouter"
    os.environ["OPENROUTER_API_KEY"] = "test-key"
    os.environ["OPENROUTER_MODEL"] = "anthropic/claude-3.5-sonnet"
    import pyinterviewbot_ai.providers as providers
    providers.reset_providers()
    yield
    providers.reset_providers()


@pytest.fixture
def client() -> Iterator[TestClient]:
    from pyinterviewbot_ai.main import app
    from fastapi.testclient import TestClient

    with TestClient(app, base_url="http://testserver") as test_client:
        yield test_client


@pytest.fixture
def auth_headers() -> dict[str, str]:
    return {"X-API-Key": "test-key"}


@pytest.fixture
def mock_llm_provider(monkeypatch):
    """Set up mock LLM provider for tests that need it."""
    import os
    os.environ["PYINTERVIEWBOT_LLM_PROVIDER"] = "mock"
    import pyinterviewbot_ai.providers as providers
    providers.reset_providers()
    yield
    providers.reset_providers()


@pytest.fixture
def openrouter_env(monkeypatch):
    """Set up environment for OpenRouter tests."""
    import os
    os.environ["PYINTERVIEWBOT_LLM_PROVIDER"] = "openrouter"
    os.environ["OPENROUTER_API_KEY"] = "test-key"
    os.environ["OPENROUTER_MODEL"] = "anthropic/claude-3.5-sonnet"
    import pyinterviewbot_ai.providers as providers
    providers.reset_providers()
    yield
    providers.reset_providers()


@pytest.fixture
def client() -> Iterator[TestClient]:
    from pyinterviewbot_ai.main import app
    from fastapi.testclient import TestClient

    with TestClient(app, base_url="http://testserver") as test_client:
        yield test_client


@pytest.fixture
def auth_headers() -> dict[str, str]:
    return {"X-API-Key": "test-key"}


@pytest.fixture
def mock_llm_provider(monkeypatch):
    """Set up mock LLM provider for tests that need it."""
    import os
    os.environ["PYINTERVIEWBOT_LLM_PROVIDER"] = "mock"
    import pyinterviewbot_ai.providers as providers
    providers.reset_providers()
    yield
    providers.reset_providers()


@pytest.fixture
def openrouter_env(monkeypatch):
    """Set up environment for OpenRouter tests."""
    import os
    os.environ["PYINTERVIEWBOT_LLM_PROVIDER"] = "openrouter"
    os.environ["OPENROUTER_API_KEY"] = "test-key"
    os.environ["OPENROUTER_MODEL"] = "anthropic/claude-3.5-sonnet"
    import pyinterviewbot_ai.providers as providers
    providers.reset_providers()
    yield
    providers.reset_providers()


@pytest.fixture
def client() -> Iterator[TestClient]:
    from pyinterviewbot_ai.main import app
    from fastapi.testclient import TestClient

    with TestClient(app, base_url="http://testserver") as test_client:
        yield test_client


@pytest.fixture
def auth_headers() -> dict[str, str]:
    return {"X-API-Key": "test-key"}


@pytest.fixture
def mock_llm_provider(monkeypatch):
    """Set up mock LLM provider for tests that need it."""
    import os
    os.environ["PYINTERVIEWBOT_LLM_PROVIDER"] = "mock"
    import pyinterviewbot_ai.providers as providers
    providers.reset_providers()
    yield
    providers.reset_providers()


@pytest.fixture
def openrouter_env(monkeypatch):
    """Set up environment for OpenRouter tests."""
    import os
    os.environ["PYINTERVIEWBOT_LLM_PROVIDER"] = "openrouter"
    os.environ["OPENROUTER_API_KEY"] = "test-key"
    os.environ["OPENROUTER_MODEL"] = "anthropic/claude-3.5-sonnet"
    import pyinterviewbot_ai.providers as providers
    providers.reset_providers()
    yield
    providers.reset_providers()


@pytest.fixture
def client() -> Iterator[TestClient]:
    from pyinterviewbot_ai.main import app
    from fastapi.testclient import TestClient

    with TestClient(app, base_url="http://testserver") as test_client:
        yield test_client


@pytest.fixture
def auth_headers() -> dict[str, str]:
    return {"X-API-Key": "test-key"}


@pytest.fixture
def mock_llm_provider(monkeypatch):
    """Set up mock LLM provider for tests that need it."""
    import os
    os.environ["PYINTERVIEWBOT_LLM_PROVIDER"] = "mock"
    import pyinterviewbot_ai.providers as providers
    providers.reset_providers()
    yield
    providers.reset_providers()


@pytest.fixture
def openrouter_env(monkeypatch):
    """Set up environment for OpenRouter tests."""
    import os
    os.environ["PYINTERVIEWBOT_LLM_PROVIDER"] = "openrouter"
    os.environ["OPENROUTER_API_KEY"] = "test-key"
    os.environ["OPENROUTER_MODEL"] = "anthropic/claude-3.5-sonnet"
    import pyinterviewbot_ai.providers as providers
    providers.reset_providers()
    yield
    providers.reset_providers()
