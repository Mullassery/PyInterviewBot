"""Environment bindings kept outside immutable workflow artifacts."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass

from .providers.registry import provider_is_known
from .workflow_models import RuntimeRequirements

SANDBOX = "sandbox"
PRODUCTION = "production"
PRODUCTION_REVISION_ENV = "PYINTERVIEWBOT_PRODUCTION_BINDING_REVISION"


class BindingError(Exception):
    pass


@dataclass(frozen=True)
class EnvironmentBinding:
    environment: str
    revision: str
    providers: dict[str, str]
    fingerprint: str

    def supports(self, requirements: RuntimeRequirements) -> bool:
        return all(self.providers.get(name) for name in requirements.capabilities)

    def safe_view(self) -> dict:
        return {
            "environment": self.environment,
            "revision": self.revision,
            "providers": dict(self.providers),
            "fingerprint": self.fingerprint,
        }


def _fingerprint(environment: str, revision: str, providers: dict[str, str]) -> str:
    payload = {
        "environment": environment,
        "revision": revision,
        "providers": providers,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def sandbox_binding() -> EnvironmentBinding:
    providers = {"llm": "mock", "asr": "mock", "tts": "mock"}
    return EnvironmentBinding(
        environment=SANDBOX,
        revision="sandbox-v1",
        providers=providers,
        fingerprint=_fingerprint(SANDBOX, "sandbox-v1", providers),
    )


def production_binding() -> EnvironmentBinding:
    revision = os.environ.get(PRODUCTION_REVISION_ENV, "").strip()
    if not revision:
        raise BindingError(
            f"{PRODUCTION_REVISION_ENV} is required before production deployment"
        )
    providers = {
        name: os.environ.get(f"PYINTERVIEWBOT_{name.upper()}_PROVIDER", "local").strip()
        for name in ("llm", "asr", "tts")
    }
    if any(not provider for provider in providers.values()):
        raise BindingError("production provider names must not be empty")
    unknown = [
        f"{kind}={provider}"
        for kind, provider in providers.items()
        if not provider_is_known(kind, provider)
    ]
    if unknown:
        raise BindingError(
            "unknown production provider selection: " + ", ".join(sorted(unknown))
        )
    return EnvironmentBinding(
        environment=PRODUCTION,
        revision=revision,
        providers=providers,
        fingerprint=_fingerprint(PRODUCTION, revision, providers),
    )


__all__ = [
    "PRODUCTION",
    "PRODUCTION_REVISION_ENV",
    "SANDBOX",
    "BindingError",
    "EnvironmentBinding",
    "production_binding",
    "sandbox_binding",
]
