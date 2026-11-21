from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

from .fake_provider import FakeProvider


@pytest.fixture(scope="session")
def provider():
    p = FakeProvider()
    p.start()
    yield p
    p.stop()


@pytest.fixture
def make_client(provider, tmp_path):
    """Factory: build an isolated app (fresh DB file + fresh cache) per test."""
    clients: list[TestClient] = []

    def factory(**overrides) -> TestClient:
        base = dict(
            api_keys=["good-1", "good-2"],
            model="openai/fake-model",
            api_base=provider.base_url,
            database_url=overrides.pop("database_url", f"sqlite:///{tmp_path}/test.db"),
            allowed_origins=["http://localhost:3000"],
            enable_failure_simulation=True,
            key_cooldown_seconds=60.0,
        )
        base.update(overrides)
        raise_exc = base.pop("raise_server_exceptions", True)
        client = TestClient(create_app(Settings(**base)), raise_server_exceptions=raise_exc)
        client.__enter__()  # runs the lifespan (cache warm-up)
        clients.append(client)
        return client

    provider.calls.clear()
    yield factory
    for c in clients:
        c.__exit__(None, None, None)


@pytest.fixture
def client(make_client):
    return make_client()


def sid() -> str:
    return "s-" + uuid.uuid4().hex[:8]
